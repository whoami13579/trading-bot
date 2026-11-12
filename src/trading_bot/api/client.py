import requests
from datetime import datetime, timezone
import time
from typing import Any, List
from trading_bot.api.models import TradingPosition
import hashlib
from trading_bot.config.trading_config import endpoint, load_credentials
from trading_bot.api.network_errors import REQUEST_TIMEOUT, TradingUnavailable, network_failure_reason
from urllib.parse import urlsplit


class TradingApi:
    def __init__(self, x_cap_api_key: str | None = None, identifier: str | None = None,
                 password: str | None = None, real_account: bool | None = None, *,
                 auto_authenticate: bool = True, environment: str | None = None,
                 account: str | None = None):
        selected = environment if environment is not None else ("live" if real_account else "demo")
        endpoint(selected)  # Validate before loading credentials or contacting an API.
        if real_account is not None and real_account != (selected == "live"):
            raise ValueError("conflicting_environment_selection")
        self._environment = selected
        self._account_selection = account
        self._login_has_active_environment = False
        self._login_account_id = None
        self._live_execution = None  # Authentication alone never authorizes LIVE mutations.
        self._last_live_request = None
        self._last_live_login = None
        self._unavailable = None
        supplied = (x_cap_api_key, identifier, password)
        if all(value is None for value in supplied):
            values = load_credentials()
            x_cap_api_key, identifier, password = (values[key] for key in ("X-CAP-API-KEY", "identifier", "password"))
        elif any(value is None for value in supplied):
            raise ValueError("all_three_credentials_required")

        self.X_CAP_API_KEY = x_cap_api_key
        self.IDENTIFIER = identifier
        self.PASSWORD = password
        self.cst = ""
        self.x_security_token = ""
        self.encryption_key = ""

        if auto_authenticate:
            self.pingService()

    @property
    def environment(self):
        return self._environment

    @property
    def BASE_URL(self):
        # An authenticated client cannot be retargeted to another environment.
        return endpoint(self._environment)

    def _invalidate_session(self, reason):
        self._unavailable = reason
        self.cst = self.x_security_token = ""
        self._login_has_active_environment = False
        self._login_account_id = None
        if self._live_execution is not None:
            self._live_execution.armed = False

    def _stop(self, reason, *, mutation=False):
        self._invalidate_session(reason)
        raise TradingUnavailable(reason, mutation=mutation) from None

    def _require_available(self):
        if self._unavailable is not None:
            raise TradingUnavailable(self._unavailable) from None

    def _request(self, method, url, **kwargs):
        self._require_available()
        path = urlsplit(url).path
        mutation = method.lower() != "get" and path != "/api/v1/session"
        if mutation and not (self.cst and self.x_security_token):
            self._stop("authentication_unavailable")
        if self.environment == "live":
            now = time.monotonic()
            delay = max(0, 0.12 - (now - self._last_live_request)) if self._last_live_request is not None else 0
            if method == "post" and url.endswith("/api/v1/session") and self._last_live_login is not None:
                delay = max(delay, 1.1 - (now - self._last_live_login))
            if delay:
                time.sleep(delay)
            self._last_live_request = time.monotonic()
            if method == "post" and url.endswith("/api/v1/session"):
                self._last_live_login = self._last_live_request
        kwargs.update(timeout=REQUEST_TIMEOUT, allow_redirects=False, verify=True)
        try:
            res = getattr(requests, method)(url, **kwargs)
        except (requests.exceptions.RequestException, OSError) as exc:
            self._stop(network_failure_reason(exc), mutation=mutation)
        # Ping has one existing authentication refresh; login retains its None-on-failure contract.
        auth_endpoint = path == "/api/v1/ping" or (path == "/api/v1/session" and method.lower() == "post")
        if res.status_code in (401, 403) and not auth_endpoint:
            self._stop("authentication_failed_http_401" if res.status_code == 401 else "permission_denied_http_403",
                       mutation=mutation)
        pending_confirmation = path.startswith("/api/v1/confirms/") and res.status_code == 404
        if res.status_code >= 300 and not auth_endpoint and not pending_confirmation and (
                method.lower() == "get" or res.status_code >= 500 or res.status_code < 400):
            self._stop(f"http_{res.status_code}", mutation=mutation)
        if res.status_code == 200:
            try:
                res.json()
            except ValueError:
                self._stop("invalid_api_response", mutation=mutation)
        return res

    def getCstAndXSecurityToken(self) -> tuple[str, str] | None:
        if self.createNewSession() is not None:
            return self.cst, self.x_security_token
        return None

    def getServerTime(self) -> str | None:
        res = self._request("get", f"{self.BASE_URL}/api/v1/time", timeout=20, allow_redirects=False)
        if res.status_code == 200:
            return res.json()

        return None

    def pingService(self) -> tuple[dict, int]:
        payload = ""
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "CST": self.cst}
        res = self._request("get",
            f"{self.BASE_URL}/api/v1/ping", json=payload, headers=headers, timeout=20, allow_redirects=False)
        if res.status_code == 200:
            return res.json(), res.status_code


        # Refresh authentication only for an expired or unauthorized session.
        if res.status_code not in (401, 403):
            self._stop(f"http_{res.status_code}")
        cstAndXSecurityToken = self.getCstAndXSecurityToken()
        if not cstAndXSecurityToken:
            self._require_available()
            self._stop("authentication_unavailable")
        self.cst, self.x_security_token = cstAndXSecurityToken

        payload = ""
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "CST": self.cst}
        res = self._request("get",
            f"{self.BASE_URL}/api/v1/ping", json=payload, headers=headers, timeout=20, allow_redirects=False)

        if res.status_code != 200:
            self._stop("authentication_failed_http_401" if res.status_code == 401 else
                       "permission_denied_http_403" if res.status_code == 403 else f"http_{res.status_code}")
        return res.json(), res.status_code

    def getEncryptionKey(self) -> dict | None:
        headers = {"X-CAP-API-KEY": self.X_CAP_API_KEY}
        res = self._request("get",
            f"{self.BASE_URL}/api/v1/session/encryptionKey",
            headers=headers,
            timeout=20, allow_redirects=False)
        if res.status_code == 200:
            self.encryption_key = res.json().get("encryptionKey")

            return res.json()

        return None

    def getSessionDetails(self) -> dict | None:
        payload = ""
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "CST": self.cst}
        res = self._request("get",
            f"{self.BASE_URL}/api/v1/session", json=payload, headers=headers, timeout=20, allow_redirects=False)
        if res.status_code == 200:
            return res.json()

        return None

    def createNewSession(self) -> dict | None:
        self._require_available()
        self.cst = self.x_security_token = ""
        self._login_has_active_environment = False
        self._login_account_id = None
        if self._live_execution is not None:
            self._live_execution.armed = False
        payload = {"identifier": self.IDENTIFIER, "password": self.PASSWORD,
                   "encryptedPassword": False}
        headers = {
            "X-CAP-API-KEY": self.X_CAP_API_KEY,
            "Content-Type": "application/json",
        }
        res = self._request("post",
            f"{self.BASE_URL}/api/v1/session", json=payload, headers=headers, timeout=20, allow_redirects=False
        )
        if res.status_code == 200:
            cst = res.headers.get("CST")
            security_token = res.headers.get("X-SECURITY-TOKEN")
            if not cst or not security_token:
                self._invalidate_session("missing_session_tokens")
                print("Login response missing required session headers")
                return None
            details = res.json()
            flag = "hasActiveLiveAccounts" if self.environment == "live" else "hasActiveDemoAccounts"
            self._login_has_active_environment = isinstance(details, dict) and details.get(flag) is True
            self._login_account_id = details.get("currentAccountId") if isinstance(details, dict) else None
            self.cst, self.x_security_token = cst, security_token
            return details
        # Do not log response bodies, credentials, or session tokens.
        self._invalidate_session("authentication_failed_http_401" if res.status_code == 401 else
                                 "permission_denied_http_403" if res.status_code == 403 else f"http_{res.status_code}")
        print(f"Failed to login: HTTP {res.status_code}")
        return None

    def verify_account_environment(self):
        """Discover the active account without switching it; reject ambiguous LIVE trading."""
        if not self._login_has_active_environment:
            raise RuntimeError("authenticated_environment_not_verified")
        if not isinstance(self._login_account_id, str) or not self._login_account_id:
            raise RuntimeError("authenticated_login_account_not_verified")
        session, accounts = self.getSessionDetails(), self.getAllAccounts()
        if not isinstance(session, dict) or not isinstance(accounts, dict):
            raise RuntimeError("account_discovery_failed")
        rows = accounts.get("accounts")
        if not isinstance(rows, list):
            raise RuntimeError("invalid_account_list")
        expected_demo = self.environment == "demo"
        eligible = [row for row in rows if row.get("status") == "ENABLED" and row.get("accountType") == "CFD"
                    and row.get("demo", expected_demo) is expected_demo]
        current = session.get("accountId")
        if current != self._login_account_id:
            raise RuntimeError("login_session_account_mismatch_no_switch_performed")
        active = [row for row in eligible if isinstance(current, str) and row.get("accountId") == current]
        if len(active) != 1:
            raise RuntimeError("active_account_environment_mismatch")
        if self._account_selection is not None:
            selected = [row for row in eligible if isinstance(row.get("accountId"), str)
                        and self._account_selection in (row["accountId"], hashlib.sha256(row["accountId"].encode()).hexdigest()[:12])]
            if len(selected) != 1 or selected[0]["accountId"] != current:
                raise RuntimeError("selected_account_not_active_no_switch_performed")
        elif self.environment == "live" and len(eligible) > 1:
            raise RuntimeError("multiple_live_accounts_require_explicit_selection")
        return active[0]

    def switchesActiveAccount(self):
        pass

    def logOutOfTheCurrentSession(self):
        pass

    def getAllAccounts(self) -> dict | None:
        payload = ""
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "CST": self.cst}
        res = self._request("get",
            f"{self.BASE_URL}/api/v1/accounts", json=payload, headers=headers, timeout=20, allow_redirects=False)
        if res.status_code == 200:
            return res.json()

        return None

    def getAccountPreferences(self) -> dict | None:
        payload = ""
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "CST": self.cst}
        res = self._request("get",
            f"{self.BASE_URL}/api/v1/accounts/preferences",
            json=payload,
            headers=headers, timeout=20, allow_redirects=False)
        if res.status_code == 200:
            return res.json()

        return None

    def updateAccountPreferences(self):
        pass

    def getAccountActivityHistory(self) -> dict | None:
        payload = ""
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "CST": self.cst}
        res = self._request("get",
            f"{self.BASE_URL}/api/v1/history/activity",
            json=payload,
            headers=headers, timeout=20, allow_redirects=False)
        if res.status_code == 200:
            return res.json()

        return None

    def getAccountTransactionsHistory(self) -> dict | None:
        payload = ""
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "CST": self.cst}
        res = self._request("get",
            f"{self.BASE_URL}/api/v1/history/transactions",
            json=payload,
            headers=headers, timeout=20, allow_redirects=False)
        if res.status_code == 200:
            return res.json()

        return None

    def adjustBalanceOfDemoAccount(self) -> dict | None:
        return None

    def getPositionOrderConfirmation(self, dealReference: str) -> dict | None:
        payload = ""
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "CST": self.cst}
        res = self._request("get",
            f"{self.BASE_URL}/api/v1/confirms/{dealReference}",
            json=payload,
            headers=headers, timeout=20, allow_redirects=False)
        if res.status_code == 200:
            return res.json()

        return None

    def getAllPositions(self) -> dict | None:
        payload = ""
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "CST": self.cst}
        res = self._request("get",
            f"{self.BASE_URL}/api/v1/positions", json=payload, headers=headers, timeout=20, allow_redirects=False)

        return res.json(), res.status_code

    def getAllPositionsList(self) -> list[TradingPosition]:
        result, code = self.getAllPositions()
        if code != 200:
            raise RuntimeError("positions_discovery_failed")

        # List comprehension to create the objects
        trading_positions_list = [
            TradingPosition(
                epic=item['market']['epic'],
                dealId=item['position']['dealId']
            )
            for item in result['positions']
            if self.environment != "live" or (self._live_execution is not None
                                              and item['position']['dealId'] in self._live_execution.owned)
        ]

        return trading_positions_list

    def createPosition(
        self,
        epic: str,
        direction: str,
        size: int,
        guaranteedStop: bool = False,
        stopLevel: int = 0,
        profitLevel: int = 0,
        trailingStop: bool = 0,
        stopDistance: int = 0,
        stopAmount: int = 0,
        profitDistance: int = 0,
        profitAmount: int = 0,
    ) -> tuple[dict, int]| None:
        self._require_available()
        if self.environment == "live":
            self.verify_account_environment()
            if self._live_execution is None:
                from trading_bot.risk.live_execution import LiveSafetyStop
                raise LiveSafetyStop("live_execution_not_armed_strategy_preflight_required")
        payload = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "guaranteedStop": guaranteedStop,
        }
        for name, value in (("stopLevel", stopLevel), ("profitLevel", profitLevel),
                            ("trailingStop", trailingStop), ("stopDistance", stopDistance),
                            ("stopAmount", stopAmount), ("profitDistance", profitDistance),
                            ("profitAmount", profitAmount)):
            if value:
                payload[name] = value
        if self.environment == "live":
            payload.setdefault("stopDistance", float(self._live_execution.args.stop_distance))
            return self._live_execution.execute("open", lambda: self._post_position(payload), payload=payload)
        return self._post_position(payload)

    def _post_position(self, payload):
        headers = {
            "X-SECURITY-TOKEN": self.x_security_token,
            "CST": self.cst,
            "Content-Type": "application/json",
        }
        res = self._request("post",
            f"{self.BASE_URL}/api/v1/positions", json=payload, headers=headers, timeout=20, allow_redirects=False)

        if res.status_code != 200:
            print(f"createPosition: HTTP {res.status_code}")

        return res.json(), res.status_code

    def getsinglePosition(self, dealId) -> dict | None:
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.x_security_token,
            "CST": self.cst,
        }
        res = self._request("get",
            f"{self.BASE_URL}/api/v1/positions/{dealId}", json=payload, headers=headers, timeout=20, allow_redirects=False)
        if res.status_code == 200:
            return res.json()

        return None

    def updatePosition(self) -> dict | None:
        return None

    def closePosition(self, dealId: str) -> tuple[dict, int]:
        self._require_available()
        if self.environment == "live":
            self.verify_account_environment()
            if self._live_execution is None:
                from trading_bot.risk.live_execution import LiveSafetyStop
                raise LiveSafetyStop("live_execution_not_armed_strategy_preflight_required")
            return self._live_execution.execute("close", lambda: self._delete_position(dealId), deal_id=dealId)
        return self._delete_position(dealId)

    def _delete_position(self, dealId):
        headers = {
        'X-SECURITY-TOKEN': self.x_security_token,
        'CST': self.cst
        }
        res = self._request("delete", f"{self.BASE_URL}/api/v1/positions/{dealId}", headers=headers, timeout=20, allow_redirects=False)

        if res.status_code != 200:
            print(f"closePosition: HTTP {res.status_code}")
        return res.json(), res.status_code

    def getAllWorkingOrders(self) -> dict | None:
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.x_security_token,
            "CST": self.cst,
        }
        res = self._request("get",
            f"{self.BASE_URL}/api/v1/workingorders", json=payload, headers=headers, timeout=20, allow_redirects=False)
        if res.status_code == 200:
            return res.json()

        return None

    def createWorkingOrder(self) -> dict | None:
        return None

    def updateWorkingOrder(self) -> dict | None:
        return None

    def deleteWorkingOrder(self) -> dict | None:
        return None

    def getAllTopLevelMarketCategories(self) -> dict | None:
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.x_security_token,
            "CST": self.cst,
        }
        res = self._request("get",
            f"{self.BASE_URL}/api/v1/marketnavigation", json=payload, headers=headers, timeout=20, allow_redirects=False)
        if res.status_code == 200:
            return res.json()

        return None

    def getAllCategorySubNodes(self, nodeId) -> dict | None:
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.x_security_token,
            "CST": self.cst,
        }
        res = self._request("get",
            f"{self.BASE_URL}/api/v1/marketnavigation/{nodeId}", json=payload, headers=headers, timeout=20, allow_redirects=False)
        if res.status_code == 200:
            return res.json()

        return None

    def getMarketsDetails(self, epics: str) -> dict | None:
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.x_security_token,
            "CST": self.cst,
        }
        res = self._request("get",
            f"{self.BASE_URL}/api/v1/markets?epics={epics}", json=payload, headers=headers, timeout=20, allow_redirects=False)
        if res.status_code == 200:
            return res.json()

        return None

    def getSingleMarketDetails(self, epic: str) -> dict | None:
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.x_security_token,
            "CST": self.cst,
        }
        res = self._request("get",
            f"{self.BASE_URL}/api/v1/markets/{epic}", json=payload, headers=headers, timeout=20, allow_redirects=False)
        if res.status_code == 200:
            return res.json()

        return None

    def getHistoricalPrices(self, epic: str, resolution: str, max: int) -> dict | None:
        '''
        Docstring for getHistoricalPrices

        :param self: Description
        :param epic: Possible values: EURUSD
        :type epic: str
        :param resolution: Possible values are MINUTE, MINUTE_5, MINUTE_15, MINUTE_30, HOUR, HOUR_4, DAY, WEEK
        :type resolution: str
        :param max: from 1 to 1000
        :type max: int
        :return: Description
        :rtype: dict | None
        '''
        # self.load_keys()
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.x_security_token,
            "CST": self.cst,
        }
        res = self._request("get",
            f"{self.BASE_URL}/api/v1/prices/{epic}?resolution={resolution}&max={max}", json=payload, headers=headers, timeout=20, allow_redirects=False)

        if res.status_code != 200:
            self._stop(f"http_{res.status_code}")

        return res.json(), res.status_code

    def getHistoricalPricesList(self, symbol: str, time_frame: str, period: int) -> list[float]:
        if time_frame == "HOUR_2":
            result, code = self.getHistoricalPrices(
                symbol, "HOUR", period * 2
            )

            if code != 200:
                self._stop(f"http_{code}")

            prices: List[float] = [item["closePrice"]["ask"] for item in result["prices"]]
            return prices[1::2]

        result, code = self.getHistoricalPrices(
            symbol, time_frame, period
        )

        if code != 200:
            self._stop(f"http_{code}")

        prices: List[float] = [item["closePrice"]["ask"] for item in result["prices"]]
        return prices

    def getClientSentimentForMarkets(self) -> dict | None:
        return None

    def getClientSentimentForMarket(self) -> dict | None:
        return None

    def getAllWatchlists(self) -> dict | None:
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.x_security_token,
            "CST": self.cst,
        }
        res = self._request("get",
            f"{self.BASE_URL}/api/v1/watchlists", json=payload, headers=headers, timeout=20, allow_redirects=False)
        if res.status_code == 200:
            return res.json()

        return None

    def createWatchlist(self) -> dict | None:
        return None

    def getSingleWatchlist(self) -> dict | None:
        return None

    def addMarketToWatchlist(self) -> dict | None:
        return None

    def deleteWatchlist(self) -> dict | None:
        return None

    def removeMarketFromWatchlist(self) -> dict | None:
        return None


    def is_market_open(self):
        """
        Checks if the current UTC time is between
        Sunday 21:00 and Friday 22:00.
        """
        now = datetime.now(timezone.utc)
        weekday = now.weekday()  # Monday is 0, Sunday is 6
        hour = now.hour

        # Case 1: Sunday after 22:00
        if weekday == 6 and hour >= 22:
            return True

        # Case 2: Monday (0) through Thursday (3) - Always open
        if 0 <= weekday <= 3:
            return True

        # Case 3: Friday before 22:00
        if weekday == 4 and hour < 22:
            return True

        # Otherwise, we are in the Friday night - Sunday evening gap
        return False


    def wait_until_open(self, check_interval=60):
        """
        Pauses execution until the market open conditions are met.
        """
        while not self.is_market_open():
            print(f"[{datetime.now(timezone.utc)}] Market closed. Waiting...")
            time.sleep(check_interval)

        print("Market is now OPEN!")

    def write_keys(self):
        """Compatibility no-op: shared .env must never cache unlabelled session tokens."""
        return None

    def load_keys(self):
        """Compatibility no-op: tokens belong only to this client and environment."""
        return None

import requests
from datetime import datetime, timezone
import time
from typing import List
from trading_bot.config.trading_config import endpoint, load_credentials
from trading_bot.api.network_errors import (
    REQUEST_TIMEOUT,
    TradingUnavailable,
    network_failure_reason,
)
from urllib.parse import urlsplit

from trading_bot.api.rest import RestComponent
from trading_bot.api.accounts import AccountsAPI
from trading_bot.api.markets import MarketsAPI
from trading_bot.api.session import SessionAPI
from trading_bot.api.positions import PositionsAPI
from trading_bot.api.watchlists import WatchlistsAPI
from trading_bot.api.working_orders import WorkingOrdersAPI


class TradingApi(
    SessionAPI,
    AccountsAPI,
    MarketsAPI,
    WatchlistsAPI,
    WorkingOrdersAPI,
    PositionsAPI,
    RestComponent,
):
    def __init__(
        self,
        x_cap_api_key: str | None = None,
        identifier: str | None = None,
        password: str | None = None,
        real_account: bool | None = None,
        *,
        auto_authenticate: bool = True,
        environment: str | None = None,
        account: str | None = None,
    ):
        selected = (
            environment
            if environment is not None
            else ("live" if real_account else "demo")
        )
        endpoint(selected)  # Validate before loading credentials or contacting an API.
        if real_account is not None and real_account != (selected == "live"):
            raise ValueError("conflicting_environment_selection")
        self._environment = selected
        self._account_selection = account
        self._login_has_active_environment = False
        self._login_account_id = None
        self._live_execution = (
            None  # Authentication alone never authorizes LIVE mutations.
        )
        self._live_send_depth = 0
        self._session_generation = 0
        self._unavailable = None
        self._mutation_outcome_uncertain = False
        self._last_authenticated_at: float | None = None
        self._cycle_identity: tuple[str, str, str] | None = None
        self._preparing_cycle = False
        self._last_cycle_ping: dict | None = None
        supplied = (x_cap_api_key, identifier, password)
        if all(value is None for value in supplied):
            values = load_credentials()
            x_cap_api_key, identifier, password = (
                values[key] for key in ("X-CAP-API-KEY", "identifier", "password")
            )
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
        self._last_authenticated_at = None
        self._login_has_active_environment = False
        self._login_account_id = None
        if self._live_execution is not None:
            self._live_execution.armed = False

    def _stop(self, reason, *, mutation=False, broker_code=None):
        self._mutation_outcome_uncertain |= mutation
        self._invalidate_session(reason)
        raise TradingUnavailable(
            reason, mutation=mutation, broker_code=broker_code
        ) from None

    def _require_available(self):
        if self._unavailable is not None:
            raise TradingUnavailable(
                self._unavailable, mutation=self._mutation_outcome_uncertain
            ) from None

    def _request(self, method, url, **kwargs):
        self._require_available()
        path = urlsplit(url).path
        mutation = method.lower() != "get" and not (
            method.lower() == "post" and path == "/api/v1/session"
        )
        if mutation and not (self.cst and self.x_security_token):
            self._stop("authentication_unavailable")
        if (
            urlsplit(url).scheme != "https"
            or urlsplit(url).netloc != urlsplit(self.BASE_URL).netloc
        ):
            raise PermissionError("api_endpoint_environment_mismatch")
        if (
            self.environment == "live"
            and method.lower() != "get"
            and not (path == "/api/v1/session" and method.lower() == "post")
        ):
            if not (
                self._live_send_depth
                and (
                    (method.lower() == "post" and path == "/api/v1/positions")
                    or (
                        method.lower() == "delete"
                        and path.startswith("/api/v1/positions/")
                    )
                )
            ):
                raise PermissionError("live_mutation_outside_execution_gate")
        from trading_bot.api.rate_limits import throttle

        throttle(
            self.X_CAP_API_KEY,
            self.environment,
            method.lower(),
            path,
            self._login_account_id,
            user_identifier=self.IDENTIFIER,
        )
        kwargs.update(timeout=REQUEST_TIMEOUT, allow_redirects=False, verify=True)
        request_started = time.monotonic()
        try:
            res = getattr(requests, method)(url, **kwargs)
        except KeyboardInterrupt:
            self._mutation_outcome_uncertain |= mutation
            self._invalidate_session("request_interrupted")
            raise
        except (requests.exceptions.RequestException, OSError) as exc:
            self._stop(network_failure_reason(exc), mutation=mutation)
        from trading_bot.api.errors import broker_error_code

        error_code = broker_error_code(res) if res.status_code >= 300 else None
        # Ping has one existing authentication refresh; login retains its None-on-failure contract.
        auth_endpoint = path == "/api/v1/ping" or (
            path == "/api/v1/session" and method.lower() == "post"
        )
        if res.status_code in (401, 403) and not auth_endpoint:
            self._stop(
                "authentication_failed_http_401"
                if res.status_code == 401
                else "permission_denied_http_403",
                mutation=mutation,
                broker_code=error_code,
            )
        pending_confirmation = (
            path.startswith("/api/v1/confirms/") and res.status_code == 404
        )
        if res.status_code >= 300 and not auth_endpoint and not pending_confirmation:
            self._stop(
                f"http_{res.status_code}", mutation=mutation, broker_code=error_code
            )
        if res.status_code == 200:
            try:
                if not (
                    method.lower() == "delete"
                    and path == "/api/v1/session"
                    and not res.content
                ):
                    if not isinstance(res.json(), dict):
                        self._stop("invalid_api_response", mutation=mutation)
            except ValueError:
                self._stop("invalid_api_response", mutation=mutation)
        if res.status_code == 200:
            auth_headers = kwargs.get("headers", {})
            if auth_headers.get("CST") and auth_headers.get("X-SECURITY-TOKEN"):
                self._last_authenticated_at = request_started
        return res

    def getServerTime(self) -> str | None:
        res = self._request(
            "get", f"{self.BASE_URL}/api/v1/time", timeout=20, allow_redirects=False
        )
        if res.status_code == 200:
            return res.json()

        return None

    def pingService(self) -> tuple[dict, int]:
        if self._cycle_identity is not None:
            self.prepare_trading_cycle()
            return self._last_cycle_ping, 200
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "CST": self.cst}
        res = self._request(
            "get",
            f"{self.BASE_URL}/api/v1/ping",
            headers=headers,
            timeout=20,
            allow_redirects=False,
        )
        if res.status_code == 200:
            return res.json(), res.status_code

        # Refresh authentication only for an expired or unauthorized session.
        if res.status_code != 401:
            self._stop(
                "permission_denied_http_403"
                if res.status_code == 403
                else f"http_{res.status_code}"
            )
        cstAndXSecurityToken = self.getCstAndXSecurityToken()
        if not cstAndXSecurityToken:
            self._require_available()
            self._stop("authentication_unavailable")
        self.cst, self.x_security_token = cstAndXSecurityToken

        headers = {"X-SECURITY-TOKEN": self.x_security_token, "CST": self.cst}
        res = self._request(
            "get",
            f"{self.BASE_URL}/api/v1/ping",
            headers=headers,
            timeout=20,
            allow_redirects=False,
        )

        if res.status_code != 200:
            self._stop(
                "authentication_failed_http_401"
                if res.status_code == 401
                else "permission_denied_http_403"
                if res.status_code == 403
                else f"http_{res.status_code}"
            )
        return res.json(), res.status_code

    def getHistoricalPricesList(
        self, symbol: str, time_frame: str, period: int
    ) -> list[float]:
        if time_frame == "HOUR_2":
            result, code = self.getHistoricalPrices(symbol, "HOUR", period * 2)

            if code != 200:
                self._stop(f"http_{code}")

            prices: List[float] = [
                item["closePrice"]["ask"] for item in result["prices"]
            ]
            return prices[1::2]

        result, code = self.getHistoricalPrices(symbol, time_frame, period)

        if code != 200:
            self._stop(f"http_{code}")

        prices: List[float] = [item["closePrice"]["ask"] for item in result["prices"]]
        return prices

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

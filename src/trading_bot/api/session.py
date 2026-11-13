"""Authenticated sessions with bounded, account-pinned cycle renewal."""

import base64
import hashlib
import time
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA
from trading_bot.api.validation import boolean, text


def encrypt_password(password: str, encryption_key: str, timestamp: int) -> str:
    if type(timestamp) is not int or timestamp < 0:
        raise ValueError("invalid_encryption_timestamp")
    try:
        key = RSA.import_key(base64.b64decode(encryption_key, validate=True))
        plaintext = base64.b64encode(f"{password}|{timestamp}".encode())
        return base64.b64encode(PKCS1_v1_5.new(key).encrypt(plaintext)).decode()
    except (ValueError, TypeError, IndexError):
        raise ValueError("invalid_password_encryption_parameters") from None


# Refresh conservatively before the documented 600-second inactivity deadline.
SESSION_RENEWAL_AFTER = 540


class SessionAPI:
    def pin_session_identity(self, verified_account: dict) -> None:
        """Bind scheduled renewal to the account already verified by startup preflight.

        This is not authorization for trading. It neither changes accounts nor arms a gate.
        """
        self._require_available()
        from trading_bot.config.trading_config import endpoint

        account_id = verified_account.get("accountId")
        identity = (self.environment, self.BASE_URL, account_id)
        if (
            not isinstance(account_id, str)
            or not account_id
            or account_id != self._login_account_id
            or not self._login_has_active_environment
            or not self.cst
            or not self.x_security_token
            or self.BASE_URL != endpoint(self.environment)
            or (self._cycle_identity is not None and self._cycle_identity != identity)
        ):
            self._stop("session_identity_changed")
        self._cycle_identity = identity

    def prepare_trading_cycle(self) -> bool:
        """Validate a scheduled cycle, renewing at most once before any strategy work.

        Only the read-only ping may initiate recovery from 401. No data request or
        mutation is replayed. A renewed LIVE gate stays disarmed and stops the cycle.
        Returns whether this boundary renewed the session.
        """
        self._require_available()  # Never clear a previous availability/uncertain-order stop.
        expected = self._cycle_identity
        if expected is None or self._preparing_cycle:
            self._stop("session_identity_not_pinned")
        if self.environment != expected[0] or self.BASE_URL != expected[1]:
            self._stop("session_identity_changed")
        self._preparing_cycle = True
        try:

            def ping():
                headers = {"CST": self.cst, "X-SECURITY-TOKEN": self.x_security_token}
                return self._request(
                    "get",
                    self.BASE_URL + "/api/v1/ping",
                    headers=headers,
                    timeout=20,
                    allow_redirects=False,
                )

            def require_ping(response):
                if response.status_code != 200:
                    self._stop(
                        "authentication_failed_http_401"
                        if response.status_code == 401
                        else "permission_denied_http_403"
                        if response.status_code == 403
                        else f"http_{response.status_code}"
                    )

            now = time.monotonic()
            renewed = (
                self._last_authenticated_at is None
                or now - self._last_authenticated_at >= SESSION_RENEWAL_AFTER
            )
            response = None
            if not renewed:
                response = ping()
                if response.status_code == 401:
                    renewed = True
                else:
                    require_ping(
                        response
                    )  # 403, rate limits and network failures never trigger login.
            if renewed:
                if self.createNewSession() is None:
                    self._require_available()
                    self._stop("session_renewal_failed")
            try:
                account = self.verify_account_environment()
            except (RuntimeError, KeyError, TypeError, ValueError, AttributeError):
                self._stop("session_identity_changed")
            if (
                self.environment != expected[0]
                or self.BASE_URL != expected[1]
                or account.get("accountId") != expected[2]
            ):
                self._stop("session_identity_changed")
            if renewed:
                response = ping()
                require_ping(
                    response
                )  # A second 401 is terminal; no authentication loop.
            self._last_cycle_ping = response.json()
            if (
                renewed
                and self.environment == "live"
                and self._live_execution is not None
            ):
                # createNewSession already disarmed this same gate. Preserve its ownership,
                # limits, lock, journal, pending intent and old tokens; never recreate/arm it.
                self._stop("live_session_renewed_requires_preflight")
            return renewed
        except KeyboardInterrupt:
            self._invalidate_session("request_interrupted")
            raise
        finally:
            self._preparing_cycle = False

    def getCstAndXSecurityToken(self) -> tuple[str, str] | None:
        if self.createNewSession() is not None:
            return self.cst, self.x_security_token
        return None

    def getEncryptionKey(self) -> dict | None:
        headers = {"X-CAP-API-KEY": self.X_CAP_API_KEY}
        res = self._request(
            "get",
            f"{self.BASE_URL}/api/v1/session/encryptionKey",
            headers=headers,
            timeout=20,
            allow_redirects=False,
        )
        if res.status_code == 200:
            self.encryption_key = res.json().get("encryptionKey")

            return res.json()

        return None

    def getSessionDetails(self) -> dict | None:
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "CST": self.cst}
        res = self._request(
            "get",
            f"{self.BASE_URL}/api/v1/session",
            headers=headers,
            timeout=20,
            allow_redirects=False,
        )
        if res.status_code == 200:
            return res.json()

        return None

    def createNewSession(self, encryptedPassword: bool = False) -> dict | None:
        self._require_available()
        self.cst = self.x_security_token = ""
        self._login_has_active_environment = False
        self._login_account_id = None
        if self._live_execution is not None:
            self._live_execution.armed = False
        boolean(encryptedPassword)
        password = self.PASSWORD
        if encryptedPassword:
            keys = self.getEncryptionKey()
            password = encrypt_password(
                password, keys["encryptionKey"], keys["timeStamp"]
            )
        self._session_generation += 1
        payload = {
            "identifier": self.IDENTIFIER,
            "password": password,
            "encryptedPassword": encryptedPassword,
        }
        headers = {
            "X-CAP-API-KEY": self.X_CAP_API_KEY,
            "Content-Type": "application/json",
        }
        res = self._request(
            "post",
            f"{self.BASE_URL}/api/v1/session",
            json=payload,
            headers=headers,
            timeout=20,
            allow_redirects=False,
        )
        if res.status_code == 200:
            cst = res.headers.get("CST")
            security_token = res.headers.get("X-SECURITY-TOKEN")
            if not cst or not security_token:
                self._invalidate_session("missing_session_tokens")
                print("Login response missing required session headers")
                return None
            details = res.json()
            flag = (
                "hasActiveLiveAccounts"
                if self.environment == "live"
                else "hasActiveDemoAccounts"
            )
            self._login_has_active_environment = (
                isinstance(details, dict) and details.get(flag) is True
            )
            self._login_account_id = (
                details.get("currentAccountId") if isinstance(details, dict) else None
            )
            self.cst, self.x_security_token = cst, security_token
            self._last_authenticated_at = time.monotonic()
            return details
        # Do not log response bodies, credentials, or session tokens.
        self._invalidate_session(
            "authentication_failed_http_401"
            if res.status_code == 401
            else "permission_denied_http_403"
            if res.status_code == 403
            else f"http_{res.status_code}"
        )
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
        eligible = [
            row
            for row in rows
            if row.get("status") == "ENABLED"
            and row.get("accountType") == "CFD"
            and row.get("demo", expected_demo) is expected_demo
        ]
        current = session.get("accountId")
        if current != self._login_account_id:
            raise RuntimeError("login_session_account_mismatch_no_switch_performed")
        active = [
            row
            for row in eligible
            if isinstance(current, str) and row.get("accountId") == current
        ]
        if len(active) != 1:
            raise RuntimeError("active_account_environment_mismatch")
        if self._account_selection is not None:
            selected = [
                row
                for row in eligible
                if isinstance(row.get("accountId"), str)
                and self._account_selection
                in (
                    row["accountId"],
                    hashlib.sha256(row["accountId"].encode()).hexdigest()[:12],
                )
            ]
            if len(selected) != 1 or selected[0]["accountId"] != current:
                raise RuntimeError("selected_account_not_active_no_switch_performed")
        elif self.environment == "live" and len(eligible) > 1:
            raise RuntimeError("multiple_live_accounts_require_explicit_selection")
        return active[0]

    def switchesActiveAccount(self, accountId: str, *, authorization=None) -> dict:
        text(accountId)
        self._authorize(authorization, "account-switch")
        accounts = self.getAllAccounts()["accounts"]
        matches = [
            a
            for a in accounts
            if a.get("accountId") == accountId
            and a.get("status") == "ENABLED"
            and a.get("accountType") == "CFD"
            and a.get("demo", True) is True
        ]
        if len(matches) != 1:
            raise PermissionError("target_account_not_verified")
        headers = {"CST": self.cst, "X-SECURITY-TOKEN": self.x_security_token}
        res = self._request(
            "put",
            self.BASE_URL + "/api/v1/session",
            headers=headers,
            json={"accountId": accountId},
        )
        if res.status_code != 200:
            self._stop(f"http_{res.status_code}", mutation=True)
        self.cst = res.headers.get("CST", "")
        self.x_security_token = res.headers.get("X-SECURITY-TOKEN", "")
        if not self.cst or not self.x_security_token:
            self._stop("missing_session_tokens", mutation=True)
        self._login_account_id = accountId
        self._account_selection = accountId
        self._session_generation += 1
        try:
            self.verify_account_environment()
        except (RuntimeError, KeyError, TypeError, ValueError):
            self._stop("account_verification_failed", mutation=True)
        return res.json()

    def logOutOfTheCurrentSession(self) -> dict:
        try:
            return self._api_call("delete", "/session")
        finally:
            self._invalidate_session("authentication_unavailable")
            self._session_generation += 1

import requests
from contextlib import contextmanager
import logging
from datetime import datetime, timezone
import time
from typing import List
from trading_bot.config.trading_config import endpoint, load_credentials
from trading_bot.api.network_errors import (
    REQUEST_TIMEOUT,
    TradingUnavailable,
    endpoint_template,
    network_failure_reason,
)
from trading_bot.api.retry import RETRYABLE_READ_STATUSES, RetryPolicy, is_transient
from urllib.parse import urlsplit

from trading_bot.api.rest import RestComponent
from trading_bot.api.accounts import AccountsAPI
from trading_bot.api.markets import MarketsAPI
from trading_bot.api.session import SessionAPI
from trading_bot.api.positions import PositionsAPI
from trading_bot.api.watchlists import WatchlistsAPI
from trading_bot.api.working_orders import WorkingOrdersAPI

_LOGGER = logging.getLogger(__name__)
_HTTP_METHODS = frozenset({"get", "post", "put", "delete", "patch", "head", "options"})


def _method_label(method):
    return method.upper() if method.lower() in _HTTP_METHODS else "UNKNOWN"


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
        self._read_retry_policy = None  # Opt-in; GET requests only.
        self._read_retry_deadline = None  # Optional monotonic bound on retry backoff.
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

    def configure_read_retries(self, policy: RetryPolicy | None) -> None:
        """Enable bounded retries for read-only GETs; ``None`` restores single attempts.

        This never affects login, logout or trade-changing requests.
        """
        if policy is not None and not isinstance(policy, RetryPolicy):
            raise TypeError("retry_policy_required")
        self._read_retry_policy = policy

    @contextmanager
    def read_retry_deadline(self, deadline):
        """Within this block, read-retry backoff never extends past ``deadline``.

        ``deadline`` is a ``time.monotonic()`` value. A retry that would start at or
        after it is not attempted, and the read fails with its network reason.
        """
        previous = getattr(self, "_read_retry_deadline", None)
        self._read_retry_deadline = deadline
        try:
            yield
        finally:
            self._read_retry_deadline = previous

    def _wait_for_read_retry(self, policy, retries, reason, method, path):
        """Sleep before another GET attempt; False when the request must stop now."""
        if policy is None or method.lower() != "get" or not is_transient(reason):
            return False
        labels = (self.environment.upper(), _method_label(method), endpoint_template(path))
        if retries >= policy.max_retries:
            if policy.max_retries:
                _LOGGER.error(
                    "API read retries exhausted environment=%s method=%s endpoint=%s "
                    "exception_category=%s attempts=%d",
                    *labels, reason, retries + 1,
                )
            return False
        delay = policy.delay(retries + 1)
        bound = getattr(self, "_read_retry_deadline", None)
        if bound is not None:
            remaining = bound - time.monotonic()
            if remaining <= 0:
                _LOGGER.error(
                    "API read retries stopped environment=%s method=%s endpoint=%s "
                    "exception_category=%s attempts=%d: confirmation deadline reached",
                    *labels, reason, retries + 1,
                )
                return False
            delay = min(delay, remaining)
        _LOGGER.warning(
            "API read retry scheduled environment=%s method=%s endpoint=%s "
            "exception_category=%s retry=%d max_retries=%d delay_seconds=%.2f",
            *labels, reason, retries + 1, policy.max_retries, delay,
        )
        try:
            policy.wait(delay)
        except KeyboardInterrupt:
            self._invalidate_session("request_interrupted")
            raise
        return True

    def _read_retry_deadline_reached(self, reason, method, path):
        """Whether a retry would start at or after the read-retry deadline.

        Checked after the backoff sleep and after rate limiting, immediately before
        the retry would reach the transport.
        """
        bound = getattr(self, "_read_retry_deadline", None)
        if bound is None or time.monotonic() < bound:
            return False
        _LOGGER.error(
            "API read retries stopped environment=%s method=%s endpoint=%s "
            "exception_category=%s: confirmation deadline reached before the retry started",
            self.environment.upper(), _method_label(method), endpoint_template(path), reason,
        )
        return True

    def _live_send_checkpoint(self):
        """Inside the LIVE gate: refuse a request whose strategy cycle has expired.

        Raised before the transport is called, so the gate knows nothing was sent.
        """
        # Only a strategy gate has a cycle; the integration round-trip gate has none.
        window_open = getattr(self._live_execution, "send_window_open", None)
        if window_open is not None and window_open() is not True:
            from trading_bot.api.rest import SendWindowClosed

            raise SendWindowClosed()

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
        gated_live = (
            self.environment == "live"
            and method.lower() != "get"
            and not (path == "/api/v1/session" and method.lower() == "post")
        )
        if gated_live:
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

        kwargs.update(timeout=REQUEST_TIMEOUT, allow_redirects=False, verify=True)
        # Only a read-only GET may be retried. Login, logout and every trade-changing
        # request are sent once: an unknown mutation outcome is never resubmitted.
        policy = self._read_retry_policy if method.lower() == "get" else None
        retries = 0
        last_reason = None
        while True:
            throttle(
                self.X_CAP_API_KEY,
                self.environment,
                method.lower(),
                path,
                self._login_account_id,
                user_identifier=self.IDENTIFIER,
            )
            # Rate limiting can sleep. Deadlines are rechecked after it, immediately
            # before the transport: nothing below this point is sent late.
            if gated_live:
                self._live_send_checkpoint()
            if retries and self._read_retry_deadline_reached(last_reason, method, path):
                self._stop(last_reason)
            request_started = time.monotonic()
            try:
                res = getattr(requests, method)(url, **kwargs)
            except KeyboardInterrupt:
                self._mutation_outcome_uncertain |= mutation
                self._invalidate_session("request_interrupted")
                raise
            except (requests.exceptions.RequestException, OSError) as exc:
                reason = network_failure_reason(exc)
                _LOGGER.warning(
                    "API transport failure timestamp=%s environment=%s method=%s "
                    "endpoint=%s elapsed_seconds=%.3f exception_category=%s",
                    datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                    self.environment.upper(),
                    _method_label(method),
                    endpoint_template(path),
                    time.monotonic() - request_started,
                    reason,
                )
                if self._wait_for_read_retry(policy, retries, reason, method, path):
                    retries += 1
                    last_reason = reason
                    continue
                self._stop(reason, mutation=mutation)
            if policy is not None and res.status_code in RETRYABLE_READ_STATUSES:
                if self._wait_for_read_retry(
                    policy, retries, f"http_{res.status_code}", method, path
                ):
                    retries += 1
                    last_reason = f"http_{res.status_code}"
                    continue
            break
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

    def wait_until_open(self, check_interval=60, *, poll=None):
        """
        Pauses execution until the market open conditions are met.

        ``poll`` keeps running while the market is closed, with the same contract as in
        ``wait_until_targets``: called at once, then again after the number of seconds
        it returns, until it returns None. It must perform read-only checks only; its
        failures propagate to the caller. The market is still checked every
        ``check_interval`` seconds.
        """
        next_poll = time.monotonic() if poll is not None else None
        while not self.is_market_open():
            print(f"[{datetime.now(timezone.utc)}] Market closed. Waiting...")
            if poll is None:
                time.sleep(check_interval)
                continue
            wake = time.monotonic() + check_interval
            while next_poll is not None and next_poll < wake:
                now = time.monotonic()
                if now >= next_poll:
                    pause = poll()  # Read-only; never evaluates a strategy or sends an order.
                    next_poll = None if pause is None else time.monotonic() + max(0.0, pause)
                    continue
                time.sleep(next_poll - now)
            time.sleep(max(0.0, wake - time.monotonic()))

        print("Market is now OPEN!")

    def write_keys(self):
        """Compatibility no-op: shared .env must never cache unlabelled session tokens."""
        return None

    def load_keys(self):
        """Compatibility no-op: tokens belong only to this client and environment."""
        return None

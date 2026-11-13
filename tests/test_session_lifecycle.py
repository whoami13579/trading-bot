"""Deterministic broker/session clocks; no network, credentials or private journals."""

from collections import defaultdict, deque
from contextlib import ExitStack, redirect_stdout
from datetime import datetime
from decimal import Decimal
import io
import json
from types import SimpleNamespace
from urllib.parse import urlsplit
import unittest
from unittest.mock import Mock, PropertyMock, patch

import requests
from trading_bot.api.client import TradingApi
from trading_bot.api.network_errors import TradingUnavailable
from trading_bot.config.trading_config import DEMO_URL, LIVE_URL
from trading_bot.risk.live_execution import LiveExecution
from trading_bot.utils import scheduler


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Broker:
    def __init__(self, clock, environment="demo"):
        self.clock, self.environment = clock, environment
        self.generation = 0
        self.account_id = "fixture-account-original"
        self.account_on_login = None
        self.demo_override = None
        self.last_use = clock.now
        self.revoked = False
        self.calls = []
        self.failures = defaultdict(deque)

    def response(self, status=200, value=None, headers=None):
        result = requests.Response()
        result.status_code = status
        result._content = json.dumps(value if value is not None else {}).encode()
        result.headers.update(headers or {})
        return result

    @property
    def tokens(self):
        return (
            "fixture-cst-generation-" + str(self.generation),
            "fixture-security-generation-" + str(self.generation),
        )

    def request(self, method, url, **kwargs):
        path = urlsplit(url).path
        self.calls.append((method, path, kwargs))
        self.assert_host(url)
        queued = self.failures[(method, path)]
        if queued:
            outcome = queued.popleft()
            if isinstance(outcome, BaseException):
                raise outcome
            return self.response(outcome, {"errorCode": "private-server-message"})
        if method == "post" and path == "/api/v1/session":
            self.generation += 1
            self.revoked = False
            self.last_use = self.clock.now
            if self.account_on_login:
                self.account_id = self.account_on_login
            return self.response(
                value={
                    "currentAccountId": self.account_id,
                    "hasActiveDemoAccounts": True,
                    "hasActiveLiveAccounts": True,
                },
                headers=dict(zip(("CST", "X-SECURITY-TOKEN"), self.tokens)),
            )
        headers = kwargs.get("headers", {})
        if (
            self.revoked
            or self.clock.now - self.last_use >= 600
            or (headers.get("CST"), headers.get("X-SECURITY-TOKEN")) != self.tokens
        ):
            return self.response(401, {"errorCode": "expired-fixture-session"})
        self.last_use = self.clock.now
        if path == "/api/v1/ping":
            return self.response(value={"status": "OK"})
        if path == "/api/v1/session":
            return self.response(value={"accountId": self.account_id})
        if path == "/api/v1/accounts":
            demo = (
                self.environment == "demo"
                if self.demo_override is None
                else self.demo_override
            )
            return self.response(
                value={
                    "accounts": [
                        {
                            "accountId": self.account_id,
                            "demo": demo,
                            "accountType": "CFD",
                            "status": "ENABLED",
                        }
                    ]
                }
            )
        if path == "/api/v1/prices/EURUSD":
            count = kwargs.get("params", {}).get("max", 48)
            return self.response(
                value={
                    "prices": [
                        {"closePrice": {"ask": 1.0 + i / 10000}} for i in range(count)
                    ]
                }
            )
        if path.startswith("/api/v1/positions") or path.startswith(
            "/api/v1/workingorders"
        ):
            return self.response(value={"dealReference": "fixture-reference"})
        raise AssertionError("Unexpected fixture route")

    def assert_host(self, url):
        assert url.startswith(
            (DEMO_URL if self.environment == "demo" else LIVE_URL) + "/api/v1/"
        )

    def count(self, method, path):
        return sum(m == method and p == path for m, p, _ in self.calls)


class SessionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.broker = Broker(self.clock)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("trading_bot.api.rate_limits.throttle"))
        self.stack.enter_context(
            patch(
                "trading_bot.api.client.time.monotonic",
                side_effect=self.clock.monotonic,
            )
        )
        for method in ("get", "post", "put", "delete"):
            self.stack.enter_context(
                patch(
                    "trading_bot.api.client.requests." + method,
                    side_effect=lambda url, _method=method, **kwargs: (
                        self.broker.request(_method, url, **kwargs)
                    ),
                )
            )
        self.api = self.start_client()

    def start_client(self, environment="demo"):
        api = TradingApi(
            "fixture-api-key",
            "fixture-identifier",
            "fixture-password",
            environment=environment,
            auto_authenticate=False,
        )
        self.assertIsNotNone(api.createNewSession())
        api.pin_session_identity(api.verify_account_environment())
        return api

    def test_initial_login_pins_environment_and_account(self):
        self.assertEqual(
            self.api._cycle_identity, ("demo", DEMO_URL, self.broker.account_id)
        )
        self.assertEqual(self.api._last_authenticated_at, self.clock.now)
        self.assertFalse(self.api.prepare_trading_cycle())
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 1)

    def test_original_hma_flow_reproduces_expired_history_get_with_headers_present(
        self,
    ):
        self.clock.advance(1385.92)
        self.api.load_keys()
        with self.assertRaises(TradingUnavailable) as caught:
            self.api.getHistoricalPricesList("EURUSD", "MINUTE_30", 48)
        self.assertEqual(caught.exception.status_code, 401)
        method, path, kwargs = self.broker.calls[-1]
        self.assertEqual((method, path), ("get", "/api/v1/prices/EURUSD"))
        self.assertEqual(kwargs["params"], {"resolution": "MINUTE_30", "max": 48})
        self.assertTrue(
            kwargs["headers"]["CST"] and kwargs["headers"]["X-SECURITY-TOKEN"]
        )
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 1)

    def test_long_scheduled_sleep_renews_once_before_history_and_uses_new_headers(self):
        old = self.broker.tokens
        with (
            patch.object(scheduler, "datetime") as now,
            patch.object(scheduler.time, "sleep", side_effect=self.clock.advance),
        ):
            now.now.return_value = datetime(2026, 11, 12, 21, 36, 56)
            scheduler.wait_until_targets([0, 30])
        self.assertGreater(self.clock.now - 1000, 600)
        self.assertTrue(self.api.prepare_trading_cycle())
        payload, status = self.api.getHistoricalPrices("EURUSD", "MINUTE_30", 48)
        self.assertEqual((status, len(payload["prices"])), (200, 48))
        self.assertNotEqual(old, self.broker.tokens)
        for method, path, kwargs in self.broker.calls:
            if path == "/api/v1/prices/EURUSD":
                self.assertEqual(
                    (kwargs["headers"]["CST"], kwargs["headers"]["X-SECURITY-TOKEN"]),
                    self.broker.tokens,
                )
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 2)
        self.assertFalse(any(m == "put" for m, _, _ in self.broker.calls))

    def test_nine_minute_safety_margin_refreshes_before_ten_minute_timeout(self):
        self.clock.advance(539)
        self.assertFalse(self.api.prepare_trading_cycle())
        self.clock.advance(540)
        self.assertTrue(self.api.prepare_trading_cycle())
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 2)

    def test_public_time_endpoint_does_not_extend_authenticated_activity(self):
        before = self.api._last_authenticated_at
        with patch(
            "trading_bot.api.client.requests.get",
            return_value=self.broker.response(value={"serverTime": 42}),
        ):
            self.clock.advance(400)
            self.api.getServerTime()
        self.assertEqual(self.api._last_authenticated_at, before)

    def test_short_cycles_keep_session_alive_without_unnecessary_logins(self):
        for _ in range(25):
            self.clock.advance(60)
            self.assertFalse(self.api.prepare_trading_cycle())
            self.assertEqual(
                self.api.getHistoricalPrices("EURUSD", "MINUTE_30", 48)[1], 200
            )
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 1)

    def test_continued_operation_over_multiple_long_cycles(self):
        for _ in range(4):
            self.clock.advance(1800)
            self.assertTrue(self.api.prepare_trading_cycle())
            self.assertEqual(
                self.api.getHistoricalPrices("EURUSD", "MINUTE_30", 48)[1], 200
            )
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 5)

    def test_unexpected_ping_401_allows_one_bounded_renewal(self):
        self.broker.revoked = True
        self.assertTrue(self.api.prepare_trading_cycle())
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 2)
        self.assertEqual(self.broker.count("get", "/api/v1/ping"), 2)

    def test_second_401_after_renewal_is_terminal(self):
        self.broker.failures[("get", "/api/v1/ping")].extend([401, 401])
        with self.assertRaises(TradingUnavailable):
            self.api.prepare_trading_cycle()
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 2)
        with self.assertRaises(TradingUnavailable):
            self.api.prepare_trading_cycle()
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 2)

    def test_403_ping_never_triggers_login(self):
        self.broker.failures[("get", "/api/v1/ping")].append(403)
        with self.assertRaises(TradingUnavailable) as caught:
            self.api.prepare_trading_cycle()
        self.assertEqual(caught.exception.reason, "permission_denied_http_403")
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 1)

    def test_legacy_ping_403_never_triggers_login(self):
        self.api._cycle_identity = None
        self.broker.failures[("get", "/api/v1/ping")].append(403)
        with self.assertRaises(TradingUnavailable):
            self.api.pingService()
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 1)

    def test_pinned_ping_uses_identity_checked_renewal(self):
        self.clock.advance(610)
        self.assertEqual(self.api.pingService(), ({"status": "OK"}, 200))
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 2)

    def test_renewal_rejects_changed_account_without_switching_it(self):
        self.clock.advance(610)
        self.broker.account_on_login = "fixture-unexpected-account"
        with self.assertRaises(TradingUnavailable) as caught:
            self.api.prepare_trading_cycle()
        self.assertEqual(caught.exception.reason, "session_identity_changed")
        self.assertFalse(self.api.cst or self.api.x_security_token)
        self.assertFalse(any(m == "put" for m, _, _ in self.broker.calls))

    def test_account_drift_without_renewal_stops(self):
        self.broker.account_id = "fixture-unexpected-account"
        with self.assertRaises(TradingUnavailable):
            self.api.prepare_trading_cycle()
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 1)

    def test_environment_change_stops_before_any_request(self):
        count = len(self.broker.calls)
        self.api._environment = "live"
        with self.assertRaises(TradingUnavailable) as caught:
            self.api.prepare_trading_cycle()
        self.assertEqual(caught.exception.reason, "session_identity_changed")
        self.assertEqual(len(self.broker.calls), count)

    def test_invalid_environment_change_stops_without_resolving_an_endpoint(self):
        before = len(self.broker.calls)
        self.api._environment = "invalid-environment"
        with self.assertRaises(TradingUnavailable) as caught:
            self.api.prepare_trading_cycle()
        self.assertEqual(caught.exception.reason, "session_identity_changed")
        self.assertEqual(len(self.broker.calls), before)

    def test_host_change_stops_before_any_request(self):
        count = len(self.broker.calls)
        with (
            patch.object(
                TradingApi, "BASE_URL", new_callable=PropertyMock, return_value=LIVE_URL
            ),
            self.assertRaises(TradingUnavailable),
        ):
            self.api.prepare_trading_cycle()
        self.assertEqual(len(self.broker.calls), count)

    def test_renewed_account_with_wrong_environment_is_rejected(self):
        self.clock.advance(610)
        self.broker.demo_override = False
        with self.assertRaises(TradingUnavailable) as caught:
            self.api.prepare_trading_cycle()
        self.assertEqual(caught.exception.reason, "session_identity_changed")

    def test_renewal_failure_never_retries_or_uses_old_tokens(self):
        for status in (401, 403):
            with self.subTest(status=status):
                api = self.start_client()
                self.clock.advance(610)
                self.broker.failures[("post", "/api/v1/session")].append(status)
                before = self.broker.count("post", "/api/v1/session")
                with (
                    redirect_stdout(io.StringIO()),
                    self.assertRaises(TradingUnavailable),
                ):
                    api.prepare_trading_cycle()
                self.assertEqual(
                    self.broker.count("post", "/api/v1/session"), before + 1
                )
                self.assertFalse(api.cst or api.x_security_token)

    def test_timeout_during_probe_or_login_latches_and_never_retries(self):
        for method, path in (("get", "/api/v1/ping"), ("post", "/api/v1/session")):
            with self.subTest(method=method):
                api = self.start_client()
                if method == "post":
                    self.clock.advance(610)
                self.broker.failures[(method, path)].append(
                    requests.ReadTimeout("private-token-marker")
                )
                before = len(self.broker.calls)
                with self.assertRaises(TradingUnavailable) as caught:
                    api.prepare_trading_cycle()
                self.assertNotIn("private-token-marker", str(caught.exception))
                self.assertEqual(len(self.broker.calls), before + 1)
                with self.assertRaises(TradingUnavailable):
                    api.prepare_trading_cycle()
                self.assertEqual(len(self.broker.calls), before + 1)

    def test_interrupted_session_request_invalidates_tokens_and_remains_stopped(self):
        self.broker.failures[("get", "/api/v1/ping")].append(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.api.prepare_trading_cycle()
        self.assertFalse(self.api.cst or self.api.x_security_token)
        self.assertFalse(self.api._preparing_cycle)
        with self.assertRaises(TradingUnavailable):
            self.api.prepare_trading_cycle()

    def test_data_get_401_is_terminal_and_is_not_automatically_replayed(self):
        self.api.prepare_trading_cycle()
        self.broker.failures[("get", "/api/v1/prices/EURUSD")].append(401)
        with self.assertRaises(TradingUnavailable):
            self.api.getHistoricalPrices("EURUSD", "MINUTE_30", 48)
        with self.assertRaises(TradingUnavailable):
            self.api.prepare_trading_cycle()
        self.assertEqual(self.broker.count("get", "/api/v1/prices/EURUSD"), 1)
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 1)

    def test_mutation_auth_errors_never_reauthenticate_or_duplicate_any_order(self):
        operations = [
            (
                "post",
                "/api/v1/positions",
                lambda a: a.createPosition("EURUSD", "BUY", 100),
            ),
            ("delete", "/api/v1/positions/owned", lambda a: a.closePosition("owned")),
            (
                "put",
                "/api/v1/positions/owned",
                lambda a: a.updatePosition(
                    "owned",
                    stopDistance=0.01,
                    authorization=a.authorize_mutation(
                        "position-update", resource_id="owned"
                    ),
                ),
            ),
            (
                "post",
                "/api/v1/workingorders",
                lambda a: a.createWorkingOrder(
                    "EURUSD",
                    "BUY",
                    100,
                    1.0,
                    "LIMIT",
                    authorization=a.authorize_mutation("working-order"),
                ),
            ),
        ]
        for status in (401, 403):
            for method, path, operation in operations:
                with self.subTest(status=status, path=path):
                    api = self.start_client()
                    before_logins = self.broker.count("post", "/api/v1/session")
                    before_orders = self.broker.count(method, path)
                    self.broker.failures[(method, path)].append(status)
                    with self.assertRaises(TradingUnavailable):
                        operation(api)
                    with self.assertRaises(TradingUnavailable):
                        api.prepare_trading_cycle()
                    with self.assertRaises(TradingUnavailable):
                        operation(api)
                    self.assertEqual(self.broker.count(method, path), before_orders + 1)
                    self.assertEqual(
                        self.broker.count("post", "/api/v1/session"), before_logins
                    )

    def test_interrupted_order_is_not_retried_by_the_next_cycle(self):
        self.broker.failures[("post", "/api/v1/positions")].append(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.api.createPosition("EURUSD", "BUY", 100)
        with self.assertRaises(TradingUnavailable) as stopped:
            self.api.prepare_trading_cycle()
        self.assertTrue(stopped.exception.outcome_uncertain)
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 1)

    def test_live_gate_is_not_rearmed_or_replaced_on_renewal(self):
        self.broker.environment = "live"
        api = self.start_client("live")
        args = SimpleNamespace(stop_distance=Decimal(".001"))
        gate = LiveExecution(api, args, "EURUSD", 100)
        gate.armed = True
        gate.owned = {"fixture-owned": {"direction": "BUY"}}
        gate.open_count = 1
        gate.charges = Decimal(".5")
        gate.pending = True
        gate.lock = Mock()
        original_lock = gate.lock
        old_tokens = gate.tokens
        api._live_execution = gate
        self.clock.advance(610)
        with (
            patch.object(gate, "arm") as arm,
            patch.object(gate, "_save") as save,
            self.assertRaises(TradingUnavailable) as caught,
        ):
            api.prepare_trading_cycle()
        self.assertEqual(
            caught.exception.reason, "live_session_renewed_requires_preflight"
        )
        self.assertIs(api._live_execution, gate)
        self.assertFalse(gate.armed)
        self.assertTrue(gate.pending)
        self.assertEqual(gate.owned, {"fixture-owned": {"direction": "BUY"}})
        self.assertEqual(
            (gate.tokens, gate.open_count, gate.charges), (old_tokens, 1, Decimal(".5"))
        )
        self.assertIs(gate.lock, original_lock)
        arm.assert_not_called()
        save.assert_not_called()
        self.assertFalse(
            any(m != "get" and p != "/api/v1/session" for m, p, _ in self.broker.calls)
        )

    def test_live_readonly_session_can_renew_but_still_cannot_trade(self):
        self.broker.environment = "live"
        api = self.start_client("live")
        self.clock.advance(610)
        self.assertTrue(api.prepare_trading_cycle())
        from trading_bot.risk.live_execution import LiveSafetyStop

        with self.assertRaises(LiveSafetyStop):
            api.createPosition("EURUSD", "BUY", 100)
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 0)

    def test_cycle_cannot_pin_a_different_account_or_start_without_identity(self):
        with self.assertRaises(TradingUnavailable):
            self.api.pin_session_identity({"accountId": "other"})
        api = self.start_client()
        api._cycle_identity = None
        with self.assertRaises(TradingUnavailable):
            api.prepare_trading_cycle()

    def test_renewal_with_missing_session_headers_stops_without_old_token_reuse(self):
        self.clock.advance(610)
        broken = self.broker.response(
            value={"currentAccountId": self.broker.account_id},
            headers={"CST": "fixture-incomplete-token"},
        )
        with (
            patch("trading_bot.api.client.requests.post", return_value=broken),
            redirect_stdout(io.StringIO()),
            self.assertRaises(TradingUnavailable) as caught,
        ):
            self.api.prepare_trading_cycle()
        self.assertEqual(caught.exception.reason, "missing_session_tokens")
        self.assertFalse(self.api.cst or self.api.x_security_token)
        self.assertFalse(self.api._preparing_cycle)

    def test_malformed_account_rows_stop_the_cycle_with_fixed_diagnostics(self):
        original = self.api.getAllAccounts
        self.api.getAllAccounts = Mock(return_value={"accounts": ["private-marker"]})
        try:
            with self.assertRaises(TradingUnavailable) as caught:
                self.api.prepare_trading_cycle()
            self.assertEqual(caught.exception.reason, "session_identity_changed")
            self.assertNotIn("private-marker", str(caught.exception))
        finally:
            self.api.getAllAccounts = original

    def test_confirmation_auth_failure_does_not_cause_a_second_opening_order(self):
        self.api.createPosition("EURUSD", "BUY", 100)
        self.broker.failures[("get", "/api/v1/confirms/fixture-reference")].append(401)
        with self.assertRaises(TradingUnavailable):
            self.api.getPositionOrderConfirmation("fixture-reference")
        with self.assertRaises(TradingUnavailable):
            self.api.prepare_trading_cycle()
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 1)

    def test_live_scheduler_keepalive_preserves_tokens_gate_and_limits(self):
        self.broker.environment = "live"
        api = self.start_client("live")
        gate = LiveExecution(
            api, SimpleNamespace(stop_distance=Decimal(".001")), "EURUSD", 100
        )
        gate.armed = True
        gate.open_count = 1
        gate.charges = Decimal(".5")
        api._live_execution = gate
        tokens = gate.tokens
        logins = self.broker.count("post", "/api/v1/session")
        with (
            patch.object(scheduler, "datetime") as now,
            patch.object(scheduler.time, "sleep", side_effect=self.clock.advance),
            redirect_stdout(io.StringIO()),
        ):
            now.now.return_value = datetime(2026, 11, 12, 21, 36, 56)
            scheduler.wait_until_targets([0, 30], keepalive=api.prepare_trading_cycle)
        self.assertFalse(api.prepare_trading_cycle())
        self.assertEqual(self.broker.count("post", "/api/v1/session"), logins)
        self.assertEqual((api.cst, api.x_security_token), tokens)
        self.assertTrue(gate.armed)
        self.assertEqual((gate.open_count, gate.charges), (1, Decimal(".5")))
        self.assertIs(api._live_execution, gate)

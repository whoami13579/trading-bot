"""Issue #3: transient network failures, pause/resume and trading safety.

Offline and deterministic: a fake broker, a fake clock, no credentials, no sockets and
no real orders. The real TradingApi, scheduler, supervisor and HMA loop are exercised.
"""

from collections import defaultdict, deque
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import logging
import random
import signal
import socket
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import MaxRetryError, NameResolutionError, ProtocolError

from trading_bot.api.client import TradingApi
from trading_bot.api.network_errors import TradingUnavailable
from trading_bot.api.retry import RetryPolicy, TRANSIENT_REASONS, is_transient
from trading_bot.config.trading_config import DEMO_URL
from trading_bot.strategies import hma
from trading_bot.utils import scheduler
from trading_bot.utils.network_recovery import (
    CycleGuard,
    GracefulShutdown,
    NetworkSupervisor,
    RecoveryPolicy,
    ShutdownRequested,
    reconcile_broker_state,
)

CREDENTIALS = {
    "X-CAP-API-KEY": "fixture-key-private",
    "identifier": "fixture-login-private",
    "password": "fixture-password-private",
}
SECRETS = (
    *CREDENTIALS.values(),
    "fixture-cst",
    "fixture-security",
    "fixture-account-private",
    "fixture-deal-private",
    "private-marker",
    "https://",
    "Traceback",
)


def dns_error():
    cause = socket.gaierror(socket.EAI_AGAIN, "private-marker")
    reason = NameResolutionError("private-marker", None, cause)
    return requests.ConnectionError(MaxRetryError(None, "/private-marker", reason))


def reset_error():
    return requests.ConnectionError(
        ProtocolError("Connection aborted.", ConnectionResetError(104, "private-marker"))
    )


class FakeClock:
    """One clock for wall time, monotonic time and every sleep."""

    def __init__(self, local_start):
        self.start = local_start  # Naive local time, as used by the scheduler.
        self.elapsed = 0.0
        self.sleeps = []
        self.on_sleep = None

    def monotonic(self):
        return 1000.0 + self.elapsed

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep(seconds)
        self.elapsed += max(0.0, seconds)

    def local(self):
        return self.start + timedelta(seconds=self.elapsed)

    def utc(self):
        return self.local().astimezone(timezone.utc)


class Broker:
    """Stateful DEMO broker: sessions, prices, positions, working orders and outages."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.failures = defaultdict(deque)
        self.outages = []  # (start, end) naive local times: every request fails DNS.
        self.positions = []
        self.working = []
        self.account_id = "fixture-account-private"
        self.account_on_login = None
        self.login_status = 200
        self.generation = 0
        self.orders = 0
        self.trend = 1  # Rising closes give a BUY HMA signal.

    @property
    def tokens(self):
        return (f"fixture-cst-{self.generation}", f"fixture-security-{self.generation}")

    @staticmethod
    def response(status=200, value=None, headers=None):
        reply = requests.Response()
        reply.status_code = status
        reply._content = json.dumps({} if value is None else value).encode()
        reply.headers.update(headers or {})
        return reply

    def request(self, method, url, **kwargs):
        path = urlsplit(url).path
        assert url.startswith(DEMO_URL + "/api/v1/"), "only the DEMO host is allowed"
        now = self.clock.local()
        self.calls.append((method, path, now))
        if any(start <= now < end for start, end in self.outages):
            raise dns_error()
        queued = self.failures[(method, path)]
        if queued:
            outcome = queued.popleft()
            if isinstance(outcome, BaseException):
                raise outcome
            return self.response(outcome, {"errorCode": "private-marker"})
        if method == "get" and path == "/api/v1/time":
            return self.response(value={"serverTime": 1})
        if method == "post" and path == "/api/v1/session":
            if self.login_status != 200:
                return self.response(self.login_status, {"errorCode": "private-marker"})
            self.generation += 1
            if self.account_on_login:
                self.account_id = self.account_on_login
            return self.response(
                value={"currentAccountId": self.account_id, "hasActiveDemoAccounts": True},
                headers=dict(zip(("CST", "X-SECURITY-TOKEN"), self.tokens)),
            )
        headers = kwargs.get("headers", {})
        if (headers.get("CST"), headers.get("X-SECURITY-TOKEN")) != self.tokens:
            return self.response(401, {"errorCode": "private-marker"})
        if method == "get" and path == "/api/v1/ping":
            return self.response(value={"status": "OK"})
        if method == "get" and path == "/api/v1/session":
            return self.response(value={"accountId": self.account_id})
        if method == "get" and path == "/api/v1/accounts":
            return self.response(value={"accounts": [{
                "accountId": self.account_id, "demo": True,
                "accountType": "CFD", "status": "ENABLED",
            }]})
        if method == "get" and path == "/api/v1/prices/EURUSD":
            count = kwargs.get("params", {}).get("max", 48)
            return self.response(value={"prices": [
                {"closePrice": {"ask": 1.1 + self.trend * i / 10000}} for i in range(count)
            ]})
        if method == "get" and path == "/api/v1/positions":
            return self.response(value={"positions": self.positions})
        if method == "post" and path == "/api/v1/positions":
            self.orders += 1
            payload = kwargs["json"]
            self.positions.append({
                "market": {"epic": payload["epic"]},
                "position": {"dealId": f"fixture-deal-private-{self.orders}",
                             "direction": payload["direction"], "size": payload["size"]},
            })
            return self.response(value={"dealReference": "o_private-marker"})
        if method == "delete" and path.startswith("/api/v1/positions/"):
            deal_id = path.rsplit("/", 1)[1]
            self.positions = [p for p in self.positions if p["position"]["dealId"] != deal_id]
            return self.response(value={"dealReference": "p_private-marker"})
        if method == "get" and path == "/api/v1/workingorders":
            return self.response(value={"workingOrders": self.working})
        raise AssertionError("unexpected fixture route")

    def count(self, method, path, since=None):
        return sum(m == method and p == path and (since is None or t >= since)
                   for m, p, t in self.calls)

    def times(self, method, path):
        return [t for m, p, t in self.calls if m == method and p == path]


def broker_patches(stack, broker, clock):
    """Route the real client to the broker and every clock to the fake clock."""
    stack.enter_context(patch("trading_bot.api.client.load_credentials", return_value=CREDENTIALS))
    stack.enter_context(patch("trading_bot.api.rate_limits.throttle"))
    for method in ("get", "post", "put", "delete"):
        stack.enter_context(patch(
            "trading_bot.api.client.requests." + method,
            side_effect=lambda url, _m=method, **kwargs: broker.request(_m, url, **kwargs),
        ))
    now = stack.enter_context(patch.object(scheduler, "datetime"))
    now.now.side_effect = clock.local
    stack.enter_context(patch("time.sleep", side_effect=clock.sleep))
    stack.enter_context(patch("time.monotonic", side_effect=clock.monotonic))
    stack.enter_context(patch("trading_bot.utils.network_recovery._utc_now", side_effect=clock.utc))
    stack.enter_context(patch("random.uniform", return_value=0.0))  # Deterministic jitter.


def client(policy=None, environment="demo"):
    api = TradingApi(*CREDENTIALS.values(), environment=environment, auto_authenticate=False)
    api.cst, api.x_security_token = "fixture-cst", "fixture-security"
    api._login_account_id = "fixture-account-private"
    api.configure_read_retries(policy)
    return api


def ok(value=None):
    return Broker.response(value=value if value is not None else {"status": "OK"})


def recorded_policy(retries=3):
    sleeps = []
    return RetryPolicy(max_retries=retries, jitter=0.0, sleep=sleeps.append), sleeps


def assert_redacted(case, *texts):
    combined = "\n".join(texts)
    for secret in SECRETS:
        case.assertNotIn(secret, combined)


class RetryPolicyTests(unittest.TestCase):
    def test_default_backoff_is_about_two_four_eight_seconds(self):
        policy = RetryPolicy(jitter=0.0)
        self.assertEqual(policy.max_retries, 3)
        self.assertEqual([policy.delay(n) for n in (1, 2, 3)], [2.0, 4.0, 8.0])

    def test_jitter_stays_within_bounds_and_delay_is_capped(self):
        policy = RetryPolicy(max_retries=10, rng=random.Random(7))
        for retry in (1, 2, 3):
            nominal = 2.0 * 2 ** (retry - 1)
            for _ in range(50):
                self.assertTrue(nominal * 0.8 <= policy.delay(retry) <= nominal * 1.2)
        self.assertEqual(RetryPolicy(max_retries=10, jitter=0.0).delay(10), 30.0)

    def test_invalid_configuration_is_rejected(self):
        for kwargs in ({"max_retries": -1}, {"max_retries": 11}, {"max_retries": True},
                       {"base_delay": float("nan")}, {"base_delay": -1},
                       {"base_delay": 10, "max_delay": 5}, {"jitter": 1.0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RetryPolicy(**kwargs)

    def test_only_network_outages_are_transient(self):
        for reason in ("dns_resolution_failed", "connection_failed", "connection_timeout",
                       "read_timeout", "network_timeout", "proxy_connection_failed",
                       "http_429", "http_502", "http_503", "http_504"):
            self.assertTrue(is_transient(reason), reason)
        for reason in ("authentication_failed_http_401", "permission_denied_http_403",
                       "tls_connection_failed", "network_request_failed", "http_400",
                       "http_500", "invalid_api_response", "session_identity_changed",
                       "session_renewal_failed", "request_interrupted", None):
            self.assertFalse(is_transient(reason), reason)


class ReadRetryTests(unittest.TestCase):
    def test_temporary_dns_failure_is_retried_with_backoff_then_succeeds(self):
        policy, sleeps = recorded_policy()
        api = client(policy)
        with (
            patch("trading_bot.api.rate_limits.throttle") as throttle,
            patch("trading_bot.api.client.requests.get",
                  side_effect=[dns_error(), dns_error(), ok()]) as get,
            self.assertLogs("trading_bot.api.client", level="WARNING") as logs,
        ):
            payload, status = api.pingService()
        self.assertEqual((payload, status), ({"status": "OK"}, 200))
        self.assertEqual(get.call_count, 3)
        self.assertEqual(throttle.call_count, 3)  # Every attempt respects rate limits.
        self.assertEqual(sleeps, [2.0, 4.0])
        self.assertIsNone(api._unavailable)
        self.assertEqual((api.cst, api.x_security_token), ("fixture-cst", "fixture-security"))
        retries = [line for line in logs.output if "retry scheduled" in line]
        self.assertEqual(len(retries), 2)
        self.assertIn("endpoint=/api/v1/ping exception_category=dns_resolution_failed "
                      "retry=1 max_retries=3 delay_seconds=2.00", retries[0])
        assert_redacted(self, *logs.output)

    def test_repeated_dns_failures_exhaust_the_bounded_budget_and_latch(self):
        policy, sleeps = recorded_policy()
        api = client(policy)
        with (
            patch("trading_bot.api.rate_limits.throttle"),
            patch("trading_bot.api.client.requests.get", side_effect=dns_error()) as get,
            self.assertLogs("trading_bot.api.client", level="WARNING") as logs,
            self.assertRaises(TradingUnavailable) as stopped,
        ):
            api.getHistoricalPricesList("EURUSD", "MINUTE_30", 48)
        self.assertEqual(get.call_count, 4)  # One request plus exactly three retries.
        self.assertEqual(sleeps, [2.0, 4.0, 8.0])
        self.assertEqual(stopped.exception.reason, "dns_resolution_failed")
        self.assertFalse(stopped.exception.outcome_uncertain)
        self.assertTrue(any("retries exhausted" in line and "attempts=4" in line for line in logs.output))
        with patch("trading_bot.api.client.requests.get") as later, self.assertRaises(TradingUnavailable):
            api.getServerTime()
        later.assert_not_called()  # The stopped client is never revived.
        assert_redacted(self, *logs.output, str(stopped.exception))

    def test_connection_timeout_and_connection_reset_are_retried(self):
        for error, category in ((requests.ConnectTimeout("private-marker"), "connection_timeout"),
                                (reset_error(), "connection_failed"),
                                (requests.ReadTimeout("private-marker"), "read_timeout")):
            with self.subTest(category=category):
                policy, sleeps = recorded_policy()
                api = client(policy)
                with (
                    patch("trading_bot.api.rate_limits.throttle"),
                    patch("trading_bot.api.client.requests.get",
                          side_effect=[error, ok({"accountId": "x"})]) as get,
                    self.assertLogs("trading_bot.api.client", level="WARNING") as logs,
                ):
                    self.assertEqual(api.getSessionDetails(), {"accountId": "x"})
                self.assertEqual(get.call_count, 2)
                self.assertEqual(sleeps, [2.0])
                self.assertIn(f"exception_category={category}", logs.output[0])

    def test_gateway_and_rate_limit_statuses_are_retried_for_reads(self):
        for status in (429, 502, 503, 504):
            with self.subTest(status=status):
                policy, sleeps = recorded_policy()
                api = client(policy)
                with (
                    patch("trading_bot.api.rate_limits.throttle"),
                    patch("trading_bot.api.client.requests.get",
                          side_effect=[Broker.response(status), ok({"prices": []})]) as get,
                    self.assertLogs("trading_bot.api.client", level="WARNING"),
                ):
                    self.assertEqual(api.getHistoricalPrices("EURUSD", "MINUTE", 1)[1], 200)
                self.assertEqual(get.call_count, 2)

    def test_authentication_and_unrecoverable_errors_are_never_retried(self):
        for status, reason in ((401, "authentication_failed_http_401"),
                               (403, "permission_denied_http_403"),
                               (400, "http_400"), (500, "http_500")):
            with self.subTest(status=status):
                policy, sleeps = recorded_policy()
                api = client(policy)
                with (
                    patch("trading_bot.api.rate_limits.throttle"),
                    patch("trading_bot.api.client.requests.get",
                          return_value=Broker.response(status)) as get,
                    self.assertRaises(TradingUnavailable) as stopped,
                ):
                    api.getAllPositions()
                get.assert_called_once()
                self.assertEqual(sleeps, [])
                self.assertEqual(stopped.exception.reason, reason)
        policy, sleeps = recorded_policy()
        api = client(policy)
        with (
            patch("trading_bot.api.rate_limits.throttle"),
            patch("trading_bot.api.client.requests.get",
                  side_effect=requests.exceptions.SSLError("private-marker")) as get,
            self.assertLogs("trading_bot.api.client", level="WARNING"),
            self.assertRaises(TradingUnavailable) as stopped,
        ):
            api.getAllPositions()
        get.assert_called_once()  # Certificate failures may be interception, not outages.
        self.assertEqual(stopped.exception.reason, "tls_connection_failed")

    def test_trade_changing_requests_are_sent_once_even_with_retries_enabled(self):
        cases = (
            ("post", lambda api: api.createPosition("EURUSD", "BUY", 100)),
            ("delete", lambda api: api.closePosition("fixture-deal-private")),
            ("put", lambda api: api.updatePosition(
                "fixture-deal-private", authorization=api.authorize_mutation(
                    "position-update", resource_id="fixture-deal-private"), stopLevel=1.0)),
            ("post", lambda api: api.createWorkingOrder(
                "EURUSD", "BUY", 100, 1.0, "LIMIT",
                authorization=api.authorize_mutation("working-order"))),
            ("delete", lambda api: api.deleteWorkingOrder(
                "fixture-deal-private", authorization=api.authorize_mutation(
                    "working-order", resource_id="fixture-deal-private"))),
        )
        for index, (method, operation) in enumerate(cases):
            for error in (dns_error(), requests.ReadTimeout("private-marker"), reset_error()):
                with self.subTest(case=index, error=type(error).__name__):
                    policy, sleeps = recorded_policy()
                    api = client(policy)
                    api._login_has_active_environment = True
                    with (
                        patch("trading_bot.api.rate_limits.throttle"),
                        patch.object(api, "verify_account_environment",
                                     return_value={"accountId": "fixture-account-private"}),
                        patch(f"trading_bot.api.client.requests.{method}", side_effect=error) as send,
                        self.assertLogs("trading_bot.api.client", level="WARNING"),
                    ):
                        with self.assertRaises(TradingUnavailable) as stopped:
                            operation(api)
                        with self.assertRaises(TradingUnavailable):
                            operation(api)  # Even an explicit second call cannot resend.
                    send.assert_called_once()
                    self.assertEqual(sleeps, [])
                    self.assertTrue(stopped.exception.outcome_uncertain)

    def test_login_is_never_retried_inside_a_request(self):
        policy, sleeps = recorded_policy()
        api = client(policy)
        with (
            patch("trading_bot.api.rate_limits.throttle"),
            patch("trading_bot.api.client.requests.post", side_effect=dns_error()) as post,
            self.assertLogs("trading_bot.api.client", level="WARNING"),
            self.assertRaises(TradingUnavailable),
        ):
            api.createNewSession()
        post.assert_called_once()
        self.assertEqual(sleeps, [])

    def test_interrupt_during_backoff_stops_without_another_attempt(self):
        policy = RetryPolicy(jitter=0.0, sleep=Mock(side_effect=KeyboardInterrupt))
        api = client(policy)
        with (
            patch("trading_bot.api.rate_limits.throttle"),
            patch("trading_bot.api.client.requests.get", side_effect=dns_error()) as get,
            self.assertLogs("trading_bot.api.client", level="WARNING"),
            self.assertRaises(KeyboardInterrupt),
        ):
            api.getAllPositions()
        get.assert_called_once()
        self.assertEqual(api._unavailable, "request_interrupted")
        self.assertFalse(api._mutation_outcome_uncertain)

    def test_retries_are_opt_in_and_reject_unknown_policies(self):
        self.assertIsNone(TradingApi(*CREDENTIALS.values(), auto_authenticate=False)._read_retry_policy)
        with self.assertRaises(TypeError):
            client().configure_read_retries({"max_retries": 3})


class RecoveredClientTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(datetime(2026, 11, 17, 21, 0))
        self.broker = Broker(self.clock)
        self.stack = ExitStack()
        broker_patches(self.stack, self.broker, self.clock)
        self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(self.assertLogs("trading_bot", level="INFO"))

    def stopped_client(self):
        args = Mock(real=False, account=None)
        from trading_bot.config.trading_config import create_trading_api

        api = create_trading_api(args)
        api.configure_read_retries(RetryPolicy(jitter=0.0))
        self.broker.outages.append((self.clock.local(), self.clock.local() + timedelta(minutes=5)))
        with self.assertRaises(TradingUnavailable):
            api.prepare_trading_cycle()
        self.clock.elapsed += 600
        return api

    def test_fresh_client_reauthenticates_same_account_and_old_client_stays_stopped(self):
        old = self.stopped_client()
        logins = self.broker.count("post", "/api/v1/session")
        new = old.recovered_client()
        self.assertIsNot(new, old)
        self.assertEqual(self.broker.count("post", "/api/v1/session"), logins + 1)
        self.assertEqual((new.cst, new.x_security_token), self.broker.tokens)
        self.assertEqual(new._cycle_identity, old._cycle_identity)
        self.assertIs(new._read_retry_policy, old._read_retry_policy)
        self.assertFalse(new.prepare_trading_cycle())
        calls = len(self.broker.calls)
        with self.assertRaises(TradingUnavailable):
            old.getServerTime()  # The original fail-closed latch is never cleared.
        self.assertEqual(len(self.broker.calls), calls)

    def test_connectivity_probe_precedes_any_login_attempt(self):
        old = self.stopped_client()
        self.broker.outages.append((self.clock.local(), self.clock.local() + timedelta(hours=1)))
        logins = self.broker.count("post", "/api/v1/session")
        with self.assertRaises(TradingUnavailable) as stopped:
            old.recovered_client()
        self.assertEqual(stopped.exception.reason, "dns_resolution_failed")
        self.assertEqual(self.broker.count("post", "/api/v1/session"), logins)
        self.assertEqual(self.broker.calls[-1][1], "/api/v1/time")

    def test_account_change_or_rejected_login_stops_without_switching(self):
        old = self.stopped_client()
        self.broker.account_on_login = "fixture-account-private-other"
        with self.assertRaises(TradingUnavailable) as changed:
            old.recovered_client()
        self.assertEqual(changed.exception.reason, "session_identity_changed")
        self.assertFalse(any(m == "put" for m, _, _ in self.broker.calls))
        self.broker.account_on_login = None
        self.broker.account_id = "fixture-account-private"
        self.broker.login_status = 401
        with self.assertRaises(TradingUnavailable) as rejected:
            old.recovered_client()
        self.assertEqual(rejected.exception.reason, "authentication_failed_http_401")

    def test_uncertain_order_and_live_clients_are_never_recovered(self):
        old = self.stopped_client()
        old._mutation_outcome_uncertain = True
        calls = len(self.broker.calls)
        with self.assertRaises(TradingUnavailable) as stopped:
            old.recovered_client()
        self.assertTrue(stopped.exception.outcome_uncertain)
        live = TradingApi(*CREDENTIALS.values(), environment="live", auto_authenticate=False)
        live._cycle_identity = ("live", live.BASE_URL, "fixture-account-private")
        with self.assertRaises(PermissionError):
            live.recovered_client()
        self.assertEqual(len(self.broker.calls), calls)


class ReconciliationTests(unittest.TestCase):
    def test_existing_positions_and_working_orders_are_counted_without_identifiers(self):
        api = Mock()
        api.getAllPositions.return_value = ({"positions": [
            {"market": {"epic": "EURUSD"}, "position": {"dealId": "fixture-deal-private-1"}},
            {"market": {"epic": "GBPUSD"}, "position": {"dealId": "fixture-deal-private-2"}},
        ]}, 200)
        api.getAllWorkingOrders.return_value = {"workingOrders": [
            {"workingOrderData": {"epic": "EURUSD", "dealId": "fixture-deal-private-3"}}]}
        with self.assertLogs("trading_bot", level="INFO") as logs:
            summary = reconcile_broker_state(api, "EURUSD")
        self.assertEqual(summary, {"positions": 2, "symbol_positions": 1,
                                   "working_orders": 1, "symbol_working_orders": 1})
        self.assertTrue(any("never creates, changes or cancels working orders" in line
                            for line in logs.output))
        assert_redacted(self, *logs.output)
        api.createPosition.assert_not_called()
        api.closePosition.assert_not_called()

    def test_malformed_broker_state_stops_instead_of_resuming(self):
        for positions, working in ((({"positions": None}, 200), {"workingOrders": []}),
                                   (({"positions": [{"market": {}}]}, 200), {"workingOrders": []}),
                                   (({"positions": []}, 200), {"workingOrders": "private-marker"})):
            api = Mock()
            api.getAllPositions.return_value = positions
            api.getAllWorkingOrders.return_value = working
            with self.subTest(positions=positions), self.assertRaises(TradingUnavailable) as stopped:
                reconcile_broker_state(api, "EURUSD")
            self.assertEqual(stopped.exception.reason, "invalid_api_response")
            self.assertFalse(is_transient(stopped.exception.reason))


class CycleGuardAndSchedulerTests(unittest.TestCase):
    def guard(self, now):
        return CycleGuard(1800, 120, clock=lambda: now[0])

    def test_each_boundary_executes_once_and_never_after_it_is_stale(self):
        boundary = datetime(2026, 11, 17, 22, 0, tzinfo=timezone.utc)
        now = [boundary + timedelta(seconds=2)]
        guard = self.guard(now)
        with self.assertLogs("trading_bot", level="WARNING") as logs:
            self.assertTrue(guard.start(boundary))
            self.assertFalse(guard.start(boundary))  # Same candle: duplicate.
            self.assertFalse(guard.start(boundary - timedelta(minutes=30)))  # Clock moved back.
            later = boundary + timedelta(minutes=30)
            now[0] = later + timedelta(seconds=121)
            self.assertFalse(guard.start(later))  # Delayed past max signal delay.
            self.assertFalse(guard.fresh("order"))
            self.assertFalse(guard.start(later))  # A stale boundary is still consumed.
        self.assertEqual(sum("boundary_already_executed" in line for line in logs.output), 3)
        self.assertEqual(sum("stale_boundary" in line for line in logs.output), 1)

    def test_freshness_is_rechecked_immediately_before_an_order(self):
        boundary = datetime(2026, 11, 17, 22, 0, tzinfo=timezone.utc)
        now = [boundary + timedelta(seconds=2)]
        guard = self.guard(now)
        self.assertTrue(guard.start(boundary))
        now[0] = boundary + timedelta(seconds=100)
        self.assertTrue(guard.fresh("order"))
        now[0] = boundary + timedelta(seconds=125)
        with self.assertLogs("trading_bot", level="WARNING"):
            self.assertFalse(guard.fresh("order"))

    def test_short_schedules_are_stale_after_one_interval(self):
        boundary = datetime(2026, 11, 17, 22, 0, tzinfo=timezone.utc)
        now = [boundary + timedelta(seconds=61)]
        with self.assertLogs("trading_bot", level="WARNING"):
            self.assertFalse(CycleGuard(60, 120, clock=lambda: now[0]).start(boundary))

    def test_boundaries_order_correctly_across_midnight(self):
        before = datetime(2026, 11, 17, 23, 30, tzinfo=timezone.utc)
        after = datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc)
        now = [before + timedelta(seconds=2)]
        guard = self.guard(now)
        self.assertTrue(guard.start(before))
        now[0] = after + timedelta(seconds=2)
        self.assertTrue(guard.start(after))  # Minute 0 of the next day is a new boundary.

    def run_scheduler(self, local_now, targets=(0, 30)):
        with (
            patch.object(scheduler, "datetime") as now,
            patch.object(scheduler.time, "sleep") as sleep,
            redirect_stdout(io.StringIO()),
        ):
            now.now.return_value = local_now
            boundary = scheduler.wait_until_targets(list(targets))
        return boundary, sleep.call_args.args[0]

    def test_scheduler_returns_the_next_future_boundary_after_an_outage(self):
        local_now = datetime(2026, 11, 17, 22, 47, 10)
        boundary, delay = self.run_scheduler(local_now)
        self.assertEqual(boundary, datetime(2026, 11, 17, 23, 0).astimezone(timezone.utc))
        self.assertEqual(boundary.tzinfo, timezone.utc)
        self.assertEqual(delay, 772.0)  # 22:30 was missed and is not replayed.

    def test_scheduler_crosses_midnight_into_the_next_day(self):
        boundary, delay = self.run_scheduler(datetime(2026, 11, 17, 23, 45, 0))
        self.assertEqual(boundary, datetime(2026, 11, 18, 0, 0).astimezone(timezone.utc))
        self.assertEqual(delay, 902.0)
        boundary, _ = self.run_scheduler(datetime(2026, 12, 31, 23, 59, 30), range(60))
        self.assertEqual(boundary, datetime(2027, 1, 1, 0, 0).astimezone(timezone.utc))

    def test_interval_follows_the_schedule(self):
        self.assertEqual(scheduler.schedule_interval_seconds([0, 30]), 1800)
        self.assertEqual(scheduler.schedule_interval_seconds(list(range(60))), 60)
        self.assertEqual(scheduler.schedule_interval_seconds([0]), 3600)


class SupervisorTests(unittest.TestCase):
    def supervisor(self, environment="demo", **policy):
        self.sleeps = []
        self.reconciled = []
        self.now = [0.0]

        def sleep(seconds):
            self.sleeps.append(seconds)
            self.now[0] += seconds

        return NetworkSupervisor(
            RecoveryPolicy(jitter=0.0, **policy), environment=environment, symbol="EURUSD",
            reconcile=lambda api, symbol: self.reconciled.append(api), sleep=sleep,
            monotonic=lambda: self.now[0],
        )

    def test_uncertain_authentication_disabled_and_live_failures_stop_immediately(self):
        cases = (
            ("demo", {}, TradingUnavailable("read_timeout", mutation=True), False),
            ("demo", {}, TradingUnavailable("dns_resolution_failed"), True),
            ("demo", {}, TradingUnavailable("authentication_failed_http_401"), False),
            ("demo", {}, TradingUnavailable("session_identity_changed"), False),
            ("demo", {"enabled": False}, TradingUnavailable("dns_resolution_failed"), False),
            ("live", {}, TradingUnavailable("dns_resolution_failed"), False),
        )
        for environment, policy, error, client_uncertain in cases:
            with self.subTest(reason=error.reason, environment=environment, policy=policy):
                supervisor = self.supervisor(environment, **policy)
                api = Mock(_mutation_outcome_uncertain=client_uncertain)
                with self.assertLogs("trading_bot", level="ERROR"), self.assertRaises(TradingUnavailable) as raised:
                    supervisor.recover(error, api)
                self.assertIs(raised.exception, error)
                api.recovered_client.assert_not_called()
                self.assertEqual(self.sleeps, [])

    def test_temporary_outage_pauses_then_reconciles_once_before_resuming(self):
        supervisor = self.supervisor(interval=60)
        api, fresh = Mock(_mutation_outcome_uncertain=False), Mock()
        api.recovered_client.side_effect = [TradingUnavailable("dns_resolution_failed"),
                                            TradingUnavailable("connection_timeout"), fresh]
        with self.assertLogs("trading_bot", level="INFO") as logs:
            self.assertIs(supervisor.recover(TradingUnavailable("dns_resolution_failed"), api), fresh)
        self.assertEqual(self.sleeps, [60.0, 60.0, 60.0])
        self.assertEqual(self.reconciled, [fresh])
        text = "\n".join(logs.output)
        for event in ("Trading paused reason=dns_resolution_failed", "Connectivity not yet recovered",
                      "Session reauthenticated", "Connectivity recovered", "Scheduled trading resumed"):
            self.assertIn(event, text)
        fresh.createPosition.assert_not_called()
        fresh.closePosition.assert_not_called()

    def test_prolonged_outage_keeps_waiting_without_giving_up_by_default(self):
        supervisor = self.supervisor(interval=60)
        api, fresh = Mock(_mutation_outcome_uncertain=False), Mock()
        api.recovered_client.side_effect = [TradingUnavailable("dns_resolution_failed")] * 720 + [fresh]
        with self.assertLogs("trading_bot", level="INFO"):
            self.assertIs(supervisor.recover(TradingUnavailable("dns_resolution_failed"), api), fresh)
        self.assertEqual(len(self.sleeps), 721)
        self.assertEqual(self.now[0], 721 * 60)  # Twelve hours, then a normal resume.

    def test_max_outage_bounds_the_pause(self):
        supervisor = self.supervisor(interval=60, max_outage=300)
        api = Mock(_mutation_outcome_uncertain=False)
        api.recovered_client.side_effect = TradingUnavailable("dns_resolution_failed")
        original = TradingUnavailable("dns_resolution_failed")
        with self.assertLogs("trading_bot", level="INFO") as logs, self.assertRaises(TradingUnavailable) as raised:
            supervisor.recover(original, api)
        self.assertIs(raised.exception, original)
        self.assertEqual(sum(self.sleeps), 300)
        self.assertTrue(any("exceeding max_outage_seconds=300" in line for line in logs.output))

    def test_authentication_failure_after_connectivity_returns_stops_recovery(self):
        supervisor = self.supervisor()
        api = Mock(_mutation_outcome_uncertain=False)
        rejected = TradingUnavailable("authentication_failed_http_401")
        api.recovered_client.side_effect = [TradingUnavailable("dns_resolution_failed"), rejected, Mock()]
        with self.assertLogs("trading_bot", level="INFO"), self.assertRaises(TradingUnavailable) as raised:
            supervisor.recover(TradingUnavailable("dns_resolution_failed"), api)
        self.assertIs(raised.exception, rejected)
        self.assertEqual(api.recovered_client.call_count, 2)  # No login loop on bad credentials.

    def test_transient_reconciliation_failure_waits_and_malformed_state_stops(self):
        supervisor = self.supervisor()
        api, first, second = Mock(_mutation_outcome_uncertain=False), Mock(), Mock()
        api.recovered_client.side_effect = [first, second]
        outcomes = iter([TradingUnavailable("read_timeout"), None])

        def reconcile(client, symbol):
            outcome = next(outcomes)
            if outcome:
                raise outcome

        supervisor.reconcile = reconcile
        with self.assertLogs("trading_bot", level="INFO"):
            self.assertIs(supervisor.recover(TradingUnavailable("dns_resolution_failed"), api), second)
        supervisor.reconcile = Mock(side_effect=TradingUnavailable("invalid_api_response"))
        api.recovered_client.side_effect = None
        with self.assertLogs("trading_bot", level="INFO"), self.assertRaises(TradingUnavailable) as raised:
            supervisor.recover(TradingUnavailable("dns_resolution_failed"), api)
        self.assertEqual(raised.exception.reason, "invalid_api_response")

    def test_shutdown_during_pause_stops_before_any_reconnection(self):
        supervisor = self.supervisor()
        supervisor._sleep = Mock(side_effect=ShutdownRequested(signal.SIGTERM))
        api = Mock(_mutation_outcome_uncertain=False)
        with self.assertLogs("trading_bot", level="INFO"), self.assertRaises(ShutdownRequested):
            supervisor.recover(TradingUnavailable("dns_resolution_failed"), api)
        api.recovered_client.assert_not_called()

    def test_startup_outage_waits_without_any_trade(self):
        supervisor = self.supervisor()
        fresh = Mock()
        factory = Mock(side_effect=[TradingUnavailable("dns_resolution_failed")] * 3 + [fresh])
        with self.assertLogs("trading_bot", level="INFO") as logs:
            self.assertIs(supervisor.start(factory), fresh)
        self.assertEqual(factory.call_count, 4)
        self.assertTrue(any("phase=startup" in line for line in logs.output))
        factory = Mock(side_effect=TradingUnavailable("authentication_failed_http_401"))
        with self.assertLogs("trading_bot", level="ERROR"), self.assertRaises(TradingUnavailable):
            supervisor.start(factory)
        factory.assert_called_once()


class GracefulShutdownTests(unittest.TestCase):
    def test_signal_during_an_order_request_is_deferred_until_it_returns(self):
        shutdown = GracefulShutdown()
        events = []
        with self.assertLogs("trading_bot", level="WARNING"), self.assertRaises(ShutdownRequested) as stopped:
            with shutdown.order_request():
                shutdown._handle(signal.SIGTERM, None)
                events.append("order request completed")
        self.assertEqual(events, ["order request completed"])
        self.assertEqual(stopped.exception.exit_code, 143)
        with self.assertRaises(ShutdownRequested):
            with shutdown.order_request():
                events.append("must not start")
        self.assertEqual(events, ["order request completed"])

    def test_second_signal_forces_an_interrupt_and_idle_signal_stops_at_once(self):
        shutdown = GracefulShutdown()
        with self.assertLogs("trading_bot", level="WARNING"), self.assertRaises(ShutdownRequested):
            with shutdown.order_request():
                shutdown._handle(signal.SIGINT, None)
                shutdown._handle(signal.SIGINT, None)
        idle = GracefulShutdown()
        with self.assertRaises(ShutdownRequested) as stopped:
            idle._handle(signal.SIGINT, None)
        self.assertEqual(stopped.exception.exit_code, 130)
        self.assertIsInstance(stopped.exception, KeyboardInterrupt)

    def test_handlers_are_installed_and_restored(self):
        before = signal.getsignal(signal.SIGTERM)
        with GracefulShutdown() as shutdown:
            self.assertEqual(signal.getsignal(signal.SIGTERM), shutdown._handle)
        self.assertIs(signal.getsignal(signal.SIGTERM), before)


class HmaRecoveryScenarioTests(unittest.TestCase):
    """The real HMA loop, TradingApi, scheduler and supervisor against the fake broker."""

    START = datetime(2026, 11, 17, 21, 50, 0)

    def setUp(self):
        self.clock = FakeClock(self.START)
        self.broker = Broker(self.clock)

    def at(self, hour, minute, second=0, day=17):
        return datetime(2026, 11, day, hour, minute, second)

    def run_hma(self, *argv, expect=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            broker_patches(stack, self.broker, self.clock)
            stack.enter_context(patch("sys.argv", ["hma.py", "--network-retry-jitter", "0", *argv]))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            logs = stack.enter_context(self.assertLogs("trading_bot", level="INFO"))
            logging.getLogger("trading_bot.tests").info("scenario start")
            if expect is None:
                hma.main()
                raised = None
            else:
                with self.assertRaises(expect) as caught:
                    hma.main()
                raised = caught.exception
        self.output = "\n".join(logs.output)
        self.stdout = stdout.getvalue()
        assert_redacted(self, self.output, self.stdout, stderr.getvalue(), str(raised or ""))
        return raised

    def executed(self):
        """Boundaries whose candles were requested (each request starts a cycle)."""
        return [t.replace(second=0) for t in self.broker.times("get", "/api/v1/prices/EURUSD")]

    def test_outage_beyond_retry_budget_pauses_recovers_reconciles_and_skips_missed_candles(self):
        self.broker.outages.append((self.at(22, 20), self.at(23, 10)))
        self.run_hma("--cycles", "3")
        # 22:30 and 23:00 occurred during the outage and are never replayed.
        self.assertEqual(self.executed(), [self.at(22, 0), self.at(23, 30), self.at(0, 0, day=18)])
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)  # No duplicate.
        self.assertEqual(len(self.broker.positions), 1)
        # Retries: the failing keepalive ping was sent four times with 2s, 4s and 8s waits.
        self.assertEqual(self.broker.count("get", "/api/v1/ping", self.at(22, 20))
                         - self.broker.count("get", "/api/v1/ping", self.at(22, 21)), 4)
        self.assertGreaterEqual(self.clock.sleeps.count(60.0), 49)  # Paused 22:20-23:10.
        # Login only once connectivity returned; probes never log in during the outage.
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 2)
        self.assertGreaterEqual(self.broker.times("post", "/api/v1/session")[1], self.at(23, 10))
        for event in ("retry scheduled", "retries exhausted", "Trading paused reason=dns_resolution_failed",
                      "Connectivity not yet recovered", "Session reauthenticated", "Connectivity recovered",
                      "Broker state reconciled positions=1 symbol_positions=1 working_orders=0",
                      "Scheduled trading resumed"):
            self.assertIn(event, self.output)
        self.assertEqual(self.stdout.count("order submitted"), 1)

    def test_temporary_dns_failure_within_budget_does_not_pause_trading(self):
        self.broker.failures[("get", "/api/v1/prices/EURUSD")].extend([dns_error(), dns_error()])
        self.run_hma("--cycles", "1")
        self.assertEqual(self.broker.count("get", "/api/v1/prices/EURUSD"), 3)
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)
        self.assertNotIn("Trading paused", self.output)

    def test_existing_position_is_reconciled_and_never_duplicated_after_recovery(self):
        self.broker.positions.append({"market": {"epic": "EURUSD"},
                                      "position": {"dealId": "fixture-deal-private-0"}})
        self.broker.outages.append((self.at(21, 59, 50), self.at(22, 3)))
        self.run_hma("--cycles", "2")
        self.assertIn("Broker state reconciled positions=1 symbol_positions=1", self.output)
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 0)
        self.assertEqual(self.executed(), [self.at(22, 30), self.at(23, 0)])
        # A later SELL signal closes it exactly once through the normal strategy rule.
        self.broker.trend = -1
        self.run_hma("--cycles", "1")
        self.assertEqual(self.broker.count("delete", "/api/v1/positions/fixture-deal-private-0"), 1)
        self.assertEqual(self.broker.positions, [])

    def test_ambiguous_order_submission_stops_without_resubmitting_or_recovering(self):
        for error in (requests.ReadTimeout("private-marker"), dns_error(), reset_error()):
            with self.subTest(error=type(error).__name__):
                self.setUp()
                self.broker.failures[("post", "/api/v1/positions")].append(error)
                stopped = self.run_hma("--cycles", "3", expect=TradingUnavailable)
                self.assertTrue(stopped.outcome_uncertain)
                self.assertIn("Order outcome may be uncertain", str(stopped))
                self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)
                self.assertEqual(self.broker.count("get", "/api/v1/time"), 0)  # No recovery probe.
                self.assertEqual(self.broker.count("post", "/api/v1/session"), 1)
                self.assertIn("Manual reconciliation of positions and working orders is required", self.output)
                self.assertNotIn("Trading paused", self.output)

    def test_ambiguous_close_stops_without_a_second_delete(self):
        self.broker.trend = -1
        self.broker.positions.append({"market": {"epic": "EURUSD"},
                                      "position": {"dealId": "fixture-deal-private-0"}})
        self.broker.failures[("delete", "/api/v1/positions/fixture-deal-private-0")].append(
            requests.ReadTimeout("private-marker"))
        stopped = self.run_hma("--cycles", "3", expect=TradingUnavailable)
        self.assertTrue(stopped.outcome_uncertain)
        self.assertEqual(sum(m == "delete" for m, _, _ in self.broker.calls), 1)

    def test_recovery_across_midnight_resumes_on_the_next_days_schedule(self):
        self.clock = FakeClock(datetime(2026, 11, 17, 23, 20, 0))
        self.broker = Broker(self.clock)
        self.broker.outages.append((self.at(23, 30, 1), self.at(0, 12, day=18)))
        # --cycles counts attempted boundaries: 23:30 is attempted but fails before data.
        self.run_hma("--cycles", "3")
        self.assertEqual(self.executed(), [self.at(0, 30, day=18), self.at(1, 0, day=18)])
        # The failing 23:30 cycle ping: one attempt plus three bounded retries.
        self.assertEqual(self.broker.count("get", "/api/v1/ping", self.at(23, 30, 1)) -
                         self.broker.count("get", "/api/v1/ping", self.at(23, 31)), 4)
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)
        self.assertIn("Scheduled trading resumed", self.output)

    def test_authentication_error_after_outage_stops_without_a_login_loop(self):
        self.broker.outages.append((self.at(22, 20), self.at(22, 40)))
        original_request = self.broker.request

        def request(method, url, **kwargs):
            if self.clock.local() >= self.at(22, 40):
                self.broker.login_status = 401
            return original_request(method, url, **kwargs)

        self.broker.request = request
        stopped = self.run_hma("--cycles", "3", expect=TradingUnavailable)
        self.assertEqual(stopped.reason, "authentication_failed_http_401")
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 2)
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)
        self.assertIn("Recovery stopped reason=authentication_failed_http_401", self.output)

    def test_prolonged_outage_recovers_with_probes_only(self):
        self.broker.outages.append((self.at(22, 20), self.at(4, 20, day=18)))
        self.run_hma("--cycles", "2", "--recovery-interval", "300")
        self.assertEqual(self.executed(), [self.at(22, 0), self.at(4, 30, day=18)])
        self.assertGreaterEqual(self.broker.count("get", "/api/v1/time"), 70)
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 2)
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)

    def test_max_outage_stops_with_the_network_reason(self):
        self.broker.outages.append((self.at(22, 20), self.at(4, 20, day=18)))
        stopped = self.run_hma("--cycles", "3", "--max-outage", "600", expect=TradingUnavailable)
        self.assertEqual(stopped.reason, "dns_resolution_failed")
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 1)

    def test_graceful_shutdown_during_recovery(self):
        self.broker.outages.append((self.at(22, 20), self.at(23, 0)))

        def on_sleep(seconds):
            if seconds == 60.0 and self.clock.local() > self.at(22, 25):
                raise ShutdownRequested(signal.SIGTERM)

        self.clock.on_sleep = on_sleep
        stopped = self.run_hma("--cycles", "3", expect=SystemExit)
        self.assertEqual(stopped.code, 143)
        self.assertIn("Shutdown complete: no further signals or orders", self.output)
        self.assertNotIn("outcome is uncertain", self.output)
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 1)
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)

    def test_startup_dns_failure_waits_then_trades_on_the_next_boundary(self):
        self.broker.outages.append((self.START, self.at(21, 54)))
        self.run_hma("--cycles", "1")
        self.assertIn("phase=startup", self.output)
        self.assertEqual(self.executed(), [self.at(22, 0)])
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)

    def test_disabled_recovery_and_zero_retries_restore_fail_closed_behavior(self):
        self.broker.outages.append((self.at(22, 20), self.at(22, 21)))
        stopped = self.run_hma("--cycles", "3", "--network-retries", "0",
                               "--no-network-recovery", expect=TradingUnavailable)
        self.assertEqual(stopped.reason, "dns_resolution_failed")
        self.assertIn("Trading stopped; no automatic retry.", str(stopped))
        self.assertEqual(self.broker.count("get", "/api/v1/ping", self.at(22, 20)), 1)
        self.assertEqual(self.broker.count("get", "/api/v1/time"), 0)

    def test_cycle_delayed_past_its_boundary_calculates_no_signal_and_places_no_order(self):
        # The host is suspended for ten minutes at each checkpoint of the 22:00 cycle.
        cases = (
            ("/api/v1/ping", self.at(22, 0), self.at(22, 0, 1), "cycle_start", False),
            ("/api/v1/ping", self.at(22, 0, 1), self.at(22, 1), "signal", False),
            ("/api/v1/prices/EURUSD", self.at(22, 0), self.at(22, 1), "order", True),
        )
        for route, start, end, stage, history_fetched in cases:
            with self.subTest(stage=stage):
                self.setUp()
                original = self.broker.request

                def suspended(method, url, _route=route, _start=start, _end=end, **kwargs):
                    if url.endswith(_route) and _start <= self.clock.local() < _end:
                        self.clock.elapsed += 600
                    return original(method, url, **kwargs)

                self.broker.request = suspended
                self.run_hma("--cycles", "2")
                self.assertIn(f"reason=stale_boundary stage={stage}", self.output)
                before = self.broker.times("get", "/api/v1/prices/EURUSD")
                self.assertEqual(any(t < self.at(22, 30) for t in before), history_fetched)
                orders = self.broker.times("post", "/api/v1/positions")
                self.assertEqual(len(orders), 1)
                self.assertGreaterEqual(orders[0], self.at(22, 30))  # Only the next fresh cycle.


class HmaLiveAndCliTests(unittest.TestCase):
    def test_live_network_exhaustion_stops_and_is_never_recovered_automatically(self):
        api = Mock(_mutation_outcome_uncertain=False)
        api.prepare_trading_cycle.side_effect = TradingUnavailable("dns_resolution_failed")
        with (
            patch("sys.argv", ["hma.py", "--REAL", "--cycles", "2"]),
            patch.object(hma, "create_trading_api", return_value=api),
            patch.object(hma, "wait_until_targets"),
            patch.object(hma, "calculate_hma_result") as signal_calculation,
            self.assertLogs("trading_bot", level="ERROR") as logs,
            self.assertRaises(TradingUnavailable),
        ):
            hma.main()
        api.recovered_client.assert_not_called()
        api.createPosition.assert_not_called()
        signal_calculation.assert_not_called()
        self.assertIn("LIVE trading never resumes automatically", "\n".join(logs.output))

    def test_live_retries_never_apply_to_the_gated_order_request(self):
        api = TradingApi(*CREDENTIALS.values(), environment="live", auto_authenticate=False)
        api.cst, api.x_security_token = "fixture-cst", "fixture-security"
        policy, sleeps = recorded_policy()
        api.configure_read_retries(policy)
        with (
            patch("trading_bot.api.rate_limits.throttle"),
            patch("trading_bot.api.client.requests.post", side_effect=dns_error()) as post,
            self.assertLogs("trading_bot.api.client", level="WARNING"),
            self.assertRaises(TradingUnavailable) as stopped,
        ):
            api._gated_send(lambda: api._post_position({"epic": "EURUSD"}))
        post.assert_called_once()
        self.assertEqual(sleeps, [])
        self.assertTrue(stopped.exception.outcome_uncertain)

    def test_invalid_network_options_fail_before_authentication(self):
        for options in (["--network-retries", "11"], ["--network-retries", "-1"],
                        ["--network-retry-delay", "nan"], ["--recovery-interval", "0"],
                        ["--network-retry-jitter", "1"], ["--max-signal-delay", "0"],
                        ["--network-retry-delay", "10", "--network-retry-max-delay", "5"]):
            with (
                self.subTest(options=options),
                patch("sys.argv", ["hma.py", *options]),
                patch.object(hma, "create_trading_api") as factory,
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                hma.main()
            factory.assert_not_called()

    def test_configured_retry_policy_reaches_the_client(self):
        api = Mock()
        with (
            patch("sys.argv", ["hma.py", "--read-only", "--cycles", "1", "--network-retries", "5",
                               "--network-retry-delay", "1", "--network-retry-max-delay", "9"]),
            patch.object(hma, "create_trading_api", return_value=api),
            patch.object(hma, "wait_until_targets"),
            redirect_stdout(io.StringIO()),
        ):
            api.getHistoricalPrices.return_value = ({"prices": []}, 200)
            hma.main()
        policy = api.configure_read_retries.call_args.args[0]
        self.assertEqual((policy.max_retries, policy.base_delay, policy.max_delay), (5, 1.0, 9.0))

    def test_every_transient_reason_is_a_known_fixed_code(self):
        from trading_bot.api.network_errors import MESSAGES

        for reason in TRANSIENT_REASONS:
            self.assertTrue(reason in MESSAGES or reason.startswith("http_"), reason)


if __name__ == "__main__":
    unittest.main()

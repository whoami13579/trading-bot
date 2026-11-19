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
from trading_bot.backtesting.timeframes import MINUTES
from trading_bot.api.network_errors import TradingUnavailable
from trading_bot.api.retry import RetryPolicy, TRANSIENT_REASONS, is_transient
from trading_bot.config.trading_config import DEMO_URL
from trading_bot.strategies import hma
from trading_bot.utils import scheduler
from trading_bot.utils.network_recovery import (
    CycleGuard,
    GracefulShutdown,
    NetworkSupervisor,
    PendingOrders,
    RecoveryPolicy,
    ShutdownRequested,
    reconcile_broker_state,
)
from trading_bot.utils.candles import completed_closes

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


def floor_utc(moment, step):
    seconds = int(step.total_seconds())
    return datetime.fromtimestamp(int(moment.timestamp()) // seconds * seconds, timezone.utc)


class Broker:
    """Stateful DEMO broker: sessions, prices, positions, working orders and outages.

    Candles carry Capital's ``snapshotTimeUTC`` interval starts; the last one is still
    forming. Every open and close returns a deal reference whose confirmation is served
    by ``/confirms``. With ``lag`` set, the position list does not yet reflect new opens
    or closes and confirmations are not yet published, until ``release_at``.
    """

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
        self.candle_lag = timedelta(0)  # How far the broker's latest candle trails real time.
        self.candle_hook = None  # Optional rewrite of the served candle rows.
        self.confirmations = {}
        self.confirm_status = "ACCEPTED"
        self.lag = False  # Acknowledged orders not yet confirmed or listed.
        self.release_at = None
        self.unlisted, self.still_listed = set(), []
        self.request_hook = None  # Called with (method, path) before a request is served.

    def release(self):
        self.lag = False
        self.unlisted.clear()
        self.still_listed.clear()

    def listed_positions(self):
        return [p for p in self.positions if p["position"]["dealId"] not in self.unlisted] + self.still_listed

    def candles(self, resolution, count):
        step = timedelta(minutes=MINUTES[resolution])
        forming = floor_utc(self.clock.utc() - self.candle_lag, step)
        rows = [{"snapshotTimeUTC": (forming - (count - 1 - i) * step).replace(tzinfo=None).isoformat(),
                 "closePrice": {"ask": 1.1 + self.trend * i / 10000}} for i in range(count)]
        return self.candle_hook(rows) if self.candle_hook else rows

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
        if self.request_hook is not None:
            self.request_hook(method, path)
            now = self.clock.local()
        if any(start <= now < end for start, end in self.outages):
            raise dns_error()
        if self.release_at is not None and now >= self.release_at:
            self.release()
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
            params = kwargs.get("params", {})
            rows = self.candles(params.get("resolution", "MINUTE_30"), params.get("max", 48))
            return self.response(value={"prices": rows})
        if method == "get" and path == "/api/v1/positions":
            return self.response(value={"positions": self.listed_positions()})
        if method == "get" and path.startswith("/api/v1/confirms/"):
            confirmation = self.confirmations.get(path.rsplit("/", 1)[1])
            if confirmation is None or self.lag:
                return self.response(404, {"errorCode": "error.not-found.dealReference"})
            return self.response(value={**confirmation, "dealStatus": self.confirm_status})
        if method == "post" and path == "/api/v1/positions":
            self.orders += 1
            payload = kwargs["json"]
            deal_id, reference = f"fixture-deal-private-{self.orders}", f"o_private-marker-{self.orders}"
            self.positions.append({
                "market": {"epic": payload["epic"]},
                "position": {"dealId": deal_id, "direction": payload["direction"], "size": payload["size"]},
            })
            if self.lag:
                self.unlisted.add(deal_id)
            self.confirmations[reference] = {
                "dealReference": reference, "epic": payload["epic"], "direction": payload["direction"],
                "size": payload["size"], "status": "OPEN",
                "affectedDeals": [{"dealId": deal_id, "status": "OPENED"}],
            }
            return self.response(value={"dealReference": reference})
        if method == "delete" and path.startswith("/api/v1/positions/"):
            deal_id = path.rsplit("/", 1)[1]
            self.orders += 1
            reference = f"p_private-marker-{self.orders}"
            closed = [p for p in self.positions if p["position"]["dealId"] == deal_id]
            self.positions = [p for p in self.positions if p["position"]["dealId"] != deal_id]
            if self.lag:
                self.still_listed.extend(closed)
            self.confirmations[reference] = {
                "dealReference": reference, "epic": "EURUSD", "status": "CLOSED",
                "affectedDeals": [{"dealId": deal_id, "status": "FULLY_CLOSED"}],
            }
            return self.response(value={"dealReference": reference})
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
    # Market hours (--days 5) and transport-failure log timestamps follow the same clock.
    utc = stack.enter_context(patch("trading_bot.api.client.datetime"))
    utc.now.side_effect = lambda *args, **kwargs: clock.utc()
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

    def run_polling_wait(self, poll, keepalive=None):
        clock = FakeClock(datetime(2026, 11, 17, 22, 0, 2))
        with (
            patch.object(scheduler, "datetime") as now,
            patch.object(scheduler.time, "monotonic", side_effect=clock.monotonic),
            patch.object(scheduler.time, "sleep", side_effect=clock.sleep),
            redirect_stdout(io.StringIO()),
        ):
            now.now.side_effect = clock.local
            boundary = scheduler.wait_until_targets([0, 30], keepalive=keepalive, poll=poll)
        return boundary, clock

    def test_poll_runs_at_once_then_as_requested_until_it_returns_none(self):
        calls = []
        pauses = iter([10.0, 10.0, 0.5, None])

        def poll():
            calls.append(clock_ref[0].elapsed)
            return next(pauses)

        clock_ref = []
        original = FakeClock.__init__

        def remember(clock, start):
            original(clock, start)
            clock_ref.append(clock)

        keepalives = []
        with patch.object(FakeClock, "__init__", remember):
            boundary, clock = self.run_polling_wait(
                poll, keepalive=lambda: keepalives.append(clock_ref[0].elapsed))
        self.assertEqual(calls, [0.0, 10.0, 20.0, 20.5])  # Prompt first check; never again after None.
        self.assertEqual(boundary, datetime(2026, 11, 17, 22, 30).astimezone(timezone.utc))
        self.assertEqual(clock.elapsed, 1800.0)  # The boundary itself is unchanged.
        self.assertEqual(keepalives, [300.0, 600.0, 900.0, 1200.0, 1500.0])  # Same cadence as before.

    def test_poll_failure_stops_the_wait_before_any_cycle(self):
        poll = Mock(side_effect=[10.0, TradingUnavailable("order_confirmation_unresolved", mutation=True)])
        with self.assertRaises(TradingUnavailable):
            _, clock = self.run_polling_wait(poll)
        self.assertEqual(poll.call_count, 2)


    def run_closed_market_wait(self, poll, open_after):
        clock = FakeClock(datetime(2026, 11, 20, 22, 0, 2))
        api = client()
        calls = []

        def market_open():
            calls.append(("market", clock.elapsed))
            return clock.elapsed >= open_after

        def recorded_poll():
            calls.append(("poll", clock.elapsed))
            return poll()

        with (
            patch.object(api, "is_market_open", side_effect=market_open),
            patch("time.monotonic", side_effect=clock.monotonic),
            patch("time.sleep", side_effect=clock.sleep),
            redirect_stdout(io.StringIO()),
        ):
            api.wait_until_open(poll=recorded_poll)
        return calls, clock

    def test_market_closed_wait_keeps_polling_until_resolved_then_waits_for_open(self):
        pauses = iter([10.0, 10.0, 0.5, None])
        calls, clock = self.run_closed_market_wait(lambda: next(pauses), open_after=180)
        self.assertEqual(calls, [("market", 0.0), ("poll", 0.0), ("poll", 10.0), ("poll", 20.0),
                                 ("poll", 20.5), ("market", 60.0), ("market", 120.0), ("market", 180.0)])
        self.assertEqual(clock.elapsed, 180.0)  # The market is still checked every 60 s.

    def test_market_closed_wait_stops_when_a_poll_fails(self):
        poll = Mock(side_effect=[10.0] * 30 + [TradingUnavailable("order_confirmation_unresolved", mutation=True)])
        with self.assertRaises(TradingUnavailable):
            self.run_closed_market_wait(poll, open_after=86400)
        self.assertEqual(poll.call_count, 31)  # Stopped at 300 s, long before the market opens.

    def test_market_closed_wait_without_poll_is_unchanged(self):
        clock = FakeClock(datetime(2026, 11, 20, 22, 0, 2))
        api = client()
        with (
            patch.object(api, "is_market_open", side_effect=[False, False, True]),
            patch("time.sleep", side_effect=clock.sleep),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            api.wait_until_open()
        self.assertEqual(clock.sleeps, [60, 60])
        self.assertIn("Market is now OPEN!", stdout.getvalue())


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
        self.assertTrue(any("reaching max_outage_seconds=300" in line for line in logs.output))

    def test_max_outage_shorter_than_the_interval_never_starts_a_probe(self):
        supervisor = self.supervisor(interval=60, max_outage=5)
        api = Mock(_mutation_outcome_uncertain=False)
        original = TradingUnavailable("dns_resolution_failed")
        with self.assertLogs("trading_bot", level="INFO") as logs, self.assertRaises(TradingUnavailable) as raised:
            supervisor.recover(original, api)
        self.assertIs(raised.exception, original)
        self.assertEqual(self.sleeps, [5.0])  # Sleeps only to the deadline, then stops.
        api.recovered_client.assert_not_called()  # No probe at or after the deadline.
        self.assertIn("trading is not resumed", "\n".join(logs.output))
        factory = Mock(side_effect=TradingUnavailable("dns_resolution_failed"))
        with self.assertLogs("trading_bot", level="INFO"), self.assertRaises(TradingUnavailable):
            supervisor.start(factory)
        factory.assert_called_once()  # Startup: the initial attempt only.

    def pending_supervisor(self, acknowledged_at):
        supervisor = self.supervisor(interval=60)
        pending = PendingOrders("EURUSD", timeout=300, monotonic=lambda: self.now[0])
        self.now[0] = acknowledged_at
        with self.assertLogs("trading_bot", level="INFO"):
            pending.record_open({"dealReference": "o_reference"}, direction="BUY", size=100)
        self.now[0] = 0.0
        supervisor.pending = pending
        return supervisor, pending

    def test_outage_with_an_unconfirmed_order_ends_at_the_confirmation_deadline(self):
        supervisor, pending = self.pending_supervisor(acknowledged_at=-170.0)  # 130 s remain.
        api = Mock(_mutation_outcome_uncertain=False)
        api.recovered_client.side_effect = TradingUnavailable("dns_resolution_failed")
        with self.assertLogs("trading_bot", level="INFO") as logs, \
                self.assertRaises(TradingUnavailable) as raised:
            supervisor.recover(TradingUnavailable("dns_resolution_failed"), api)
        self.assertEqual(raised.exception.reason, "order_confirmation_unresolved")
        self.assertTrue(raised.exception.outcome_uncertain)
        self.assertEqual(self.sleeps, [60.0, 60.0, 10.0])  # The last wait lands on the deadline...
        self.assertEqual(api.recovered_client.call_count, 3)  # ...for one final check there.
        self.assertEqual(self.now[0], 130.0)
        self.assertEqual(len(pending), 1)
        self.assertIn("detail=confirmation_timeout", "\n".join(logs.output))

    def test_order_confirmed_by_the_last_check_at_the_deadline_resumes(self):
        supervisor, pending = self.pending_supervisor(acknowledged_at=-250.0)  # 50 s remain.
        api, fresh = Mock(_mutation_outcome_uncertain=False), Mock()
        fresh.getPositionOrderConfirmation.return_value = OPEN_CONFIRMATION
        fresh.getAllPositions.return_value = ({"positions": [LISTED]}, 200)
        with self.assertLogs("trading_bot", level="INFO") as logs:
            api.recovered_client.side_effect = [fresh]
            self.assertIs(supervisor.recover(TradingUnavailable("dns_resolution_failed"), api), fresh)
        self.assertEqual(self.sleeps, [50.0])
        self.assertEqual(len(pending), 0)
        self.assertIn("pending_orders=0", "\n".join(logs.output))

    def test_recovery_returning_at_or_after_the_deadline_is_not_resumed(self):
        for duration, succeeds in ((40.0, True), (50.0, True), (50.0, False)):
            with self.subTest(duration=duration, succeeds=succeeds):
                supervisor = self.supervisor(interval=60, max_outage=100)
                api, fresh = Mock(_mutation_outcome_uncertain=False), Mock()

                def slow_attempt(_duration=duration, _succeeds=succeeds):
                    self.now[0] += _duration  # Starts at 60 s, before the 100 s deadline.
                    if not _succeeds:
                        raise TradingUnavailable("connection_timeout")
                    return fresh

                api.recovered_client.side_effect = slow_attempt
                original = TradingUnavailable("dns_resolution_failed")
                with self.assertLogs("trading_bot", level="INFO") as logs, \
                        self.assertRaises(TradingUnavailable) as raised:
                    supervisor.recover(original, api)
                self.assertIs(raised.exception, original)
                self.assertEqual(api.recovered_client.call_count, 1)  # No probe after the deadline.
                self.assertEqual(self.sleeps, [60.0])
                text = "\n".join(logs.output)
                self.assertIn("reaching max_outage_seconds=100; trading is not resumed", text)
                self.assertNotIn("Scheduled trading resumed", text)
                self.assertNotIn("Connectivity recovered", text)
                fresh.createPosition.assert_not_called()

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


OPEN_CONFIRMATION = {
    "dealReference": "o_reference", "dealStatus": "ACCEPTED", "epic": "EURUSD", "direction": "BUY",
    "size": 100.0, "affectedDeals": [{"dealId": "deal-1", "status": "OPENED"}],
}
LISTED = {"market": {"epic": "EURUSD"}, "position": {"dealId": "deal-1"}}


class PendingOrderTests(unittest.TestCase):
    def api(self, confirmations, positions=()):
        api = Mock()
        api.getPositionOrderConfirmation.side_effect = list(confirmations)
        api.getAllPositions.return_value = ({"positions": list(positions)}, 200)
        return api

    def tracker(self, now=None, **kwargs):
        self.now = now if now is not None else [0.0]
        self.sleeps = []
        return PendingOrders("EURUSD", monotonic=lambda: self.now[0], sleep=self.sleeps.append, **kwargs)

    def test_open_resolves_only_when_confirmed_and_listed_using_reads_only(self):
        pending = self.tracker()
        with self.assertLogs("trading_bot", level="INFO"):
            pending.record_open({"dealReference": "o_reference"}, direction="BUY", size=100)
        api = self.api([None, dict(OPEN_CONFIRMATION, dealStatus="PENDING"), OPEN_CONFIRMATION,
                        OPEN_CONFIRMATION], positions=[])
        with self.assertLogs("trading_bot", level="WARNING"):
            self.assertFalse(pending.resolve(api))  # 404: not yet published.
            self.assertFalse(pending.resolve(api))  # PENDING.
            self.assertFalse(pending.resolve(api))  # Accepted, but positions do not show it yet.
        api.getAllPositions.return_value = ({"positions": [LISTED]}, 200)
        with self.assertLogs("trading_bot", level="INFO"):
            self.assertTrue(pending.resolve(api))
        self.assertEqual(len(pending), 0)
        api.getPositionOrderConfirmation.assert_called_with("o_reference")
        for mutation in ("createPosition", "closePosition", "updatePosition", "createWorkingOrder"):
            getattr(api, mutation).assert_not_called()

    def test_close_resolves_once_the_position_is_gone(self):
        pending = self.tracker()
        with self.assertLogs("trading_bot", level="INFO"):
            pending.record_close({"dealReference": "p_reference"}, deal_id="deal-1")
        close = {"dealReference": "p_reference", "dealStatus": "ACCEPTED"}
        api = self.api([close, close], positions=[LISTED])
        with self.assertLogs("trading_bot", level="WARNING"):
            self.assertFalse(pending.resolve(api))
        api.getAllPositions.return_value = ({"positions": []}, 200)
        with self.assertLogs("trading_bot", level="INFO"):
            self.assertTrue(pending.resolve(api))

    def test_rejected_malformed_or_mismatched_confirmations_stop_for_manual_reconciliation(self):
        cases = {
            "rejected": dict(OPEN_CONFIRMATION, dealStatus="REJECTED"),
            "other_reference": dict(OPEN_CONFIRMATION, dealReference="o_other"),
            "direction": dict(OPEN_CONFIRMATION, direction="SELL"),
            "size": dict(OPEN_CONFIRMATION, size=50),
            "epic": dict(OPEN_CONFIRMATION, epic="GBPUSD"),
            "two_deals": dict(OPEN_CONFIRMATION, affectedDeals=[{"dealId": "a", "status": "OPENED"}] * 2),
            "not_opened": dict(OPEN_CONFIRMATION, affectedDeals=[{"dealId": "deal-1", "status": "DELETED"}]),
            "not_a_dict": ["private-marker"],
        }
        for name, confirmation in cases.items():
            with self.subTest(case=name):
                pending = self.tracker()
                with self.assertLogs("trading_bot", level="INFO"):
                    pending.record_open({"dealReference": "o_reference"}, direction="BUY", size=100)
                with self.assertLogs("trading_bot", level="CRITICAL") as logs, \
                        self.assertRaises(TradingUnavailable) as stopped:
                    pending.resolve(self.api([confirmation], positions=[LISTED]))
                self.assertEqual(stopped.exception.reason, "order_confirmation_mismatch")
                self.assertTrue(stopped.exception.outcome_uncertain)
                self.assertEqual(len(pending), 1)  # Still unresolved: never forgotten.
                assert_redacted(self, *logs.output)
                self.assertNotIn("o_reference", "\n".join(logs.output))

    def test_acknowledgement_without_a_reference_cannot_be_confirmed_and_stops(self):
        for result in ({}, {"dealReference": ""}, {"dealReference": "../x"}, None):
            with self.subTest(result=result):
                pending = self.tracker()
                with self.assertLogs("trading_bot", level="INFO"):
                    pending.record_open(result, direction="BUY", size=100)
                api = self.api([])
                with self.assertLogs("trading_bot", level="CRITICAL"), self.assertRaises(TradingUnavailable) as stopped:
                    pending.resolve(api)
                self.assertEqual(stopped.exception.reason, "order_confirmation_mismatch")
                api.getPositionOrderConfirmation.assert_not_called()

    def test_unresolved_order_blocks_until_the_timeout_then_stops(self):
        pending = self.tracker(timeout=300)
        with self.assertLogs("trading_bot", level="INFO"):
            pending.record_open({"dealReference": "o_reference"}, direction="BUY", size=100)
        api = self.api([None] * 3)
        with self.assertLogs("trading_bot", level="WARNING") as logs:
            self.assertFalse(pending.resolve(api))
            self.now[0] = 299.0
            self.assertFalse(pending.resolve(api))
        self.assertEqual(sum("reason=order_confirmation_pending" in line for line in logs.output), 2)
        self.now[0] = 300.0
        with self.assertLogs("trading_bot", level="CRITICAL"), self.assertRaises(TradingUnavailable) as stopped:
            pending.resolve(api)
        self.assertEqual(stopped.exception.reason, "order_confirmation_unresolved")
        self.assertTrue(stopped.exception.outcome_uncertain)

    def test_polling_waits_between_confirmation_reads(self):
        pending = self.tracker()
        with self.assertLogs("trading_bot", level="INFO"):
            pending.record_open({"dealReference": "o_reference"}, direction="BUY", size=100)
        api = self.api([None, None, OPEN_CONFIRMATION], positions=[LISTED])
        with self.assertLogs("trading_bot", level="INFO"):
            self.assertTrue(pending.resolve(api, attempts=3))
        self.assertEqual(self.sleeps, [1.0, 1.0])

    def test_waits_between_attempts_never_pass_the_deadline(self):
        pending = self.tracker(timeout=300)
        with self.assertLogs("trading_bot", level="INFO"):
            pending.record_open({"dealReference": "o_reference"}, direction="BUY", size=100)
        self.now[0] = 299.5
        self.sleeps = []
        pending._sleep = lambda seconds: (self.sleeps.append(seconds), self.now.__setitem__(0, self.now[0] + seconds))
        api = self.api([None, None, None])
        with self.assertLogs("trading_bot", level="CRITICAL"), self.assertRaises(TradingUnavailable) as stopped:
            pending.resolve(api, attempts=3)
        self.assertEqual(self.sleeps, [0.5])  # Bounded by the remaining time, then stopped.
        self.assertEqual(api.getPositionOrderConfirmation.call_count, 2)
        self.assertEqual(stopped.exception.reason, "order_confirmation_unresolved")

    def test_poll_checks_with_reads_only_and_schedules_the_next_check_by_the_deadline(self):
        pending = self.tracker(timeout=300, poll_interval=10)
        self.assertIsNone(pending.poll(Mock()))  # Nothing pending: no reads at all.
        with self.assertLogs("trading_bot", level="INFO"):
            pending.record_open({"dealReference": "o_reference"}, direction="BUY", size=100)
        self.assertTrue(pending.pollable())
        api = self.api([None] * 3 + [OPEN_CONFIRMATION], positions=[LISTED])
        with self.assertLogs("trading_bot", level="WARNING") as logs:
            self.assertEqual(pending.poll(api), 10)
            self.now[0] = 295.0
            self.assertEqual(pending.poll(api), 5)  # The last check lands on the deadline.
        self.assertEqual(sum("Order confirmation pending" in line for line in logs.output), 1)
        self.assertFalse(any("Scheduled cycle skipped" in line for line in logs.output))
        self.now[0] = 300.0
        with self.assertLogs("trading_bot", level="CRITICAL"), self.assertRaises(TradingUnavailable) as stopped:
            pending.poll(api)
        self.assertEqual(stopped.exception.reason, "order_confirmation_unresolved")
        self.assertEqual(api.getPositionOrderConfirmation.call_count, 3)  # Checked once more first.
        pending.entries[0]["since"] = 300.0  # A fresh entry that the broker has now confirmed.
        with self.assertLogs("trading_bot", level="INFO"):
            self.assertIsNone(pending.poll(api))
        self.assertEqual(len(pending), 0)
        for mutation in ("createPosition", "closePosition", "updatePosition", "createWorkingOrder"):
            getattr(api, mutation).assert_not_called()

    def test_read_retry_backoff_is_bounded_by_the_confirmation_deadline(self):
        policy, sleeps = recorded_policy()
        api = client(policy)
        pending = PendingOrders("EURUSD", timeout=300, monotonic=lambda: 1000.0)
        with self.assertLogs("trading_bot", level="INFO"):
            pending.record_open({"dealReference": "o_reference"}, direction="BUY", size=100)
        now = [1295.0]  # Five seconds before the deadline.

        def wait(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        api.configure_read_retries(RetryPolicy(max_retries=3, jitter=0.0, sleep=wait))
        with (
            patch("trading_bot.api.rate_limits.throttle"),
            patch("time.monotonic", side_effect=lambda: now[0]),
            patch("trading_bot.api.client.requests.get", side_effect=dns_error()) as get,
            self.assertLogs("trading_bot", level="WARNING") as logs,
            self.assertRaises(TradingUnavailable) as stopped,
            pending.bounded_reads(api),
        ):
            api.getPositionOrderConfirmation("o_reference")
        self.assertEqual(sleeps, [2.0, 3.0])  # 2 s, then capped to the 3 s left; no 8 s wait.
        # The backoff ends exactly on the deadline: that retry is never started.
        self.assertEqual(get.call_count, 2)
        self.assertEqual(stopped.exception.reason, "dns_resolution_failed")
        self.assertIn("confirmation deadline reached", "\n".join(logs.output))
        self.assertIsNone(api._read_retry_deadline)  # Restored after the block.

        # Polling applies the same bound itself, on every confirmation read.
        api = client()
        sleeps.clear()
        now[0] = 1295.0
        api.configure_read_retries(RetryPolicy(max_retries=3, jitter=0.0, sleep=wait))
        pending._monotonic = lambda: now[0]
        with (
            patch("trading_bot.api.rate_limits.throttle"),
            patch("time.monotonic", side_effect=lambda: now[0]),
            patch("trading_bot.api.client.requests.get", side_effect=dns_error()),
            self.assertLogs("trading_bot", level="WARNING"),
            self.assertRaises(TradingUnavailable) as stopped,
        ):
            pending.poll(api)
        self.assertEqual(sleeps, [2.0, 3.0])
        self.assertEqual(stopped.exception.reason, "dns_resolution_failed")

    def test_live_tracker_is_disabled_because_the_live_gate_confirms_synchronously(self):
        pending = self.tracker(enabled=False)
        pending.record_open({"dealReference": "o_reference"}, direction="BUY", size=100)
        api = Mock()
        self.assertTrue(pending.resolve(api))
        self.assertEqual(api.mock_calls, [])


UTC_BOUNDARY = datetime(2026, 11, 17, 22, 0, tzinfo=timezone.utc)


class ConfirmationDeadlineTests(unittest.TestCase):
    """Third review, 2: the confirmation deadline holds through retries and reconciliation.

    The real client request path, read retries and rate limiter run against a mocked
    DEMO transport. One fake clock drives monotonic time and every sleep. The order is
    acknowledged at t=0, so its deadline is t=300 s.
    """

    def setUp(self):
        from trading_bot.api import rate_limits

        self.now = [1000.0]
        self.sleeps = []
        self.reads = []  # (path, start seconds, end seconds)
        self.script = deque()  # Per GET: (seconds the read takes, outcome).
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(
            rate_limits, "_STATE", defaultdict(rate_limits._STATE.default_factory)))
        self.stack.enter_context(patch("time.monotonic", side_effect=lambda: self.now[0]))
        self.stack.enter_context(patch("time.sleep", side_effect=self.sleep))
        self.stack.enter_context(patch("trading_bot.api.client.requests.get", side_effect=self.get))
        self.post = self.stack.enter_context(patch("trading_bot.api.client.requests.post"))
        self.delete = self.stack.enter_context(patch("trading_bot.api.client.requests.delete"))
        self.api = client(RetryPolicy(max_retries=3, base_delay=2.0, jitter=0.0, sleep=self.sleep))
        self.pending = PendingOrders("EURUSD", timeout=300, monotonic=lambda: self.now[0], sleep=self.sleep)
        with self.assertLogs("trading_bot", level="INFO"):
            self.pending.record_open({"dealReference": "o_reference"}, direction="BUY", size=100)

    def at(self, seconds):
        self.now[0] = 1000.0 + seconds

    def elapsed(self):
        return round(self.now[0] - 1000.0, 3)

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now[0] += seconds

    def get(self, url, **kwargs):
        path = urlsplit(url).path
        start = self.elapsed()
        duration, outcome = self.script.popleft()
        self.now[0] += duration
        self.reads.append((path, start, self.elapsed()))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    @staticmethod
    def accepted():
        return ok({"dealReference": "o_reference", "dealStatus": "ACCEPTED", "epic": "EURUSD",
                   "direction": "BUY", "size": 100,
                   "affectedDeals": [{"dealId": "fixture-deal-private-1", "status": "OPENED"}]})

    @staticmethod
    def listed():
        return ok({"positions": [{"market": {"epic": "EURUSD"},
                                  "position": {"dealId": "fixture-deal-private-1"}}]})

    def supervisor(self):
        return NetworkSupervisor(RecoveryPolicy(interval=60, jitter=0), environment="demo",
                                 symbol="EURUSD", sleep=self.sleep, monotonic=lambda: self.now[0],
                                 pending=self.pending)

    def assert_still_pending_without_mutations(self):
        self.assertEqual(len(self.pending), 1)
        self.post.assert_not_called()
        self.delete.assert_not_called()

    def test_failed_read_at_299s_never_retries_at_300s_and_stops_for_reconciliation(self):
        # The reproduction: GET fails at 299 s, backoff to 300 s, a retry taking 4 s confirms.
        self.at(299)
        self.script.extend([(0, reset_error()), (4, self.accepted()), (0, self.listed())])
        with self.assertLogs("trading_bot", level="WARNING") as logs, \
                self.assertRaises(TradingUnavailable) as failed:
            self.pending.poll(self.api)
        self.assertEqual(failed.exception.reason, "connection_failed")
        self.assertEqual(self.sleeps, [1.0])  # Backoff capped to the second left.
        self.assertEqual(self.reads, [("/api/v1/confirms/o_reference", 299, 299)])  # No retry at 300 s.
        self.assertEqual(self.elapsed(), 300)
        self.assertIn("confirmation deadline reached before the retry started", "\n".join(logs.output))
        self.assert_still_pending_without_mutations()

        # The HMA loop hands the outage to the supervisor: it stops at once, no login.
        with self.assertLogs("trading_bot", level="WARNING") as logs, \
                self.assertRaises(TradingUnavailable) as stopped:
            self.supervisor().recover(failed.exception, self.api)
        self.assertEqual(stopped.exception.reason, "order_confirmation_unresolved")
        self.assertTrue(stopped.exception.outcome_uncertain)
        self.assertEqual(len(self.reads), 1)
        self.assertEqual(self.elapsed(), 300)  # Never 304 s.
        self.assertIn("detail=confirmation_timeout", "\n".join(logs.output))
        self.assert_still_pending_without_mutations()

    def test_retry_delayed_past_the_deadline_by_the_rate_limiter_never_starts(self):
        self.api.configure_read_retries(
            RetryPolicy(max_retries=3, base_delay=0.05, jitter=0.0, sleep=self.sleep))
        self.at(299.9)
        self.script.extend([(0, reset_error()), (0, self.accepted()), (0, self.listed())])
        with self.assertLogs("trading_bot", level="WARNING"), \
                self.assertRaises(TradingUnavailable) as failed:
            self.pending.poll(self.api)
        self.assertEqual(failed.exception.reason, "connection_failed")
        # 0.05 s backoff to 299.95 s, then the limiter's 0.06 s to 300.01 s: not sent.
        self.assertEqual(self.sleeps, [0.05, 0.06])
        self.assertEqual(len(self.reads), 1)
        self.assert_still_pending_without_mutations()

    def test_slow_read_returning_success_after_the_deadline_never_clears_the_order(self):
        for case, steps, reads in (
            ("confirmation", [(5, self.accepted()), (0, self.listed())],
             [("/api/v1/confirms/o_reference", 299, 304)]),
            ("positions", [(1, self.accepted()), (3, self.listed())],
             [("/api/v1/confirms/o_reference", 298, 299), ("/api/v1/positions", 299, 302)]),
        ):
            with self.subTest(case):
                self.setUp()
                self.at(reads[0][1])
                self.script.extend(steps)
                with self.assertLogs("trading_bot", level="WARNING") as logs, \
                        self.assertRaises(TradingUnavailable) as stopped:
                    self.pending.poll(self.api)
                self.assertEqual(stopped.exception.reason, "order_confirmation_unresolved")
                self.assertTrue(stopped.exception.outcome_uncertain)
                self.assertEqual(self.reads, reads)
                self.assertIn("detail=confirmation_timeout", "\n".join(logs.output))
                self.assert_still_pending_without_mutations()  # Success after 300 s does not count.

                # Through recovery as well: a resolution finishing late does not resume.
                self.setUp()
                self.at(reads[0][1])
                self.script.extend(steps)
                with self.assertLogs("trading_bot", level="WARNING"), \
                        self.assertRaises(TradingUnavailable) as stopped:
                    self.pending.resolve(self.api)
                self.assertEqual(stopped.exception.reason, "order_confirmation_unresolved")
                self.assert_still_pending_without_mutations()

    def test_no_confirmation_read_starts_after_the_deadline(self):
        self.at(300.5)
        with self.assertLogs("trading_bot", level="WARNING"), \
                self.assertRaises(TradingUnavailable) as stopped:
            self.pending.poll(self.api)
        self.assertEqual(stopped.exception.reason, "order_confirmation_unresolved")
        self.assertEqual(self.reads, [])
        self.assert_still_pending_without_mutations()

    def test_confirmation_and_positions_finishing_in_time_resume_trading(self):
        self.at(295)
        self.script.extend([(2, self.accepted()), (2, self.listed())])
        with self.assertLogs("trading_bot", level="INFO") as logs:
            self.assertTrue(self.pending.resolve(self.api))
        self.assertEqual(self.reads, [("/api/v1/confirms/o_reference", 295, 297),
                                      ("/api/v1/positions", 297, 299)])
        self.assertEqual(len(self.pending), 0)
        self.assertIn("Order confirmed operation=open", "\n".join(logs.output))
        self.assertIsNone(self.pending.deadline())

        # A retry that starts before the deadline and confirms in time also counts.
        self.setUp()
        self.at(296)
        self.script.extend([(0, reset_error()), (1, self.accepted()), (0.5, self.listed())])
        with self.assertLogs("trading_bot", level="INFO"):
            self.assertIsNone(self.pending.poll(self.api))
        self.assertEqual(self.reads, [("/api/v1/confirms/o_reference", 296, 296),
                                      ("/api/v1/confirms/o_reference", 298, 299),
                                      ("/api/v1/positions", 299, 299.5)])
        self.assertEqual(len(self.pending), 0)


def candle_rows(*starts):
    return {"prices": [{"snapshotTimeUTC": start.replace(tzinfo=None).isoformat(),
                        "snapshotTime": "private-marker-local-time",
                        "closePrice": {"ask": float(index)}} for index, start in enumerate(starts)]}


class CompletedCandleTests(unittest.TestCase):
    def at(self, hour, minute, day=17):
        return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc)

    def test_latest_completed_candle_is_required_and_the_forming_one_is_discarded(self):
        payload = candle_rows(self.at(21, 0), self.at(21, 30), self.at(22, 0))
        self.assertEqual(completed_closes(payload, "MINUTE_30", UTC_BOUNDARY), [0.0, 1.0])

    def test_stale_or_gapped_history_skips_the_cycle(self):
        cases = {
            "three_hours_old": candle_rows(self.at(18, 0), self.at(18, 30), self.at(19, 0)),
            "forming_only_after_a_gap": candle_rows(self.at(20, 30), self.at(22, 0)),
            "empty": {"prices": []},
        }
        for name, payload in cases.items():
            with self.subTest(case=name), self.assertLogs("trading_bot", level="WARNING") as logs:
                self.assertIsNone(completed_closes(payload, "MINUTE_30", UTC_BOUNDARY))
            self.assertIn("reason=expected_candle_missing timeframe=MINUTE_30", logs.output[0])

    def test_schedule_override_uses_the_candle_timeframe_not_the_schedule(self):
        payload = candle_rows(self.at(21, 0), self.at(21, 30), self.at(22, 0))
        for minute in (5, 25):  # MINUTE_5 schedule inside the 22:00 candle.
            boundary = UTC_BOUNDARY + timedelta(minutes=minute)
            self.assertEqual(completed_closes(payload, "MINUTE_30", boundary), [0.0, 1.0])
        half_past = UTC_BOUNDARY + timedelta(minutes=30)
        self.assertEqual(completed_closes(payload, "MINUTE_30", half_past), [0.0, 1.0, 2.0])
        with self.assertLogs("trading_bot", level="WARNING"):  # 22:30 needs the 22:00 candle.
            self.assertIsNone(completed_closes(candle_rows(self.at(21, 0), self.at(21, 30)),
                                               "MINUTE_30", half_past))
        daily = candle_rows(self.at(22, 0, day=15), self.at(22, 0, day=16), self.at(22, 0, day=17))
        self.assertEqual(completed_closes(daily, "DAY", self.at(22, 0)), [0.0, 1.0])

    def test_malformed_unordered_or_duplicate_candles_skip_the_cycle(self):
        good = candle_rows(self.at(21, 0), self.at(21, 30))
        cases = {
            "duplicate": candle_rows(self.at(21, 30), self.at(21, 30)),
            "unordered": candle_rows(self.at(21, 30), self.at(21, 0)),
            "missing_timestamp": {"prices": [{"closePrice": {"ask": 1.0}}]},
            "bad_timestamp": {"prices": [{"snapshotTimeUTC": "private-marker", "closePrice": {"ask": 1.0}}]},
            "boolean_close": {"prices": [dict(good["prices"][0], closePrice={"ask": True})]},
            "nan_close": {"prices": [dict(good["prices"][0], closePrice={"ask": float("nan")})]},
            "not_a_list": {"prices": {"a": 1}},
            "not_a_dict": None,
        }
        for name, payload in cases.items():
            with self.subTest(case=name), self.assertLogs("trading_bot", level="WARNING") as logs:
                self.assertIsNone(completed_closes(payload, "MINUTE_30", UTC_BOUNDARY))
            self.assertIn("reason=invalid_candle_data", logs.output[0])
            assert_redacted(self, *logs.output)


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


    # --- Acknowledged orders awaiting broker confirmation (review finding 1) ---

    def ack_time(self, method="post", path="/api/v1/positions"):
        (acknowledged,) = self.broker.times(method, path)
        return acknowledged

    def test_acknowledged_buy_not_yet_listed_is_never_resent_across_an_outage(self):
        self.broker.lag = True  # BUY: HTTP 200, but no confirmation and zero listed positions.
        self.broker.outages.append((self.at(22, 1), self.at(23, 10)))  # DNS fails before 22:30.
        stopped = self.run_hma("--cycles", "4", expect=TradingUnavailable)
        self.assertEqual(stopped.reason, "order_confirmation_unresolved")
        self.assertTrue(stopped.outcome_uncertain)
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)  # Never a second BUY.
        self.assertEqual(self.broker.listed_positions(), [])  # A naive bot would buy again here.
        self.assertEqual(self.executed(), [self.at(22, 0)])
        # The outage cannot outlast the confirmation deadline: stopped at 300 s, not at 23:10.
        elapsed = (self.clock.local() - self.ack_time()).total_seconds()
        self.assertTrue(300 <= elapsed < 301, elapsed)
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 1)
        for event in ("Order acknowledged operation=open", "Trading paused reason=dns_resolution_failed",
                      "Trading stopped reason=order_confirmation_unresolved", "not tracked across a restart"):
            self.assertIn(event, self.output)
        self.assertNotIn("Scheduled trading resumed", self.output)

    def test_unconfirmed_buy_is_rechecked_on_the_recovered_session_then_stops_at_the_deadline(self):
        self.broker.lag = True
        self.broker.outages.append((self.at(22, 1), self.at(22, 3)))
        stopped = self.run_hma("--cycles", "4", expect=TradingUnavailable)
        self.assertEqual(stopped.reason, "order_confirmation_unresolved")
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 2)  # One recovery login.
        self.assertIn("Scheduled trading resumed environment=DEMO pending_orders=1", self.output)
        confirms = self.broker.times("get", "/api/v1/confirms/o_private-marker-1")
        self.assertTrue(any(t >= self.at(22, 3) for t in confirms))  # Polled on the new session.
        elapsed = (self.clock.local() - self.ack_time()).total_seconds()
        self.assertTrue(300 <= elapsed < 301, elapsed)
        self.assertEqual(self.executed(), [self.at(22, 0)])

    def test_buy_confirmed_after_recovery_resumes_without_a_second_buy(self):
        self.broker.lag = True
        self.broker.release_at = self.at(22, 3)  # Confirmed and listed once the network returns.
        self.broker.outages.append((self.at(22, 1), self.at(22, 3)))
        self.run_hma("--cycles", "3")
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)
        self.assertEqual(self.executed(), [self.at(22, 0), self.at(22, 30), self.at(23, 0)])
        self.assertIn("Order confirmed operation=open", self.output)
        self.assertIn("Scheduled trading resumed environment=DEMO pending_orders=0", self.output)
        self.assertNotIn("not tracked across a restart", self.output)

    def test_pending_confirmation_pauses_cycles_until_the_broker_reflects_the_order(self):
        self.clock = FakeClock(self.at(21, 59, 30))
        self.broker = Broker(self.clock)
        self.broker.lag = True
        self.broker.release_at = self.at(22, 3, 30)
        self.run_hma("--time-frame", "MINUTE", "--cycles", "5")
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)
        # 22:01-22:03 calculate no signal; 22:04 sees the listed position and buys nothing.
        self.assertEqual(self.executed(), [self.at(22, 0), self.at(22, 4)])
        self.assertEqual(self.output.count("reason=order_confirmation_pending"), 3)
        self.assertNotIn("Trading stopped", self.output)

    def test_acknowledged_close_still_listed_is_never_resent_across_an_outage(self):
        self.broker.trend = -1
        self.broker.positions.append({"market": {"epic": "EURUSD"},
                                      "position": {"dealId": "fixture-deal-private-0"}})
        self.broker.lag = True
        self.broker.outages.append((self.at(22, 1), self.at(23, 10)))
        stopped = self.run_hma("--cycles", "3", expect=TradingUnavailable)
        self.assertEqual(stopped.reason, "order_confirmation_unresolved")
        self.assertEqual(self.broker.count("delete", "/api/v1/positions/fixture-deal-private-0"), 1)
        self.assertEqual(len(self.broker.listed_positions()), 1)  # Still listed after the close.
        delete = self.ack_time("delete", "/api/v1/positions/fixture-deal-private-0")
        self.assertTrue(300 <= (self.clock.local() - delete).total_seconds() < 301)
        self.assertIn("Trading stopped reason=order_confirmation_unresolved operation=close", self.output)

    def test_close_confirmed_after_recovery_is_not_resent(self):
        self.broker.trend = -1
        self.broker.positions.append({"market": {"epic": "EURUSD"},
                                      "position": {"dealId": "fixture-deal-private-0"}})
        self.broker.lag = True
        self.broker.release_at = self.at(22, 3)
        self.broker.outages.append((self.at(22, 1), self.at(22, 3)))
        self.run_hma("--cycles", "3")
        self.assertEqual(sum(m == "delete" for m, _, _ in self.broker.calls), 1)
        self.assertEqual(self.broker.positions, [])
        self.assertIn("Order confirmed operation=close", self.output)

    # --- Confirmation deadline independent of the candle schedule (second review, 3) ---

    def test_persistently_pending_order_stops_at_the_deadline_on_the_default_schedule(self):
        self.broker.lag = True  # Never confirmed; no outage.
        stopped = self.run_hma("--cycles", "3", expect=TradingUnavailable)
        self.assertEqual(stopped.reason, "order_confirmation_unresolved")
        self.assertTrue(stopped.outcome_uncertain)
        acknowledged = self.ack_time()
        # Polled at once, then every 10 s, the last check at exactly 300 s; never 1,800 s.
        confirms = self.broker.times("get", "/api/v1/confirms/o_private-marker-1")
        self.assertEqual([(t - acknowledged).total_seconds() for t in confirms], list(range(0, 301, 10)))
        self.assertEqual((self.clock.local() - acknowledged).total_seconds(), 300)
        # Polling only reads: no signal, no further order, no strategy cycle before 22:30.
        self.assertEqual(self.executed(), [self.at(22, 0)])
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)
        self.assertEqual(sum(m in ("post", "delete", "put") and p != "/api/v1/session"
                             for m, p, _ in self.broker.calls), 1)
        self.assertEqual(self.output.count("Order confirmation pending operation=open"), 1)
        self.assertIn("detail=confirmation_timeout", self.output)
        self.assertIn("not tracked across a restart", self.output)

    def test_order_confirmed_in_time_lets_the_next_cycle_proceed_normally(self):
        self.broker.lag = True
        self.broker.release_at = self.at(22, 3)  # Confirmed and listed after about three minutes.
        self.run_hma("--cycles", "2")
        acknowledged = self.ack_time()
        confirms = self.broker.times("get", "/api/v1/confirms/o_private-marker-1")
        self.assertEqual(max(confirms), self.at(22, 3, 2))  # Polling ends once confirmed.
        self.assertLess((max(confirms) - acknowledged).total_seconds(), 300)
        self.assertIn("Order confirmed operation=open", self.output)
        # 22:30 evaluates normally: the listed BUY means no second order and no skip.
        self.assertEqual(self.executed(), [self.at(22, 0), self.at(22, 30)])
        self.assertGreaterEqual(self.broker.count("get", "/api/v1/positions", self.at(22, 30)), 1)
        self.assertNotIn("reason=order_confirmation_pending", self.output)
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)
        self.assertNotIn("Trading stopped", self.output)

    # --- Third review, 2: the deadline holds through retries in the real loop ---

    def test_confirmation_retry_backing_off_to_the_deadline_is_never_started(self):
        self.broker.lag = True

        def network_drops_near_the_deadline(method, path):
            if not path.startswith("/api/v1/confirms/"):
                return
            age = (self.clock.local() - self.ack_time()).total_seconds()
            if 290 <= age < 300:
                raise dns_error()
            if age >= 300:  # Late: this read would take 4 s and confirm the order.
                self.clock.elapsed += 4
                self.broker.release()

        self.broker.request_hook = network_drops_near_the_deadline
        stopped = self.run_hma("--cycles", "3", expect=TradingUnavailable)
        self.assertEqual(stopped.reason, "order_confirmation_unresolved")
        self.assertTrue(stopped.outcome_uncertain)
        acknowledged = self.ack_time()
        confirms = self.broker.times("get", "/api/v1/confirms/o_private-marker-1")
        # Polls every 10 s, then retries at 292 and 296 s; the backoff to 300 s ends there.
        self.assertEqual([(t - acknowledged).total_seconds() for t in confirms],
                         [*range(0, 291, 10), 292, 296])
        self.assertEqual((self.clock.local() - acknowledged).total_seconds(), 300)  # Not 304 s.
        self.assertEqual(self.broker.listed_positions(), [])  # Never released: not even read.
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)
        self.assertEqual(self.broker.count("post", "/api/v1/session"), 1)  # No recovery login.
        self.assertEqual(self.executed(), [self.at(22, 0)])
        self.assertIn("confirmation deadline reached before the retry started", self.output)
        self.assertIn("detail=confirmation_timeout", self.output)
        self.assertNotIn("Order confirmed", self.output)
        self.assertNotIn("Scheduled trading resumed", self.output)

    # --- Third review, 3: confirmations stay polled while the market is closed ---

    @staticmethod
    def utc_local(day, hour, minute, second=0):
        """Naive local time for a UTC moment: market hours are UTC, the scheduler is local."""
        moment = datetime(2026, 11, day, hour, minute, second, tzinfo=timezone.utc)
        return moment.astimezone().replace(tzinfo=None)

    def friday_close(self):
        self.assertEqual(datetime(2026, 11, 20).weekday(), 4)  # Friday.
        self.clock = FakeClock(self.utc_local(20, 21, 58, 30))
        self.broker = Broker(self.clock)
        self.broker.lag = True  # BUY acknowledged at 21:59:02 UTC, just before the close.

    def strategy_traffic(self, start, end):
        """Every request in [start, end) other than the read-only confirmation checks.

        A confirmation check reads ``/confirms`` and, once confirmed, the position list.
        """
        return [(m, p) for m, p, t in self.broker.calls
                if start <= t < end and not (m == "get" and (
                    p.startswith("/api/v1/confirms/") or p == "/api/v1/positions"))]

    def test_order_acknowledged_before_friday_close_stops_at_its_deadline_not_sunday(self):
        self.friday_close()
        stopped = self.run_hma("--time-frame", "MINUTE", "--days", "5", "--cycles", "5",
                               expect=TradingUnavailable)
        self.assertEqual(stopped.reason, "order_confirmation_unresolved")
        self.assertTrue(stopped.outcome_uncertain)
        acknowledged = self.ack_time()
        self.assertEqual(acknowledged, self.utc_local(20, 21, 59, 2))
        self.assertEqual((self.clock.local() - acknowledged).total_seconds(), 300)  # Friday 22:04:02.
        confirms = self.broker.times("get", "/api/v1/confirms/o_private-marker-1")
        self.assertEqual([(t - acknowledged).total_seconds() for t in confirms], list(range(0, 301, 10)))
        closed = self.utc_local(20, 22, 0, 2)
        self.assertTrue(any(t >= closed for t in confirms))  # Still polled after the close.
        self.assertIn("Market closed. Waiting...", self.stdout)
        # Read-only while closed: no strategy cycle, signal, session work or order.
        self.assertEqual(self.strategy_traffic(closed, self.clock.local() + timedelta(1)), [])
        self.assertEqual(self.executed(), [self.utc_local(20, 21, 59)])
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)
        self.assertIn("detail=confirmation_timeout", self.output)
        self.assertIn("not tracked across a restart", self.output)

    def test_order_confirmed_during_the_weekend_wait_lets_the_next_open_cycle_run(self):
        self.friday_close()
        self.broker.release_at = self.utc_local(20, 22, 2)  # Confirmed and listed after the close.
        self.run_hma("--time-frame", "MINUTE", "--days", "5", "--cycles", "3")
        confirms = self.broker.times("get", "/api/v1/confirms/o_private-marker-1")
        self.assertEqual(max(confirms), self.utc_local(20, 22, 2, 2))  # Polling ended once confirmed.
        self.assertIn("Order confirmed operation=open", self.output)
        self.assertIn("Market is now OPEN!", self.stdout)
        reopened = self.utc_local(22, 22, 0)  # Sunday 22:00 UTC.
        self.assertEqual(self.strategy_traffic(self.utc_local(20, 22, 0, 2), reopened), [])
        # Friday's 22:00 boundary is stale at reopening and is skipped; Sunday 22:01 runs.
        self.assertIn("reason=stale_boundary stage=cycle_start", self.output)
        self.assertEqual(self.executed(), [self.utc_local(20, 21, 59), self.utc_local(22, 22, 1)])
        self.assertGreaterEqual(self.broker.count("get", "/api/v1/positions", reopened), 1)
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)  # Already long: no second BUY.
        self.assertNotIn("reason=order_confirmation_pending", self.output)
        self.assertNotIn("Trading stopped", self.output)
        self.assertNotIn("not tracked across a restart", self.output)

    # --- Acknowledgements and controlled exits (second review, 2) ---

    def test_deferred_sigterm_during_a_successful_buy_records_the_acknowledgement(self):
        self.broker.lag = True

        def sigterm_during_order(method, path):
            if method == "post" and path == "/api/v1/positions":
                signal.raise_signal(signal.SIGTERM)  # Deferred: the order request is in flight.

        self.broker.request_hook = sigterm_during_order
        stopped = self.run_hma("--cycles", "3", expect=SystemExit)
        self.assertEqual(stopped.code, 143)
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)
        lines = self.output.splitlines()
        order = [next(i for i, line in enumerate(lines) if text in line) for text in (
            "Shutdown requested signal=SIGTERM; deferred", "Order acknowledged operation=open",
            "Shutdown complete", "1 acknowledged order(s) are not yet confirmed")]
        self.assertEqual(order, sorted(order))
        self.assertIn("reconcile positions on Capital.com before restarting", self.output)
        self.assertNotIn("outcome is uncertain", self.output)  # The BUY outcome itself is known.

    def test_deferred_sigterm_during_a_successful_close_records_the_acknowledgement(self):
        self.broker.trend = -1
        self.broker.positions.append({"market": {"epic": "EURUSD"},
                                      "position": {"dealId": "fixture-deal-private-0"}})
        self.broker.lag = True

        def sigterm_during_close(method, path):
            if method == "delete":
                signal.raise_signal(signal.SIGTERM)

        self.broker.request_hook = sigterm_during_close
        stopped = self.run_hma("--cycles", "3", expect=SystemExit)
        self.assertEqual(stopped.code, 143)
        self.assertEqual(sum(m == "delete" for m, _, _ in self.broker.calls), 1)
        self.assertIn("Order acknowledged operation=close", self.output)
        self.assertIn("1 acknowledged order(s) are not yet confirmed", self.output)
        self.assertNotIn("outcome is uncertain", self.output)

    def test_forced_interrupt_during_an_order_keeps_the_uncertain_outcome_warning(self):
        def double_sigterm(method, path):
            if method == "post" and path == "/api/v1/positions":
                signal.raise_signal(signal.SIGTERM)
                signal.raise_signal(signal.SIGTERM)  # Repeated: forces an immediate interrupt.

        self.broker.request_hook = double_sigterm
        stopped = self.run_hma("--cycles", "3", expect=SystemExit)
        self.assertEqual(stopped.code, 143)
        self.assertIn("an order request was interrupted and its outcome is uncertain", self.output)
        self.assertNotIn("Order acknowledged", self.output)

    def test_cycle_limited_exit_with_an_unresolved_order_warns_before_restart(self):
        self.broker.lag = True
        self.run_hma("--cycles", "1")  # Ends normally right after the acknowledged BUY.
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)
        self.assertIn("CRITICAL:trading_bot.utils.network_recovery:1 acknowledged order(s) are not "
                      "yet confirmed", self.output)
        self.assertIn("reconcile positions on Capital.com before restarting", self.output)

        self.setUp()  # Confirmed while waiting for the next boundary: nothing to warn about.
        self.run_hma("--cycles", "2")
        self.assertIn("Order confirmed operation=open", self.output)
        self.assertNotIn("acknowledged order(s) are not yet confirmed", self.output)

    # --- Candle freshness (review finding 2) ---

    def test_fresh_boundary_with_three_hour_old_candles_skips_the_cycle(self):
        self.broker.candle_lag = timedelta(hours=3)
        self.run_hma("--cycles", "1")
        self.assertEqual(self.executed(), [self.at(22, 0)])  # Fresh boundary; history fetched.
        self.assertIn("reason=expected_candle_missing timeframe=MINUTE_30", self.output)
        self.assertEqual(self.broker.count("get", "/api/v1/positions", self.at(22, 0)), 0)
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 0)

    def test_incomplete_current_candle_never_reaches_the_signal(self):
        from trading_bot.strategies.indicators import hma_signal

        served = []

        def crash_forming_candle(rows):
            rows[-1]["closePrice"]["ask"] = 0.5  # The still-forming 22:00 candle.
            served.extend(rows)
            return rows

        self.broker.candle_hook = crash_forming_candle
        self.run_hma("--cycles", "1")
        closes = [row["closePrice"]["ask"] for row in served]
        forming = floor_utc(self.at(22, 0).astimezone(timezone.utc), timedelta(minutes=30))
        self.assertEqual(served[-1]["snapshotTimeUTC"], forming.replace(tzinfo=None).isoformat())
        self.assertEqual(hma_signal(closes, 16), "SELL")  # Including it would flip the signal...
        self.assertEqual(hma_signal(closes[:-1], 16), "BUY")
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)  # ...but only completed ones count.

        self.setUp()  # The forming candle alone, with the expected completed one missing.
        self.broker.candle_hook = lambda rows: rows[:-3] + rows[-1:]
        self.run_hma("--cycles", "1")
        self.assertIn("reason=expected_candle_missing", self.output)
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 0)

    def test_schedule_override_validates_against_the_candle_timeframe(self):
        # A MINUTE_5 schedule from 21:50: the first boundary is 21:55 and the latest
        # completed MINUTE_30 candle is 21:00 (the 21:30 one is still forming).
        starts = []
        self.broker.candle_hook = lambda rows: starts.extend(r["snapshotTimeUTC"] for r in rows) or rows
        self.run_hma("--schedule-time-frame", "MINUTE_5", "--cycles", "1")
        self.assertEqual(self.executed(), [self.at(21, 55)])
        spacing = {datetime.fromisoformat(b) - datetime.fromisoformat(a) for a, b in zip(starts, starts[1:])}
        self.assertEqual(spacing, {timedelta(minutes=30)})  # Candle resolution is unchanged.
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 1)

        self.setUp()  # Same override; the broker's feed is 65 minutes behind, ending at 20:30.
        self.broker.candle_lag = timedelta(minutes=65)
        self.run_hma("--schedule-time-frame", "MINUTE_5", "--cycles", "1")
        self.assertIn("reason=expected_candle_missing", self.output)
        self.assertEqual(self.broker.count("post", "/api/v1/positions"), 0)

    # --- Freshness inside a close batch (review finding 3) ---

    def test_batch_close_rechecks_freshness_before_each_close(self):
        self.broker.trend = -1
        for n in (0, 1):
            self.broker.positions.append({"market": {"epic": "EURUSD"},
                                          "position": {"dealId": f"fixture-deal-private-{n}"}})
        state = {"listed": False}

        def timing(method, path):
            if method == "get" and path == "/api/v1/positions" and not state["listed"] \
                    and self.clock.local() >= self.at(22, 0):
                state["listed"] = True  # The first close starts 119 s after the boundary...
                self.clock.elapsed = (self.at(22, 1, 59) - self.START).total_seconds()
            elif method == "delete":
                self.clock.elapsed += 3  # ...and returns at 122 s, past the 120 s limit.

        self.broker.request_hook = timing
        self.run_hma("--cycles", "1", "--max-signal-delay", "120")
        deletes = [(p, t) for m, p, t in self.broker.calls if m == "delete"]
        self.assertEqual(deletes, [("/api/v1/positions/fixture-deal-private-0", self.at(22, 1, 59))])
        self.assertEqual([p["position"]["dealId"] for p in self.broker.positions], ["fixture-deal-private-1"])
        self.assertIn("reason=stale_boundary stage=order", self.output)
        self.assertIn("Order confirmed operation=close", self.output)  # Reconciled before the check.


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

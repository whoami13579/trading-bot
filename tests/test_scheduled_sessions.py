"""Cycle-boundary, scheduler CLI, and read-only diagnostic regressions."""

from contextlib import ExitStack, redirect_stdout, redirect_stderr
from datetime import datetime
import importlib
import io
import unittest
from unittest.mock import Mock, patch

import requests
from trading_bot.api.network_errors import TradingUnavailable
from trading_bot.config.trading_config import DEMO_URL, LIVE_URL
from trading_bot.strategies import hma, sma, moving_average
from trading_bot.tools.session_diagnostics import (
    demo_readonly_transport,
    main as diagnostic_main,
)
from trading_bot.utils import scheduler


class EndCycle(BaseException):
    pass


class ScheduledStrategyTests(unittest.TestCase):
    def assert_hma_submission_message(self, direction):
        api = Mock()
        api.getAllPositionsList.return_value = []
        api.createPosition.return_value = ({"dealReference": "private-reference"}, 200)
        output = io.StringIO()
        with (
            patch("sys.argv", ["hma.py", "--cycles", "1", "--direction", direction]),
            patch.object(hma, "create_trading_api", return_value=api),
            patch.object(hma, "wait_until_targets"),
            patch.object(hma, "calculate_hma_result", return_value=direction),
            redirect_stdout(output),
        ):
            hma.main()
        api.createPosition.assert_called_once_with("EURUSD", direction, 100)
        api.closePosition.assert_not_called()
        color = hma.colors.BLUE if direction == "BUY" else hma.colors.RED
        self.assertEqual(output.getvalue(), f">>> {color}{direction}{hma.colors.ENDC} order submitted\n")
        for text in ("confirmed", "filled", "private-reference"):
            self.assertNotIn(text, output.getvalue())

    def test_hma_buy_response_dictionary_logs_buy_submitted(self):
        self.assert_hma_submission_message("BUY")

    def test_hma_sell_response_dictionary_logs_sell_submitted(self):
        self.assert_hma_submission_message("SELL")

    def run_readonly_hma(self, options=()):
        api = Mock()
        api.prepare_trading_cycle.side_effect = [True, False, False]
        api.getHistoricalPrices.return_value = ({"prices": [{}] * 48}, 200)
        with (
            patch("sys.argv", ["hma.py", "--read-only", "--cycles", "3", *options]),
            patch.object(hma, "create_trading_api", return_value=api) as factory,
            patch.object(hma, "wait_until_targets") as wait,
            patch.object(
                hma,
                "calculate_hma_result",
                side_effect=AssertionError("No signals in read-only mode"),
            ) as signal,
            redirect_stdout(io.StringIO()),
        ):
            hma.main()
        self.assertEqual(api.prepare_trading_cycle.call_count, 3)
        self.assertEqual(api.getHistoricalPrices.call_count, 3)
        self.assertFalse(factory.call_args.kwargs["order_execution"])
        api.createPosition.assert_not_called()
        api.closePosition.assert_not_called()
        api.updatePosition.assert_not_called()
        api.getAllPositionsList.assert_not_called()
        signal.assert_not_called()
        return api, wait

    def test_hma_production_default_remains_thirty_minutes(self):
        api, wait = self.run_readonly_hma()
        self.assertEqual(hma.DEFAULT_TIMEFRAME, "MINUTE_30")
        for call in wait.call_args_list:
            self.assertEqual(call.args, ([0, 30],))
        api.getHistoricalPrices.assert_called_with("EURUSD", "MINUTE_30", 48)

    def test_hma_scheduling_override_does_not_change_candle_resolution(self):
        api, wait = self.run_readonly_hma(["--schedule-time-frame", "MINUTE"])
        for call in wait.call_args_list:
            self.assertEqual(call.args, (list(range(60)),))
        api.getHistoricalPrices.assert_called_with("EURUSD", "MINUTE_30", 48)

    def test_legacy_timeframe_option_still_selects_data_and_schedule(self):
        api, wait = self.run_readonly_hma(["--time_frame", "MINUTE_5"])
        for call in wait.call_args_list:
            self.assertEqual(call.args, (list(range(0, 60, 5)),))
        api.getHistoricalPrices.assert_called_with("EURUSD", "MINUTE_5", 48)

    def test_readonly_mode_and_cli_validation_precede_authentication(self):
        for options in (
            ["--read-only", "--REAL"],
            ["--cycles", "0"],
            ["--schedule-time-frame", "HOUR"],
            ["--time_frame", "BAD"],
        ):
            with (
                self.subTest(options=options),
                patch("sys.argv", ["hma.py", *options]),
                patch.object(hma, "create_trading_api") as factory,
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                hma.main()
            factory.assert_not_called()

    def test_renewal_failure_stops_before_the_signal_or_any_order(self):
        api = Mock()
        api.prepare_trading_cycle.side_effect = TradingUnavailable(
            "session_renewal_failed"
        )
        with (
            patch("sys.argv", ["hma.py"]),
            patch.object(hma, "create_trading_api", return_value=api),
            patch.object(hma, "wait_until_targets"),
            patch.object(hma, "calculate_hma_result") as signal,
            self.assertRaises(TradingUnavailable),
        ):
            hma.main()
        signal.assert_not_called()
        api.createPosition.assert_not_called()
        api.closePosition.assert_not_called()

    def test_multiple_scheduled_readonly_cycles_stop_cleanly_on_auth_failure(self):
        api = Mock()
        api.prepare_trading_cycle.side_effect = [
            False,
            TradingUnavailable("authentication_failed_http_401"),
        ]
        api.getHistoricalPrices.return_value = ({"prices": [{}] * 48}, 200)
        with (
            patch("sys.argv", ["hma.py", "--read-only", "--cycles", "3"]),
            patch.object(hma, "create_trading_api", return_value=api),
            patch.object(hma, "wait_until_targets") as wait,
            redirect_stdout(io.StringIO()),
            self.assertRaises(TradingUnavailable),
        ):
            hma.main()
        self.assertEqual(wait.call_count, 2)
        self.assertEqual(api.getHistoricalPrices.call_count, 1)
        api.createPosition.assert_not_called()

    def test_each_hma_family_validates_after_scheduler_and_market_waits(self):
        for name in ("hma", "ema_hma", "multiple_hma"):
            module = importlib.import_module("trading_bot.strategies." + name)
            events = []
            api = Mock()
            api.is_market_open.return_value = False
            api.wait_until_open.side_effect = lambda: events.append("market-wait")
            api.prepare_trading_cycle.side_effect = lambda: events.append("session")
            api.getAllPositionsList.return_value = []
            api.createPosition.return_value = ({}, 200)
            calls = iter([None, EndCycle()])

            def wait(targets, *, keepalive=None):
                result = next(calls)
                if isinstance(result, BaseException):
                    raise result
                events.append("schedule-wait")

            with self.subTest(strategy=name), ExitStack() as stack:
                stack.enter_context(patch("sys.argv", [name + ".py", "--days", "5"]))
                stack.enter_context(
                    patch.object(module, "create_trading_api", return_value=api)
                )
                stack.enter_context(
                    patch.object(module, "wait_until_targets", side_effect=wait)
                )
                stack.enter_context(
                    patch.object(
                        module,
                        "calculate_hma_result",
                        side_effect=lambda *args: events.append("signal") or "BUY",
                    )
                )
                if name == "ema_hma":
                    stack.enter_context(
                        patch.object(
                            module, "calculate_two_ema_result", return_value="BUY"
                        )
                    )
                if name == "multiple_hma":
                    stack.enter_context(
                        patch.object(module, "calculate_multiple", return_value="BUY")
                    )
                stack.enter_context(redirect_stdout(io.StringIO()))
                with self.assertRaises(EndCycle):
                    module.main()
            self.assertLess(events.index("schedule-wait"), events.index("market-wait"))
            self.assertLess(events.index("market-wait"), events.index("session"))
            self.assertLess(events.index("session"), events.index("signal"))
            api.load_keys.assert_not_called()
            api.createPosition.assert_called_once_with("EURUSD", "BUY", 100)

    def test_sma_cycle_checks_session_before_fetch_or_order(self):
        api = Mock()
        events = []
        api.prepare_trading_cycle.side_effect = lambda: events.append("session")
        api.getHistoricalPrices.side_effect = lambda *args: (
            events.append("history") or {"prices": [{"openPrice": {"ask": 2}}]},
            200,
        )
        api.createPosition.side_effect = lambda *args: (
            events.append("order") or {},
            200,
        )
        with (
            patch("sys.argv", ["sma.py"]),
            patch.object(sma, "create_trading_api", return_value=api),
            patch.object(sma, "get_buffer_prices", return_value=[1] * 21),
            patch.object(sma.time, "sleep", side_effect=EndCycle()),
            redirect_stdout(io.StringIO()),
            self.assertRaises(EndCycle),
        ):
            sma.main()
        self.assertEqual(events, ["session", "history", "order"])

    def test_moving_average_failure_preserves_buffer_and_skips_history(self):
        prices = [1, 2, 3]
        api = Mock()
        api.prepare_trading_cycle.side_effect = TradingUnavailable(
            "session_renewal_failed"
        )
        with self.assertRaises(TradingUnavailable):
            moving_average.update_prices(prices, api)
        self.assertEqual(prices, [1, 2, 3])
        api.getHistoricalPrices.assert_not_called()


class SchedulerTests(unittest.TestCase):
    def test_supported_minute_mappings(self):
        for frame, step in (
            ("MINUTE", 1),
            ("MINUTE_5", 5),
            ("MINUTE_15", 15),
            ("MINUTE_30", 30),
        ):
            self.assertEqual(
                scheduler.timeframe_to_minutes(frame), list(range(0, 60, step))
            )
        with self.assertRaises(ValueError):
            scheduler.timeframe_to_minutes("HOUR")

    def test_target_validation_happens_before_sleep(self):
        with patch.object(scheduler.time, "sleep") as sleep:
            for targets in (None, [], [60], [-1], [True], ["30"]):
                with self.subTest(targets=targets), self.assertRaises(ValueError):
                    scheduler.wait_until_targets(targets)
            sleep.assert_not_called()

    def test_short_schedule_keeps_existing_two_second_settle_delay(self):
        with (
            patch.object(scheduler, "datetime") as now,
            patch.object(scheduler.time, "sleep") as sleep,
            redirect_stdout(io.StringIO()),
        ):
            now.now.return_value = datetime(2026, 11, 12, 22, 15, 30)
            scheduler.wait_until_targets(list(range(60)))
        sleep.assert_called_once_with(32.0)


class ReadonlyDiagnosticSafetyTests(unittest.TestCase):
    def test_final_prepared_request_allowlist_blocks_live_and_all_mutations(self):
        with patch.object(
            requests.sessions.Session, "send", return_value=Mock(status_code=200)
        ) as original:
            records = []
            with demo_readonly_transport(records, login_budget=2):
                for method, path, base in (
                    ("GET", "/api/v1/session", LIVE_URL),
                    ("POST", "/api/v1/positions", DEMO_URL),
                    ("PUT", "/api/v1/positions/owned", DEMO_URL),
                    ("DELETE", "/api/v1/positions/owned", DEMO_URL),
                    ("PUT", "/api/v1/session", DEMO_URL),
                    ("DELETE", "/api/v1/session", DEMO_URL),
                    ("GET", "/api/v1/accounts/preferences", DEMO_URL),
                ):
                    with (
                        self.subTest(method=method, path=path),
                        self.assertRaises(PermissionError),
                    ):
                        requests.Session().send(
                            requests.Request(method, base + path).prepare()
                        )
            original.assert_not_called()

    def test_transport_records_only_safe_metadata_and_bounds_logins(self):
        with patch.object(
            requests.sessions.Session, "send", return_value=Mock(status_code=200)
        ) as original:
            records = []
            with demo_readonly_transport(records, login_budget=1):
                request = requests.Request(
                    "GET",
                    DEMO_URL + "/api/v1/session",
                    headers={
                        "CST": "private-marker",
                        "X-SECURITY-TOKEN": "private-marker",
                    },
                ).prepare()
                requests.Session().send(request)
                login = requests.Request(
                    "POST",
                    DEMO_URL + "/api/v1/session",
                    json={"password": "private-marker"},
                ).prepare()
                requests.Session().send(login)
                with self.assertRaises(PermissionError):
                    requests.Session().send(login)
            self.assertEqual(len(records), 2)
            self.assertNotIn("private-marker", str(records))
            self.assertTrue(records[0]["authentication_headers_present"])
            kwargs = original.call_args.kwargs
            self.assertIs(kwargs["verify"], True)
            self.assertIs(kwargs["allow_redirects"], False)

    def test_invalid_diagnostic_arguments_cannot_authenticate(self):
        for args in (
            ["--REAL"],
            ["--probe-expired", "--idle-seconds", "1"],
            ["--cycles", "0"],
            ["--idle-seconds", "3601"],
        ):
            with (
                self.subTest(args=args),
                patch("trading_bot.tools.session_diagnostics.TradingApi") as factory,
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                diagnostic_main(args)
            factory.assert_not_called()


class ReadonlyFailureTests(unittest.TestCase):
    def test_malformed_history_stops_readonly_mode_with_a_fixed_message(self):
        api = Mock()
        api.getHistoricalPrices.return_value = ({}, 200)
        with (
            patch("sys.argv", ["hma.py", "--read-only", "--cycles", "1"]),
            patch.object(hma, "create_trading_api", return_value=api),
            patch.object(hma, "wait_until_targets"),
            patch.object(hma, "calculate_hma_result") as signal,
            self.assertRaises(SystemExit) as caught,
        ):
            hma.main()
        self.assertIn("read-only cycle failed", str(caught.exception))
        signal.assert_not_called()
        api.createPosition.assert_not_called()


class CombinedDiagnosticTests(unittest.TestCase):
    def run_broker(self, probe_failures=()):
        from test_session_lifecycle import Broker, Clock
        from trading_bot.api.client import TradingApi
        from trading_bot.tools.session_diagnostics import run_diagnostic

        clock = Clock()
        broker = Broker(clock)
        if probe_failures:
            broker.failures[("get", "/api/v1/prices/EURUSD")].extend(
                [200, *probe_failures]
            )
        with ExitStack() as stack:
            stack.enter_context(patch("trading_bot.api.rate_limits.throttle"))
            stack.enter_context(
                patch(
                    "trading_bot.api.client.time.monotonic", side_effect=clock.monotonic
                )
            )
            stack.enter_context(
                patch(
                    "trading_bot.tools.session_diagnostics.time.sleep",
                    side_effect=clock.advance,
                )
            )
            stack.enter_context(
                patch(
                    "trading_bot.tools.session_diagnostics.TradingApi",
                    side_effect=lambda **kwargs: TradingApi(
                        "fixture-key",
                        "fixture-identifier",
                        "fixture-password",
                        **kwargs,
                    ),
                )
            )
            for method in ("get", "post", "put", "delete"):
                stack.enter_context(
                    patch(
                        "trading_bot.api.client.requests." + method,
                        side_effect=lambda url, _method=method, **kwargs: (
                            broker.request(_method, url, **kwargs)
                        ),
                    )
                )
            stack.enter_context(redirect_stdout(io.StringIO()))
            report = run_diagnostic(idle_seconds=610, cycles=3, probe_expired=True)
        return report, broker

    def test_same_session_expiration_and_renewal_are_verified_without_signals_or_mutations(
        self,
    ):
        report, broker = self.run_broker()
        self.assertTrue(report["passed"])
        self.assertTrue(report["expiration_reproduced"])
        self.assertTrue(report["same_authentication_headers"])
        self.assertEqual(report["idle_history_status"], 401)
        self.assertEqual([c["renewed"] for c in report["cycles"]], [True, False, False])
        self.assertFalse(report["signals_computed"])
        self.assertEqual(broker.count("post", "/api/v1/session"), 2)
        self.assertFalse(
            any(m != "get" and p != "/api/v1/session" for m, p, _ in broker.calls)
        )

    def test_one_diagnostic_read_network_failure_can_be_probed_once_more(self):
        report, broker = self.run_broker(
            [requests.ConnectionError("private-marker"), 401]
        )
        self.assertTrue(report["passed"])
        self.assertTrue(report["expiration_reproduced"])
        self.assertEqual(report["probe_network_failures"], ["connection_failed"])
        self.assertNotIn("private-marker", str(report))
        self.assertEqual(broker.count("get", "/api/v1/prices/EURUSD"), 6)

    def test_diagnostic_network_probe_is_bounded_and_not_confused_with_expiration(self):
        report, broker = self.run_broker(
            [
                requests.ConnectionError("private-marker"),
                requests.ConnectionError("private-marker"),
            ]
        )
        self.assertTrue(
            report["passed"]
        )  # Renewal/cycles passed; expiration was not observed.
        self.assertNotIn("idle_history_status", report)
        self.assertEqual(report["probe_network_failures"], ["connection_failed"] * 2)
        self.assertEqual(broker.count("get", "/api/v1/prices/EURUSD"), 6)


class SchedulerKeepaliveTests(unittest.TestCase):
    def test_long_wait_keeps_session_alive_without_changing_target_time(self):
        from test_session_lifecycle import Clock

        clock = Clock()
        checks = []
        with (
            patch.object(scheduler, "datetime") as now,
            patch.object(scheduler.time, "monotonic", side_effect=clock.monotonic),
            patch.object(scheduler.time, "sleep", side_effect=clock.advance),
            redirect_stdout(io.StringIO()),
        ):
            now.now.return_value = datetime(2026, 11, 12, 21, 36, 56)
            scheduler.wait_until_targets(
                [0, 30], keepalive=lambda: checks.append(clock.now)
            )
        self.assertEqual(checks, [1300, 1600, 1900, 2200])
        self.assertEqual(clock.now, 2386)

    def test_keepalive_failure_stops_wait_before_any_cycle(self):
        from test_session_lifecycle import Clock

        clock = Clock()
        keepalive = Mock(side_effect=TradingUnavailable("permission_denied_http_403"))
        with (
            patch.object(scheduler, "datetime") as now,
            patch.object(scheduler.time, "monotonic", side_effect=clock.monotonic),
            patch.object(scheduler.time, "sleep", side_effect=clock.advance),
            redirect_stdout(io.StringIO()),
        ):
            now.now.return_value = datetime(2026, 11, 12, 21, 36, 56)
            with self.assertRaises(TradingUnavailable):
                scheduler.wait_until_targets([0, 30], keepalive=keepalive)
        keepalive.assert_called_once()
        self.assertEqual(clock.now, 1300)

    def test_minute_schedule_does_not_add_unnecessary_mid_wait_pings(self):
        from test_session_lifecycle import Clock

        clock = Clock()
        keepalive = Mock()
        with (
            patch.object(scheduler, "datetime") as now,
            patch.object(scheduler.time, "monotonic", side_effect=clock.monotonic),
            patch.object(scheduler.time, "sleep", side_effect=clock.advance),
            redirect_stdout(io.StringIO()),
        ):
            now.now.return_value = datetime(2026, 11, 12, 22, 15, 30)
            scheduler.wait_until_targets(list(range(60)), keepalive=keepalive)
        keepalive.assert_not_called()
        self.assertEqual(clock.now, 1032)

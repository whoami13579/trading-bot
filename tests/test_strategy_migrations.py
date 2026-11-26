"""Per-strategy migrations to the shared single-position execution (issue #9).

Each strategy keeps its signal rule, defaults and options; scheduling, confirmation,
ownership, recovery, shutdown and LIVE gating are the shared engine's. Offline only:
mocked brokers, the real LIVE gate fixture of test_strategy_environments.
"""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import importlib
import io
import random
import unittest
from unittest.mock import Mock, patch

from test_execution_assumptions import LiveGateCase
from trading_bot.execution.single_position import ExecutionHalted
from trading_bot.risk import live_execution
from trading_bot.strategies import moving_average, registry, sma
from trading_bot.strategies.signals import price_average


class StartupReached(BaseException):
    pass


def opens_history(values):
    """getHistoricalPrices for ask-open strategies: the newest ``count`` of ``values()``."""
    return lambda symbol, timeframe, count: (
        {"prices": [{"openPrice": {"ask": v}} for v in values()[-count:]]}, 200)


def run_main(module, argv, api, waits, **patches):
    """Run ``module.main`` against ``api``; ``waits`` drives the scheduler."""
    with ExitStack() as stack:
        stack.enter_context(patch("sys.argv", [module.__name__, *argv]))
        factory = stack.enter_context(patch.object(module, "create_trading_api", return_value=api))
        stack.enter_context(patch.object(module, "wait_until_targets", side_effect=waits))
        for name, value in patches.items():
            stack.enter_context(patch.object(module, name, value))
        stdout = stack.enter_context(redirect_stdout(io.StringIO()))
        stack.enter_context(redirect_stderr(io.StringIO()))
        try:
            module.main()
            raised = None
        except BaseException as exc:  # Returned to the test for inspection.
            raised = exc
    return raised, factory, stdout.getvalue()


def original_first_decision(windows):
    """Index of the first deciding window in the original moving_average.py loop."""
    i, prices = 0, windows[0]
    if prices[-1] < price_average(prices):
        while prices[-1] < price_average(prices):
            i += 1
            prices = windows[i]
    else:
        while prices[-1] > price_average(prices):
            i += 1
            prices = windows[i]
    return i + 1  # The main loop reads one more price before its first decision.


class CommonOptionTests(unittest.TestCase):
    MIGRATED = ("moving_average", "sma")

    def test_shared_options_are_validated_before_authentication(self):
        for name in self.MIGRATED:
            module = importlib.import_module(f"trading_bot.strategies.{name}")
            for options in (["--read-only", "--REAL"], ["--cycles", "0"], ["--max-signal-delay", "0"],
                            ["--network-retries", "11"],
                            ["--network-retry-delay", "10", "--network-retry-max-delay", "5"]):
                with self.subTest(strategy=name, options=options):
                    raised, factory, _ = run_main(module, options, Mock(), [])
                    self.assertIsInstance(raised, SystemExit)
                    self.assertEqual(raised.code, 2)
                    factory.assert_not_called()

    def test_shared_options_reach_the_engine(self):
        for name in self.MIGRATED:
            module = importlib.import_module(f"trading_bot.strategies.{name}")
            api = Mock()
            api.getHistoricalPrices.return_value = ({"prices": [{"openPrice": {"ask": 1}}] * 300}, 200)
            with self.subTest(strategy=name):
                raised, factory, _ = run_main(
                    module, ["--read-only", "--cycles", "1", "--network-retries", "5",
                             "--network-retry-delay", "1", "--network-retry-max-delay", "9"], api, [None])
                self.assertIsNone(raised)
                policy = api.configure_read_retries.call_args.args[0]
                self.assertEqual((policy.max_retries, policy.base_delay, policy.max_delay), (5, 1.0, 9.0))
                self.assertIs(factory.call_args.kwargs["order_execution"], False)


class MovingAverageMigrationTests(unittest.TestCase):
    def test_startup_wait_is_the_original_loops(self):
        rng = random.Random(20261126)
        checked = 0
        for _ in range(400):
            level, windows = 100, []
            series = [level]
            for _ in range(80):
                series.append(series[-1] + rng.choice((-2, -1, 0, 0, 1, 2)))
            windows = [series[i - 20:i] for i in range(20, len(series) + 1)]
            try:
                expected = original_first_decision(windows)
            except IndexError:
                continue  # The original would still be waiting at the end of the series.
            if expected >= len(windows):
                continue
            startup = moving_average.StartupCross()
            ready = [startup.ready(w) for w in windows]
            self.assertEqual(ready.index(True), expected)
            self.assertTrue(all(ready[expected:]))
            checked += 1
        self.assertGreater(checked, 300)
        # Equal newest price and mean at startup: the next window decides.
        startup = moving_average.StartupCross()
        self.assertEqual([startup.ready(w) for w in ([1] * 20, [1] * 19 + [3])], [False, True])

    def test_defaults_schedule_and_window_are_unchanged(self):
        api = Mock()
        api.getHistoricalPrices.return_value = ({"prices": [{"openPrice": {"ask": 1}}] * 20}, 200)
        raised, factory, out = run_main(moving_average, ["--read-only", "--cycles", "2"], api,
                                        [None, None])
        self.assertIsNone(raised)
        api.getHistoricalPrices.assert_called_with("EURUSD", "MINUTE", 20)
        self.assertEqual(factory.call_args.kwargs, {"symbol": "EURUSD", "size": 100, "order_execution": False})
        api.forbid_mutations.assert_called_once_with()
        for method in ("createPosition", "closePosition", "getAllPositionsList"):
            getattr(api, method).assert_not_called()
        self.assertIn("DEMO read-only cycle 2: MINUTE history HTTP 200; 20 prices", out)

    def test_each_boundary_reads_the_full_window_of_newest_opens(self):
        # The rolling in-memory list of the old loop is replaced by the broker's window,
        # so skipped cycles can never leave gaps or duplicates in it.
        values = [1.0] * 19 + [1.0, 2.0]
        api = Mock()
        api.getAllPositionsList.return_value = []
        api.createPosition.return_value = ({"dealReference": "r1"}, 200)
        api.getHistoricalPrices.side_effect = [
            ({"prices": [{"openPrice": {"ask": v}} for v in values[:20]]}, 200),
            ({"prices": [{"openPrice": {"ask": v}} for v in values[1:]]}, 200),
        ]
        raised, factory, out = run_main(moving_average, ["--cycles", "2"], api, [None, None])
        self.assertIsNone(raised)
        self.assertEqual(factory.call_args.kwargs, {"symbol": "EURUSD", "size": 100})
        self.assertEqual([c.args for c in api.getHistoricalPrices.call_args_list],
                         [("EURUSD", "MINUTE", 20)] * 2)
        api.createPosition.assert_called_once_with("EURUSD", "BUY", 100)
        self.assertIn("BUY", out)

    def test_short_or_malformed_history_skips_the_cycle(self):
        for payload in ({"prices": [{"openPrice": {"ask": 1}}] * 5}, {"prices": [{}] * 20},
                        {"prices": [{"openPrice": {"ask": float("nan")}}] * 20}, {}):
            with self.subTest(payload=str(payload)[:40]):
                api = Mock()
                api.getHistoricalPrices.return_value = (payload, 200)
                raised, _, _ = run_main(moving_average, ["--cycles", "2"], api, [None, None])
                self.assertIsNone(raised)
                api.getAllPositionsList.assert_not_called()
                api.createPosition.assert_not_called()

    def test_live_entry_goes_through_the_gate_and_the_reversal_is_refused(self):
        case = LiveGateCase("setUp")
        case.setUp()
        try:
            case.gate.arm()
            api = case.api
            # Flat (startup), crossing, BUY above the mean, then SELL below it.
            windows = iter([[1.0] * 20, [1.0] * 19 + [1.0], [1.0] * 19 + [2.0],
                            [1.0] * 19 + [2.0], [1.0] * 19 + [0.5]])
            current = {"window": None}

            def wait(*args, **kwargs):
                current["window"] = next(windows)

            api.getHistoricalPrices = Mock(side_effect=opens_history(lambda: current["window"]))
            api.prepare_trading_cycle = Mock(return_value=False)
            raised, _, _ = run_main(moving_average, ["--REAL"], api, wait)
            self.assertIsInstance(raised, ExecutionHalted)
            self.assertEqual(raised.reason, "reversal_requires_confirmed_close")
            self.assertEqual([c.args[0] for c in case.send.call_args_list], ["post"])
            self.assertEqual(case.send.call_args.kwargs["json"]["stopDistance"], 0.001)
            self.assertEqual(list(case.gate.owned), ["owned"])
            self.assertEqual([p["position"]["direction"] for p in case.positions], ["BUY"])
            self.assertFalse(case.gate.failed)  # The gate saw no refused order.
        finally:
            case.doCleanups()

    def test_registry_and_legacy_entry_are_unchanged(self):
        strategy = registry.get("moving-average")
        self.assertEqual(strategy.trading_entry, "trading_bot.strategies.moving_average:main")
        self.assertEqual(strategy.trading_options, frozenset())
        self.assertIs(importlib.import_module("main"), moving_average)


class SmaMigrationTests(unittest.TestCase):
    def test_defaults_schedule_and_window_are_unchanged(self):
        api = Mock()
        api.getHistoricalPrices.return_value = ({"prices": [{"openPrice": {"ask": 1}}] * 21}, 200)
        raised, factory, out = run_main(sma, ["--read-only", "--cycles", "1"], api, [None])
        self.assertIsNone(raised)
        api.getHistoricalPrices.assert_called_with("EURUSD", "MINUTE", 21)
        self.assertEqual((sma.FAST_PERIOD, sma.SLOW_PERIOD), (9, 21))
        for method in ("createPosition", "closePosition", "getAllPositionsList"):
            getattr(api, method).assert_not_called()

    def test_decision_is_the_backtest_rule_with_equal_values_holding(self):
        rising = [float(i) for i in range(21)]
        self.assertEqual(sma.target(rising, None), "BUY")
        self.assertEqual(sma.target(rising[::-1], None), "SELL")
        self.assertEqual(sma.target([1.0] * 21, None), "HOLD")
        self.assertEqual(sma.target([1.0] * 21, "SELL"), "SELL")  # Held: no order.

    def test_symbol_size_and_order_reach_the_broker_once_per_side(self):
        api = Mock()
        api.getHistoricalPrices.return_value = (
            {"prices": [{"openPrice": {"ask": float(i)}} for i in range(21)]}, 200)
        api.getAllPositionsList.return_value = []
        api.createPosition.return_value = ({"dealReference": "r1"}, 200)
        raised, factory, _ = run_main(sma, ["--symbol", "AUDUSD", "--size", "200", "--cycles", "1"],
                                      api, [None])
        self.assertIsNone(raised)
        api.createPosition.assert_called_once_with("AUDUSD", "BUY", 200)
        self.assertEqual(factory.call_args.kwargs, {"symbol": "AUDUSD", "size": 200})

    def test_registry_and_legacy_entry_are_unchanged(self):
        strategy = registry.get("sma")
        self.assertEqual(strategy.trading_entry, "trading_bot.strategies.sma:main")
        self.assertEqual(strategy.trading_options, frozenset())
        self.assertIs(importlib.import_module("sma"), sma)


if __name__ == "__main__":
    unittest.main()

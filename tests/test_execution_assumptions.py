"""PR #8 review regressions: online versus simulated execution, candle selection,
report warnings and per-strategy CLI option validation. Offline, mocks only.
"""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import dataclasses
from datetime import datetime, timedelta, timezone
import importlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_strategy_backtests import (
    PIP, TUESDAY, FakeBroker, FakeProvider, StopLoop, candle, consecutive, sequential_targets,
    session_series, thirty, walk,
)
import test_strategy_environments as environments
from test_strategy_environments import StartupReached
from trading_bot.backtesting import adapters, cli
from trading_bot.backtesting.cli import SIMULATION_OPTIONS, option_names
from trading_bot.backtesting.strategy import EmaHmaStrategy, MultipleHmaStrategy, SmaCrossoverStrategy
from trading_bot.risk import live_execution
from trading_bot.strategies import registry, signals
from trading_bot.strategies.indicators import MULTIPLE_HISTORY
from trading_bot.tools import trade

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 17, tzinfo=timezone.utc)
MODULES = {"moving-average": "moving_average", "sma": "sma", "hma": "hma",
           "ema-hma": "ema_hma", "multiple-hma": "multiple_hma"}


def run_trade(argv):
    with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
        try:
            code = trade.main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def flip_series():
    """Ask opens: flat, rising (fast SMA above slow), falling, rising, falling."""
    levels = [110000] * 21
    for direction in (1, -1, 1, -1):
        for _ in range(25):
            levels.append(levels[-1] + 10 * direction)
    return consecutive(levels, "MINUTE")


class Account(FakeBroker):
    """DEMO account in one position mode. Netting: an opposite same-size market order
    closes the open position. Hedging: it opens a second, offsetting position."""

    def __init__(self, frames, clock, mode):
        super().__init__(frames, clock)
        self.mode, self.exposure = mode, []

    def createPosition(self, symbol, direction, size):
        opposite = [p for p in self.positions if p.direction != direction]
        if self.mode == "netting" and opposite:
            self.positions.remove(opposite[0])
        else:
            self.positions.append(Mock(epic=symbol, dealId=f"d{len(self.orders)}", direction=direction))
        self.orders.append((self.calls, self.clock["k"], direction))
        net = sum(100 if p.direction == "BUY" else -100 for p in self.positions)
        self.exposure.append((net, len(self.positions)))
        return {"dealReference": "ref"}, 200


class ReversalExecutionTests(unittest.TestCase):
    """Same signals, different positions: online orders versus simulated reversals."""

    def drive(self, broker, candles, clock):
        module = importlib.import_module("trading_bot.strategies.sma")

        def sleep(_):
            clock["k"] += 1
            if clock["k"] >= len(candles):
                raise StopLoop

        with ExitStack() as stack:
            stack.enter_context(patch("sys.argv", ["sma"]))
            stack.enter_context(patch.object(module, "create_trading_api", return_value=broker))
            stack.enter_context(patch.object(module.time, "sleep", side_effect=sleep))
            stack.enter_context(redirect_stdout(io.StringIO()))
            with self.assertRaises(StopLoop):
                module.main()

    def test_demo_account_modes_differ_from_the_simulated_reversals(self):
        candles = flip_series()
        results = {}
        for mode in ("netting", "hedging"):
            clock = {"k": 20}
            broker = Account({"MINUTE": lambda k: candles[:k + 1]}, clock, mode)
            self.drive(broker, candles, clock)
            results[mode] = broker
        netting, hedging = results["netting"], results["hedging"]
        sides = [side for _, _, side in netting.orders]
        # Identical online signals in both modes: alternating BUY/SELL orders.
        self.assertEqual(sides, [side for _, _, side in hedging.orders])
        self.assertEqual(sides, ["BUY", "SELL", "BUY", "SELL"])
        # The backtest strategy reaches the same sides, as reversals.
        targets = [t for _, t in sequential_targets(SmaCrossoverStrategy(), candles)]
        changes = [t for a, t in zip([None] + targets, targets) if t != a]
        self.assertEqual(changes, sides)
        # Positions differ: simulation reverses long/short; the account does not.
        simulated = [100 if side == "BUY" else -100 for side in changes]
        self.assertEqual(simulated, [100, -100, 100, -100])
        self.assertEqual([net for net, _ in netting.exposure], [100, 0, 100, 0])
        self.assertEqual(hedging.exposure, [(100, 1), (0, 2), (100, 3), (0, 4)])
        # Executed legs: one order per signal online, close + open per reversal simulated.
        self.assertEqual(len(netting.orders), 4)
        self.assertEqual(1 + 2 * (len(changes) - 1), 7)

    def test_live_gate_stops_the_process_at_the_first_opposite_order(self):
        case = LiveGateCase("setUp")
        case.setUp()
        try:
            gate, api = case.gate, case.api
            gate.arm()
            candles = flip_series()
            clock = {"k": 20}
            opens = [float(c.prices.open) for c in candles]
            api.getHistoricalPrices = Mock(side_effect=lambda symbol, tf, count: (
                {"prices": [{"openPrice": {"ask": v}} for v in opens[:clock["k"] + 1][-count:]]}, 200))
            api.prepare_trading_cycle = Mock(return_value=False)
            module = importlib.import_module("trading_bot.strategies.sma")

            def sleep(_):
                clock["k"] += 1
                if clock["k"] >= len(candles):
                    raise AssertionError("LIVE run should have stopped")

            with patch("sys.argv", ["sma", "--REAL"]), \
                    patch.object(module, "create_trading_api", return_value=api), \
                    patch.object(module.time, "sleep", side_effect=sleep), \
                    redirect_stdout(io.StringIO()), \
                    self.assertRaises(live_execution.LiveSafetyStop) as stopped:
                module.main()
            self.assertIn("live_open_order_limit_reached", str(stopped.exception))
            # One opening POST was sent; the opposite order was never sent and the
            # owned BUY position stays open with its broker stop.
            self.assertEqual([call.args[0] for call in case.send.call_args_list], ["post"])
            self.assertEqual([p["position"]["direction"] for p in case.positions], ["BUY"])
        finally:
            case.doCleanups()


class LiveGateCase(unittest.TestCase):
    """The armed LIVE gate fixture of test_strategy_environments, without its tests."""

    setUp = environments.LiveExecutionTests.setUp
    write_profile = environments.LiveExecutionTests.write_profile
    fake_mutation = environments.LiveExecutionTests.fake_mutation
    fake_confirmation = environments.LiveExecutionTests.fake_confirmation


class FormingCandleTests(unittest.TestCase):
    """Online ema-hma/multiple-hma windows end with the forming candle; backtests don't."""

    def test_ema_hma_online_decisions_follow_the_forming_candle_window(self):
        candles = consecutive(walk(260, 31), "MINUTE_5")
        strategy = EmaHmaStrategy(long_period=40, short_period=8, hma_period=6)

        def forming(k):
            # At boundary k + 1 (+2 s) the broker also returns the just-started candle,
            # whose close is still its opening price.
            nxt = candles[k + 1] if k + 1 < len(candles) else candles[k]
            start = candles[k].timestamp + timedelta(minutes=5)
            return candles[:k + 1] + (candle(start, round(nxt.prices.open / PIP),
                                             round(nxt.prices.open / PIP), "MINUTE_5"),)

        first = strategy.warmup - 1
        clock = {"k": first - 1}
        broker = FakeBroker({"MINUTE_5": forming}, clock)
        before, after = [], []

        def wait(*args, **kwargs):
            if clock["k"] >= first:
                after.append(broker.side())
            if clock["k"] >= len(candles) - 2:
                raise StopLoop
            clock["k"] += 1
            before.append(broker.side())

        module = importlib.import_module("trading_bot.strategies.ema_hma")
        with patch("sys.argv", ["ema_hma", "--long_period", "40", "--short_period", "8",
                                "--hma_period", "6", "--time_frame", "MINUTE_5"]), \
                patch.object(module, "create_trading_api", return_value=broker), \
                patch.object(module, "wait_until_targets", side_effect=wait), \
                redirect_stdout(io.StringIO()), self.assertRaises(StopLoop):
            module.main()
        differs = 0
        for i, (prior, posterior) in enumerate(zip(before, after)):
            k = first + i
            online_window = forming(k)[-strategy.warmup:]
            completed_window = candles[k + 1 - strategy.warmup:k + 1]
            self.assertEqual(strategy.target(online_window, prior), posterior, k)
            differs += strategy.target(completed_window, prior) != posterior
        self.assertGreater(len(after), 150)
        self.assertGreater(differs, 0)  # The forming candle changes real decisions.

    def test_multiple_hma_online_vote_includes_the_long_term_candle_in_progress(self):
        strategy = MultipleHmaStrategy(hma_period=6, long_term="HOUR", time_frame="MINUTE_30")
        history = thirty(TUESDAY - timedelta(days=21), strategy.warmup + 3)
        # The newest 30m candle opens an hour: that hour is in progress at this boundary.
        end = next(i for i in range(len(history) - 1, 0, -1) if history[i - 1].timestamp.minute == 0)
        window = history[end - strategy.warmup:end]
        completed = strategy.long_term_closes(window)
        self.assertEqual(len(completed), MULTIPLE_HISTORY)
        newest = window[-1]
        self.assertNotIn(float(newest.prices.close), completed[-1:])
        # A broker without the completed-candle filter returns the hour in progress last.
        online = completed[1:] + [float(newest.prices.close)]
        self.assertNotEqual(online, completed)
        # At the following boundary that hour has closed and the backtest includes it.
        later = history[end - strategy.warmup + 1:end + 1]
        self.assertEqual(strategy.long_term_closes(later)[-1], float(history[end].prices.close))


class ReportWarningTests(unittest.TestCase):
    START = datetime(2026, 9, 15, tzinfo=timezone.utc)
    END = datetime(2026, 9, 15, 6, tzinfo=timezone.utc)

    def test_declared_differences(self):
        a = adapters.ADAPTERS
        self.assertEqual(a["sma"].online_differences,
                         (adapters.REVERSAL, adapters.POLLED_OPENS, adapters.LIVE_LIMITS))
        self.assertEqual(a["moving-average"].online_differences, a["sma"].online_differences)
        self.assertEqual(a["ema-hma"].online_differences,
                         (adapters.FORMING_CANDLES, adapters.LIVE_LIMITS))
        self.assertEqual(a["multiple-hma"].online_differences,
                         (adapters.FORMING_CANDLES, adapters.FORMING_LONG_TERM, adapters.LIVE_LIMITS))
        self.assertEqual(a["hma"].online_differences, (adapters.LIVE_LIMITS,))
        self.assertEqual((a["donchian"].online_differences, a["grid"].online_differences), ((), ()))
        for key, adapter in a.items():  # Only order-capable strategies have online runs.
            self.assertEqual(bool(adapter.online_differences), registry.get(key).tradable, key)

    def run_cli(self, argv, root, series, label):
        output = Path(root, label)
        with ExitStack() as stack:
            stack.enter_context(patch("trading_bot.backtesting.data.provider",
                                      return_value=FakeProvider(series)))
            out = stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            code = cli.main([*argv, "--cache-dir", str(Path(root, "cache")), "--output-dir", str(output),
                             "--start", self.START.isoformat(), "--end", self.END.isoformat()], now=NOW)
        self.assertEqual(code, 0)
        folder = next(output.iterdir())
        report = json.loads((folder / "report.json").read_text())
        report["data"].pop("cache_hit")
        return out.getvalue(), report, (folder / "equity.html").read_text()

    def test_terminal_json_and_html_warnings_change_no_result(self):
        series = session_series(self.START - timedelta(days=10), self.END, "MINUTE")
        with tempfile.TemporaryDirectory() as root:
            text, report, page = self.run_cli(["--strategy", "sma"], root, series, "warned")
            silent = dataclasses.replace(adapters.ADAPTERS["sma"], online_differences=())
            with patch.dict(adapters.ADAPTERS, {"sma": silent}), \
                    patch.dict(cli.ADAPTERS, {"sma": silent}):
                plain_text, plain, plain_page = self.run_cli(["--strategy", "sma"], root, series, "plain")
        lines = text.splitlines()
        self.assertEqual(lines[0], "========== BACKTEST REPORT ==========")
        self.assertTrue(lines[1].startswith("!!! ONLINE AND BACKTEST EXECUTION DIFFER"))
        self.assertLess(text.index("stop and reverse"), text.index("Strategy:"))
        self.assertIn("live_open_order_limit_reached", text)
        self.assertEqual(report["online_differences"], list(adapters.ADAPTERS["sma"].online_differences))
        self.assertIn("Online and backtest execution differ", page)
        self.assertIn("stop and reverse", page)
        self.assertNotIn("online_differences", plain)
        self.assertNotIn("Online and backtest execution differ", plain_page)
        # Explanatory metadata only: trades, numbers, assumptions and hashes are identical.
        self.assertEqual({k: v for k, v in report.items() if k != "online_differences"}, plain)
        self.assertEqual([l for l in lines if not l.startswith(("!!!", "  -", "    ", "-----", "Results:"))],
                         [l for l in plain_text.splitlines() if not l.startswith("Results:")])

    def test_simulation_only_strategies_have_no_warning(self):
        from test_grid_backtesting import GRID_ARGS, oscillating_snapshot

        with tempfile.TemporaryDirectory() as root:
            snapshot = Path(root, "grid.json.gz")
            oscillating_snapshot(snapshot)
            with redirect_stdout(io.StringIO()) as out:
                code = cli.main([*GRID_ARGS, "--data-source", "snapshot", "--snapshot", str(snapshot),
                                 "--start", "2023-11-01", "--end", "2023-11-02",
                                 "--output-dir", str(Path(root, "out"))], now=NOW)
            report = json.loads(next(Path(root, "out").glob("*/report.json")).read_text())
        self.assertEqual(code, 0)
        self.assertNotIn("online_differences", report)
        self.assertNotIn("!!!", out.getvalue())


SAMPLE = {"--hma-period": "16", "--hma_period": "16", "--direction": "BUY", "--time-frame": "MINUTE_5",
          "--time_frame": "MINUTE_5", "--long_period": "200", "--short_period": "20",
          "--long_term": "HOUR_4", "--symbol": "EURUSD", "--size": "100"}


class OptionValidationTests(unittest.TestCase):
    def test_option_names_are_the_parser_options(self):
        parser = cli.build_parser()
        self.assertEqual(option_names(), {o for a in parser._actions for o in a.option_strings})
        for source in (ROOT / "src/trading_bot").rglob("*.py"):  # No private argparse use.
            self.assertNotIn("._actions", source.read_text(), source)

    def online(self, key, argv):
        """Exit status of the unchanged online parser, or 'startup' if it got there."""
        module = importlib.import_module(f"trading_bot.strategies.{MODULES[key]}")
        with patch("sys.argv", [key, *argv]), \
                patch.object(module, "create_trading_api", side_effect=StartupReached), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                module.main()
            except StartupReached:
                return "startup"
            except SystemExit as exc:
                return exc.code
        return None

    def test_declared_trading_options_match_each_online_parser(self):
        common = {"--symbol", "--size"}
        for key in (s.key for s in registry.with_mode(registry.DEMO)):
            strategy = registry.get(key)
            accepted = common | strategy.trading_options
            self.assertLessEqual(strategy.trading_options, option_names(), key)
            # tb-trade consumes these itself; they never reach a strategy parser.
            for option in sorted(option_names() - {"-h", "--help", "--strategy", "--list-strategies"}):
                with self.subTest(strategy=key, option=option):
                    if option in accepted:
                        self.assertEqual(self.online(key, [option, SAMPLE[option]]), "startup")
                    elif key == "hma" and option in {"--backtest", *SIMULATION_OPTIONS}:
                        continue  # hma.py --backtest options: parsed, refused by tb-trade.
                    else:
                        self.assertEqual(self.online(key, [option, "1"]), 2)
                        self.assertIn(option, trade.backtest_only_options(strategy))

    def test_tb_trade_refuses_options_of_other_strategies(self):
        cases = {"sma": ["--hma-period", "5"], "moving-average": ["--long_term", "HOUR"],
                 "ema-hma": ["--long_term", "HOUR"], "multiple-hma": ["--long_period", "100"],
                 "hma": ["--average-period", "5"]}
        for key, extra in cases.items():
            with self.subTest(strategy=key), \
                    patch.object(registry.Strategy, "load_trading_entry",
                                 side_effect=AssertionError("must not dispatch")):
                code, _, err = run_trade(["--strategy", key, *extra])
                self.assertEqual(code, 2)
                self.assertIn(f"not accepted by tb-trade --strategy {key}", err)
        code, _, err = run_trade(["--strategy", "ema-hma", "--long-period", "100"])
        self.assertIn("--long-period -> --long_period", err)

    def test_tb_trade_forwards_each_strategys_own_options(self):
        for key, extra in {"hma": ["--hma-period", "20", "--time-frame", "MINUTE_5"],
                           "ema-hma": ["--long_period", "100", "--time_frame", "MINUTE_15"],
                           "multiple-hma": ["--long_term", "HOUR", "--hma_period", "10"],
                           "sma": ["--symbol", "EURUSD", "--size", "200"]}.items():
            entry = Mock(return_value=None)
            with self.subTest(strategy=key), \
                    patch.object(registry.Strategy, "load_trading_entry", return_value=entry):
                code, _, err = run_trade(["--strategy", key, *extra])
                self.assertIsNone(code, err)
                entry.assert_called_once_with()

    def test_help_lists_every_strategy_from_the_registry(self):
        code, out, _ = run_trade(["--help"])
        self.assertEqual(code, 0)
        flat = " ".join(out.split())
        backtestable = ", ".join(s.key for s in registry.with_mode(registry.BACKTEST))
        self.assertIn(f"Historical simulations of {backtestable} use", flat)
        self.assertEqual(len(registry.with_mode(registry.BACKTEST)), 7)
        with redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["--help"])
        for key in registry.STRATEGIES:
            self.assertIn(key, out.getvalue())


if __name__ == "__main__":
    unittest.main()

"""Unified backtesting framework: pinned regressions, shared engine/data, CLI, safety."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import gzip
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from backtest_fixtures import ORIGIN, canonical, fingerprint, hourly_candles
from trading_bot.backtesting import cli, donchian_cli, runner
from trading_bot.backtesting.adapters import ADAPTERS, DONCHIAN, HMA
from trading_bot.backtesting.cache import Cache
from trading_bot.backtesting.data import load_history, load_snapshot, warmup_start
from trading_bot.backtesting.engine import (
    CONTINUOUS,
    RESEARCH_WINDOW,
    Ledger,
    SimulationConfig,
    decision_function,
    run,
    simulate,
    statistics,
)
from trading_bot.backtesting.evaluation import (
    StageRegistry,
    canonical_sha256,
    chronological_windows,
    registered_holdouts,
)
from trading_bot.backtesting.models import Candle, Dataset, DataError, OHLC
from trading_bot.backtesting.strategy import HmaStrategy
from trading_bot.backtesting.study_data import END, START
from trading_bot.backtesting.study_engine import COSTS, research_quotes, simulate_window
from trading_bot.backtesting import study_engine
from trading_bot.backtesting.donchian_study import splits
from trading_bot.strategies.donchian import DonchianStrategy
from trading_bot.strategies import hma

from test_donchian_cli import GUARD, fixture as snapshot_fixture

ROOT = Path(__file__).resolve().parents[1]
REGRESSION = json.loads(
    gzip.decompress((Path(__file__).parent / "fixtures/backtest_regression.json.gz").read_bytes())
)
CANDLES = hourly_candles()
START_AT = datetime.fromisoformat(REGRESSION["start"])
END_AT = datetime.fromisoformat(REGRESSION["end"])
NOW = datetime(2026, 11, 17, tzinfo=timezone.utc)


def bar(i, mid, *, spread="0.0002", stamp=None):
    mid, half = D(str(mid)), D(spread) / 2
    sides = [OHLC(*(mid + h for _ in range(4))) for h in (-half, half)]
    return Candle(stamp or ORIGIN + timedelta(hours=i), sides[1], "EURUSD", "HOUR",
                  "dukascopy", *sides)


def numeric(value):
    if isinstance(value, dict):
        return {k: numeric(v) for k, v in value.items()}
    if isinstance(value, list):
        return [numeric(v) for v in value]
    if isinstance(value, str):
        try:
            return D(value)
        except ArithmeticError:
            return value
    return value


class PinnedRegressionTests(unittest.TestCase):
    """Results pinned from the pre-refactor engines must not change at all."""

    def test_fixture_candles_are_unchanged(self):
        self.assertEqual(fingerprint(CANDLES), REGRESSION["candles_sha256"])
        self.assertEqual(len(CANDLES), REGRESSION["count"])

    def test_hma_continuous_results_are_unchanged(self):
        cases = {
            "hma_16_buy_unmodeled": (HmaStrategy(16, "BUY"), SimulationConfig()),
            "hma_16_sell_all_costs": (
                HmaStrategy(16, "SELL"),
                SimulationConfig(1000, 100, 2, "0.05", "0.2", "0.0001"),
            ),
            "hma_8_buy_small_balance": (HmaStrategy(8, "BUY"), SimulationConfig("100", 100, 1)),
        }
        for name, (strategy, config) in cases.items():
            with self.subTest(name):
                r = run(CANDLES, strategy, config, start=START_AT, end=END_AT)
                got = canonical({
                    "statistics": statistics(r),
                    "trades": [asdict(t) for t in r.ledger.trades],
                    "equity_curve": r.equity_curve,
                    "decisions": r.decisions,
                    "rejections": r.ledger.rejections,
                    "candles": r.candles,
                    "open_position": asdict(r.ledger.position) if r.ledger.position else None,
                })
                self.assertEqual(got, REGRESSION["continuous"][name])
        self.assertGreater(len(REGRESSION["continuous"]["hma_8_buy_small_balance"]["rejections"]), 0)

    def test_donchian_and_hma_research_results_are_unchanged(self):
        cases = {
            "donchian_20_10_base": (DonchianStrategy(), COSTS[0]),
            "donchian_20_10_high": (DonchianStrategy(), COSTS[1]),
            "donchian_20_10_financing": (DonchianStrategy(), COSTS[2]),
            "donchian_5_3_base": (DonchianStrategy(5, 3), COSTS[0]),
            "hma_16_buy_base": (HmaStrategy(), COSTS[0]),
            "hma_16_buy_financing": (HmaStrategy(), COSTS[2]),
        }
        for name, (strategy, case) in cases.items():
            with self.subTest(name):
                stats, trades, curve = study_engine.simulate(CANDLES, strategy, case, START_AT, END_AT)
                got = canonical({"statistics": stats, "trades": trades, "equity_curve": curve})
                self.assertEqual(got, REGRESSION["research"][name])


class SharedEngineTests(unittest.TestCase):
    def test_both_modes_are_the_same_loop_with_different_policies(self):
        direct = simulate(CANDLES, HmaStrategy(), Ledger(SimulationConfig()), start=START_AT,
                          end=END_AT, policy=CONTINUOUS)
        wrapped = run(CANDLES, HmaStrategy(), SimulationConfig(), start=START_AT, end=END_AT)
        self.assertEqual(statistics(direct), statistics(wrapped))
        self.assertEqual(direct.ledger.trades, wrapped.ledger.trades)
        result, stats = simulate_window(CANDLES, DonchianStrategy(), COSTS[0], START_AT, END_AT)
        self.assertIs(result.policy, RESEARCH_WINDOW)
        self.assertEqual(stats["remaining_positions"], 0)

    def test_declared_policy_differences_on_identical_input(self):
        class AlwaysBuy:
            name, warmup = "AlwaysBuy", 2

            def target(self, candles):
                return "BUY"

        rows = (bar(0, 1.1), bar(1, 1.1), bar(2, 1.11), bar(3, 1.12), bar(4, 1.13))
        start = rows[2].timestamp
        continuous = simulate(rows, AlwaysBuy(), Ledger(SimulationConfig()), start=start,
                              end=rows[-1].end, policy=CONTINUOUS)
        research = simulate(rows, AlwaysBuy(), Ledger(SimulationConfig()), start=start,
                            end=rows[-1].end, policy=RESEARCH_WINDOW)
        # Continuous primes the first scored open from warm-up and keeps the position.
        self.assertEqual(continuous.ledger.position.entry_timestamp, rows[2].timestamp)
        self.assertEqual(len(continuous.ledger.trades), 0)
        # Research decides first at the first scored close and ends flat.
        self.assertEqual(research.ledger.trades[0].entry_timestamp, rows[3].timestamp)
        self.assertIsNone(research.ledger.position)
        self.assertTrue(research.trade_records[0]["boundary_exit"])

    def test_position_awareness_is_explicit_not_name_based(self):
        class NamedDonchianOneArg:
            name, warmup = "Donchian", 1

            def target(self, candles):
                return "BUY"

        seen = []

        class OtherTwoArg:
            name, warmup = "Other", 1

            def target(self, candles, position):
                seen.append(position)
                return "BUY"

        self.assertEqual(decision_function(NamedDonchianOneArg())((), "SELL"), "BUY")
        decision_function(OtherTwoArg())((), "BUY")
        self.assertEqual(seen, ["BUY"])
        # Donchian receives the held side: a long exits (None) where flat would enter short.
        window = [bar(0, 1.1), bar(1, 1.0), bar(2, 0.9)]
        decide = decision_function(DonchianStrategy(2, 1))
        self.assertEqual((decide(window, None), decide(window, "BUY")), ("SELL", None))

    def test_strategies_only_see_completed_past_candles(self):
        for policy in (CONTINUOUS, RESEARCH_WINDOW):
            calls = []

            class Spy:
                name, warmup = "Spy", 5

                def target(self, candles, position=None):
                    calls.append(tuple(candles))
                    return "BUY" if len(calls) % 7 < 4 else None

            result = simulate(CANDLES, Spy(), Ledger(SimulationConfig()), start=START_AT,
                              end=END_AT, policy=policy)
            decisions = {d["timestamp"] for d in result.decisions}
            for window in calls:
                self.assertTrue(all(a.end <= b.timestamp for a, b in zip(window, window[1:])))
            for window in calls[-len(result.decisions):]:
                self.assertIn(window[-1].end, decisions)
            for trade in result.trade_records:
                if trade["entry_signal_time"] is not None:
                    self.assertGreaterEqual(trade["entry_timestamp"], trade["entry_signal_time"])
                if trade["exit_signal_time"] is not None:
                    self.assertGreaterEqual(trade["exit_timestamp"], trade["exit_signal_time"])

    def test_future_candles_do_not_change_earlier_research_or_continuous_fills(self):
        cut = CANDLES[300].timestamp
        altered = CANDLES[:300] + tuple(bar(0, 2, stamp=c.timestamp) for c in CANDLES[300:])
        for strategy in (DonchianStrategy(), HmaStrategy()):
            a = run(CANDLES, strategy, SimulationConfig(), start=START_AT, end=END_AT)
            b = run(altered, strategy, SimulationConfig(), start=START_AT, end=END_AT)
            self.assertEqual([t for t in a.ledger.trades if t.exit_timestamp < cut],
                             [t for t in b.ledger.trades if t.exit_timestamp < cut])
            first = simulate_window(CANDLES, strategy, COSTS[0], START_AT, cut)[1]
            second = simulate_window(altered, strategy, COSTS[0], START_AT, cut)[1]
            self.assertEqual(first, second)

    def test_bid_ask_fills_and_cost_decomposition_are_independent_arithmetic(self):
        class Script:
            name, warmup = "Script", 1
            plan = {0: "BUY", 1: "BUY", 2: None, 3: "SELL", 4: "BUY", 5: None}

            def target(self, candles):
                return self.plan.get(int((candles[-1].timestamp - ORIGIN).total_seconds() // 3600))

        rows = tuple(bar(i, D("1.1") + D(i) / 1000, spread=f"0.000{i + 1}") for i in range(7))
        config = SimulationConfig(1000, 100, 1, "0.5", "0.2", None)
        result = simulate(rows, Script(), Ledger(config), start=rows[0].timestamp,
                          end=rows[-1].end, policy=CONTINUOUS)
        long, short, reversal = result.trade_records
        slip = D("0.2") * D("0.0001")
        # Long: decision at bar 0 close, buy bar 1 open at ask+slip, sell bar 3 open at bid-slip.
        self.assertEqual(long["entry_price"], D("1.101") + D("0.0001") + slip)
        self.assertEqual(long["exit_price"], D("1.103") - D("0.0002") - slip)
        self.assertEqual(long["spread_cost"], (D("0.0001") + D("0.0002")) * 100)
        self.assertEqual(long["slippage_cost"], 2 * slip * 100)
        self.assertEqual(long["commission_cost"], 1)
        self.assertEqual(long["midpoint_gross_pnl"], D("0.002") * 100)
        self.assertEqual(long["net_pnl"], long["midpoint_gross_pnl"] - long["spread_cost"]
                         - long["slippage_cost"] - long["commission_cost"])
        # Reversal SELL -> BUY at bar 5 open: each trade carries only its own legs.
        self.assertEqual(short["direction"], "SELL")
        self.assertEqual(short["spread_cost"], (D("0.00025") + D("0.0003")) * 100)
        self.assertEqual(reversal["direction"], "BUY")
        self.assertEqual(reversal["entry_signal_time"], rows[4].end)
        self.assertEqual(reversal["spread_cost"], (D("0.0003") + D("0.00035")) * 100)
        self.assertEqual(sum(t["spread_cost"] for t in result.trade_records),
                         result.ledger.embedded_spread)


class SharedDataTests(unittest.TestCase):
    start = datetime(2026, 9, 14, tzinfo=timezone.utc)
    end = datetime(2026, 9, 26, tzinfo=timezone.utc)

    def provider(self):
        fetch_start = warmup_start(self.start, "HOUR", 48)
        rows = tuple(c for c in hourly_candles(missing=())
                     if fetch_start <= c.timestamp and c.end <= self.end)
        source = Mock()
        source.fetch.return_value = Dataset(
            rows, fetch_start, self.end, "dukascopy", "EURUSD", "HOUR",
            {"source_resolution": "HOUR", "complete_provider_buckets": True,
             "no_quote_ranges": []})
        return source

    def test_strategies_reuse_one_cached_download_and_identical_candles(self):
        source = self.provider()
        with tempfile.TemporaryDirectory() as root, \
                patch("trading_bot.backtesting.data.provider", return_value=source):
            def load(strategy):
                return load_history("dukascopy", "EURUSD", "HOUR", self.start, self.end,
                                    strategy.warmup, Cache(root), now=NOW)

            sets = [load(HmaStrategy()), load(DonchianStrategy())]
            source.fetch.assert_called_once()  # Warm-ups <= 48 share one download.
            sets.append(load(HmaStrategy(20)))  # 60 candles need earlier history.
            self.assertEqual(source.fetch.call_count, 2)
        performance = [[c for c in s.candles if c.timestamp >= self.start] for s in sets]
        self.assertEqual(performance[0], performance[1])
        self.assertEqual(performance[0], performance[2])
        self.assertEqual([s.metadata["warmup_candles"] for s in sets], [48, 21, 60])

    def test_snapshot_loader_matches_study_frames_and_validates(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "snapshot.json.gz"
            snapshot_fixture(path)
            start, end = START, START + timedelta(days=1)
            dataset = load_snapshot(path, "EURUSD", "HOUR", start, end, 21, required_warmup=20)
            from trading_bot.backtesting.study_data import read_snapshot
            frames, _ = study_engine.prepare_frames(read_snapshot(path)[0], start, end)
            hourly = frames["HOUR"]
            self.assertEqual(dataset.candles, tuple(c for c in hourly if c.end <= start)[-21:]
                             + tuple(c for c in hourly if c.timestamp >= start))
            self.assertEqual(dataset.metadata["data_sha256"], fingerprint(dataset.candles))
            self.assertFalse(dataset.metadata["forward_filled"])
            with self.assertRaisesRegex(DataError, "supported timeframes"):
                load_snapshot(path, "EURUSD", "MINUTE_5", start, end, 21)
            with self.assertRaisesRegex(DataError, "need 30 completed bars"):
                load_snapshot(path, "EURUSD", "HOUR", start, end, 30)
            with self.assertRaisesRegex(DataError, "align"):
                load_snapshot(path, "EURUSD", "HOUR_4", start + timedelta(hours=1), end, 1)


class CliTests(unittest.TestCase):
    def plan(self, *argv):
        args = cli.build_parser().parse_args(list(argv))
        return runner.plan(cli.request_from_args(args), now=NOW)

    def test_strategy_defaults_preserve_existing_commands(self):
        p = self.plan("--strategy", "hma")
        self.assertEqual((p.time_frame, p.data_source, p.execution.name), ("MINUTE_30", "dukascopy", "continuous"))
        self.assertEqual((p.strategy.period, p.strategy.direction), (16, "BUY"))
        self.assertEqual((p.start, p.end), (NOW - timedelta(days=30), NOW))
        self.assertEqual(p.execution.config, SimulationConfig())
        p = self.plan("--strategy", "donchian")
        self.assertEqual((p.time_frame, p.data_source, p.execution.name), ("HOUR", "snapshot", "research"))
        self.assertEqual((p.strategy.entry, p.strategy.exit), (20, 10))
        self.assertEqual((p.start, p.end, p.execution.case), (START, END, COSTS[0]))
        self.assertEqual(p.execution.config.size, 100)
        self.assertEqual(p.execution.config.initial_balance, 1000)
        self.assertEqual(self.plan().adapter, HMA)  # trading-bot-backtest stays HMA.

    def test_requested_examples_and_overrides(self):
        for key in ("hma", "donchian"):
            p = self.plan("--strategy", key, "--symbol", "EURUSD", "--time-frame", "HOUR",
                          "--size", "100")
            self.assertEqual((p.adapter.key, p.time_frame), (key, "HOUR"))
        p = self.plan("--strategy", "donchian", "--data-source", "dukascopy",
                      "--execution-model", "continuous", "--slippage-pips", "0.1",
                      "--start", "2025-01-01", "--end", "2025-02-01")
        self.assertEqual((p.execution.name, p.execution.config.slippage_pips), ("continuous", D("0.1")))
        p = self.plan("--strategy", "hma", "--execution-model", "research", "--cost-case",
                      "high", "--data-source", "snapshot", "--initial-balance", "5000")
        self.assertEqual((p.execution.case, p.execution.config.initial_balance), (COSTS[1], 5000))
        p = self.plan("--strategy", "donchian", "--data-source", "yfinance", "--lookback-days", "10")
        self.assertEqual(p.end - p.start, timedelta(days=10))

    def test_invalid_combinations_fail_before_any_data_access(self):
        bad = (
            ["--strategy", "donchian", "--hma-period", "20"],
            ["--strategy", "hma", "--entry-lookback", "25"],
            ["--strategy", "hma", "--cost-case", "high"],
            ["--strategy", "donchian", "--slippage-pips", "1"],
            ["--strategy", "donchian", "--commission-per-order", "1"],
            ["--strategy", "hma", "--lookback-days", "0"],
            ["--strategy", "unknown"],
            ["--REAL"],
            ["--account", "x"],
            ["--strategy", "hma", "--read-only"],
            ["--strategy", "donchian", "--entry-lookback", "25"],  # frozen holdout
        )
        for argv in bad:
            with self.subTest(argv=argv), ExitStack() as stack:
                fetch = stack.enter_context(patch("trading_bot.backtesting.runner.load_history"))
                snap = stack.enter_context(patch("trading_bot.backtesting.runner.load_snapshot"))
                stack.enter_context(redirect_stderr(io.StringIO()))
                with self.assertRaises(SystemExit) as raised:
                    cli.main(argv, now=NOW)
                self.assertEqual(raised.exception.code, 2)
                fetch.assert_not_called()
                snap.assert_not_called()
        for argv in (["--strategy", "hma", "--hma-period", "1"],
                     ["--strategy", "hma", "--initial-balance", "0"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()) as err, \
                    patch("trading_bot.backtesting.runner.load_history") as fetch:
                self.assertEqual(cli.main(argv, now=NOW), 1)
                self.assertIn("Backtest failed", err.getvalue())
                fetch.assert_not_called()

    def test_unified_donchian_matches_legacy_command_and_saves_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            snapshot = Path(td) / "s.json.gz"
            snapshot_fixture(snapshot)
            dates = ["--start", "2023-11-01", "--end", "2023-11-02", "--snapshot", str(snapshot)]
            with redirect_stdout(io.StringIO()) as legacy:
                self.assertEqual(donchian_cli.main(["--backtest", *dates]), 0)
            with redirect_stdout(io.StringIO()) as unified:
                self.assertEqual(cli.main(["--strategy", "donchian", *dates, "--output-dir",
                                           str(Path(td) / "out")], now=NOW), 0)
            self.assertEqual([line for line in unified.getvalue().splitlines()
                              if not line.startswith("Results:")],
                             legacy.getvalue().splitlines())
            folder = next((Path(td) / "out").iterdir())
            report = json.loads((folder / "report.json").read_text())
            self.assertEqual(D(report["statistics"]["net_pnl"]), D("0.428"))
            self.assertEqual(D(report["summary"]["gross_pnl"]), D("0.45"))
            self.assertEqual(report["unmodeled_costs"], ["historical_financing"])
            self.assertIn("commission 0 USD/order (assumed, not a verified fee)", report["assumed_costs"])
            self.assertEqual(len(report["reproducibility"]["configuration_sha256"]), 64)
            for name in ("trades.csv", "trade_details.csv", "equity.csv", "equity.html"):
                self.assertTrue((folder / name).exists(), name)

    def test_unified_hma_matches_hma_py_backtest(self):
        data = SharedDataTests("test_strategies_reuse_one_cached_download_and_identical_candles")
        source = data.provider()
        dates = ["--start", data.start.isoformat(), "--end", data.end.isoformat()]
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            stack.enter_context(patch("trading_bot.backtesting.data.provider", return_value=source))
            stack.enter_context(patch.object(hma, "create_trading_api", side_effect=AssertionError))
            stack.enter_context(redirect_stdout(io.StringIO()))
            common = ["--cache-dir", root + "/cache", "--time-frame", "HOUR", *dates]
            stack.enter_context(patch("sys.argv", ["hma.py", "--backtest", *common,
                                                   "--output-dir", root + "/legacy"]))
            hma.main()
            self.assertEqual(cli.main(["--strategy", "hma", *common, "--output-dir",
                                       root + "/unified"]), 0)
            legacy, unified = (json.loads(next(Path(root, d).glob("*/report.json")).read_text())
                               for d in ("legacy", "unified"))
        # hma.py parses --size as a float (100.0); values are compared as Decimals.
        legacy["data"].pop("cache_hit"), unified["data"].pop("cache_hit")
        for key in ("statistics", "trades", "equity_curve", "decisions", "open_positions",
                    "unmodeled_costs", "data", "hma_period", "direction"):
            self.assertEqual(numeric(legacy[key]), numeric(unified[key]), key)
        source.fetch.assert_called_once()

    def test_adapters_are_independent(self):
        self.assertEqual(set(ADAPTERS), {"hma", "donchian"})
        a, b = HMA.build(hma_period=20), HMA.build()
        self.assertEqual((a.period, b.period), (20, 16))
        self.assertEqual(DONCHIAN.build(entry_lookback=25).exit, 10)
        hma_first = run(CANDLES, HMA.build(), SimulationConfig(), start=START_AT, end=END_AT)
        study_engine.simulate(CANDLES, DONCHIAN.build(), COSTS[1], START_AT, END_AT)
        hma_again = run(CANDLES, HMA.build(), SimulationConfig(), start=START_AT, end=END_AT)
        self.assertEqual(statistics(hma_first), statistics(hma_again))
        with self.assertRaises(ValueError):
            runner.plan(runner.BacktestRequest("donchian", {"hma_period": 4}), now=NOW)


class ResearchSafeguardTests(unittest.TestCase):
    def test_study_splits_are_the_shared_walk_forward_windows(self):
        self.assertEqual(splits(), chronological_windows(START, END))
        holdout = registered_holdouts()[0]
        self.assertEqual((holdout.start, holdout.end), (splits()[-1]["start"], END))

    def test_configuration_hash_is_deterministic_and_sensitive(self):
        record = {"strategy": "donchian", "parameters": {"entry_lookback": 20}, "size": D(100)}
        self.assertEqual(canonical_sha256(record), canonical_sha256(dict(reversed(record.items()))))
        self.assertNotEqual(canonical_sha256(record),
                            canonical_sha256(dict(record, parameters={"entry_lookback": 21})))

    def test_stage_registry_refuses_repeats_and_changed_frozen_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            registry, out = StageRegistry(td), Path(td)
            (out / "study.json").write_text("{}")
            (out / "development.json").write_text("[]")
            inputs = (("study_sha256", "study.json"), ("development_sha256", "development.json"))
            registry.begin("development")
            with self.assertRaises(ValueError):
                registry.begin("development")
            registry.freeze(inputs, decision="test")
            with self.assertRaises(ValueError):
                registry.freeze(inputs)
            (out / "development.json").write_text("[1]")
            with self.assertRaisesRegex(ValueError, "changed after freeze"):
                registry.begin("holdout", frozen_inputs=inputs)
            (out / "development.json").write_text("[]")
            registry.begin("holdout", frozen_inputs=inputs)
            with self.assertRaises(ValueError):
                registry.begin("holdout", frozen_inputs=inputs)

    def test_research_quotes_never_change_signal_candles(self):
        candle = CANDLES[100]
        executed = research_quotes(COSTS[1])(candle)
        self.assertEqual(executed.prices, candle.prices)
        self.assertGreater(executed.ask.close - executed.bid.close, candle.ask.close - candle.bid.close)


class IsolationTests(unittest.TestCase):
    def test_backtesting_modules_never_import_broker_or_risk_code(self):
        package = ROOT / "src/trading_bot/backtesting"
        offenders = []
        for path in package.rglob("*.py"):
            text = path.read_text()
            if ("trading_bot.api" in text or "trading_bot.risk" in text) and path.name != "study_data.py":
                offenders.append(path.name)
        # study_data's explicit, DEMO-only history acquisition command is the sole exception.
        self.assertEqual(offenders, [])

    def test_fresh_interpreter_unified_cli_is_offline_and_broker_free(self):
        with tempfile.TemporaryDirectory() as td:
            snapshot = Path(td) / ".cache/donchian/eurusd-30m.json.gz"
            snapshot_fixture(snapshot)
            for strategy in ("donchian", "hma"):
                argv = [sys.executable, "-c", GUARD, str(ROOT / "scripts/backtest.py"),
                        "--strategy", strategy, "--data-source", "snapshot",
                        "--execution-model", "research", "--start", "2023-11-01",
                        "--end", "2023-11-02", "--output-dir", str(Path(td) / "out")]
                if strategy == "hma":
                    argv += ["--hma-period", "4"]
                child = subprocess.run(argv, cwd=td, capture_output=True, text=True, timeout=30)
                self.assertEqual(child.returncode, 0, child.stderr)
                self.assertIn("Environment:             DEMO (simulated only)", child.stdout)
                self.assertIn("All orders are simulated", child.stdout)


if __name__ == "__main__":
    unittest.main()

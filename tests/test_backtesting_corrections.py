"""Regression tests for the PR #2 review findings.

1. Gap detection follows the candle timeframe (1m ... 4H), DST and weekends.
2. Rollover financing uses each new position's own entry midpoint, including
   direct reversals; rejected entries never create a financed position.
3. Gross P&L is midpoint P&L, reconciled to net equity for open positions.
4. Holdout protection compares the complete preregistered specification.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import io
from contextlib import redirect_stdout
import unittest

from trading_bot.backtesting import runner
from trading_bot.backtesting.calendar import active_gap, expected_open
from trading_bot.backtesting.engine import (
    CONTINUOUS,
    RESEARCH_WINDOW,
    Ledger,
    SimulationConfig,
    pnl_decomposition,
    simulate,
    statistics,
)
from trading_bot.backtesting.evaluation import HoldoutReuseError, holdout_check, registered_holdouts
from trading_bot.backtesting.models import Candle, Dataset, OHLC
from trading_bot.backtesting.reporting import print_report
from trading_bot.backtesting.study_engine import COSTS, RolloverLedger
from trading_bot.backtesting.timeframes import duration, floor_time

UTC = timezone.utc
TUESDAY = datetime(2026, 10, 13, 8, tzinfo=UTC)  # 4H-aligned, mid-session.
TIMEFRAMES = ("MINUTE", "MINUTE_5", "MINUTE_15", "MINUTE_30", "HOUR", "HOUR_4")


def bar(stamp, mid, tf="HOUR", spread="0.0002"):
    mid, half = D(str(mid)), D(spread) / 2
    sides = [OHLC(*(mid + h for _ in range(4))) for h in (-half, half)]
    return Candle(stamp, sides[1], "EURUSD", tf, "dukascopy", *sides)


def old_active_gap(start, end):
    """The original fixed-30m scan, kept here to prove 30m/1H/4H equivalence."""
    cursor = start
    while cursor + timedelta(minutes=30) <= end:
        if expected_open(cursor, "dukascopy"):
            return True
        cursor += timedelta(minutes=30)
    return False


class GapDetectionTests(unittest.TestCase):
    def test_one_missing_candle_is_detected_for_every_timeframe(self):
        for tf in TIMEFRAMES:
            step = duration(tf)
            with self.subTest(tf=tf):
                # Previous candle ends at +1 step; next starts at +2: one missing slot.
                self.assertTrue(active_gap(TUESDAY + step, TUESDAY + 2 * step, tf))
                self.assertFalse(active_gap(TUESDAY + step, TUESDAY + step, tf))
        # The old fixed 30m scan missed every sub-30m gap.
        self.assertFalse(old_active_gap(TUESDAY + timedelta(minutes=5), TUESDAY + timedelta(minutes=10)))

    def test_weekend_closures_are_not_gaps_but_edge_candles_are_dst_aware(self):
        weekends = {
            "summer EDT": (datetime(2026, 10, 16, 21, tzinfo=UTC), datetime(2026, 10, 18, 21, tzinfo=UTC)),
            "winter EST": (datetime(2026, 11, 13, 22, tzinfo=UTC), datetime(2026, 11, 15, 22, tzinfo=UTC)),
        }
        for label, (close, open_) in weekends.items():
            self.assertTrue(expected_open(close - timedelta(minutes=1), "dukascopy"))
            self.assertFalse(expected_open(close, "dukascopy"))
            self.assertTrue(expected_open(open_, "dukascopy"))
            for tf in TIMEFRAMES:
                step = duration(tf)
                last_end = floor_time(close - timedelta(minutes=1), tf) + step
                first_start = floor_time(open_, tf)
                with self.subTest(label=label, tf=tf):
                    self.assertFalse(active_gap(last_end, first_start, tf))
                    # Missing last Friday candle / first Sunday candle are real gaps,
                    # including 4H slots that straddle the close/open.
                    self.assertTrue(active_gap(last_end - step, first_start, tf))
                    self.assertTrue(active_gap(last_end, first_start + step, tf))

    def test_30m_1h_4h_results_match_the_original_scan(self):
        cursor = datetime(2026, 3, 1, tzinfo=UTC)  # Spans the US DST change.
        for tf in ("MINUTE_30", "HOUR", "HOUR_4"):
            step = duration(tf)
            stamp = cursor
            for _ in range(24 * 21 * 60 // int(step.total_seconds() // 60)):
                for missing in (1, 2, 5):
                    end = stamp + missing * step
                    self.assertEqual(active_gap(stamp, end, tf), old_active_gap(stamp, end),
                                     (tf, stamp, missing))
                stamp += step

    def test_research_policy_applies_short_timeframe_gaps(self):
        class AlwaysBuy:
            name, warmup = "AlwaysBuy", 3

            def target(self, candles):
                return "BUY"

        for tf in ("MINUTE", "MINUTE_5", "MINUTE_15"):
            step = duration(tf)
            rows = [bar(TUESDAY + i * step, "1.1", tf) for i in range(12) if i != 4]
            with self.subTest(tf=tf, case="pending entry"):
                r = simulate(rows, AlwaysBuy(), Ledger(SimulationConfig()), start=rows[3].timestamp,
                             end=rows[-1].end, policy=RESEARCH_WINDOW)
                self.assertEqual(r.cancelled_gap_entries, 1)
                # Warm-up resets: 3 fresh candles after the gap, then next open.
                self.assertEqual(r.trade_records[0]["entry_timestamp"], TUESDAY + 8 * step)
            with self.subTest(tf=tf, case="held position"):
                r = simulate(rows, AlwaysBuy(), Ledger(SimulationConfig()), start=rows[2].timestamp,
                             end=rows[-1].end, policy=RESEARCH_WINDOW)
                self.assertEqual(r.gap_holding_events, 1)
                self.assertEqual(r.cancelled_gap_entries, 0)
                self.assertEqual(len(r.trade_records), 1)  # Held through, closed at boundary.
            with self.subTest(tf=tf, case="continuous ignores gaps"):
                r = simulate(rows, AlwaysBuy(), Ledger(SimulationConfig()), start=rows[3].timestamp,
                             end=rows[-1].end, policy=CONTINUOUS)
                self.assertEqual(r.cancelled_gap_entries, 0)


class ReversalFinancingTests(unittest.TestCase):
    MONDAY = datetime(2026, 10, 12, 12, tzinfo=UTC)

    def ledger(self, **config):
        config = dict(dict(commission_per_order=0, slippage_pips=0), **config)
        return RolloverLedger(SimulationConfig(**config), COSTS[2])

    @staticmethod
    def charge(mid, nights=1):
        return D(mid) * 100 * D("0.05") / 360 * nights

    def test_every_new_position_uses_its_own_entry_midpoint(self):
        ledger = self.ledger()
        t = self.MONDAY
        steps = [("BUY", "1.10"), ("SELL", "1.30"), ("BUY", "1.20")]  # flat->long->short->long
        for day, (side, mid) in enumerate(steps):
            stamp = t + timedelta(days=day)
            ledger.target(side, bar(stamp, mid))
            self.assertEqual(ledger.entry_mid, D(mid))
            # Exactly one weekday NY 17:00 rollover (Mon, Tue; Wed below) per position.
            ledger.accrue_financing(stamp + timedelta(hours=12))
        ledger.target(None, bar(t + timedelta(days=2, hours=13), "1.20"))
        financing = [c["financing_cost"] for c in ledger.trade_costs]
        self.assertEqual(financing, [self.charge("1.10"), self.charge("1.30"),
                                     self.charge("1.20", nights=3)])  # Wednesday triple.
        self.assertEqual(ledger.costs, sum(financing))
        self.assertEqual([t.direction for t in ledger.trades], ["BUY", "SELL", "BUY"])

    def test_flat_to_short_and_hold_do_not_reset_midpoint(self):
        ledger = self.ledger()
        ledger.target("SELL", bar(self.MONDAY, "1.25"))
        ledger.target("SELL", bar(self.MONDAY + timedelta(hours=1), "1.40"))  # Hold.
        self.assertEqual(ledger.entry_mid, D("1.25"))
        ledger.accrue_financing(self.MONDAY + timedelta(hours=12))
        self.assertEqual(ledger.position.financing, self.charge("1.25"))

    def test_rejected_entries_never_refresh_or_finance(self):
        ledger = self.ledger(initial_balance=50)
        ledger.target("BUY", bar(self.MONDAY, "1.10"))  # Margin 110 > 50: rejected.
        self.assertIsNone(ledger.position)
        self.assertFalse(hasattr(ledger, "entry_mid"))
        # Reversal whose new entry is rejected after the close pays commission.
        ledger = self.ledger(initial_balance=120, commission_per_order=5)
        ledger.target("BUY", bar(self.MONDAY, "1.10"))
        ledger.target("SELL", bar(self.MONDAY + timedelta(hours=1), "1.10"))
        self.assertIsNone(ledger.position)
        self.assertEqual(len(ledger.rejections), 1)
        self.assertEqual(ledger.entry_mid, D("1.10"))  # Belongs to the closed long only.
        before = ledger.costs
        ledger.accrue_financing(self.MONDAY + timedelta(days=2))
        self.assertEqual(ledger.costs, before)  # Nothing financed while flat.


class GrossPnlTests(unittest.TestCase):
    def run_hold(self, side, mids, *, spread="0.0004", policy=CONTINUOUS, **config):
        class Hold:
            name, warmup = "Hold", 1

            def target(self, candles):
                return side

        rows = [bar(TUESDAY + timedelta(hours=i), m, spread=spread) for i, m in enumerate(mids)]
        config = dict(dict(initial_balance=1000, size=100, commission_per_order="0.5",
                           slippage_pips="0.2"), **config)
        result = simulate(rows, Hold(), Ledger(SimulationConfig(**config)),
                          start=rows[0].timestamp, end=rows[-1].end, policy=policy)
        return result, pnl_decomposition(result), statistics(result)

    def assert_reconciles(self, pnl, stats):
        self.assertEqual(pnl["net_pnl"], stats["net_pnl"])
        self.assertEqual(pnl["gross_pnl"] - pnl["modeled_costs"] - pnl["open_valuation_spread"],
                         stats["net_pnl"])
        self.assertEqual(pnl["gross_pnl"], pnl["realized_gross_pnl"] + pnl["unrealized_gross_pnl"])

    def test_open_long_and_short_gross_is_midpoint_move(self):
        mids = ["1.1000", "1.1000", "1.1020", "1.1050"]
        for side, sign in (("BUY", 1), ("SELL", -1)):
            with self.subTest(side=side):
                result, pnl, stats = self.run_hold(side, mids)
                self.assertIsNotNone(result.ledger.position)
                # Entry at bar 1 open (mid 1.1000), valued at last close (mid 1.1050).
                self.assertEqual(pnl["gross_pnl"], sign * D("0.0050") * 100)
                self.assertEqual(pnl["realized_gross_pnl"], 0)
                self.assertEqual(pnl["open_valuation_spread"], D("0.0002") * 100)
                self.assertEqual(pnl["open_entry_spread_and_slippage"],
                                 (D("0.0002") + D("0.00002")) * 100)
                self.assertEqual(pnl["modeled_costs"], D("0.5") + (D("0.0002") + D("0.00002")) * 100)
                self.assertEqual(stats["unrealized_pnl"], pnl["unrealized_gross_pnl"]
                                 - pnl["open_valuation_spread"] - pnl["open_entry_spread_and_slippage"])
                self.assert_reconciles(pnl, stats)
                # The previous formula (net + modeled) understated gross by the
                # unexecuted exit half-spread.
                self.assertEqual(stats["net_pnl"] + pnl["modeled_costs"],
                                 pnl["gross_pnl"] - D("0.02"))

    def test_unchanged_midpoint_has_zero_gross_and_costs_explain_net(self):
        _, pnl, stats = self.run_hold("BUY", ["1.1"] * 4)
        self.assertEqual(pnl["gross_pnl"], 0)
        self.assertEqual(stats["net_pnl"], -(pnl["modeled_costs"] + pnl["open_valuation_spread"]))
        self.assert_reconciles(pnl, stats)

    def test_flat_ending_research_window_gross_equals_closed_midpoint_pnl(self):
        result, pnl, stats = self.run_hold("BUY", ["1.1000", "1.1000", "1.1020", "1.1050"],
                                           policy=RESEARCH_WINDOW)
        self.assertIsNone(result.ledger.position)
        self.assertEqual(pnl["open_valuation_spread"], 0)
        self.assertEqual(pnl["unrealized_gross_pnl"], 0)
        self.assertEqual(pnl["gross_pnl"],
                         sum(t["midpoint_gross_pnl"] for t in result.trade_records))
        self.assertEqual(pnl["gross_pnl"], stats["net_pnl"] + pnl["modeled_costs"])
        self.assert_reconciles(pnl, stats)

    def test_report_shows_realized_and_unrealized_split(self):
        start = TUESDAY + timedelta(hours=6)
        rows = tuple(bar(TUESDAY + timedelta(hours=i), D("1.1") + D(i) / 10000) for i in range(12))
        dataset = Dataset(rows, start, rows[-1].end, "dukascopy", "EURUSD", "HOUR",
                          {"warmup_candles": 6, "data_sha256": "test"})
        plan = runner.plan(runner.BacktestRequest(
            "hma", {"hma_period": 2}, time_frame="HOUR", data_source="dukascopy",
            start=start, end=rows[-1].end), now=datetime(2026, 11, 17, tzinfo=UTC))
        report = runner.execute(plan, dataset)
        summary = report["summary"]
        self.assertEqual(report["statistics"]["remaining_positions"], 1)
        self.assertGreater(summary["open_valuation_spread"], 0)
        self.assertEqual(summary["gross_pnl"],
                         summary["realized_gross_pnl"] + summary["unrealized_gross_pnl"])
        self.assertEqual(summary["net_pnl"], summary["gross_pnl"] - summary["modeled_costs"]
                         - summary["open_valuation_spread"])
        with redirect_stdout(io.StringIO()) as out:
            print_report(report)
        for label in ("Gross P&L:", "Realized gross P&L:", "Unrealized gross P&L:",
                      "Open valuation spread:", "Realized cash P&L:", "Unrealized P&L:"):
            self.assertIn(label, out.getvalue())


class HoldoutSpecificationTests(unittest.TestCase):
    INSIDE = (datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC))
    BEFORE = (datetime(2025, 5, 1, tzinfo=UTC), datetime(2025, 6, 1, tzinfo=UTC))
    SNAPSHOT = "f7ad8f615eb2dacd9a79417934f99f09a18da32f3c291f37ed605c4db1eda72a"

    def spec(self, **changes):
        base = {"strategy": "donchian", "parameters": {"entry_lookback": 20, "exit_lookback": 10},
                "symbol": "EURUSD", "timeframe": "HOUR", "data_source": "snapshot",
                "execution_model": "research", "cost_case": "base", "size": D(100),
                "initial_balance": D(1000), "leverage": D(1)}
        return dict(base, **changes)

    def test_registry_is_the_studys_own_holdout_plan(self):
        (holdout,) = registered_holdouts()
        roles = {(s.strategy, s.timeframe, s.cost_case): s.role for s in holdout.specifications}
        self.assertEqual(len(roles), 18)  # 21 holdout runs minus 3 cash comparators.
        self.assertEqual(roles[("donchian", "HOUR", "base")], "primary")
        self.assertEqual(roles[("donchian", "HOUR", "high")], "primary_cost_sensitivity")
        self.assertEqual(roles[("donchian", "HOUR", "financing")], "primary_cost_sensitivity")
        self.assertEqual(roles[("donchian", "MINUTE_30", "base")], "robustness")
        self.assertEqual(roles[("donchian", "HOUR_4", "high")], "robustness")
        self.assertEqual(roles[("hma", "HOUR", "base")], "comparator")
        self.assertTrue(all(s.parameters in ({"entry_lookback": 20, "exit_lookback": 10},
                                             {"hma_period": 16, "direction": "BUY"})
                            for s in holdout.specifications))

    def test_preregistered_primary_and_robustness_are_recognized(self):
        primary = holdout_check(self.spec(), *self.INSIDE, dataset_sha256=self.SNAPSHOT)
        self.assertEqual(primary[0]["preregistered_role"], "primary")
        self.assertTrue(primary[0]["registered_dataset"])
        self.assertFalse(primary[0]["acknowledged_reuse"])
        robust = holdout_check(self.spec(timeframe="HOUR_4", cost_case="high"), *self.INSIDE)
        self.assertEqual(robust[0]["preregistered_role"], "robustness")

    def test_changed_specifications_are_refused_without_acknowledgement(self):
        changes = {
            "timeframe": dict(timeframe="MINUTE_5"),
            "execution model": dict(execution_model="continuous", cost_case=None),
            "data source": dict(data_source="dukascopy"),
            "parameters": dict(parameters={"entry_lookback": 25, "exit_lookback": 10}),
            "size": dict(size=D(200)),
            "initial balance": dict(initial_balance=D(5000)),
        }
        for label, change in changes.items():
            with self.subTest(label):
                with self.assertRaises(HoldoutReuseError):
                    holdout_check(self.spec(**change), *self.INSIDE)
                acknowledged = holdout_check(self.spec(**change), *self.INSIDE, allow=True)
                self.assertTrue(acknowledged[0]["acknowledged_reuse"])
                self.assertIsNone(acknowledged[0]["preregistered_role"])
                self.assertEqual(holdout_check(self.spec(**change), *self.BEFORE), [])
        with self.assertRaises(HoldoutReuseError):
            holdout_check(self.spec(), *self.INSIDE, dataset_sha256="0" * 64)

    def test_unprotected_strategies_are_disclosed_not_refused(self):
        hma = self.spec(strategy="hma", parameters={"hma_period": 20, "direction": "BUY"},
                        execution_model="continuous", cost_case=None, data_source="dukascopy")
        result = holdout_check(hma, *self.INSIDE)
        self.assertEqual((result[0]["preregistered_role"], result[0]["acknowledged_reuse"]),
                         (None, False))

    def test_runner_rechecks_dataset_identity_after_loading(self):
        now = datetime(2026, 11, 17, tzinfo=UTC)
        start = datetime(2026, 5, 4, tzinfo=UTC)
        rows = tuple(bar(start - timedelta(hours=21) + timedelta(hours=i), "1.1") for i in range(40))
        plan = runner.plan(runner.BacktestRequest("donchian", start=start,
                                                  end=rows[-1].end), now=now)
        self.assertEqual(plan.holdouts[0]["registered_dataset"], None)  # Not read yet.
        for sha, refused in ((self.SNAPSHOT, False), ("0" * 64, True)):
            dataset = Dataset(rows, start, rows[-1].end, "dukascopy", "EURUSD", "HOUR",
                              {"snapshot_candles_sha256": sha, "warmup_candles": 21})
            with self.subTest(sha=sha[:4]):
                if refused:
                    with self.assertRaises(HoldoutReuseError):
                        runner.execute(plan, dataset)
                else:
                    report = runner.execute(plan, dataset)
                    self.assertTrue(report["holdout_overlap"][0]["registered_dataset"])
                    self.assertEqual(report["holdout_overlap"][0]["preregistered_role"], "primary")

    def test_cli_refuses_changed_configurations_before_data_access(self):
        from unittest.mock import patch
        from contextlib import redirect_stderr
        from trading_bot.backtesting import cli

        for argv in (["--time-frame", "MINUTE_30", "--execution-model", "continuous"],
                     ["--data-source", "dukascopy", "--start", "2026-05-01", "--end", "2026-06-01"],
                     ["--time-frame", "MINUTE_15", "--data-source", "dukascopy",
                      "--start", "2026-05-01", "--end", "2026-05-08"],
                     ["--size", "250"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()) as err, \
                    patch("trading_bot.backtesting.runner.load_history") as fetch, \
                    patch("trading_bot.backtesting.runner.load_snapshot") as snap:
                with self.assertRaises(SystemExit) as raised:
                    cli.main(["--strategy", "donchian", *argv])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("preregistered holdout specifications", err.getvalue())
                fetch.assert_not_called()
                snap.assert_not_called()


if __name__ == "__main__":
    unittest.main()

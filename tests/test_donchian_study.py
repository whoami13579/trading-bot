"""Independent synthetic checks for research execution, leakage and accounting."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from trading_bot.strategies.donchian import DonchianStrategy
from trading_bot.backtesting.models import Candle, OHLC, DataError
from trading_bot.backtesting.engine import SimulationConfig
from trading_bot.backtesting.study_engine import (
    COSTS, RolloverLedger, execution_candle, simulate, prepare_frames,
)
from trading_bot.backtesting.study_data import capital_candles, read_snapshot, write_snapshot
from trading_bot.backtesting.donchian_study import splits, configurations, freeze, run_stage

BASE = datetime(2026, 10, 12, tzinfo=timezone.utc)


def bar(hour, close, open_=None, stamp=None, tf="HOUR", spread=".0002"):
    mid = D(str(close))
    open_ = mid if open_ is None else D(str(open_))
    half = D(spread) / 2
    sides = [OHLC(open_ + h, max(open_, mid) + h, min(open_, mid) + h, mid + h)
             for h in (-half, half)]
    return Candle(stamp or BASE + timedelta(hours=hour), sides[1], "EURUSD", tf,
                  "dukascopy", *sides)


def sample():
    closes = [1, 1.01, 1.02, 1.03, 1.05, 1, .98, .96, 1.01, 1.02]
    opens = {4: 1.04, 6: .99, 7: .97, 9: 1.02}
    return tuple(bar(i, c, opens.get(i)) for i, c in enumerate(closes))


class DonchianStudyTests(unittest.TestCase):
    def test_excludes_current_strict_comparisons_and_exit_first(self):
        strategy = DonchianStrategy(2, 1)
        self.assertEqual(strategy.target([bar(0, 1), bar(1, 1.1), bar(2, 1.2)]), "BUY")
        self.assertIsNone(strategy.target([bar(0, 1), bar(1, 1.1), bar(2, 1.1)]))
        self.assertEqual(strategy.target([bar(0, 1.1), bar(1, 1), bar(2, .9)]), "SELL")
        self.assertIsNone(strategy.target([bar(0, 1.1), bar(1, 1), bar(2, .9)], "BUY"))
        self.assertIsNone(strategy.target([bar(0, 1), bar(1, 1.1), bar(2, 1.2)], "SELL"))

    def test_signals_use_midpoint_instead_of_ask(self):
        bars = [bar(0, 1), bar(1, 1.01), bar(2, 1.005, spread=".1")]
        self.assertGreater(bars[-1].ask.close, bars[-2].ask.close)
        self.assertIsNone(DonchianStrategy(2, 1).target(bars))

    def test_long_short_next_opens_and_independent_cost_arithmetic(self):
        bars = sample()
        stats, trades, _ = simulate(bars, DonchianStrategy(2, 1), COSTS[0],
                                    BASE + timedelta(hours=3), BASE + timedelta(hours=10))
        self.assertEqual(len(trades), 2)
        long, short = trades
        self.assertEqual(long["entry_price"], D("1.04011"))
        self.assertEqual(long["exit_price"], D(".98989"))
        self.assertEqual(short["entry_price"], D(".96989"))
        self.assertEqual(short["exit_price"], D("1.02011"))
        self.assertEqual(short["entry_signal_time"], bars[6].end)
        self.assertEqual(short["entry_timestamp"], bars[7].open_time)
        self.assertEqual(long["exit_timestamp"], bars[6].open_time)
        self.assertEqual(stats["midpoint_gross_pnl"], D(-10))
        self.assertEqual(stats["modeled_costs"], D(".044"))
        self.assertEqual(stats["net_pnl"], D("-10.044"))
        self.assertEqual(stats["remaining_positions"], 0)

    def test_clean_flat_boundaries_no_warmup_entry_and_no_future_leak(self):
        bars = sample()
        start, end = BASE + timedelta(hours=3), BASE + timedelta(hours=6)
        first = simulate(bars, DonchianStrategy(2, 1), COSTS[0], start, end)
        altered = bars[:6] + tuple(bar(i, 2) for i in range(6, 10))
        self.assertEqual(first, simulate(altered, DonchianStrategy(2, 1), COSTS[0], start, end))
        self.assertTrue(first[1][0]["boundary_exit"])
        self.assertEqual(first[1][0]["entry_timestamp"], bars[4].timestamp)
        self.assertEqual(first[1][0]["exit_timestamp"], end)
        second = simulate(bars, DonchianStrategy(2, 1), COSTS[0], end, bars[-1].end)
        self.assertTrue(all(t["entry_timestamp"] > end for t in second[1]))
        self.assertEqual(second[0]["remaining_positions"], 0)

    def test_unexpected_gap_cancels_entry_but_preserves_exit(self):
        bars = sample()
        entry_gap = bars[:4] + bars[5:]
        result = simulate(entry_gap, DonchianStrategy(2, 1), COSTS[0], bars[3].timestamp, bars[-1].end)
        self.assertEqual(result[0]["cancelled_gap_entries"], 1)
        self.assertTrue(all(t["entry_timestamp"] > bars[6].timestamp for t in result[1]))
        exit_gap = bars[:6] + bars[7:]
        result = simulate(exit_gap, DonchianStrategy(2, 1), COSTS[0], bars[3].timestamp, bars[-1].end)
        self.assertEqual(result[1][0]["exit_timestamp"], bars[7].open_time)
        self.assertEqual(result[0]["gap_holding_events"], 1)

    def test_weekend_pending_order_fills_next_observed_open(self):
        class AlwaysBuy:
            name, warmup = "AlwaysBuy", 1

            def target(self, candles):
                return "BUY"

        fri = datetime(2026, 10, 16, 20, tzinfo=timezone.utc)
        sun = datetime(2026, 10, 18, 21, tzinfo=timezone.utc)
        bars = (bar(0, 1.1, stamp=fri), bar(1, 1.11, stamp=sun))
        result = simulate(bars, AlwaysBuy(), COSTS[0], fri, sun + timedelta(hours=1))
        self.assertEqual(result[1][0]["entry_timestamp"], sun)
        self.assertEqual(result[0]["cancelled_gap_entries"], 0)

    def test_rollover_wednesday_triple_dst_and_no_double_charge(self):
        # Wednesday NY 17:00 is 21:00 UTC in summer and 22:00 in winter.
        for stamp in (datetime(2026, 10, 14, 21, tzinfo=timezone.utc),
                      datetime(2026, 11, 11, 22, tzinfo=timezone.utc)):
            for side in ("BUY", "SELL"):
                ledger = RolloverLedger(SimulationConfig(commission_per_order=0, slippage_pips=0), COSTS[2])
                c = bar(0, 1.2, stamp=stamp - timedelta(hours=1))
                ledger.target(side, c)
                ledger.accrue_financing(stamp)
                self.assertEqual(ledger.costs, D("1.2") * 100 * D(".05") / 360 * 3)
                ledger.accrue_financing(stamp)
                self.assertEqual(ledger.rollovers, 1)

    def test_high_spread_and_midpoint_fallback(self):
        c = bar(0, 1.2)
        high = execution_candle(c, COSTS[1])
        self.assertEqual(high.ask.open - high.bid.open, D(".0004"))
        mid = Candle(c.timestamp, OHLC(1.2, 1.2, 1.2, 1.2), "EURUSD", "HOUR", "midpoint")
        self.assertEqual(execution_candle(mid, COSTS[0]).ask.open - execution_candle(mid, COSTS[0]).bid.open, D(".0001"))
        self.assertEqual(execution_candle(mid, COSTS[1]).ask.open - execution_candle(mid, COSTS[1]).bid.open, D(".0002"))

    def test_high_cost_independent_both_sides_and_no_financing_before_rollover(self):
        stats, trades, _ = simulate(sample(), DonchianStrategy(2, 1), COSTS[1],
                                    BASE + timedelta(hours=3), BASE + timedelta(hours=10))
        self.assertEqual(trades[0]["entry_price"], D("1.04025"))
        self.assertEqual(trades[0]["exit_price"], D(".98975"))
        self.assertEqual(trades[1]["entry_price"], D(".96975"))
        self.assertEqual(trades[1]["exit_price"], D("1.02025"))
        self.assertEqual(stats["midpoint_gross_pnl"], D(-10))
        self.assertEqual(stats["modeled_costs"], D(".1"))
        self.assertEqual(stats["financing_cost"], 0)
        self.assertEqual(stats["net_pnl"], D("-10.1"))

    def test_snapshot_integrity_duplicates_incomplete_and_conflicting_quotes(self):
        bars = tuple(bar(0, 1.1, stamp=BASE + timedelta(minutes=30*i), tf="MINUTE_30")
                     for i in range(6))
        with tempfile.TemporaryDirectory() as td, \
                patch("trading_bot.backtesting.study_data.FETCH_START", BASE), \
                patch("trading_bot.backtesting.study_data.END", bars[-1].end):
            path = Path(td) / "snapshot.gz"
            write_snapshot(path, bars, {"source": "test"})
            self.assertEqual(read_snapshot(path)[0], bars)
            write_snapshot(path, bars + (bars[-1],), {"source": "test"})
            with self.assertRaises(DataError):
                read_snapshot(path)
            write_snapshot(path, bars + (bar(0, 1.1, stamp=bars[-1].end, tf="MINUTE_30"),), {"source": "test"})
            with self.assertRaises(DataError):
                read_snapshot(path)
            import gzip
            with gzip.open(path, "rt") as f:
                value = json.load(f)
            value["candles"][0]["prices"]["close"] = "1.5"
            with gzip.open(path, "wt") as f:
                json.dump(value, f)
            with self.assertRaisesRegex(DataError, "fingerprint"):
                read_snapshot(path)

    def test_missing_constituents_not_hidden_by_aggregation_and_completed_only(self):
        bars = tuple(bar(0, 1.2, stamp=BASE + timedelta(minutes=30*i), tf="MINUTE_30")
                     for i in (0, 1, 3, 4, 5))
        frames, audit = prepare_frames(bars, BASE, BASE + timedelta(hours=3))
        self.assertEqual(audit["missing_active_30m_bars"], 1)
        self.assertNotIn(BASE + timedelta(hours=1), [c.timestamp for c in frames["HOUR"]])
        self.assertEqual(frames["HOUR_4"], ())

    def test_capital_utc_and_bid_ask_mapping(self):
        payload = {"prices": [{"snapshotTimeUTC": "2026-10-12T00:00:00",
                              "snapshotTime": "2026-10-12T02:00:00",
                              **{k: {"bid": 1.1, "ask": 1.1002} for k in
                                 ("openPrice", "highPrice", "lowPrice", "closePrice")}}]}
        c = capital_candles(payload)[0]
        self.assertEqual(c.timestamp, BASE)
        self.assertEqual(c.bid.open, D("1.1"))
        self.assertEqual(c.ask.open, D("1.1002"))

    def test_split_and_search_budget_no_neighbours_in_holdout(self):
        windows = splits()
        self.assertEqual(len(windows), 5)
        self.assertTrue(all(a["end"] == b["start"] for a,b in zip(windows, windows[1:])))
        self.assertEqual(windows[-1]["start"].isoformat(), "2026-03-27T00:00:00+00:00")
        self.assertEqual(len(list(configurations("development"))) * 4, 180)
        self.assertEqual(len(list(configurations("holdout"))), 21)
        self.assertFalse(any(label.startswith("donchian_") and label != "donchian_20_10"
                             for _,label,_,_ in configurations("holdout")))

    def test_holdout_cannot_be_repeated_or_freeze_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td)
            (output / "holdout_started.json").write_text("{}")
            with patch("trading_bot.backtesting.donchian_study.load_prepared", return_value=({}, {})):
                with self.assertRaises(ValueError):
                    run_stage(output, "holdout")
                with self.assertRaises(ValueError):
                    freeze(output)


if __name__ == "__main__":
    unittest.main()

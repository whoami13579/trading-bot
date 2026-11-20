"""Candle-directed fixed-grid backtests: deterministic, offline, hand-computed fills.

Unless stated otherwise the scenarios use EMA(3) (alpha 1/2), levels every
0.0010 from 1.0950 to 1.1050, 100 EUR per level, a 300 EUR position cap and
bid/ask candles 0.0002 apart around the listed midpoint prices.
"""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import csv
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from backtest_fixtures import hourly_candles
from trading_bot.backtesting import cli, runner
from trading_bot.backtesting.adapters import GRID
from trading_bot.backtesting.engine import SimulationConfig
from trading_bot.backtesting.grid import GridBook, grid_pnl, grid_statistics, simulate_grid
from trading_bot.backtesting.models import Candle, OHLC
from trading_bot.backtesting.study_data import FETCH_START, START, END, write_snapshot
from trading_bot.strategies.grid import Ema, GridStrategy

from test_donchian_cli import GUARD, candle as half_hour
from test_unified_backtesting import NOW, SharedDataTests

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = datetime(2026, 9, 1, tzinfo=timezone.utc)  # Tuesday
HALF = D("0.0001")
STRATEGY = GridStrategy(D("1.0950"), D("1.1050"), ema_period=3, grid_intervals=10,
                        level_size=D(100), max_position=D(300))


def bar(i, o, h, low, c, *, half=HALF, quotes=True):
    mid = [D(v) for v in (o, h, low, c)]
    stamp = ORIGIN + timedelta(hours=i)
    if not quotes:
        return Candle(stamp, OHLC(*mid), "EURUSD", "HOUR", "fixture")
    bid = OHLC(*(m - half for m in mid))
    ask = OHLC(*(m + half for m in mid))
    return Candle(stamp, ask, "EURUSD", "HOUR", "fixture", bid, ask)


def flat(i, price, **kw):
    return bar(i, price, price, price, price, **kw)


def mirror(c):
    """Reflect midpoint prices around 1.1000 (levels map onto themselves)."""
    m = D("2.2000")
    mid = [(getattr(c.bid, f) + getattr(c.ask, f)) / 2 for f in ("open", "high", "low", "close")]
    o, h, low, close = (m - mid[0], m - mid[2], m - mid[1], m - mid[3])
    i = int((c.timestamp - ORIGIN) / timedelta(hours=1))
    return bar(i, str(o), str(h), str(low), str(close))


# Warm-up closes 1.1000, 1.1000, 1.1010: EMA(3) seeds at 1.10033.. -> long, reference 1.1010.
WARM = [flat(0, "1.1000"), flat(1, "1.1000"), bar(2, "1.1000", "1.1010", "1.1000", "1.1010")]
LONG = WARM + [
    bar(3, "1.1010", "1.1012", "1.0996", "1.1012"),  # buy 1.1000 touches (ask low 1.0997)
    bar(4, "1.1005", "1.1014", "1.1004", "1.1013"),  # sell exit 1.1010 touches (bid high 1.1013)
    bar(5, "1.1013", "1.1015", "1.0997", "1.1015"),  # restored buy 1.1000 touches again
]


def simulate(candles, strategy=STRATEGY, config=None, *, start=ORIGIN, end=None):
    end = end or candles[-1].end
    return simulate_grid(candles, strategy, config or SimulationConfig(), start=start, end=end)


def at(i):
    return ORIGIN + timedelta(hours=i)


class StrategyRuleTests(unittest.TestCase):
    def test_ema_seeds_with_mean_then_recurses(self):
        ema = Ema(3)
        self.assertEqual([ema.update(D(v)) for v in ("1", "2")], [None, None])
        self.assertEqual(ema.update(D("3")), D(2))
        self.assertEqual(ema.update(D("4")), D(3))  # 2 + (4 - 2) / 2

    def test_direction_rule_and_equality_keeps_current(self):
        rule = GridStrategy.direction
        self.assertEqual(rule(D(2), D(1)), "BUY")
        self.assertEqual(rule(D(1), D(2)), "SELL")
        self.assertIsNone(rule(D(1), D(1)))
        self.assertEqual(rule(D(1), D(1), "SELL"), "SELL")
        self.assertEqual(rule(D(1), D(1), "BUY"), "BUY")
        self.assertIsNone(rule(D(5), None, "BUY"))  # No EMA yet: flat.

    def test_fixed_levels_and_entry_selection(self):
        levels = STRATEGY.levels
        self.assertEqual(levels[0], D("1.0950"))
        self.assertEqual(levels[-1], D("1.1050"))
        self.assertTrue(all(b - a == D("0.0010") for a, b in zip(levels, levels[1:])))
        # Long: strictly below the reference, nearest first, never the top level.
        self.assertEqual(STRATEGY.entry_levels("BUY", D("1.1010")), (5, 4, 3, 2, 1, 0))
        self.assertEqual(STRATEGY.entry_levels("BUY", D("2")), tuple(range(9, -1, -1)))
        # Short: strictly above the reference, nearest first, never the bottom level.
        self.assertEqual(STRATEGY.entry_levels("SELL", D("1.0990")), (5, 6, 7, 8, 9, 10))
        self.assertEqual(STRATEGY.entry_levels("SELL", D("1")), tuple(range(1, 11)))
        self.assertEqual(STRATEGY.entry_levels("SELL", D("1.2")), ())
        # Uneven division rounds to the 0.00001 tick.
        thirds = GridStrategy(D("1.1"), D("1.2"), grid_intervals=3).levels
        self.assertEqual(thirds, (D("1.10000"), D("1.13333"), D("1.16667"), D("1.20000")))

    def test_invalid_inputs_are_rejected(self):
        bad = (
            dict(grid_lower=D("1.2"), grid_upper=D("1.1")),
            dict(grid_lower=D("0"), grid_upper=D("1.1")),
            dict(grid_lower=None, grid_upper=D("1.1")),
            dict(grid_lower="nan", grid_upper=D("1.1")),
            dict(ema_period=1),
            dict(ema_period=2.0),
            dict(grid_intervals=0),
            dict(grid_intervals=1001),
            dict(level_size=D(0)),
            dict(max_position=D(50)),
            dict(grid_lower=D("1.10000"), grid_upper=D("1.10002"), grid_intervals=10),
        )
        base = dict(grid_lower=D("1.1"), grid_upper=D("1.2"))
        for change in bad:
            with self.subTest(change=change), self.assertRaises(ValueError):
                GridStrategy(**dict(base, **change))


class WarmupTests(unittest.TestCase):
    def test_flat_until_ema_is_warm(self):
        result = simulate(LONG)
        targets = [d["target"] for d in result.decisions]
        self.assertEqual(targets[:2], [None, None])
        self.assertEqual([d["ema"] for d in result.decisions[:2]], [None, None])
        self.assertEqual(targets[2:], ["BUY"] * 4)
        # Nothing is placed before the first open after the warm decision.
        self.assertEqual(min(o.created_at for o in result.ledger.orders), at(3))
        for point in result.equity_curve[1:7]:  # Opens/closes of candles 0-2.
            self.assertEqual(point["working_orders"], 0)
        activation = result.ledger.events[0]
        self.assertEqual((activation["action"], activation["timestamp"]), ("ACTIVATE", at(3)))
        self.assertEqual((activation["reference"], activation["signal_time"]),
                         (D("1.1010"), at(3)))

    def test_warmup_history_primes_the_first_scored_open(self):
        result = simulate(LONG, start=at(3))
        self.assertEqual(result.candles, 3)
        self.assertEqual(len(result.decisions), 3)  # Warm-up closes are not scored.
        first = result.ledger.events[0]
        self.assertEqual((first["timestamp"], first["reference"]), (at(3), D("1.1010")))
        self.assertEqual(simulate(LONG).ledger.trades, result.ledger.trades)

    def test_decisions_use_only_completed_candles(self):
        candles = hourly_candles()[:200]
        strategy = replace(STRATEGY, ema_period=12)
        full = simulate(candles, strategy)
        for decision in full.decisions:
            prefix = candles[: decision["index"] + 1]
            self.assertEqual(strategy.target(prefix), decision["target"])
        # Truncating the future leaves every earlier decision, fill and mark unchanged.
        early = simulate(candles[:121], strategy)
        self.assertEqual(early.decisions, [d for d in full.decisions if d["index"] <= 120])
        self.assertEqual(early.ledger.events, [e for e in full.ledger.events if e["index"] <= 120])
        marks = len(early.equity_curve) - 1  # Initial + open/close marks, no final mark.
        self.assertEqual(early.equity_curve[:marks], full.equity_curve[:marks])


class GridCycleTests(unittest.TestCase):
    def test_long_grid_cycle_and_replenishment(self):
        result = simulate(LONG)
        book = result.ledger
        entries = [o for o in book.orders if o.role == "entry"]
        self.assertEqual([(o.id, o.price, o.created_at, o.eligible_index) for o in entries[:3]],
                         [(1, D("1.1000"), at(3), 3), (2, D("1.0990"), at(3), 3),
                          (3, D("1.0980"), at(3), 3)])
        exit_order = book.orders[3]
        self.assertEqual((exit_order.role, exit_order.side, exit_order.price,
                          exit_order.eligible_index), ("exit", "SELL", D("1.1010"), 4))
        # The candle that filled the buy also reached 1.1010 (bid high 1.1011),
        # but the exit only becomes eligible on the next candle.
        self.assertEqual(exit_order.fill_time, at(5))
        [trade] = book.trades
        self.assertEqual((trade.direction, trade.entry_price, trade.exit_price, trade.gross_pnl),
                         ("BUY", D("1.1000"), D("1.1010"), D("0.1000")))
        self.assertEqual((trade.entry_timestamp, trade.exit_timestamp), (at(4), at(5)))
        restored = book.orders[4]
        self.assertEqual((restored.role, restored.price, restored.created_at,
                          restored.eligible_index, restored.status),
                         ("entry", D("1.1000"), at(5), 5, "filled"))
        [lot] = book.lots.values()
        self.assertEqual((lot.entry_price, lot.entry_order_id, lot.entry_timestamp),
                         (D("1.1000"), restored.id, at(6)))
        stats = grid_statistics(result)
        self.assertEqual((stats["total_trades"], stats["open_lots"], stats["working_orders"]),
                         (1, 1, 3))
        self.assertEqual(stats["peak_committed_units"], D(300))
        self.assertEqual(stats["realized_pnl"], D("0.1000"))
        self.assertEqual(len([o for o in book.orders if o.role == "entry"]), 4)

    def test_short_grid_cycle_mirrors_the_long_cycle(self):
        result = simulate([mirror(c) for c in LONG])
        book = result.ledger
        self.assertEqual([d["target"] for d in result.decisions], [None, None] + ["SELL"] * 4)
        self.assertEqual([(o.side, o.price) for o in book.orders[:3]],
                         [("SELL", D("1.1000")), ("SELL", D("1.1010")), ("SELL", D("1.1020"))])
        exit_order = book.orders[3]
        self.assertEqual((exit_order.side, exit_order.price, exit_order.eligible_index),
                         ("BUY", D("1.0990"), 4))
        [trade] = book.trades
        self.assertEqual((trade.direction, trade.entry_price, trade.exit_price, trade.gross_pnl),
                         ("SELL", D("1.1000"), D("1.0990"), D("0.1000")))
        self.assertEqual(book.orders[4].price, D("1.1000"))  # Restored short entry.
        [lot] = book.lots.values()
        self.assertEqual((lot.direction, lot.entry_price), ("SELL", D("1.1000")))

    def test_buys_use_ask_and_sells_use_bid(self):
        # Wider spread: the buy at 1.0990 needs the *ask* low to reach it.
        wide = WARM + [bar(3, "1.1010", "1.1012", "1.0988", "1.1012", half=D("0.0003"))]
        book = simulate(wide).ledger
        self.assertEqual([lot.entry_price for lot in book.lots.values()], [D("1.1000")])
        # Exit sells need the *bid* high: mid 1.1011 with a 3-pip half-spread is not enough.
        wide.append(bar(4, "1.1005", "1.1011", "1.1004", "1.1011", half=D("0.0003")))
        self.assertEqual(simulate(wide).ledger.trades, [])
        wide[-1] = bar(4, "1.1005", "1.1013", "1.1004", "1.1013", half=D("0.0003"))
        self.assertEqual([t.exit_price for t in simulate(wide).ledger.trades], [D("1.1010")])

    def test_indicative_quotes_without_bid_ask(self):
        plain = [bar(i, c.prices.open - HALF, c.prices.high - HALF, c.prices.low - HALF,
                     c.prices.close - HALF, quotes=False) for i, c in enumerate(LONG)]
        result = simulate(plain)
        self.assertEqual([(t.entry_price, t.exit_price) for t in result.ledger.trades],
                         [(D("1.1000"), D("1.1010"))])
        self.assertEqual(result.ledger.embedded_spread, 0)


class DirectionChangeTests(unittest.TestCase):
    CANDLES = LONG + [
        bar(6, "1.1008", "1.1008", "1.0995", "1.0996"),  # close below EMA -> short
        flat(7, "1.0994"),
    ]

    def test_flip_cancels_orders_flattens_lots_then_activates_short_grid(self):
        result = simulate(self.CANDLES, config=SimulationConfig(slippage_pips=D("0.1")))
        book = result.ledger
        self.assertEqual(result.decisions[-2]["target"], "SELL")
        cancelled = [o for o in book.orders if o.status == "cancelled"]
        self.assertEqual(sorted((o.role, o.price) for o in cancelled),
                         [("entry", D("1.0980")), ("entry", D("1.0990")),
                          ("exit", D("1.1010"))])
        self.assertTrue(all(o.closed_at == at(7) and o.reason == "direction_change"
                            for o in cancelled))
        flatten = book.trades[-1]
        # Market sell at the next observed bid open minus 0.1 pip slippage.
        self.assertEqual((flatten.exit_timestamp, flatten.exit_price),
                         (at(7), D("1.0993") - D("0.00001")))
        self.assertEqual(flatten.gross_pnl, (D("1.09929") - D("1.1000")) * 100)
        record = book.trade_records[-1]
        self.assertEqual((record["exit_reason"], record["exit_signal_time"]),
                         ("direction_change", at(7)))
        self.assertEqual(record["slippage_cost"], D("0.001"))
        actions = [(e["action"], e.get("grid_id")) for e in book.events]
        self.assertLess(actions.index(("CLOSE", 1)), actions.index(("ACTIVATE", 2)))
        short = book.grid
        self.assertEqual((short.direction, short.reference, short.activated_at),
                         ("SELL", D("1.0996"), at(7)))
        working = sorted((o.side, o.price) for o in book.working.values())
        self.assertEqual(working, [("SELL", D("1.1000")), ("SELL", D("1.1010")),
                                   ("SELL", D("1.1020"))])
        self.assertEqual(book.lots, {})
        stats = grid_statistics(result)
        self.assertEqual((stats["direction_changes"], stats["grid_activations"],
                          stats["flatten_exits"], stats["grid_exits"]), (1, 2, 1, 1))

    def test_flip_on_last_candle_leaves_reported_inventory(self):
        result = simulate(self.CANDLES[:-1])
        from trading_bot.backtesting.grid import ending_inventory

        inventory = ending_inventory(result)
        self.assertEqual(len(inventory["open_lots"]), 1)
        self.assertEqual(inventory["working_orders"], [])  # Cancelled at the signal close.
        self.assertEqual(inventory["pending_activation"]["direction"], "SELL")
        self.assertIsNone(inventory["direction"])


class GapAndMultiLevelTests(unittest.TestCase):
    def test_gap_through_fills_at_the_open_and_waits_one_candle(self):
        candles = WARM + [
            bar(3, "1.0985", "1.1012", "1.0984", "1.1012"),  # opens below 1.1000 and 1.0990
            bar(4, "1.1020", "1.1022", "1.1018", "1.1021"),  # opens above both exits
        ]
        book = simulate(candles).ledger
        opened = [e for e in book.events if e["action"] == "OPEN"]
        self.assertEqual([(e["level"], e["price"], e["fill_type"], e["timestamp"])
                          for e in opened],
                         [(D("1.1000"), D("1.0986"), "gap_open", at(3)),
                          (D("1.0990"), D("1.0986"), "gap_open", at(3))])
        self.assertEqual([(t.entry_price, t.exit_price, t.exit_timestamp) for t in book.trades],
                         [(D("1.0986"), D("1.1019"), at(4))] * 2)
        self.assertEqual([r["exit_fill_type"] for r in book.trade_records], ["gap_open"] * 2)
        restored = sorted((o.price, o.created_at, o.eligible_index)
                          for o in book.working.values() if o.created_at == at(4))
        self.assertEqual(restored, [(D("1.0990"), at(4), 5), (D("1.1000"), at(4), 5)])
        self.assertEqual(sum(o.price == D("1.0980") and o.status == "working"
                             for o in book.orders), 1)

    def test_multiple_levels_fill_along_the_up_first_path(self):
        candles = LONG[:4] + [bar(4, "1.1005", "1.1012", "1.0975", "1.1012")]
        book = simulate(candles).ledger
        tail = [(e["action"], e.get("level", e.get("price"))) for e in book.events[2:]]
        self.assertEqual(tail, [("CLOSE", D("1.1010")), ("OPEN", D("1.0990")),
                                ("OPEN", D("1.0980"))])
        self.assertEqual(sorted(lot.entry_price for lot in book.lots.values()),
                         [D("1.0980"), D("1.0990")])
        # The entry restored at 1.1000 was created by a fill: ask low 1.0976 does
        # not fill it on this candle.
        restored = [o for o in book.working.values() if o.role == "entry"]
        self.assertEqual([(o.price, o.eligible_index) for o in restored], [(D("1.1000"), 5)])
        self.assertEqual(book.committed_units(), D(300))

    def test_down_first_path_fills_entries_before_exits(self):
        candles = LONG[:4] + [bar(4, "1.1005", "1.1030", "1.0985", "1.1012")]
        book = simulate(candles).ledger
        self.assertEqual([e["action"] for e in book.events[2:]], ["OPEN", "CLOSE"])
        self.assertEqual(book.events[2]["level"], D("1.0990"))


class ExposureAndFundingTests(unittest.TestCase):
    def test_position_cap_counts_open_lots_and_working_entries(self):
        strategy = replace(STRATEGY, max_position=D(200))
        # Candle 4 opens below both exits and above 1.0980: the cap blocks 1.0980.
        candles = WARM + [bar(3, "1.1010", "1.1012", "1.0955", "1.1012"), flat(4, "1.0995")]
        result = simulate(candles, strategy)
        book = result.ledger
        self.assertEqual(sorted(o.price for o in book.orders if o.role == "entry"),
                         [D("1.0990"), D("1.1000")])
        self.assertEqual(len(book.lots), 2)
        stats = grid_statistics(result)
        self.assertEqual((stats["peak_open_units"], stats["peak_committed_units"],
                          stats["peak_open_lots"]), (D(200), D(200), 2))
        self.assertEqual(stats["peak_open_notional_usd"], D("219.90"))
        for point in result.equity_curve:
            self.assertLessEqual(point["open_units"] + point["working_entry_units"], 200)

    def test_funds_are_checked_on_admission(self):
        result = simulate(LONG, config=SimulationConfig(initial_balance=D(250)))
        book = result.ledger
        self.assertNotIn(D("1.0980"), [o.price for o in book.orders])
        [rejection] = book.rejections
        self.assertEqual((rejection["reason"], rejection["price"], rejection["timestamp"]),
                         ("insufficient_available_funds_on_admission", D("1.0980"), at(3)))
        self.assertEqual(rejection["required_margin"], D("109.8000"))
        self.assertGreaterEqual(book.funds_rejection_attempts, 3)  # Retried each open.
        self.assertEqual(len(book.trades), 1)  # The funded levels still cycle.
        self.assertTrue(all(p["available_funds"] >= 0 for p in result.equity_curve))

    def test_funds_are_rechecked_on_fill(self):
        wide = GridStrategy(D("1.05"), D("1.15"), ema_period=3, grid_intervals=10,
                            level_size=D(1000), max_position=D(3000))
        config = SimulationConfig(initial_balance=D(225), leverage=D(10))
        candles = [flat(0, "1.1000"), flat(1, "1.1000"),
                   bar(2, "1.1000", "1.1050", "1.1000", "1.1050"),
                   bar(3, "1.1050", "1.1060", "1.0850", "1.1060"),
                   flat(4, "1.1060")]
        book = simulate(candles, wide, config).ledger
        reasons = [r["reason"] for r in book.rejections]
        self.assertEqual(reasons, ["insufficient_available_funds_on_admission",
                                   "insufficient_available_funds_on_fill"])
        rejected = [o for o in book.orders if o.status == "rejected"]
        self.assertEqual([(o.price, o.reason) for o in rejected],
                         [(D("1.09"), "insufficient_available_funds")])
        self.assertEqual([lot.entry_price for lot in book.lots.values()], [D("1.10")])
        # The open 1.10 lot recovered; the rejected level is re-admitted at a later open.
        readmitted = [o for o in book.working.values() if o.price == D("1.09")]
        self.assertEqual([o.created_at for o in readmitted], [at(4)])


class AccountingTests(unittest.TestCase):
    CONFIG = SimulationConfig(commission_per_order=D("0.02"), slippage_pips=D("0.1"),
                              financing_per_unit_day=D("0.00002"))
    STRATEGY = GridStrategy(D("1.0990"), D("1.1070"), ema_period=20, grid_intervals=40,
                            level_size=D(100), max_position=D(400))

    def run_fixture(self):
        candles = hourly_candles()
        return simulate(candles, self.STRATEGY, self.CONFIG, start=candles[30].timestamp)

    def test_long_run_never_duplicates_fills_and_reconciles(self):
        result = self.run_fixture()
        book = result.ledger
        stats = grid_statistics(result)
        self.assertGreater(stats["grid_exits"], 20)
        self.assertGreater(stats["flatten_exits"], 20)
        self.assertGreater(stats["direction_changes"], 20)
        filled_entries = [o for o in book.orders if o.role == "entry" and o.status == "filled"]
        lot_ids = [o.lot_id for o in filled_entries]
        self.assertEqual(len(lot_ids), len(set(lot_ids)))
        closed = [r["lot_id"] for r in book.trade_records]
        self.assertEqual(len(closed), len(set(closed)))
        self.assertEqual(set(closed) | set(book.lots), set(lot_ids))
        self.assertTrue(all(o.fill_time is None for o in book.orders
                            if o.status in ("cancelled", "rejected", "working")))
        for point in result.equity_curve:
            self.assertLessEqual(point["open_units"] + point["working_entry_units"], 400)
        # Cash: initial + realized gross - every commission - every financing charge.
        # (Financing divides by 86400; summation order differs only at 1e-24.)
        self.assertAlmostEqual(
            book.balance,
            self.CONFIG.initial_balance + sum(t.gross_pnl for t in book.trades)
            - book.commission - book.financing,
            delta=D("1e-18"),
        )
        self.assertEqual(book.commission, D("0.02") * sum(
            o.status == "filled" for o in book.orders))
        pnl = grid_pnl(result)  # Raises if gross/net does not reconcile.
        self.assertEqual(pnl["net_pnl"], stats["net_pnl"])
        self.assertEqual(stats["trading_costs"], pnl["modeled_costs"])
        self.assertEqual(stats["realized_pnl"] + stats["unrealized_pnl"], stats["net_pnl"])

    def test_identical_inputs_give_identical_results(self):
        a, b = self.run_fixture(), self.run_fixture()
        self.assertEqual(a.equity_curve, b.equity_curve)
        self.assertEqual(a.ledger.orders, b.ledger.orders)


def quoted(i, bid, ask):
    """One candle with explicit bid and ask O/H/L/C (no symmetric spread)."""
    ask = OHLC(*(D(v) for v in ask))
    return Candle(at(i), ask, "EURUSD", "HOUR", "fixture", OHLC(*(D(v) for v in bid)), ask)


def long_book(strategy, config, reference, first):
    book = GridBook(config, strategy)
    book.activate({"direction": "BUY", "reference": D(reference),
                   "signal_time": first.timestamp}, first, 0)
    return book


def step(book, candle, index):
    """Drive one scored candle exactly as simulate_grid does (direction held)."""
    book.accrue_financing(candle.open_time)
    if book.grid is not None:
        book.admit(candle, index)
    book.fill_candle(candle, index)
    book.accrue_financing(candle.end)
    book.check_invariants()


class ReviewRegressionTests(unittest.TestCase):
    """Regressions for the PR #6 review findings on de2a760."""

    def test_intrabar_fills_follow_the_midpoint_path_segments(self):
        # Half-pip levels: lot at 1.10005 exits at 1.10010; a buy works at 1.10000.
        strategy = GridStrategy(D("1.09950"), D("1.10050"), ema_period=2, grid_intervals=20,
                                level_size=D(100), max_position=D(200))
        setup = quoted(0, bid=("1.10006", "1.10007", "1.10002", "1.10004"),
                       ask=("1.10008", "1.10009", "1.10004", "1.10006"))
        book = long_book(strategy, SimulationConfig(initial_balance=D("220.05")),
                         "1.10008", setup)
        step(book, setup, 0)
        [lot] = book.lots.values()
        self.assertEqual(lot.entry_price, D("1.10005"))
        # Midpoint O 1.1002, H 1.1011, L 1.09995: down first, so the ask touches the
        # buy on the first segment before the bid reaches the exit on the second.
        # By raw per-series distance the exit (0.0003) would come before the buy (0.0004).
        review = quoted(1, bid=("1.1000", "1.1010", "1.0999", "1.1005"),
                        ask=("1.1004", "1.1012", "1.1000", "1.1007"))
        step(book, review, 1)
        buy = next(o for o in book.orders if o.price == D("1.10000"))
        self.assertEqual((buy.status, buy.reason), ("rejected", "insufficient_available_funds"))
        # With the lot still open: 220.05 - 0.045 unrealized - 110.005 lot margin
        # - 0.04 immediate spread = 109.96 < 110.00 required.
        [rejection] = [r for r in book.rejections
                       if r["reason"] == "insufficient_available_funds_on_fill"]
        self.assertEqual((rejection["price"], rejection["available_funds"]),
                         (D("1.10000"), D("109.960")))
        [trade] = book.trades  # The exit still fills afterwards, at its limit.
        self.assertEqual((trade.entry_price, trade.exit_price), (D("1.10005"), D("1.10010")))
        self.assertEqual(book.lots, {})
        # The entry restored by that exit is created by a fill: next candle only,
        # although the ask low (1.1000) is below it.
        restored = [o for o in book.working.values() if o.role == "entry"]
        self.assertEqual([(o.price, o.eligible_index, o.created_at) for o in restored],
                         [(D("1.10005"), 2, at(2))])

    def test_path_segments_order_exits_before_entries_only_at_the_same_point(self):
        # Up-first midpoint: the sell exit (first segment) precedes the buy (second).
        strategy = GridStrategy(D("1.09950"), D("1.10050"), ema_period=2, grid_intervals=20,
                                level_size=D(100), max_position=D(200))
        setup = quoted(0, bid=("1.10006", "1.10007", "1.10002", "1.10004"),
                       ask=("1.10008", "1.10009", "1.10004", "1.10006"))
        book = long_book(strategy, SimulationConfig(), "1.10008", setup)
        step(book, setup, 0)
        up = quoted(1, bid=("1.10006", "1.10012", "1.09990", "1.10000"),
                    ask=("1.10008", "1.10014", "1.09992", "1.10002"))
        step(book, up, 1)
        self.assertEqual([e["action"] for e in book.events[1:]], ["OPEN", "CLOSE", "OPEN"])
        self.assertEqual(book.events[-1]["price"], D("1.10000"))

    FUNDING_STRATEGY = GridStrategy(D("1.0950"), D("1.1050"), ema_period=3, grid_intervals=10,
                                    level_size=D(100), max_position=D(200))
    # 100 units x 0.024 USD/unit/day x 1 hour = 0.10 USD per candle per open lot.
    FINANCING = D("0.024")

    def funding_book(self, balance, strategy=None):
        strategy = strategy or self.FUNDING_STRATEGY
        first = bar(0, "1.1005", "1.1006", "1.0998", "1.1004")  # buy 1.1000 touches
        book = long_book(strategy, SimulationConfig(initial_balance=D(balance),
                                                    financing_per_unit_day=self.FINANCING),
                         "1.1010", first)
        step(book, first, 0)
        self.assertEqual([lot.entry_price for lot in book.lots.values()], [D("1.1000")])
        self.assertEqual(book.financing, 0)  # Opened at the candle end.
        return book

    def test_financing_accrues_before_the_entry_funds_check(self):
        # The 1.0990 buy touches at the candle end. Before financing: 220.09 - 0.12
        # unrealized - 110.00 lot margin - 0.02 immediate spread = 109.95, i.e. $0.05
        # above the 109.90 margin. The lot's hour of financing ($0.10) removes it.
        book = self.funding_book("220.09")
        step(book, bar(1, "1.1000", "1.1001", "1.0988", "1.0995"), 1)
        buy = next(o for o in book.orders if o.price == D("1.0990"))
        self.assertEqual(buy.status, "rejected")
        [rejection] = [r for r in book.rejections
                       if r["reason"] == "insufficient_available_funds_on_fill"]
        self.assertEqual(rejection["available_funds"], D("109.85"))
        self.assertEqual(book.financing, D("0.1"))  # Charged once, not again at the end.

    def test_shared_fill_timestamp_charges_each_interval_once(self):
        strategy = replace(self.FUNDING_STRATEGY, max_position=D(300))
        book = self.funding_book("1000", strategy)
        # Buys at 1.0990 and 1.0980 both touch, sharing the candle-end timestamp.
        step(book, bar(1, "1.1000", "1.1001", "1.0975", "1.0995"), 1)
        self.assertEqual(sorted(lot.entry_price for lot in book.lots.values()),
                         [D("1.0980"), D("1.0990"), D("1.1000")])
        self.assertEqual(book.financing, D("0.1"))  # Only the first lot's hour.
        step(book, flat(2, "1.0985"), 2)  # No exit or entry is reached.
        self.assertEqual(book.financing, D("0.4"))  # + three lots for one more hour.
        self.assertEqual(book.balance, D("1000") - D("0.4"))

    def test_peak_open_notional_is_tracked_independently_of_peak_units(self):
        strategy = replace(STRATEGY, max_position=D(100))
        candles = WARM + [
            bar(3, "1.0985", "1.1012", "1.0984", "1.1012"),  # buy 1.1000 gaps at ask 1.0986
            bar(4, "1.1020", "1.1022", "1.1018", "1.1021"),  # exit 1.1010 gaps at bid 1.1019
            bar(5, "1.1020", "1.1022", "1.0998", "1.1022"),  # restored buy touches 1.1000
        ]
        result = simulate(candles, strategy)
        book = result.ledger
        self.assertEqual([(t.entry_price, t.exit_price) for t in book.trades],
                         [(D("1.0986"), D("1.1019"))])
        self.assertEqual([lot.entry_price for lot in book.lots.values()], [D("1.1000")])
        stats = grid_statistics(result)
        self.assertEqual(stats["peak_open_units"], D(100))
        self.assertEqual(stats["peak_open_lots"], 1)
        self.assertEqual(stats["peak_open_notional_usd"], D("110.00"))

    def test_any_explicit_size_is_rejected_for_grid_only(self):
        argv = ["--strategy", "grid", "--grid-lower", "1.15", "--grid-upper", "1.19",
                "--size", "100"]
        with ExitStack() as stack:
            fetch = stack.enter_context(patch("trading_bot.backtesting.runner.load_history"))
            snap = stack.enter_context(patch("trading_bot.backtesting.runner.load_snapshot"))
            err = stack.enter_context(redirect_stderr(io.StringIO()))
            with self.assertRaises(SystemExit) as raised:
                cli.main(argv, now=NOW)
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--size does not apply to --strategy grid", err.getvalue())
        fetch.assert_not_called()
        snap.assert_not_called()
        with self.assertRaises(runner.PlanError):
            runner.plan(runner.BacktestRequest(
                "grid", {"grid_lower": D("1.15"), "grid_upper": D("1.19")}, size=D(100)),
                now=NOW)
        # HMA and Donchian keep accepting --size (and default to 100 without it).
        # Dates before the frozen Donchian holdout, which refuses non-preregistered sizes.
        dates = ["--start", "2024-01-01", "--end", "2024-02-01"]
        for key in ("hma", "donchian"):
            args = cli.build_parser().parse_args(["--strategy", key, "--size", "250", *dates])
            self.assertEqual(runner.plan(cli.request_from_args(args), now=NOW)
                             .execution.config.size, D(250))
            args = cli.build_parser().parse_args(["--strategy", key, *dates])
            self.assertEqual(runner.plan(cli.request_from_args(args), now=NOW)
                             .execution.config.size, D(100))


def oscillating_snapshot(path):
    """30m snapshot fixture: flat warm-up, then a repeating 4-hour sawtooth above 1.1000."""
    wave = (0, 6, 2, 8, 4, 10, 6, 12)
    rows = [half_hour(FETCH_START, D("1.1"))]
    rows += [half_hour(START - timedelta(minutes=30 * i), D("1.1")) for i in range(48, 0, -1)]
    rows += [half_hour(START + timedelta(minutes=30 * i), D("1.1") + D("0.0001") * wave[i % 8])
             for i in range(48)]
    rows.append(half_hour(END - timedelta(days=2), D("1.1")))
    write_snapshot(path, rows, {"source": "fixture"})


GRID_ARGS = ["--strategy", "grid", "--grid-lower", "1.0980", "--grid-upper", "1.1060",
             "--grid-intervals", "16", "--ema-period", "4", "--max-position", "300"]


class GridCliTests(unittest.TestCase):
    def plan(self, *argv):
        args = cli.build_parser().parse_args(list(argv))
        return runner.plan(cli.request_from_args(args), now=NOW)

    def test_defaults_and_parameters(self):
        p = self.plan("--strategy", "grid", "--grid-lower", "1.15", "--grid-upper", "1.19")
        self.assertIs(p.adapter, GRID)
        self.assertEqual((p.time_frame, p.data_source, p.execution.name),
                         ("HOUR", "dukascopy", "grid"))
        s = p.strategy
        self.assertEqual((s.ema_period, s.grid_intervals, s.level_size, s.max_position),
                         (50, 10, D(100), D(500)))
        self.assertEqual(p.execution.config.size, D(100))
        p = self.plan(*GRID_ARGS, "--level-size", "250", "--max-position", "1000",
                      "--commission-per-order", "0.5", "--time-frame", "MINUTE_15")
        self.assertEqual((p.strategy.level_size, p.strategy.max_position), (D(250), D(1000)))
        self.assertEqual((p.execution.config.size, p.execution.config.commission_per_order),
                         (D(250), D("0.5")))
        self.assertEqual(p.specification["parameters"]["grid_lower"], D("1.0980"))

    def test_invalid_combinations_fail_before_data_access(self):
        bad = (
            ["--strategy", "grid"],
            ["--strategy", "grid", "--grid-lower", "1.1"],
            [*GRID_ARGS, "--size", "50"],
            [*GRID_ARGS, "--size", "100"],  # Even when equal to --level-size.
            [*GRID_ARGS, "--execution-model", "continuous"],
            [*GRID_ARGS, "--execution-model", "research"],
            [*GRID_ARGS, "--cost-case", "base"],
            [*GRID_ARGS, "--hma-period", "20"],
            ["--strategy", "hma", "--execution-model", "grid"],
            ["--strategy", "donchian", "--grid-lower", "1.1"],
            [*GRID_ARGS, "--grid-lower", "abc"],
            [*GRID_ARGS, "--REAL"],
            [*GRID_ARGS, "--account", "x"],
            [*GRID_ARGS, "--max-orders", "3"],
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
        for argv in ([*GRID_ARGS, "--ema-period", "1"],
                     [*GRID_ARGS, "--grid-upper", "1.0970"],
                     [*GRID_ARGS, "--max-position", "50"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()) as err, \
                    patch("trading_bot.backtesting.runner.load_history") as fetch:
                self.assertEqual(cli.main(argv, now=NOW), 1)
                self.assertIn("Backtest failed", err.getvalue())
                fetch.assert_not_called()

    def test_provider_run_saves_grid_report_artifacts(self):
        data = SharedDataTests("test_strategies_reuse_one_cached_download_and_identical_candles")
        source = data.provider()
        argv = ["--strategy", "grid", "--grid-lower", "1.0990", "--grid-upper", "1.1070",
                "--grid-intervals", "40", "--ema-period", "20", "--max-position", "400",
                "--commission-per-order", "0.02", "--slippage-pips", "0.1",
                "--financing-usd-per-unit-day", "0.00002",
                "--start", data.start.isoformat(), "--end", data.end.isoformat()]
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            stack.enter_context(patch("trading_bot.backtesting.data.provider",
                                      return_value=source))
            out = stack.enter_context(redirect_stdout(io.StringIO()))
            self.assertEqual(cli.main([*argv, "--cache-dir", root + "/cache",
                                       "--output-dir", root + "/out"], now=NOW), 0)
            folder = next(Path(root, "out").iterdir())
            report = json.loads((folder / "report.json").read_text())
            with (folder / "trade_details.csv").open() as f:
                details = list(csv.DictReader(f))
            with (folder / "grid_orders.csv").open() as f:
                orders = list(csv.DictReader(f))
            with (folder / "equity.csv").open() as f:
                equity_columns = next(csv.reader(f))
            self.assertTrue((folder / "equity.html").exists())
        source.fetch.assert_called_once()
        text = out.getvalue()
        for label in ("Execution model:         grid", "Peak exposure:", "Ending inventory:",
                      "Trading costs:", "Max drawdown:", "Realized cash P&L:",
                      "Unrealized P&L:", "Environment:             DEMO (simulated only)"):
            self.assertIn(label, text)
        stats = report["statistics"]
        self.assertEqual(report["strategy_key"], "grid")
        self.assertEqual(report["execution_model"], "grid")
        self.assertGreater(stats["total_trades"], 0)
        self.assertEqual(len(details), stats["total_trades"])
        self.assertIn("lot_id", details[0])
        self.assertEqual(len(orders), stats["orders_placed"])
        self.assertIn("reserved_margin", equity_columns)
        self.assertEqual(report["unmodeled_costs"], [])
        self.assertEqual(D(report["total_cost_estimate"]), D(stats["trading_costs"]))
        self.assertEqual(len(report["grid"]["levels"]), 41)
        inventory = report["grid"]["ending_inventory"]
        self.assertEqual(len(inventory["open_lots"]), stats["open_lots"])
        self.assertEqual(report["open_positions"], inventory["open_lots"])
        self.assertIn("fills", report["assumptions"])
        self.assertEqual(len(report["reproducibility"]["configuration_sha256"]), 64)

    def test_fresh_interpreter_grid_cli_is_offline_and_broker_free(self):
        with tempfile.TemporaryDirectory() as td:
            snapshot = Path(td) / "grid.json.gz"
            oscillating_snapshot(snapshot)
            argv = [sys.executable, "-c", GUARD, str(ROOT / "scripts/backtest.py"), *GRID_ARGS,
                    "--data-source", "snapshot", "--snapshot", str(snapshot),
                    "--start", "2023-11-01", "--end", "2023-11-02",
                    "--output-dir", str(Path(td) / "out")]
            child = subprocess.run(argv, cwd=td, capture_output=True, text=True, timeout=60)
            self.assertEqual(child.returncode, 0, child.stderr)
            report = json.loads(next(Path(td, "out").glob("*/report.json")).read_text())
        self.assertIn("Execution model:         grid", child.stdout)
        self.assertIn("All orders are simulated", child.stdout)
        self.assertGreater(report["statistics"]["grid_exits"], 0)
        self.assertEqual(report["configuration"]["size"], "100")


if __name__ == "__main__":
    unittest.main()

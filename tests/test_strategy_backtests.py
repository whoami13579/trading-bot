"""Historical backtests of moving-average, sma, ema-hma and multiple-hma.

Offline only. Covers the shared signal rules (manual values and the independent
reference implementations in reference_indicators.py), the candle strategies,
causal multi-timeframe alignment, equivalence with the unchanged online loops
driven by mocked broker APIs, engine/CLI integration, costs and reports.
"""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import importlib
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import reference_indicators as ref
from trading_bot.backtesting import cli, runner
from trading_bot.backtesting.adapters import ADAPTERS
from trading_bot.backtesting.calendar import completed_aggregates, completed_groups, slot_expected
from trading_bot.backtesting.engine import SimulationConfig, run
from trading_bot.backtesting.models import Candle, Dataset, DataError, OHLC
from trading_bot.backtesting.strategy import (
    EmaHmaStrategy,
    MovingAverageStrategy,
    MultipleHmaStrategy,
    SmaCrossoverStrategy,
)
from trading_bot.backtesting.timeframes import duration, floor_time, resample
from trading_bot.strategies import indicators, signals
from trading_bot.strategies.indicators import (
    MULTIPLE_HISTORY,
    calculate_ema,
    calculate_hma,
    calculate_sma,
    hma_signal,
    multiple_signal,
    two_ema_signal,
)

NOW = datetime(2026, 11, 17, tzinfo=timezone.utc)
TUESDAY = datetime(2026, 9, 15, tzinfo=timezone.utc)
PIP = D("0.00001")
NEW = ("moving-average", "sma", "ema-hma", "multiple-hma")


class StopLoop(BaseException):
    """Ends a mocked online loop; BaseException so no strategy loop swallows it."""


def walk(count, seed=20261125, level=110000):
    """Deterministic integer random walk in 0.00001 steps with frequent reversals."""
    state, values = seed, []
    for _ in range(count):
        state = (state * 1103515245 + 12345) % 2**31
        level += (state >> 8) % 41 - 20
        values.append(level)
    return values


def candle(stamp, open_, close, timeframe, *, spread=6):
    ask = OHLC(open_ * PIP, max(open_, close) * PIP + PIP, min(open_, close) * PIP - PIP, close * PIP)
    bid = OHLC(*(v - spread * PIP for v in (ask.open, ask.high, ask.low, ask.close)))
    return Candle(stamp, ask, "EURUSD", timeframe, "dukascopy", bid, ask)


def session_series(start, end, timeframe, seed=7):
    """One candle for every slot the FX calendar expects, opens chained to closes."""
    step = duration(timeframe)
    rows, stamp, previous = [], start, None
    slots = []
    while stamp < end:
        if slot_expected(stamp, step):
            slots.append(stamp)
        stamp += step
    for stamp, close in zip(slots, walk(len(slots), seed)):
        open_ = previous if previous is not None else close
        rows.append(candle(stamp, open_, close, timeframe))
        previous = close
    return tuple(rows)


def consecutive(prices, timeframe, start=TUESDAY, *, opens=None):
    """Candles from closes (and optional distinct opens) on consecutive slots."""
    step = duration(timeframe)
    opens = opens or prices
    return tuple(candle(start + i * step, o, c, timeframe) for i, (o, c) in enumerate(zip(opens, prices)))


def sequential_targets(strategy, candles):
    """Decision after every complete window, feeding back the previous target."""
    position, targets = None, []
    for end in range(strategy.warmup, len(candles) + 1):
        position = strategy.target(candles[end - strategy.warmup:end], position)
        targets.append((candles[end - 1].end, position))
    return targets


class SignalRuleTests(unittest.TestCase):
    def test_price_side_and_moving_average_manual_values(self):
        self.assertEqual(signals.price_side(2, 1), "BUY")
        self.assertEqual(signals.price_side(1, 2), "SELL")
        self.assertIsNone(signals.price_side(1.5, 1.5))
        self.assertEqual(signals.price_average([1.0, 2.0, 3.0, 6.0]), 3.0)
        self.assertEqual(signals.moving_average_side([1.0, 2.0, 3.0, 6.0]), "BUY")   # 6 > 3
        self.assertEqual(signals.moving_average_side([6.0, 5.0, 4.0, 1.0]), "SELL")  # 1 < 4
        self.assertIsNone(signals.moving_average_side([1.0, 3.0, 2.0]))              # 2 == 2

    def test_sma_side_manual_values(self):
        prices = [1.0] * 12 + [2.0] * 9  # SMA9 = 2, SMA21 = (12 + 18) / 21
        self.assertEqual(signals.sma_side(prices, 9, 21), "BUY")
        self.assertEqual(signals.sma_side(prices[::-1], 9, 21), "SELL")
        self.assertIsNone(signals.sma_side([1.5] * 21, 9, 21))

    def test_two_ema_signal_matches_independent_reference(self):
        for seed in range(12):
            prices = [v / 100000 for v in walk(60, seed)]
            with self.subTest(seed=seed):
                expected = "SELL" if ref.ema(prices, 10) < ref.ema(prices, 60) else "BUY"
                self.assertEqual(two_ema_signal(prices, 60, 10), expected)
        flat = [1.1] * 30
        self.assertEqual(two_ema_signal(flat, 30, 5), "BUY")  # Equal EMAs are BUY online.

    def test_multiple_signal_matches_independent_reference_and_all_outcomes_occur(self):
        seen = set()
        for seed in range(60):
            prices = [v / 100000 for v in walk(MULTIPLE_HISTORY, seed)]
            with self.subTest(seed=seed):
                self.assertEqual(multiple_signal(prices), ref.multiple_signal(prices))
            seen.add(multiple_signal(prices))
        self.assertEqual(seen, {"BUY", "SELL", "Neutral"})
        rising = [1 + i / 1000 for i in range(MULTIPLE_HISTORY)]
        self.assertEqual(multiple_signal(rising), "BUY")
        self.assertEqual(multiple_signal(rising[::-1]), "SELL")
        with self.assertRaises(ValueError):
            multiple_signal(rising[:199])

    def test_online_wrappers_still_compute_the_original_formulas(self):
        """calculate_two_ema_result/calculate_multiple, inlined as before the extraction."""

        def original_two_ema(prices, long_period, short_period):
            long_ema = calculate_ema(prices, long_period)[-1]
            short_ema = calculate_ema(prices, short_period)[-1]
            return "SELL" if short_ema < long_ema else "BUY"

        def original_multiple(prices):
            score = 0
            for period in [10, 20, 30, 50, 100, 200]:
                score += -1 if prices[-1] < calculate_sma(prices, period) else 1
            for period in [10, 20, 30, 50, 100, 200]:
                score += -1 if prices[-1] < calculate_ema(prices, period)[-1] else 1
            values = calculate_hma(prices, 27)
            score += 1 if values[-2] < values[-1] else -1
            return "BUY" if 1 < score else "SELL" if score < -1 else "Neutral"

        for seed in range(20):
            prices = [v / 100000 for v in walk(MULTIPLE_HISTORY, seed)]
            api = Mock()
            api.getHistoricalPricesList.side_effect = lambda symbol, tf, n: prices[-n:]
            with self.subTest(seed=seed):
                self.assertEqual(indicators.calculate_multiple(api, "EURUSD", "HOUR_4"),
                                 original_multiple(prices))
                self.assertEqual(api.getHistoricalPricesList.call_args.args, ("EURUSD", "HOUR_4", 210))
                self.assertEqual(indicators.calculate_two_ema_result(api, "EURUSD", "MINUTE_5", 200, 20),
                                 original_two_ema(prices[-200:], 200, 20))
                self.assertEqual(api.getHistoricalPricesList.call_args.args, ("EURUSD", "MINUTE_5", 200))

    def test_position_rules(self):
        for side, position, expected in (("BUY", None, "BUY"), ("SELL", "BUY", "SELL"),
                                         ("BUY", "BUY", "BUY"), (None, "SELL", "SELL"),
                                         (None, None, None)):
            self.assertEqual(signals.reversal_target(side, position), expected)
        for trend, trigger, position, expected in (
                ("BUY", "BUY", None, "BUY"), ("SELL", "SELL", None, "SELL"),
                ("BUY", "SELL", "BUY", None), ("SELL", "BUY", None, None),
                ("Neutral", "BUY", "BUY", None),
                ("SELL", "SELL", "BUY", "BUY"),  # Agreement holds even an opposite position.
                ("BUY", "BUY", "BUY", "BUY")):
            self.assertEqual(signals.agreement_target(trend, trigger, position), expected)

    def test_online_defaults_come_from_the_shared_module(self):
        modules = {name: importlib.import_module(f"trading_bot.strategies.{name}")
                   for name in ("moving_average", "sma", "ema_hma", "multiple_hma")}
        self.assertEqual((modules["sma"].FAST_PERIOD, modules["sma"].SLOW_PERIOD), (9, 21))
        ema = modules["ema_hma"]
        self.assertEqual((ema.DEFAULT_LONG_PERIOD, ema.DEFAULT_SHORT_PERIOD, ema.DEFAULT_HMA_PERIOD,
                          ema.DEFAULT_TIME_FRAME), (200, 20, 16, "MINUTE_5"))
        multiple = modules["multiple_hma"]
        self.assertEqual((multiple.DEFAULT_HMA_PERIOD, multiple.DEFAULT_SHORT_TERM,
                          multiple.DEFAULT_LONG_TERM), (20, "MINUTE_30", "HOUR_4"))
        self.assertIs(modules["moving_average"].price_average, signals.price_average)
        for key, strategy in (("moving-average", MovingAverageStrategy()), ("sma", SmaCrossoverStrategy()),
                              ("ema-hma", EmaHmaStrategy()), ("multiple-hma", MultipleHmaStrategy())):
            adapter = ADAPTERS[key]
            self.assertEqual(adapter.values(adapter.build()), adapter.values(strategy))
        self.assertEqual((ADAPTERS["moving-average"].time_frame, ADAPTERS["sma"].time_frame,
                          ADAPTERS["ema-hma"].time_frame, ADAPTERS["multiple-hma"].time_frame),
                         ("MINUTE", "MINUTE", "MINUTE_5", "MINUTE_30"))


class StrategyTests(unittest.TestCase):
    def test_warmups(self):
        self.assertEqual(MovingAverageStrategy().warmup, 20)
        self.assertEqual(SmaCrossoverStrategy().warmup, 21)
        self.assertEqual(EmaHmaStrategy().warmup, 200)
        self.assertEqual(EmaHmaStrategy(long_period=30, hma_period=16).warmup, 48)
        self.assertEqual(MultipleHmaStrategy().warmup, 8 * 212)
        self.assertEqual(MultipleHmaStrategy(long_term="HOUR", time_frame="MINUTE_30").warmup, 2 * 212)
        self.assertEqual(MultipleHmaStrategy(hma_period=200, long_term="HOUR",
                                             time_frame="MINUTE_30").warmup, 600)

    def test_parameter_validation(self):
        invalid = (
            lambda: MovingAverageStrategy(1), lambda: MovingAverageStrategy(2.0),
            lambda: SmaCrossoverStrategy(21, 21), lambda: SmaCrossoverStrategy(30, 21),
            lambda: SmaCrossoverStrategy(0, 21), lambda: EmaHmaStrategy(20, 20),
            lambda: EmaHmaStrategy(hma_period=1), lambda: EmaHmaStrategy(long_period=1),
            lambda: MultipleHmaStrategy(hma_period=1),
            lambda: MultipleHmaStrategy(long_term="MINUTE_30"),  # Not longer.
            lambda: MultipleHmaStrategy(long_term="MINUTE_15"),  # Shorter.
            lambda: MultipleHmaStrategy(long_term="HOUR", time_frame="HOUR"),
            lambda: MultipleHmaStrategy(long_term="WEEK"),
        )
        for index, build in enumerate(invalid):
            with self.subTest(case=index), self.assertRaises(ValueError):
                build()
        MultipleHmaStrategy(long_term="HOUR", time_frame="MINUTE_15")
        MultipleHmaStrategy(long_term="HOUR_4", time_frame="MINUTE_5")
        SmaCrossoverStrategy(1, 2)

    def test_moving_average_uses_ask_opens_and_reverses(self):
        s = MovingAverageStrategy(4)
        rising_opens = consecutive([100, 100, 100, 100], "MINUTE", opens=[100, 100, 100, 104])
        self.assertEqual(s.target(rising_opens, None), "BUY")       # 104 > 101.
        self.assertEqual(s.target(rising_opens, "SELL"), "BUY")
        falling = consecutive([200, 200, 200, 200], "MINUTE", opens=[104, 104, 104, 100])
        self.assertEqual(s.target(falling, "BUY"), "SELL")          # Reversal on the open.
        flat = consecutive([150] * 4, "MINUTE", opens=[100, 101, 102, 101])
        self.assertEqual(s.target(flat, "SELL"), "SELL")            # Equal: hold.
        self.assertIsNone(s.target(flat, None))

    def test_sma_crossover_transitions(self):
        s = SmaCrossoverStrategy(2, 4)
        up = consecutive([200] * 4, "MINUTE", opens=[100, 100, 104, 104])   # SMA2 104 > SMA4 102
        down = consecutive([200] * 4, "MINUTE", opens=[104, 104, 100, 100])
        flat = consecutive([200] * 4, "MINUTE", opens=[102] * 4)
        self.assertEqual(s.target(up, None), "BUY")
        self.assertEqual(s.target(up, "BUY"), "BUY")
        self.assertEqual(s.target(down, "BUY"), "SELL")
        self.assertEqual(s.target(flat, "SELL"), "SELL")

    def test_ema_hma_agreement_rules_with_reference_values(self):
        s = EmaHmaStrategy(long_period=12, short_period=4, hma_period=4)
        for seed in range(30):
            closes = walk(12, seed)
            window = consecutive(closes, "MINUTE_5")
            prices = [float(c.prices.close) for c in window]
            trend = "SELL" if ref.ema(prices, 4) < ref.ema(prices, 12) else "BUY"
            previous, current = ref.hma_tail(prices, 4)
            trigger = "BUY" if previous < current else "SELL"
            for position in (None, "BUY", "SELL"):
                with self.subTest(seed=seed, position=position):
                    expected = None if trend != trigger else (position or trigger)
                    self.assertEqual(s.target(window, position), expected)

    def test_strategies_only_read_the_supplied_window(self):
        """Engine decisions on a prefix equal those on the full series (no look-ahead)."""
        cases = ((MovingAverageStrategy(), consecutive(walk(200, 1), "MINUTE")),
                 (SmaCrossoverStrategy(), consecutive(walk(200, 2), "MINUTE")),
                 (EmaHmaStrategy(long_period=40, short_period=10, hma_period=6),
                  consecutive(walk(200, 3), "MINUTE_5")))
        for strategy, candles in cases:
            start, end = candles[strategy.warmup].timestamp, candles[-1].end
            full = run(candles, strategy, SimulationConfig(), start=start, end=end)
            cut = candles[:150]
            prefix = run(cut, strategy, SimulationConfig(), start=start, end=cut[-1].end)
            with self.subTest(strategy=strategy.name):
                self.assertEqual(prefix.decisions, full.decisions[:len(prefix.decisions)])
                self.assertGreater(len({d["target"] for d in full.decisions}), 1)

    def test_engine_executes_the_sequential_decisions_at_the_next_open(self):
        strategy, candles = SmaCrossoverStrategy(), consecutive(walk(240, 4), "MINUTE")
        start = candles[strategy.warmup].timestamp
        result = run(candles, strategy, SimulationConfig(), start=start, end=candles[-1].end)
        expected = [t for t in sequential_targets(strategy, candles) if t[0] > start]
        self.assertEqual([(d["timestamp"], d["target"]) for d in result.decisions], expected)
        opens = [e for e in result.ledger.events if e["action"] == "OPEN"]
        self.assertGreater(len(opens), 2)
        decided = dict(sequential_targets(strategy, candles))
        for event in opens:  # Filled at the open right after the deciding close.
            self.assertEqual(decided[event["timestamp"]], event["direction"])
            fill = next(c for c in candles if c.timestamp == event["timestamp"])
            self.assertEqual(event["price"], fill.ask.open if event["direction"] == "BUY" else fill.bid.open)
        sides = [e["direction"] for e in opens]
        self.assertTrue(all(a != b for a, b in zip(sides, sides[1:])))  # Strict reversals.


def thirty(start, count, *, skip=()):
    """``count`` 30m candles on expected FX slots from ``start``; ``skip`` slots are missing."""
    rows, stamp = [], start
    values = iter(walk(count * 2, 9))
    while len(rows) < count:
        if slot_expected(stamp, timedelta(minutes=30)) and stamp not in skip:
            value = next(values)
            rows.append(candle(stamp, value, value + 3, "MINUTE_30"))
        elif stamp in skip:
            next(values)
        stamp += timedelta(minutes=30)
    return tuple(rows)


class MultiTimeframeTests(unittest.TestCase):

    def test_only_closed_buckets_are_available_at_each_decision(self):
        candles = thirty(TUESDAY, 24)  # 00:00-12:00 on a Tuesday.
        at = lambda hour: TUESDAY + timedelta(hours=hour)
        upto = lambda hour: tuple(c for c in candles if c.end <= at(hour))
        # At 03:30 the 00:00-04:00 bucket is still in progress: nothing is available.
        self.assertEqual(completed_groups(upto(3.5), "HOUR_4", as_of=at(3.5)), ())
        # At 04:00 (close of the 03:30 candle) it has closed and is available.
        groups = completed_groups(upto(4), "HOUR_4", as_of=at(4))
        self.assertEqual([g[0].timestamp for g in groups], [at(0)])
        self.assertEqual(len(groups[0]), 8)
        # At 07:30 only the first bucket; at 08:00 both.
        self.assertEqual(len(completed_groups(upto(7.5), "HOUR_4", as_of=at(7.5))), 1)
        self.assertEqual(len(completed_groups(upto(8), "HOUR_4", as_of=at(8))), 2)
        with self.assertRaises(DataError):  # A candle ending after the decision time.
            completed_groups(upto(8), "HOUR_4", as_of=at(7.5))
        with self.assertRaises(DataError):
            completed_groups(upto(8)[::-1], "HOUR_4", as_of=at(8))

    def test_aggregates_match_resample_and_close_is_the_last_constituent(self):
        candles = thirty(TUESDAY, 48)
        as_of = candles[-1].end
        self.assertEqual(completed_aggregates(candles, "HOUR_4", as_of=as_of),
                         resample(candles, "HOUR_4", completed_before=as_of))
        for members, aggregate in zip(completed_groups(candles, "HOUR_4", as_of=as_of),
                                      completed_aggregates(candles, "HOUR_4", as_of=as_of)):
            self.assertEqual(aggregate.prices.close, members[-1].prices.close)
            self.assertEqual(aggregate.prices.open, members[0].prices.open)

    def test_truncated_and_missing_buckets_are_excluded_never_filled(self):
        candles = thirty(TUESDAY, 48)
        # The window starts at 01:00: the 00:00-04:00 bucket is cut off.
        cut = candles[2:]
        groups = completed_groups(cut, "HOUR_4", as_of=cut[-1].end)
        self.assertEqual(groups[0][0].timestamp, TUESDAY + timedelta(hours=4))
        # A missing 05:30 candle removes the 04:00-08:00 bucket only.
        gap = thirty(TUESDAY, 47, skip={TUESDAY + timedelta(hours=5, minutes=30)})
        stamps = [g[0].timestamp for g in completed_groups(gap, "HOUR_4", as_of=gap[-1].end)]
        self.assertNotIn(TUESDAY + timedelta(hours=4), stamps)
        self.assertIn(TUESDAY, stamps)
        self.assertIn(TUESDAY + timedelta(hours=8), stamps)

    def test_weekend_edge_buckets_are_complete_without_closed_slots(self):
        friday = datetime(2026, 9, 18, 16, tzinfo=timezone.utc)  # NY close 21:00 UTC (EDT).
        candles = thirty(friday, 60)
        stamps = {floor_time(g[0].timestamp, "HOUR_4"): len(g)
                  for g in completed_groups(candles, "HOUR_4", as_of=candles[-1].end)}
        self.assertEqual(stamps[datetime(2026, 9, 18, 20, tzinfo=timezone.utc)], 2)   # 20:00, 20:30
        self.assertEqual(stamps[datetime(2026, 9, 20, 20, tzinfo=timezone.utc)], 6)   # Sunday 21:00-
        self.assertNotIn(datetime(2026, 9, 19, 0, tzinfo=timezone.utc), stamps)        # Closed.

    def test_multiple_hma_long_term_series_is_causal(self):
        s = MultipleHmaStrategy(hma_period=4, long_term="HOUR", time_frame="MINUTE_30")
        candles = thirty(TUESDAY - timedelta(days=21), s.warmup + 40)
        checked = False
        for end in range(s.warmup, len(candles)):
            window = candles[end - s.warmup:end]
            as_of = window[-1].end
            closes = s.long_term_closes(window)
            # Reference: complete hourly buckets of the whole history, ended by as_of.
            hourly = [g for g in completed_groups(candles[:end], "HOUR", as_of=as_of)]
            self.assertEqual(closes, [float(g[-1].prices.close) for g in hourly[-MULTIPLE_HISTORY:]])
            self.assertEqual(len(closes), MULTIPLE_HISTORY)
            # The newest candle may open an hour that is still in progress: it is
            # visible to the 30m HMA but must not reach the long-term series.
            newest = window[-1]
            if newest.end.minute == 30:
                moved = window[:-1] + (candle(newest.timestamp, 50000, 99999, "MINUTE_30"),)
                self.assertEqual(s.long_term_closes(moved), closes)
                checked = True
        self.assertTrue(checked)

    def test_multiple_hma_holds_until_enough_complete_long_term_buckets(self):
        s = MultipleHmaStrategy(hma_period=4, long_term="HOUR", time_frame="MINUTE_30")
        candles = thirty(TUESDAY - timedelta(days=21), s.warmup)
        # Remove five candles in different hours: five buckets fewer than 210 remain.
        sparse = tuple(c for i, c in enumerate(candles) if i not in {50, 90, 130, 170, 210})
        window = candles[-s.warmup:]
        self.assertEqual(len(s.long_term_closes(window)), MULTIPLE_HISTORY)
        self.assertLess(len(s.long_term_closes(sparse[-(s.warmup - 5):])), MULTIPLE_HISTORY)
        for position in (None, "BUY", "SELL"):
            self.assertEqual(s.target(sparse[-(s.warmup - 5):], position), position)
        with self.assertRaises(ValueError):
            s.target(consecutive([1] * s.warmup, "HOUR"), None)  # Wrong signal timeframe.

    def test_multiple_hma_decision_uses_reference_vote(self):
        s = MultipleHmaStrategy(hma_period=4, long_term="HOUR", time_frame="MINUTE_30")
        candles = thirty(TUESDAY - timedelta(days=21), s.warmup + 60)
        outcomes = set()
        for end in range(s.warmup, len(candles) + 1):
            window = candles[end - s.warmup:end]
            closes = s.long_term_closes(window)
            trend = ref.multiple_signal(closes)
            short = [float(c.prices.close) for c in window[-12:]]
            previous, current = ref.hma_tail(short, 4)
            trigger = "BUY" if previous < current else "SELL"
            for position in (None, "BUY"):
                expected = None if trend != trigger else (position or trigger)
                self.assertEqual(s.target(window, position), expected)
            outcomes.add(trend == trigger)
        self.assertEqual(outcomes, {True, False})  # Both agreement and disagreement occur.


class FakeBroker:
    """Mocked online API serving prices as of a test-controlled clock. No network.

    Orders are accepted at once: each returns its own deal reference, whose broker
    confirmation names the opened deal, as the shared execution's DEMO confirmation
    lifecycle requires (issue #9).
    """

    def __init__(self, frames, clock):
        self.frames, self.clock = frames, clock
        self.positions, self.orders, self.calls = [], [], 0
        self.confirmations = {}

    def candles(self, timeframe, count):
        series = self.frames[timeframe](self.clock["k"])
        return series[-count:]

    def getHistoricalPrices(self, symbol, timeframe, count):
        self.calls += 1
        rows = self.candles(timeframe, count)
        return {"prices": [{"openPrice": {"ask": float(c.prices.open)},
                            "closePrice": {"ask": float(c.prices.close)}} for c in rows]}, 200

    def getHistoricalPricesList(self, symbol, timeframe, count):
        return [float(c.prices.close) for c in self.candles(timeframe, count)]

    def prepare_trading_cycle(self):
        return False

    def is_market_open(self):
        return True

    def configure_read_retries(self, policy):
        pass

    def getAllPositionsList(self):
        return list(self.positions)

    def getAllPositions(self):
        return {"positions": [{"market": {"epic": p.epic}, "position": {"dealId": p.dealId}}
                              for p in self.positions]}, 200

    def getPositionOrderConfirmation(self, reference):
        return self.confirmations.get(reference)

    def _reference(self, **confirmation):
        reference = f"ref-{len(self.orders)}"
        self.confirmations[reference] = {"dealReference": reference, "dealStatus": "ACCEPTED",
                                         **confirmation}
        return {"dealReference": reference}, 200

    def createPosition(self, symbol, direction, size):
        self.orders.append((self.calls, self.clock["k"], direction))
        deal = f"d{len(self.orders)}"
        self.positions.append(Mock(epic=symbol, dealId=deal, direction=direction))
        return self._reference(epic=symbol, direction=direction, size=size,
                               affectedDeals=[{"dealId": deal, "status": "OPENED"}])

    def closePosition(self, deal_id):
        self.orders.append((self.calls, self.clock["k"], "CLOSE"))
        self.positions = [p for p in self.positions if p.dealId != deal_id]
        return self._reference(affectedDeals=[{"dealId": deal_id, "status": "FULLY_CLOSED"}])

    def side(self):
        return self.positions[0].direction if self.positions else None


class OnlineEquivalenceTests(unittest.TestCase):
    """The unchanged online loops and the backtest strategies decide identically."""

    def drive(self, module_name, broker, argv, stop, **patches):
        module = importlib.import_module(f"trading_bot.strategies.{module_name}")
        with ExitStack() as stack:
            stack.enter_context(patch("sys.argv", [module_name, *argv]))
            stack.enter_context(patch.object(module, "create_trading_api", return_value=broker))
            stack.enter_context(redirect_stdout(io.StringIO()))
            for name, value in patches.items():
                stack.enter_context(patch.object(module, name, value))
            if "wait_until_targets" not in patches:
                stack.enter_context(patch.object(module.time, "sleep", side_effect=stop))
            with self.assertRaises(StopLoop):
                module.main()

    def test_sma_loop(self):
        """Shared execution (issue #9): each boundary's 21 newest opens decide as the
        backtest does, until the first reversal stops trading (no opposite order, #10)."""
        from trading_bot.execution.single_position import ExecutionHalted

        candles = consecutive(walk(160, 11), "MINUTE")
        clock = {"k": 19}
        broker = FakeBroker({"MINUTE": lambda k: candles[:k + 1]}, clock)
        seen = []

        def record(prices, fast, slow):
            seen.append((list(prices), broker.side()))
            return signals.sma_side(prices, fast, slow)

        def wait(*args, **kwargs):
            clock["k"] += 1
            if clock["k"] >= len(candles):
                raise StopLoop

        module = importlib.import_module("trading_bot.strategies.sma")
        with patch("sys.argv", ["sma"]), \
                patch.object(module, "create_trading_api", return_value=broker), \
                patch.object(module, "wait_until_targets", side_effect=wait), \
                patch.object(module, "sma_side", side_effect=record), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                self.assertRaises(ExecutionHalted) as halted:
            module.main()
        self.assertEqual(halted.exception.reason, "reversal_requires_confirmed_close")
        strategy = SmaCrossoverStrategy()
        for (prices, before), (k, (_, after)) in zip(seen, enumerate(seen[1:] + [(None, broker.side())])):
            # The window is exactly the 21 newest candle opens at that boundary.
            self.assertEqual(prices, [float(c.prices.open) for c in candles[k:k + 21]])
            window = consecutive([round(p / float(PIP)) for p in prices], "MINUTE")
            if k < len(seen) - 1:
                self.assertEqual(strategy.target(window, before), after, k)
        prices, before = seen[-1]
        window = consecutive([round(p / float(PIP)) for p in prices], "MINUTE")
        self.assertNotIn(strategy.target(window, before), (before, None))  # The refused reversal.
        self.assertEqual([side for _, _, side in broker.orders], [before])
        self.assertGreater(len(seen), 3)

    def test_moving_average_loop_after_its_startup_wait(self):
        """Shared execution (issue #9): every decision matches the backtest rule until the
        first reversal, which stops trading instead of sending an opposite order (#10)."""
        from trading_bot.execution.single_position import ExecutionHalted

        candles = consecutive(walk(160, 12), "MINUTE")
        clock = {"k": 19}
        broker = FakeBroker({"MINUTE": lambda k: candles[:k + 1]}, clock)
        seen = []

        def record(prices):
            seen.append((list(prices), broker.side()))
            return signals.moving_average_side(prices)

        def wait(*args, **kwargs):
            clock["k"] += 1
            if clock["k"] >= len(candles):
                raise StopLoop

        module = importlib.import_module("trading_bot.strategies.moving_average")
        with patch("sys.argv", ["moving_average"]), \
                patch.object(module, "create_trading_api", return_value=broker), \
                patch.object(module, "wait_until_targets", side_effect=wait), \
                patch.object(module, "moving_average_side", side_effect=record), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                self.assertRaises(ExecutionHalted) as halted:
            module.main()
        self.assertEqual(halted.exception.reason, "reversal_requires_confirmed_close")
        strategy = MovingAverageStrategy()
        statuses = [s for _, s in seen[1:]] + [broker.side()]
        for (prices, before), after in zip(seen[:-1], statuses):
            window = consecutive([round(p / float(PIP)) for p in prices], "MINUTE")
            self.assertEqual(strategy.target(window, before), after)
        # The last decision was the reversal the backtest makes; online it was refused.
        prices, before = seen[-1]
        window = consecutive([round(p / float(PIP)) for p in prices], "MINUTE")
        self.assertNotIn(strategy.target(window, before), (before, None))
        self.assertEqual([side for _, _, side in broker.orders], [before])  # One entry, no reversal.
        self.assertGreater(len(seen), 3)
        # The startup wait: the first windows decided nothing (no order before the crossing).
        self.assertGreater(broker.orders[0][1], 20)

    def run_scheduled(self, module_name, frames, argv, cycles, first):
        clock = {"k": first - 1}
        broker = FakeBroker(frames, clock)
        after = []

        def wait(*args, **kwargs):
            if clock["k"] >= first:
                after.append(broker.side())
            if clock["k"] - first + 1 >= cycles:
                raise StopLoop
            clock["k"] += 1

        self.drive(module_name, broker, argv, None, wait_until_targets=wait)
        return after, broker

    def test_ema_hma_loop(self):
        candles = consecutive(walk(260, 21), "MINUTE_5")
        strategy = EmaHmaStrategy(long_period=40, short_period=8, hma_period=6)
        first = strategy.warmup - 1
        cycles = len(candles) - first
        online, broker = self.run_scheduled(
            "ema_hma", {"MINUTE_5": lambda k: candles[:k + 1]},
            ["--long_period", "40", "--short_period", "8", "--hma_period", "6",
             "--time_frame", "MINUTE_5"], cycles, first)
        expected = [t for _, t in sequential_targets(strategy, candles)]
        self.assertEqual(online, expected[:len(online)])
        self.assertEqual(len(online), cycles)
        self.assertGreater(sum(1 for _, _, side in broker.orders if side == "CLOSE"), 2)

    def test_multiple_hma_loop_with_completed_long_term_candles(self):
        strategy = MultipleHmaStrategy(hma_period=6, long_term="HOUR_4", time_frame="MINUTE_30")
        history = thirty(TUESDAY - timedelta(days=60), strategy.warmup + 120)

        def long_term(k):
            # What a broker serving only completed 4H candles would return at this cycle.
            groups = completed_groups(history[:k + 1], "HOUR_4", as_of=history[k].end)
            return [Mock(prices=Mock(close=g[-1].prices.close)) for g in groups]

        first = strategy.warmup - 1
        cycles = len(history) - first
        online, broker = self.run_scheduled(
            "multiple_hma", {"MINUTE_30": lambda k: history[:k + 1], "HOUR_4": long_term},
            ["--hma_period", "6", "--short_term", "MINUTE_30", "--long_term", "HOUR_4"], cycles, first)
        expected = [t for _, t in sequential_targets(strategy, history)]
        self.assertEqual(online, expected[:len(online)])
        self.assertGreater(len(set(online)), 1)
        self.assertTrue(any(side == "CLOSE" for _, _, side in broker.orders))


class FakeProvider:
    """Mocked public-data provider: serves a fixed generated series for any range."""

    def __init__(self, series):
        self.series, self.calls = series, 0

    def fetch(self, symbol, timeframe, start, end):
        self.calls += 1
        rows = tuple(c for c in self.series if c.timeframe == timeframe
                     and start <= c.timestamp and c.end <= end)
        return Dataset(rows, start, end, "dukascopy", symbol, timeframe,
                       {"source_resolution": timeframe, "complete_provider_buckets": True,
                        "no_quote_ranges": []})


class CliTests(unittest.TestCase):
    START = datetime(2026, 9, 15, tzinfo=timezone.utc)
    END = datetime(2026, 9, 15, 8, tzinfo=timezone.utc)

    def plan(self, *argv):
        args = cli.build_parser().parse_args(list(argv))
        return runner.plan(cli.request_from_args(args), now=NOW)

    def test_defaults(self):
        expected = {
            "moving-average": ("MINUTE", {"average_period": 20}),
            "sma": ("MINUTE", {"fast_period": 9, "slow_period": 21}),
            "ema-hma": ("MINUTE_5", {"long_period": 200, "short_period": 20, "hma_period": 16}),
            "multiple-hma": ("MINUTE_30", {"hma_period": 20, "long_term": "HOUR_4"}),
        }
        for key, (time_frame, parameters) in expected.items():
            with self.subTest(strategy=key):
                p = self.plan("--strategy", key)
                self.assertEqual((p.time_frame, p.data_source, p.execution.name),
                                 (time_frame, "dukascopy", "continuous"))
                self.assertEqual(p.adapter.values(p.strategy), parameters)
                self.assertEqual(p.execution.config.size, 100)
                self.assertEqual((p.end - p.start).days, 30)

    def test_shared_hma_period_default_depends_on_strategy(self):
        self.assertEqual(self.plan("--strategy", "hma").strategy.period, 16)
        self.assertEqual(self.plan("--strategy", "ema-hma").strategy.hma_period, 16)
        self.assertEqual(self.plan("--strategy", "multiple-hma").strategy.hma_period, 20)
        self.assertEqual(self.plan("--strategy", "multiple-hma", "--hma_period", "12").strategy.hma_period, 12)
        self.assertEqual(self.plan("--strategy", "ema-hma", "--long_period", "100",
                                   "--short-period", "10").strategy.long_period, 100)

    def test_overrides_including_the_signal_timeframe(self):
        p = self.plan("--strategy", "multiple-hma", "--time-frame", "MINUTE_15", "--long-term", "HOUR")
        self.assertEqual((p.strategy.time_frame, p.strategy.long_term, p.strategy.warmup),
                         ("MINUTE_15", "HOUR", 4 * 212))
        p = self.plan("--strategy", "sma", "--time-frame", "MINUTE_5", "--fast-period", "5",
                      "--slow-period", "30", "--size", "200", "--initial-balance", "5000",
                      "--leverage", "2", "--commission-per-order", "0.1", "--slippage-pips", "0.2",
                      "--financing-usd-per-unit-day", "0.0001", "--data-source", "yfinance",
                      "--lookback-days", "5")
        self.assertEqual((p.time_frame, p.data_source, p.strategy.fast_period, p.strategy.slow_period),
                         ("MINUTE_5", "yfinance", 5, 30))
        c = p.execution.config
        self.assertEqual((c.size, c.initial_balance, c.leverage, c.commission_per_order, c.slippage_pips),
                         (200, 5000, 2, D("0.1"), D("0.2")))

    def invoke(self, argv):
        with ExitStack() as stack:
            fetch = stack.enter_context(patch("trading_bot.backtesting.runner.load_history"))
            snap = stack.enter_context(patch("trading_bot.backtesting.runner.load_snapshot"))
            stack.enter_context(redirect_stdout(io.StringIO()))
            err = stack.enter_context(redirect_stderr(io.StringIO()))
            try:
                code = cli.main(argv, now=NOW)
            except SystemExit as exc:
                code = exc.code
        fetch.assert_not_called()
        snap.assert_not_called()
        return code, err.getvalue()

    def test_inapplicable_and_invalid_parameters_fail_before_data_access(self):
        usage = (["--strategy", "sma", "--hma-period", "5"], ["--strategy", "moving-average", "--direction", "SELL"],
                 ["--strategy", "ema-hma", "--long-term", "HOUR"], ["--strategy", "hma", "--fast-period", "3"],
                 ["--strategy", "donchian", "--average-period", "5"], ["--strategy", "grid", "--long-period", "50"],
                 ["--strategy", "multiple-hma", "--long-term", "WEEK"], ["--strategy", "sma", "--grid-lower", "1"],
                 ["--strategy", "sma", "--cost-case", "high"], ["--strategy", "ema-hma", "--execution-model", "grid"],
                 ["--strategy", "moving-average", "--REAL"], ["--strategy", "multiple-hma", "--cycles", "2"],
                 ["--strategy", "sma", "--level-size", "5"])
        for argv in usage:
            with self.subTest(argv=argv):
                self.assertEqual(self.invoke(argv)[0], 2)
        values = (["--strategy", "sma", "--fast-period", "21"], ["--strategy", "moving-average", "--average-period", "1"],
                  ["--strategy", "ema-hma", "--short-period", "200"], ["--strategy", "ema-hma", "--hma-period", "1"],
                  ["--strategy", "multiple-hma", "--long-term", "MINUTE_15"],
                  ["--strategy", "multiple-hma", "--time-frame", "HOUR_4"])
        for argv in values:
            with self.subTest(argv=argv):
                code, err = self.invoke(argv)
                self.assertEqual(code, 1)
                self.assertIn("Backtest failed", err)

    def run_cli(self, argv, series, root):
        provider = FakeProvider(series)
        with ExitStack() as stack:
            stack.enter_context(patch("trading_bot.backtesting.data.provider", return_value=provider))
            out = stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            output = Path(root, "out", str(len(list(Path(root).glob("out/*")))))
            code = cli.main([*argv, "--cache-dir", str(Path(root, "cache")), "--output-dir", str(output),
                             "--start", self.START.isoformat(), "--end", self.END.isoformat()], now=NOW)
        report = json.loads(next(output.glob("*/report.json")).read_text())
        return code, out.getvalue(), report, output

    def series(self, timeframe, days=10):
        return session_series(self.START - timedelta(days=days), self.END, timeframe)

    def test_end_to_end_reports_for_every_new_strategy(self):
        cases = (("moving-average", ["--strategy", "moving-average"], "MINUTE"),
                 ("sma", ["--strategy", "sma"], "MINUTE"),
                 ("ema-hma", ["--strategy", "ema-hma"], "MINUTE_5"),
                 ("multiple-hma", ["--strategy", "multiple-hma", "--time-frame", "MINUTE_15",
                                   "--long-term", "HOUR", "--hma-period", "6"], "MINUTE_15"))
        days = {"MINUTE_15": 25}
        with tempfile.TemporaryDirectory() as root:
            for key, argv, timeframe in cases:
                with self.subTest(strategy=key):
                    series = self.series(timeframe, days.get(timeframe, 10))
                    code, text, report, folder = self.run_cli(argv, series, root)
                    self.assertEqual(code, 0, text)
                    self.assertEqual((report["strategy_key"], report["timeframe"], report["execution_model"]),
                                     (key, timeframe, "continuous"))
                    self.assertEqual(report["parameters"], ADAPTERS[key].values(
                        runner.plan(cli.request_from_args(cli.build_parser().parse_args(argv)),
                                    now=NOW).strategy))
                    self.assertGreater(report["statistics"]["total_trades"], 0)
                    self.assertEqual(report["reproducibility"]["configuration"]["strategy"], key)
                    self.assertEqual(len(report["reproducibility"]["data_sha256"]), 64)
                    self.assertIn("All orders are simulated", text)
                    self.assertIn("Environment:             DEMO (simulated only)", text)
                    for name in ("trades.csv", "trade_details.csv", "equity.csv", "equity.html"):
                        self.assertTrue(next(folder.iterdir()).joinpath(name).exists(), name)

    def test_determinism_and_cost_accounting(self):
        argv = ["--strategy", "sma", "--commission-per-order", "0.05", "--slippage-pips", "0.1",
                "--financing-usd-per-unit-day", "0.0002"]
        series = self.series("MINUTE")
        with tempfile.TemporaryDirectory() as root:
            _, _, first, _ = self.run_cli(argv, series, root)
            _, _, second, _ = self.run_cli(argv, series, root)
            _, _, free, _ = self.run_cli(["--strategy", "sma"], series, root)
        for report in (first, second, free):
            report["data"].pop("cache_hit")
        self.assertEqual(first, second)
        self.assertEqual(first["reproducibility"]["configuration_sha256"],
                         second["reproducibility"]["configuration_sha256"])
        stats, summary = first["statistics"], first["summary"]
        legs = stats["opened_trades"] + stats["total_trades"]
        self.assertGreater(legs, 4)
        self.assertEqual(D(summary["embedded_slippage"]), D("0.001") * legs)  # 0.1 pip x 100 units.
        financing = D(summary["modeled_cash_costs"]) - D("0.05") * legs
        self.assertGreater(financing, 0)
        self.assertEqual(first["unmodeled_costs"], [])
        self.assertEqual(set(free["unmodeled_costs"]),
                         {"commission_per_order", "slippage_pips", "financing_per_unit_day"})
        self.assertEqual(D(free["summary"]["modeled_cash_costs"]), 0)
        # Same signals, so the same trades; costs alone lower the result.
        self.assertEqual(stats["total_trades"], free["statistics"]["total_trades"])
        self.assertLess(D(stats["net_pnl"]), D(free["statistics"]["net_pnl"]))
        net = D(summary["gross_pnl"]) - D(summary["modeled_costs"]) - D(summary["open_valuation_spread"])
        # The engine reconciles to 1e-18; repeating-decimal financing rounds in JSON.
        self.assertLess(abs(net - D(summary["net_pnl"])), D("1e-18"))

    def test_backtest_never_loads_broker_modules(self):
        import subprocess
        import sys
        code = ("import sys\n"
                "from datetime import datetime, timezone\n"
                "from trading_bot.backtesting import cli, runner\n"
                "for key in ('moving-average', 'sma', 'ema-hma', 'multiple-hma'):\n"
                "    args = cli.build_parser().parse_args(['--strategy', key])\n"
                "    runner.plan(cli.request_from_args(args), now=datetime(2026, 11, 17, tzinfo=timezone.utc))\n"
                "bad = [m for m in sys.modules if m.startswith(('trading_bot.api', 'trading_bot.risk',"
                " 'trading_bot.config.trading_config', 'trading_bot.strategies.sma',"
                " 'trading_bot.strategies.moving_average', 'trading_bot.strategies.ema_hma',"
                " 'trading_bot.strategies.multiple_hma'))]\n"
                "assert not bad, bad\n")
        child = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                               timeout=60, cwd=tempfile.gettempdir())
        self.assertEqual(child.returncode, 0, child.stderr)


if __name__ == "__main__":
    unittest.main()

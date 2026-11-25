"""Candle adapters around the online strategies' pure signal rules.

Each class turns completed candles into the price lists the online strategy
reads, then applies the same rule functions that the online strategy calls
(``trading_bot.strategies.indicators`` and ``trading_bot.strategies.signals``).
``target`` sees only completed candles, oldest first; position-aware rules also
receive the current simulated direction. Nothing here touches a broker.
"""

from dataclasses import dataclass
from trading_bot.strategies import signals
from trading_bot.strategies.indicators import (
    MULTIPLE_HISTORY,
    hma_signal,
    multiple_signal,
    two_ema_signal,
)
from .calendar import completed_groups
from .timeframes import MINUTES


def _period(value, name, minimum):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")


def _ask_opens(candles):
    # The online polling loops read openPrice.ask; Candle.prices is the ask side
    # for bid/ask data and the indicative price otherwise.
    return [float(c.prices.open) for c in candles]


def _ask_closes(candles):
    # getHistoricalPricesList reads closePrice.ask.
    return [float(c.prices.close) for c in candles]


@dataclass(frozen=True)
class HmaStrategy:
    period: int = 16
    direction: str = "BUY"
    name: str = "HMA"

    def __post_init__(self):
        if type(self.period) is not int or self.period < 2:
            raise ValueError("HMA period must be an integer at least two")
        if self.direction not in ("BUY", "SELL"):
            raise ValueError("HMA direction must be BUY or SELL")

    @property
    def warmup(self):
        return self.period * 3

    def target(self, candles):
        signal = hma_signal([float(c.prices.close) for c in candles], self.period)
        return self.direction if signal == self.direction else None


@dataclass(frozen=True)
class MovingAverageStrategy:
    """moving_average.py: newest ask open against the mean of the last N opens."""

    average_period: int = signals.MOVING_AVERAGE_PERIOD
    name: str = "Moving average"
    position_aware = True

    def __post_init__(self):
        _period(self.average_period, "Moving-average period", 2)

    @property
    def warmup(self):
        return self.average_period

    def target(self, candles, position):
        opens = _ask_opens(candles[-self.average_period:])
        return signals.reversal_target(signals.moving_average_side(opens), position)


@dataclass(frozen=True)
class SmaCrossoverStrategy:
    """sma.py: SMA(fast) against SMA(slow) of the last ``slow_period`` ask opens."""

    fast_period: int = signals.SMA_FAST_PERIOD
    slow_period: int = signals.SMA_SLOW_PERIOD
    name: str = "SMA crossover"
    position_aware = True

    def __post_init__(self):
        _period(self.fast_period, "SMA fast period", 1)
        _period(self.slow_period, "SMA slow period", 2)
        if self.fast_period >= self.slow_period:
            raise ValueError("SMA fast period must be shorter than the slow period")

    @property
    def warmup(self):
        return self.slow_period

    def target(self, candles, position):
        opens = _ask_opens(candles[-self.slow_period:])
        side = signals.sma_side(opens, self.fast_period, self.slow_period)
        return signals.reversal_target(side, position)


@dataclass(frozen=True)
class EmaHmaStrategy:
    """ema_hma.py: EMA(short)/EMA(long) trend over exactly ``long_period`` ask
    closes and the HMA slope over the last ``3 * hma_period`` closes."""

    long_period: int = signals.EMA_HMA_LONG_PERIOD
    short_period: int = signals.EMA_HMA_SHORT_PERIOD
    hma_period: int = signals.EMA_HMA_HMA_PERIOD
    name: str = "EMA/HMA"
    position_aware = True

    def __post_init__(self):
        _period(self.long_period, "EMA long period", 2)
        _period(self.short_period, "EMA short period", 1)
        _period(self.hma_period, "HMA period", 2)
        if self.short_period >= self.long_period:
            raise ValueError("EMA short period must be shorter than the long period")

    @property
    def warmup(self):
        return max(self.long_period, 3 * self.hma_period)

    def target(self, candles, position):
        closes = _ask_closes(candles)
        trend = two_ema_signal(closes[-self.long_period:], self.long_period, self.short_period)
        trigger = hma_signal(closes[-3 * self.hma_period:], self.hma_period)
        return signals.agreement_target(trend, trigger, position)


@dataclass(frozen=True)
class MultipleHmaStrategy:
    """multiple_hma.py: the 13-vote multiple-indicator rule over the last 210
    completed ``long_term`` candles and the HMA slope of the signal timeframe.

    The long-term candles are aggregated from the completed signal-timeframe
    candles in the window. Only buckets that had fully closed by the decision
    time, with no missing constituent, are used. Until 210 such buckets exist,
    no decision is made and the current position is held, as the online loop
    does when the indicator raises for lack of history.
    """

    hma_period: int = signals.MULTIPLE_HMA_HMA_PERIOD
    long_term: str = signals.MULTIPLE_HMA_LONG_TERM
    time_frame: str = signals.MULTIPLE_HMA_SHORT_TERM
    name: str = "Multiple HMA"
    position_aware = True

    def __post_init__(self):
        _period(self.hma_period, "HMA period", 2)
        if self.long_term not in MINUTES or self.time_frame not in MINUTES:
            raise ValueError("Unsupported multiple-HMA timeframe")
        ratio, remainder = divmod(MINUTES[self.long_term], MINUTES[self.time_frame])
        if ratio < 2 or remainder:
            raise ValueError(
                f"--long-term {self.long_term} must be a whole multiple, at least twice, "
                f"of the signal --time-frame {self.time_frame}"
            )

    @property
    def ratio(self):
        return MINUTES[self.long_term] // MINUTES[self.time_frame]

    @property
    def warmup(self):
        # 210 complete long-term buckets, plus the bucket still in progress at
        # the newest close and the bucket cut off at the oldest one.
        return max(3 * self.hma_period, self.ratio * (MULTIPLE_HISTORY + 2))

    def long_term_closes(self, candles):
        """Closes of the newest 210 complete long-term candles: each bucket's close
        is the close of its last constituent, as in the OHLC aggregation."""
        groups = completed_groups(candles, self.long_term, as_of=candles[-1].end)
        return _ask_closes(members[-1] for members in groups[-MULTIPLE_HISTORY:])

    def target(self, candles, position):
        if candles[-1].timeframe != self.time_frame:
            raise ValueError("Candles do not match the configured signal timeframe")
        closes = self.long_term_closes(candles)
        if len(closes) < MULTIPLE_HISTORY:
            return position  # Online: the indicator raises; the cycle does nothing.
        trend = multiple_signal(closes)
        trigger = hma_signal(_ask_closes(candles[-3 * self.hma_period:]), self.hma_period)
        return signals.agreement_target(trend, trigger, position)

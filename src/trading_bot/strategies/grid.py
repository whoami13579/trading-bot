"""Pure candle-directed fixed-grid rules. No order, account or broker capability.

Direction: an EMA of completed signal closes (midpoint when bid/ask exists,
otherwise the source close). A close above the EMA selects the long grid, a
close below selects the short grid, and a close equal to the EMA keeps the
current direction (flat if none). Nothing is selected until the EMA is warm.

Grid: ``grid_intervals`` evenly spaced intervals between ``grid_lower`` and
``grid_upper`` give ``grid_intervals + 1`` fixed price levels, rounded to the
0.00001 EURUSD price tick. The levels never move during a run.

- Long grid: buy limits at levels strictly below the reference close; each
  filled lot exits with a sell limit one level higher.
- Short grid: sell limits at levels strictly above the reference close; each
  filled lot exits with a buy limit one level lower.

Bounds are either given explicitly (``GridStrategy``) or measured from the
completed candles before the start (``AutoGridStrategy``); either way the levels
are computed once and stay fixed for the whole run.

Order simulation, funding and exposure limits live in
``trading_bot.backtesting.grid``; this module only answers pure questions.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

TICK = Decimal("0.00001")
MAX_INTERVALS = 1000


def _decimal(value, name):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{name} must be a finite number") from None
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite number")
    return result


class Ema:
    """Incremental EMA seeded with the simple mean of the first ``period`` values.

    ``update`` returns None during warm-up (fewer than ``period`` values), then
    the EMA including the value just supplied. alpha = 2 / (period + 1).
    """

    def __init__(self, period):
        self.period = period
        self.alpha = Decimal(2) / (period + 1)
        self._seed = []
        self.value = None

    def update(self, value):
        if self.value is None:
            self._seed.append(value)
            if len(self._seed) == self.period:
                self.value = sum(self._seed, Decimal(0)) / self.period
                self._seed = []
        else:
            self.value += self.alpha * (value - self.value)
        return self.value


@dataclass(frozen=True)
class GridStrategy:
    grid_lower: Decimal
    grid_upper: Decimal
    ema_period: int = 50
    grid_intervals: int = 10
    level_size: Decimal = Decimal(100)
    max_position: Decimal = Decimal(500)
    name: str = "Grid"

    def __post_init__(self):
        if self.grid_lower is None or self.grid_upper is None:
            raise ValueError("Grid lower and upper prices are required")
        for field in ("grid_lower", "grid_upper", "level_size", "max_position"):
            object.__setattr__(self, field, _decimal(getattr(self, field), field))
        if type(self.ema_period) is not int or self.ema_period < 2:
            raise ValueError("EMA period must be an integer at least two")
        if type(self.grid_intervals) is not int or not 1 <= self.grid_intervals <= MAX_INTERVALS:
            raise ValueError(f"Grid intervals must be an integer from 1 to {MAX_INTERVALS}")
        if not 0 < self.grid_lower < self.grid_upper:
            raise ValueError("Grid prices require 0 < lower < upper")
        if self.level_size <= 0:
            raise ValueError("Level size must be positive")
        if self.max_position < self.level_size:
            raise ValueError("Maximum position must be at least one level size")
        levels = self.levels
        if levels[0] <= 0 or any(b <= a for a, b in zip(levels, levels[1:])):
            raise ValueError(
                "Grid levels must be distinct after rounding to the 0.00001 price tick; "
                "widen the range or use fewer intervals"
            )

    @property
    def warmup(self):
        """Completed candles before the EMA (and so any direction) exists."""
        return self.ema_period

    @property
    def levels(self):
        span = self.grid_upper - self.grid_lower
        return tuple(
            (self.grid_lower + span * i / self.grid_intervals).quantize(
                TICK, rounding=ROUND_HALF_EVEN
            )
            for i in range(self.grid_intervals + 1)
        )

    @staticmethod
    def signal_close(candle):
        """Midpoint close when bid/ask exists, otherwise the source close."""
        if candle.bid is not None:
            return (candle.bid.close + candle.ask.close) / 2
        return candle.prices.close

    @staticmethod
    def direction(close, ema, current=None):
        """BUY above the EMA, SELL below, keep ``current`` on equality or warm-up."""
        if ema is None:
            return None
        if close > ema:
            return "BUY"
        if close < ema:
            return "SELL"
        return current

    def target(self, candles, position=None):
        """Pure recomputation over ``candles`` (oldest first, all completed).

        Equivalent to the simulator's incremental decisions; used to verify that
        decisions depend only on candles completed at that time.
        """
        ema, current = Ema(self.ema_period), None
        for candle in candles:
            close = self.signal_close(candle)
            current = self.direction(close, ema.update(close), current)
        return current

    def entry_levels(self, direction, reference):
        """Entry level indices for a grid seeded at ``reference``, nearest first.

        Only levels with an exit level inside the grid qualify: the top level
        cannot open a long lot and the bottom level cannot open a short lot.
        """
        levels = self.levels
        if direction == "BUY":
            return tuple(i for i in range(len(levels) - 2, -1, -1) if levels[i] < reference)
        if direction == "SELL":
            return tuple(i for i in range(1, len(levels)) if levels[i] > reference)
        raise ValueError("Grid direction must be BUY or SELL")

    @staticmethod
    def exit_level(direction, index):
        return index + 1 if direction == "BUY" else index - 1


DEFAULT_LOOKBACK = 100


@dataclass(frozen=True)
class AutoGridStrategy:
    """A grid whose bounds come from the ``grid_lookback`` candles before the start.

    It only describes the request: ``resolve`` turns measured bounds into an
    ordinary ``GridStrategy`` (same tick rounding and validation), which is the
    only object the simulator ever runs. ``warmup`` is the completed history to
    load (enough for both the bounds and the EMA); the EMA itself still warms up
    on exactly ``ema_period`` candles.
    """

    grid_lookback: int = DEFAULT_LOOKBACK
    ema_period: int = 50
    grid_intervals: int = 10
    level_size: Decimal = Decimal(100)
    max_position: Decimal = Decimal(500)
    name: str = "Grid"

    def __post_init__(self):
        if type(self.grid_lookback) is not int or self.grid_lookback < 1:
            raise ValueError("Grid lookback must be a positive integer number of candles")
        # Validate every non-bound parameter exactly as a manual grid would.
        probe = self.resolve(Decimal(1), Decimal(2))
        object.__setattr__(self, "level_size", probe.level_size)
        object.__setattr__(self, "max_position", probe.max_position)

    @property
    def warmup(self):
        """Completed candles to load before the start (bounds and EMA)."""
        return max(self.ema_period, self.grid_lookback)

    @property
    def minimum_history(self):
        """Fewer candles still let the EMA warm up; the bounds check reports it."""
        return self.ema_period

    def resolve(self, grid_lower, grid_upper):
        return GridStrategy(
            grid_lower,
            grid_upper,
            ema_period=self.ema_period,
            grid_intervals=self.grid_intervals,
            level_size=self.level_size,
            max_position=self.max_position,
            name=self.name,
        )

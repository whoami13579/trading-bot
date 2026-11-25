"""Strategy adapters: the only per-strategy code the shared backtester needs.

An adapter names a pure strategy, declares its CLI parameters and their
preserved defaults, builds the strategy, and states the defaults that make the
unified CLI reproduce the strategy's existing backtest command.

Adding a strategy = a pure ``target(completed_candles[, position])`` class with a
``warmup`` plus one ``StrategyAdapter`` entry in ``ADAPTERS`` (and its capability
entry in ``trading_bot.strategies.registry``). A strategy whose native execution
is ``"grid"`` manages many limit orders and runs only on the grid execution model.

Several strategies may declare the same option (for example ``--hma-period``);
it is then one CLI option whose default and validity depend on --strategy.
"""

import argparse
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Callable

from trading_bot.strategies.donchian import DonchianStrategy
from trading_bot.strategies import signals
from trading_bot.strategies.grid import DEFAULT_LOOKBACK, AutoGridStrategy, GridStrategy
from .strategy import (
    EmaHmaStrategy,
    HmaStrategy,
    MovingAverageStrategy,
    MultipleHmaStrategy,
    SmaCrossoverStrategy,
)
from .timeframes import MINUTES


def decimal_option(value):
    """argparse type for exact finite Decimal prices and quantities."""
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise argparse.ArgumentTypeError("A finite numeric value is required") from None
    if not result.is_finite():
        raise argparse.ArgumentTypeError("A finite numeric value is required")
    return result


@dataclass(frozen=True)
class Parameter:
    flags: tuple[str, ...]
    dest: str
    default: object
    type: Callable = str
    choices: tuple | None = None
    help: str = ""


@dataclass(frozen=True)
class StrategyAdapter:
    key: str
    parameters: tuple[Parameter, ...]
    factory: Callable[..., object]
    describe: Callable[[object], str]
    signal_price: str
    # Defaults reproducing the strategy's existing backtest command.
    time_frame: str
    data_source: str
    execution: str
    # Optional argument-combination check on the explicit parameters; returns an
    # error message, raised as a usage error before any data access.
    check: Callable[[dict], str | None] | None = None
    # Parameter dest -> strategy attribute, where they differ.
    attributes: dict = field(default_factory=dict)
    # The factory also receives the resolved signal timeframe (multi-timeframe rules).
    uses_time_frame: bool = False

    def defaults(self):
        return {p.dest: p.default for p in self.parameters}

    def build(self, **values):
        """Build from explicit values; omitted/None values use preserved defaults."""
        merged = self.defaults()
        merged.update({k: v for k, v in values.items() if v is not None})
        return self.factory(**merged)

    def values(self, strategy):
        # A parameter the built strategy does not carry (e.g. grid bounds in
        # automatic mode) is recorded as None.
        return {p.dest: getattr(strategy, self.attributes.get(p.dest, p.dest), None)
                for p in self.parameters}


def _hma(hma_period, direction):
    return HmaStrategy(hma_period, direction)


def _donchian(entry_lookback, exit_lookback):
    return DonchianStrategy(entry_lookback, exit_lookback)


HMA = StrategyAdapter(
    key="hma",
    parameters=(
        Parameter(("--hma-period", "--hma_period"), "hma_period", 16, int,
                  help="HMA period, integer >= 2 (default 16)"),
        Parameter(("--direction",), "direction", "BUY", str, ("BUY", "SELL"),
                  help="Configured side; the opposite signal goes flat (default BUY)"),
    ),
    factory=_hma,
    describe=lambda s: f"HMA ({s.direction} / flat), period {s.period}",
    signal_price="source close: ask close for bid/ask data, indicative close otherwise",
    time_frame="MINUTE_30",
    data_source="dukascopy",
    execution="continuous",
    attributes={"hma_period": "period"},
)

DONCHIAN = StrategyAdapter(
    key="donchian",
    parameters=(
        Parameter(("--entry-lookback",), "entry_lookback", 20, int,
                  help="Completed closes in the entry channel (default 20)"),
        Parameter(("--exit-lookback",), "exit_lookback", 10, int,
                  help="Completed closes in the exit channel (default 10)"),
    ),
    factory=_donchian,
    describe=lambda s: f"Donchian (long / short), {s.entry}/{s.exit} bars",
    signal_price="midpoint close when bid/ask exists, otherwise source close",
    time_frame="HOUR",
    data_source="snapshot",
    execution="research",
    attributes={"entry_lookback": "entry", "exit_lookback": "exit"},
)

def _grid(ema_period, grid_lower, grid_upper, grid_intervals, level_size, max_position,
          grid_lookback):
    common = dict(ema_period=ema_period, grid_intervals=grid_intervals,
                  level_size=level_size, max_position=max_position)
    if grid_lower is None and grid_upper is None:
        return AutoGridStrategy(
            DEFAULT_LOOKBACK if grid_lookback is None else grid_lookback, **common
        )
    return GridStrategy(grid_lower, grid_upper, **common)


def _check_grid(parameters):
    lower, upper = parameters.get("grid_lower"), parameters.get("grid_upper")
    if (lower is None) != (upper is None):
        given, missing = (("--grid-lower", "--grid-upper") if upper is None
                          else ("--grid-upper", "--grid-lower"))
        return (
            f"{given} was given without {missing}: pass both for manual bounds, or "
            "neither for automatic bounds from the last --grid-lookback candles "
            f"(default {DEFAULT_LOOKBACK})"
        )
    if lower is not None and parameters.get("grid_lookback") is not None:
        return ("--grid-lookback applies only to automatic bounds; omit it or omit "
                "--grid-lower/--grid-upper")
    return None


def _describe_grid(s):
    if isinstance(s, AutoGridStrategy):
        bounds = f"automatic bounds from the last {s.grid_lookback} candles"
    else:
        bounds = f"{s.grid_lower}-{s.grid_upper}"
    return f"Grid (EMA {s.ema_period} long / short), {s.grid_intervals} intervals {bounds}"


GRID = StrategyAdapter(
    key="grid",
    parameters=(
        Parameter(("--ema-period",), "ema_period", 50, int,
                  help="EMA lookback in completed candles; flat until warm (default 50)"),
        Parameter(("--grid-lower",), "grid_lower", None, decimal_option,
                  help="Lowest grid price; give with --grid-upper for manual bounds, "
                  "omit both for automatic bounds"),
        Parameter(("--grid-upper",), "grid_upper", None, decimal_option,
                  help="Highest grid price; give with --grid-lower for manual bounds"),
        Parameter(("--grid-lookback",), "grid_lookback", None, int,
                  help="Automatic bounds only: lowest low / highest high of the last N "
                  "completed --time-frame candles ending at or before --start "
                  f"(default {DEFAULT_LOOKBACK})"),
        Parameter(("--grid-intervals",), "grid_intervals", 10, int,
                  help="Evenly spaced intervals between the bounds (default 10)"),
        Parameter(("--level-size",), "level_size", Decimal(100), decimal_option,
                  help="EUR base units per grid level; replaces --size (default 100)"),
        Parameter(("--max-position",), "max_position", Decimal(500), decimal_option,
                  help="Cap on open lot + working entry units in EUR (default 500)"),
    ),
    factory=_grid,
    describe=_describe_grid,
    signal_price="midpoint close when bid/ask exists, otherwise source close",
    time_frame="HOUR",
    data_source="dukascopy",
    execution="grid",
    check=_check_grid,
)

ASK_OPEN = "ask open of each completed candle (indicative open without bid/ask data)"
ASK_CLOSE = "ask close of each completed candle (indicative close without bid/ask data)"


def _hma_period(default):
    return Parameter(("--hma-period", "--hma_period"), "hma_period", default, int,
                     help=f"HMA period, integer >= 2 (default {default})")


MOVING_AVERAGE = StrategyAdapter(
    key="moving-average",
    parameters=(
        Parameter(("--average-period",), "average_period", signals.MOVING_AVERAGE_PERIOD, int,
                  help="Candles in the rolling mean, integer >= 2 (default "
                  f"{signals.MOVING_AVERAGE_PERIOD}; fixed in the online strategy)"),
    ),
    factory=MovingAverageStrategy,
    describe=lambda s: f"Moving average (stop and reverse), {s.average_period} candle opens",
    signal_price=ASK_OPEN,
    time_frame=signals.POLLING_TIME_FRAME,
    data_source="dukascopy",
    execution="continuous",
)

SMA = StrategyAdapter(
    key="sma",
    parameters=(
        Parameter(("--fast-period",), "fast_period", signals.SMA_FAST_PERIOD, int,
                  help=f"Fast SMA period (default {signals.SMA_FAST_PERIOD}; fixed online)"),
        Parameter(("--slow-period",), "slow_period", signals.SMA_SLOW_PERIOD, int,
                  help="Slow SMA period, longer than the fast period (default "
                  f"{signals.SMA_SLOW_PERIOD}; fixed online)"),
    ),
    factory=SmaCrossoverStrategy,
    describe=lambda s: f"SMA {s.fast_period}/{s.slow_period} crossover (stop and reverse)",
    signal_price=ASK_OPEN,
    time_frame=signals.POLLING_TIME_FRAME,
    data_source="dukascopy",
    execution="continuous",
)

EMA_HMA = StrategyAdapter(
    key="ema-hma",
    parameters=(
        Parameter(("--long-period", "--long_period"), "long_period", signals.EMA_HMA_LONG_PERIOD,
                  int, help=f"Long EMA period (default {signals.EMA_HMA_LONG_PERIOD})"),
        Parameter(("--short-period", "--short_period"), "short_period",
                  signals.EMA_HMA_SHORT_PERIOD, int,
                  help=f"Short EMA period (default {signals.EMA_HMA_SHORT_PERIOD})"),
        _hma_period(signals.EMA_HMA_HMA_PERIOD),
    ),
    factory=EmaHmaStrategy,
    describe=lambda s: (f"EMA {s.short_period}/{s.long_period} trend with HMA "
                        f"{s.hma_period} confirmation"),
    signal_price=ASK_CLOSE,
    time_frame=signals.EMA_HMA_TIME_FRAME,
    data_source="dukascopy",
    execution="continuous",
)

MULTIPLE_HMA = StrategyAdapter(
    key="multiple-hma",
    parameters=(
        _hma_period(signals.MULTIPLE_HMA_HMA_PERIOD),
        Parameter(("--long-term", "--long_term"), "long_term", signals.MULTIPLE_HMA_LONG_TERM,
                  str, tuple(MINUTES),
                  help="Timeframe of the multiple-indicator vote, a whole multiple of "
                  f"--time-frame (default {signals.MULTIPLE_HMA_LONG_TERM})"),
    ),
    factory=MultipleHmaStrategy,
    describe=lambda s: (f"Multiple HMA: {s.long_term} 13-indicator vote with "
                        f"{s.time_frame} HMA {s.hma_period} confirmation"),
    signal_price=ASK_CLOSE + "; long-term candles aggregated from completed signal candles",
    time_frame=signals.MULTIPLE_HMA_SHORT_TERM,
    data_source="dukascopy",
    execution="continuous",
    uses_time_frame=True,
)

ADAPTERS = {
    adapter.key: adapter
    for adapter in (MOVING_AVERAGE, SMA, HMA, EMA_HMA, MULTIPLE_HMA, DONCHIAN, GRID)
}


def adapter_for(key):
    try:
        return ADAPTERS[key]
    except KeyError:
        raise ValueError(f"Unsupported strategy: {key}") from None

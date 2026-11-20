"""Strategy adapters: the only per-strategy code the shared backtester needs.

An adapter names a pure strategy, declares its CLI parameters and their
preserved defaults, builds the strategy, and states the defaults that make the
unified CLI reproduce the strategy's existing backtest command.

Adding a strategy = a pure ``target(completed_candles[, position])`` class with a
``warmup`` plus one ``StrategyAdapter`` entry in ``ADAPTERS``. A strategy whose
native execution is ``"grid"`` manages many limit orders and runs only on the
grid execution model.
"""

import argparse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable

from trading_bot.strategies.donchian import DonchianStrategy
from trading_bot.strategies.grid import GridStrategy
from .strategy import HmaStrategy


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
    required: bool = False  # No default; must be given explicitly.


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

    def defaults(self):
        return {p.dest: p.default for p in self.parameters}

    def build(self, **values):
        """Build from explicit values; omitted/None values use preserved defaults."""
        merged = self.defaults()
        merged.update({k: v for k, v in values.items() if v is not None})
        return self.factory(**merged)

    def values(self, strategy):
        return {p.dest: getattr(strategy, _ATTRIBUTE.get(p.dest, p.dest))
                for p in self.parameters}


_ATTRIBUTE = {"hma_period": "period", "entry_lookback": "entry", "exit_lookback": "exit"}


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
)

def _grid(ema_period, grid_lower, grid_upper, grid_intervals, level_size, max_position):
    return GridStrategy(
        grid_lower,
        grid_upper,
        ema_period=ema_period,
        grid_intervals=grid_intervals,
        level_size=level_size,
        max_position=max_position,
    )


GRID = StrategyAdapter(
    key="grid",
    parameters=(
        Parameter(("--ema-period",), "ema_period", 50, int,
                  help="EMA lookback in completed candles; flat until warm (default 50)"),
        Parameter(("--grid-lower",), "grid_lower", None, decimal_option,
                  help="Lowest grid price (required)", required=True),
        Parameter(("--grid-upper",), "grid_upper", None, decimal_option,
                  help="Highest grid price (required)", required=True),
        Parameter(("--grid-intervals",), "grid_intervals", 10, int,
                  help="Evenly spaced intervals between the bounds (default 10)"),
        Parameter(("--level-size",), "level_size", Decimal(100), decimal_option,
                  help="EUR base units per grid level; replaces --size (default 100)"),
        Parameter(("--max-position",), "max_position", Decimal(500), decimal_option,
                  help="Cap on open lot + working entry units in EUR (default 500)"),
    ),
    factory=_grid,
    describe=lambda s: (
        f"Grid (EMA {s.ema_period} long / short), {s.grid_intervals} intervals "
        f"{s.grid_lower}-{s.grid_upper}"
    ),
    signal_price="midpoint close when bid/ask exists, otherwise source close",
    time_frame="HOUR",
    data_source="dukascopy",
    execution="grid",
)

ADAPTERS = {adapter.key: adapter for adapter in (HMA, DONCHIAN, GRID)}


def adapter_for(key):
    try:
        return ADAPTERS[key]
    except KeyError:
        raise ValueError(f"Unsupported strategy: {key}") from None

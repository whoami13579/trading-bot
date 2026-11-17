"""Strategy adapters: the only per-strategy code the shared backtester needs.

An adapter names a pure strategy, declares its CLI parameters and their
preserved defaults, builds the strategy, and states the defaults that make the
unified CLI reproduce the strategy's existing backtest command.

Adding a strategy = a pure ``target(completed_candles[, position])`` class with a
``warmup`` plus one ``StrategyAdapter`` entry in ``ADAPTERS``.
"""

from dataclasses import dataclass
from typing import Callable

from trading_bot.strategies.donchian import DonchianStrategy
from .strategy import HmaStrategy


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

ADAPTERS = {adapter.key: adapter for adapter in (HMA, DONCHIAN)}


def adapter_for(key):
    try:
        return ADAPTERS[key]
    except KeyError:
        raise ValueError(f"Unsupported strategy: {key}") from None

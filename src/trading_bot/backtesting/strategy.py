"""HMA adapter targets the same configured side/flat state as the automated strategy."""

from dataclasses import dataclass
from trading_bot.strategies.indicators import hma_signal


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

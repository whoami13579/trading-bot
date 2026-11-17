"""Pure, position-aware completed-close Donchian rules. No order capability."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DonchianStrategy:
    entry: int = 20
    exit: int = 10
    name: str = "Donchian"

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in (self.entry, self.exit)):
            raise ValueError("Lookbacks must be positive integers")

    @property
    def warmup(self):
        return max(self.entry, self.exit) + 1

    def target(self, candles, direction=None):
        if direction not in (None, "BUY", "SELL"):
            raise ValueError("Invalid position direction")
        if len(candles) < self.warmup:
            return direction
        closes = [(c.bid.close + c.ask.close) / 2 if c.bid else c.prices.close
                  for c in candles]
        current = closes[-1]
        preceding = closes[-self.exit - 1:-1]
        # An exit consumes this close. Earliest new decision is the next close.
        if direction == "BUY":
            return None if current < min(preceding) else direction
        if direction == "SELL":
            return None if current > max(preceding) else direction
        preceding = closes[-self.entry - 1:-1]
        if current > max(preceding):
            return "BUY"
        if current < min(preceding):
            return "SELL"
        return None


def main(argv=None):
    """Dispatch direct execution to the offline single-run backtest CLI."""
    from trading_bot.backtesting.donchian_cli import main as backtest_main

    return backtest_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())

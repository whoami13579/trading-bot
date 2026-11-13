"""Deprecated quote-buffer API, adapted to the shared historical execution engine.

These compatibility calls retain one-unit, same-observation fills and two-sided
reversals. New backtesting commands use next-open fills and the live HMA side/flat
rule. No separate accounting or execution engine is maintained here.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from trading_bot.strategies.indicators import (
    calculate_hma,
    calculate_wma as calculate_wma,
)
from trading_bot.backtesting.engine import Ledger, SimulationConfig, run
from trading_bot.backtesting.models import Candle, OHLC

SYMBOL = "EURUSD"
HMA_PERIOD = 20
TIME = "MINUTE_15"


def _price(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Backtest prices must be finite and positive")
    return value


def _candle(price, index=0):
    stamp = datetime(2000, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)
    price = _price(price)
    return Candle(stamp, OHLC(price, price, price, price), SYMBOL, "MINUTE", "legacy")


@dataclass(frozen=True)
class ClosedTrade:
    direction: str
    opening: float
    closing: float
    pnl: float


class Portfolio:
    def __init__(self):
        self._ledger = Ledger(SimulationConfig(0, 1, legacy_unfunded=True))
        self._index = 0

    @property
    def direction(self):
        return self._ledger.position.direction if self._ledger.position else None

    @property
    def price(self):
        return (
            float(self._ledger.position.entry_price) if self._ledger.position else None
        )

    @property
    def balance(self):
        return float(self._ledger.balance)

    @property
    def trades(self):
        return [
            ClosedTrade(
                t.direction, float(t.entry_price), float(t.exit_price), float(t.net_pnl)
            )
            for t in self._ledger.trades
        ]

    def target(self, direction, price):
        if direction not in ("BUY", "SELL"):
            raise ValueError("Backtest direction must be BUY or SELL")
        self._ledger.target(direction, _candle(price, self._index), field="close")
        self._index += 1

    def buy(self, price):
        self.target("BUY", price)

    def sell(self, price):
        self.target("SELL", price)

    def unrealized(self, price):
        return float(self._ledger.unrealized(_price(price)))


@dataclass(frozen=True)
class BacktestResult:
    realized_pnl: float
    unrealized_pnl: float
    open_direction: str | None
    open_price: float | None
    trades: tuple[ClosedTrade, ...]
    signals: tuple[tuple[int, str, float], ...]


def run_hma_backtest(prices, period=HMA_PERIOD):
    if type(period) is not int or period < 2:
        raise ValueError("HMA period must be an integer of at least 2")

    class LegacySlope:
        name = "legacy HMA quote-buffer"
        warmup = period * 3
        current = None

        def target(self, candles):
            values = calculate_hma([float(c.prices.close) for c in candles], period)
            if len(values) >= 2 and values[-1] != values[-2]:
                self.current = "BUY" if values[-1] > values[-2] else "SELL"
            return self.current

    candles = [_candle(p, i) for i, p in enumerate(prices)]
    origin = datetime(2000, 1, 1, tzinfo=timezone.utc)
    result = run(
        candles,
        LegacySlope(),
        SimulationConfig(0, 1, legacy_unfunded=True),
        start=origin,
        end=candles[-1].end if candles else origin,
        execution="observed_close",
        warmup_extra=1,
    )
    ledger = result.ledger
    p = ledger.position
    return BacktestResult(
        float(ledger.balance),
        float(result.equity_curve[-1]["unrealized_pnl"]),
        p.direction if p else None,
        float(p.entry_price) if p else None,
        tuple(
            ClosedTrade(
                t.direction, float(t.entry_price), float(t.exit_price), float(t.net_pnl)
            )
            for t in ledger.trades
        ),
        tuple(
            (e["index"], e["direction"], float(e["price"]))
            for e in ledger.events
            if e["action"] == "OPEN"
        ),
    )


class TestStrategy(Portfolio):
    """Legacy supplied-history buffer. The historical CLI never constructs a broker."""

    def __init__(self, tb):
        super().__init__()
        result, status = tb.getHistoricalPrices(SYMBOL, TIME, 1000)
        if status != 200:
            raise ValueError("Historical prices unavailable for backtest")
        self.all_prices = [_price(row["openPrice"]["ask"]) for row in result["prices"]]

    def get_price(self, n):
        if type(n) is not int or n < 1:
            raise ValueError("Price count must be positive")
        if len(self.all_prices) < n:
            return None
        result = self.all_prices[:n]
        del self.all_prices[:n]
        return result

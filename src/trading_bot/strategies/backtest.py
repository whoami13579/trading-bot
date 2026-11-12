"""Offline HMA simulation using chronological open quotes and one-unit positions.

Signals see only the current/past quotes. Fills assume the observed quote without
slippage; results are gross quote-price differences, not broker-realized money.
An open final position is marked separately and is never silently liquidated.
"""
from dataclasses import dataclass
import math

from trading_bot.strategies.indicators import calculate_hma, calculate_wma

SYMBOL = "EURUSD"
HMA_PERIOD = 20
TIME = "MINUTE_15"


def _price(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Backtest prices must be finite and positive")
    return value


@dataclass(frozen=True)
class ClosedTrade:
    direction: str
    opening: float
    closing: float
    pnl: float


class Portfolio:
    """A signal targets one direction; repeated signals never toggle a position."""

    def __init__(self):
        self.direction = None
        self.price = None
        self.balance = 0.0
        self.trades = []

    def target(self, direction, price):
        price = _price(price)
        if direction not in ("BUY", "SELL"):
            raise ValueError("Backtest direction must be BUY or SELL")
        if self.direction == direction:
            return
        if self.direction is not None:
            pnl = (price - self.price) * (1 if self.direction == "BUY" else -1)
            self.balance += pnl
            self.trades.append(ClosedTrade(self.direction, self.price, price, pnl))
        self.direction, self.price = direction, price

    def buy(self, price):
        self.target("BUY", price)

    def sell(self, price):
        self.target("SELL", price)

    def unrealized(self, price):
        price = _price(price)
        return 0.0 if self.direction is None else (price - self.price) * (1 if self.direction == "BUY" else -1)


@dataclass(frozen=True)
class BacktestResult:
    realized_pnl: float
    unrealized_pnl: float
    open_direction: str | None
    open_price: float | None
    trades: tuple[ClosedTrade, ...]
    signals: tuple[tuple[int, str, float], ...]


def run_hma_backtest(prices, period=HMA_PERIOD):
    """Match the legacy three-period rolling warmup and HMA slope rule.

    Flat slopes hold the current position. A reversal closes the prior side and
    opens the new side at the observed quote; future observations are not read.
    """
    if not isinstance(period, int) or isinstance(period, bool) or period < 2:
        raise ValueError("HMA period must be an integer of at least 2")
    portfolio = Portfolio()
    window, signals = [], []
    warmup = period * 3
    last = None
    for index, price in enumerate(prices):
        price = _price(price)
        last = price
        window.append(price)
        if len(window) <= warmup:
            continue
        window.pop(0)
        values = calculate_hma(window, period)
        if len(values) < 2 or values[-1] == values[-2]:
            continue
        direction = "BUY" if values[-1] > values[-2] else "SELL"
        if direction != portfolio.direction:
            portfolio.target(direction, price)
            signals.append((index, direction, price))
    return BacktestResult(portfolio.balance, portfolio.unrealized(last) if last is not None else 0.0,
                          portfolio.direction, portfolio.price, tuple(portfolio.trades), tuple(signals))


class TestStrategy(Portfolio):
    """Legacy historical-price buffer adapter; never calls any order method."""

    def __init__(self, tb):
        super().__init__()
        result, status = tb.getHistoricalPrices(SYMBOL, TIME, 1000)
        if status != 200:
            raise ValueError("Historical prices unavailable for backtest")
        self.all_prices = [_price(row["openPrice"]["ask"]) for row in result["prices"]]

    def get_price(self, n):
        if not isinstance(n, int) or n < 1:
            raise ValueError("Price count must be positive")
        if len(self.all_prices) < n:
            return None
        result = self.all_prices[:n]
        del self.all_prices[:n]
        return result

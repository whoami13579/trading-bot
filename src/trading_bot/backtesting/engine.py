"""One deterministic execution/accounting engine, with no broker dependencies."""

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, Sequence
from .models import Candle, DataError, dec
from .timeframes import normalize

ZERO = Decimal(0)
PIP = Decimal(".0001")


@dataclass(frozen=True)
class SimulationConfig:
    initial_balance: Decimal = Decimal(1000)
    size: Decimal = Decimal(100)
    leverage: Decimal = Decimal(
        1
    )  # Explicit simulator collateral assumption, not broker leverage.
    commission_per_order: Decimal | None = None
    slippage_pips: Decimal | None = None
    financing_per_unit_day: Decimal | None = None
    legacy_unfunded: bool = False  # Only the deprecated one-unit quote-buffer adapter.

    def __post_init__(self):
        for name in (
            "initial_balance",
            "size",
            "leverage",
            "commission_per_order",
            "slippage_pips",
            "financing_per_unit_day",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, dec(value))
        if (
            self.size <= 0
            or self.leverage < 1
            or self.initial_balance < 0
            or (not self.legacy_unfunded and self.initial_balance == 0)
        ):
            raise ValueError("Balance/size must be positive and leverage at least one")
        if any(
            v is not None and v < 0
            for v in (
                self.commission_per_order,
                self.slippage_pips,
                self.financing_per_unit_day,
            )
        ):
            raise ValueError("Configured cost assumptions must be nonnegative")


@dataclass
class Position:
    direction: str
    size: Decimal
    entry_timestamp: datetime
    entry_price: Decimal
    margin: Decimal
    entry_commission: Decimal
    financing: Decimal
    last_financing_time: datetime


@dataclass(frozen=True)
class Trade:
    entry_timestamp: datetime
    exit_timestamp: datetime
    direction: str
    size: Decimal
    entry_price: Decimal
    exit_price: Decimal
    gross_pnl: Decimal
    costs: (
        Decimal  # Cash commissions + financing; spread/slippage are embedded in fills.
    )
    net_pnl: Decimal
    balance_after_closing: Decimal


class Ledger:
    """Cash changes only for realized P&L and expensed fees, never for notional/margin."""

    def __init__(self, config: SimulationConfig):
        self.config = config
        self.balance = config.initial_balance
        self.position: Position | None = None
        self.trades: list[Trade] = []
        self.costs = ZERO
        self.embedded_slippage = ZERO
        self.embedded_spread = ZERO
        self.rejections = []
        self.events = []
        self.margin_breaches = 0

    def liquidation_price(self, candle, field):
        return candle.quote(
            "bid" if self.position and self.position.direction == "BUY" else "ask",
            field,
        )

    def unrealized(self, price):
        p = self.position
        return (
            ZERO
            if p is None
            else (dec(price) - p.entry_price)
            * p.size
            * (1 if p.direction == "BUY" else -1)
        )

    def equity(self, price):
        return self.balance + self.unrealized(price)

    def accrue_financing(self, stamp):
        p = self.position
        if p is None:
            return
        days = dec((stamp - p.last_financing_time).total_seconds()) / 86400
        if days < 0:
            raise ValueError("Financing time moved backwards")
        charge = p.size * (self.config.financing_per_unit_day or ZERO) * days
        self.balance -= charge
        self.costs += charge
        p.financing += charge
        p.last_financing_time = stamp

    def target(self, direction, candle, *, field="open", stamp=None, index=None):
        """None targets flat; a matching side holds. Reversal first closes the prior side."""
        if direction not in (None, "BUY", "SELL"):
            raise ValueError("Invalid simulated direction")
        stamp = stamp or (candle.open_time if field == "open" else candle.end)
        self.accrue_financing(stamp)
        p = self.position
        if p and p.direction == direction:
            return
        commission = self.config.commission_per_order or ZERO
        slip = (self.config.slippage_pips or ZERO) * PIP
        if p:
            quote = candle.quote("bid" if p.direction == "BUY" else "ask", field)
            price = quote - slip if p.direction == "BUY" else quote + slip
            if price <= 0:
                raise ValueError("Slippage produced a nonpositive exit price")
            pnl = self.unrealized(price)
            self.balance += pnl - commission
            self.costs += commission
            self.embedded_slippage += p.size * slip
            if candle.bid:
                self.embedded_spread += (
                    (getattr(candle.ask, field) - getattr(candle.bid, field))
                    * p.size
                    / 2
                )
            expenses = p.entry_commission + commission + p.financing
            self.trades.append(
                Trade(
                    p.entry_timestamp,
                    stamp,
                    p.direction,
                    p.size,
                    p.entry_price,
                    price,
                    pnl,
                    expenses,
                    pnl - expenses,
                    self.balance,
                )
            )
            self.events.append(
                {
                    "action": "CLOSE",
                    "timestamp": stamp,
                    "index": index,
                    "direction": p.direction,
                    "price": price,
                }
            )
            self.position = None
        if direction is not None:
            quote = candle.quote("ask" if direction == "BUY" else "bid", field)
            price = quote + slip if direction == "BUY" else quote - slip
            mark = candle.quote("bid" if direction == "BUY" else "ask", field)
            if price <= 0:
                raise ValueError("Slippage produced a nonpositive entry price")
            margin = self.config.size * price / self.config.leverage
            immediate = (
                (mark - price) * self.config.size * (1 if direction == "BUY" else -1)
            )
            if (
                not self.config.legacy_unfunded
                and self.balance - commission + immediate < margin
            ):
                self.rejections.append(
                    {
                        "timestamp": stamp,
                        "reason": "insufficient_available_funds",
                        "required_margin": margin,
                    }
                )
                return
            if self.config.legacy_unfunded:
                margin = ZERO
            self.balance -= commission
            self.costs += commission
            self.embedded_slippage += self.config.size * slip
            if candle.bid:
                self.embedded_spread += (
                    (getattr(candle.ask, field) - getattr(candle.bid, field))
                    * self.config.size
                    / 2
                )
            self.position = Position(
                direction,
                self.config.size,
                stamp,
                price,
                margin,
                commission,
                ZERO,
                stamp,
            )
            self.events.append(
                {
                    "action": "OPEN",
                    "timestamp": stamp,
                    "index": index,
                    "direction": direction,
                    "price": price,
                }
            )

    def snapshot(self, stamp, price, phase):
        equity = self.equity(price)
        margin = self.position.margin if self.position else ZERO
        available = equity - margin
        if available < 0:
            self.margin_breaches += 1
        return {
            "timestamp": stamp,
            "phase": phase,
            "balance": self.balance,
            "equity": equity,
            "unrealized_pnl": self.unrealized(price),
            "mark_price": dec(price),
            "used_margin": margin,
            "available_funds": available,
        }


class Strategy(Protocol):
    name: str
    warmup: int

    def target(self, candles: Sequence[Candle]) -> str | None: ...


@dataclass
class SimulationResult:
    config: SimulationConfig
    strategy: str
    start: datetime
    end: datetime
    ledger: Ledger
    equity_curve: list[dict]
    decisions: list[dict]
    candles: int


def run(
    candles,
    strategy: Strategy,
    config: SimulationConfig,
    *,
    start,
    end,
    execution="next_open",
    warmup_extra=0,
) -> SimulationResult:
    """Completed close -> target -> next actual open. Never liquidates at final close.

    observed_close is reserved for the deprecated quote-buffer adapter, never the CLI.
    It uses this same engine/ledger while retaining its explicitly optimistic old fills.
    """
    if execution not in ("next_open", "observed_close"):
        raise ValueError("Unknown execution model")
    candles = normalize(candles, completed_before=end)
    if candles and any(c.symbol != "EURUSD" for c in candles):
        raise DataError("Only EURUSD unit contracts and USD accounts are supported")
    ledger = Ledger(config)
    history = deque(maxlen=strategy.warmup)
    curve = [ledger.snapshot(start, ZERO, "initial")]
    decisions = []
    pending = None
    ready = False
    count = 0
    last_mark = ZERO
    last_quote_time = None
    for index, candle in enumerate(candles):
        eligible = start <= candle.timestamp < end
        if eligible:
            count += 1
            if execution == "next_open" and ready:
                ledger.target(pending, candle, stamp=candle.open_time, index=index)
            # Mark both the actual execution opportunity and completed close; no intrabar path guessed.
            curve.append(
                ledger.snapshot(
                    candle.open_time, ledger.liquidation_price(candle, "open"), "open"
                )
            )
            ledger.accrue_financing(candle.end)
        history.append(candle)
        if (
            len(history) >= strategy.warmup
            and index >= strategy.warmup - 1 + warmup_extra
        ):
            pending = strategy.target(tuple(history))
            ready = True
            if eligible:
                decisions.append(
                    {"timestamp": candle.end, "target": pending, "index": index}
                )
                if execution == "observed_close":
                    ledger.target(
                        pending, candle, field="close", stamp=candle.end, index=index
                    )
        if eligible:
            last_mark = ledger.liquidation_price(candle, "close")
            last_quote_time = candle.end
            curve.append(ledger.snapshot(candle.end, last_mark, "close"))
    if count:
        ledger.accrue_financing(end)
        final = ledger.snapshot(end, last_mark, "final_mark")
        final["quote_timestamp"] = last_quote_time
        curve.append(final)
    return SimulationResult(
        config, strategy.name, start, end, ledger, curve, decisions, count
    )


def statistics(result):
    ledger = result.ledger
    wins = [t.net_pnl for t in ledger.trades if t.net_pnl > 0]
    losses = [t.net_pnl for t in ledger.trades if t.net_pnl < 0]
    peak = result.config.initial_balance
    drawdown = ZERO
    drawdown_pct = ZERO
    for point in result.equity_curve:
        peak = max(peak, point["equity"])
        drawdown = max(drawdown, peak - point["equity"])
        if peak > 0:
            drawdown_pct = max(drawdown_pct, (peak - point["equity"]) / peak * 100)
    final = result.equity_curve[-1]
    gross_profit = sum(wins, ZERO)
    gross_loss = -sum(losses, ZERO)
    return {
        "initial_balance": result.config.initial_balance,
        "final_balance": ledger.balance,
        "final_equity": final["equity"],
        "net_pnl": final["equity"] - result.config.initial_balance,
        "total_return_pct": (final["equity"] / result.config.initial_balance - 1) * 100
        if result.config.initial_balance
        else None,
        "realized_pnl": ledger.balance - result.config.initial_balance,
        "unrealized_pnl": final["unrealized_pnl"],
        "closed_trades_net_pnl": sum((t.net_pnl for t in ledger.trades), ZERO),
        "total_trades": len(ledger.trades),
        "opened_trades": sum(e["action"] == "OPEN" for e in ledger.events),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "breakeven_trades": len(ledger.trades) - len(wins) - len(losses),
        "win_rate_pct": dec(len(wins)) / len(ledger.trades) * 100
        if ledger.trades
        else None,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "profit_factor_status": "finite"
        if gross_loss
        else "infinite_no_losses"
        if gross_profit
        else "undefined_no_profits_or_losses",
        "max_drawdown": drawdown,
        "max_drawdown_pct": drawdown_pct,
        "known_cash_costs": ledger.costs,
        "embedded_slippage": ledger.embedded_slippage,
        "embedded_spread_estimate": ledger.embedded_spread,
        "remaining_positions": int(ledger.position is not None),
        "remaining_base_units": ledger.position.size if ledger.position else ZERO,
        "remaining_notional_usd": ledger.position.size * final["mark_price"]
        if ledger.position
        else ZERO,
        "used_margin": final["used_margin"],
        "available_funds": final["available_funds"],
        "rejected_entries": len(ledger.rejections),
        "margin_breach_observations": ledger.margin_breaches,
    }

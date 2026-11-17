"""One deterministic execution/accounting engine, with no broker dependencies.

Every strategy runs through the same event loop (``simulate``) and the same
Decimal ``Ledger``. Behavioral differences between the historical HMA mode and
the Donchian research windows are explicit ``ExecutionPolicy`` values, quote
transforms and ledger financing models, never strategy-name checks.
"""

from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime
from decimal import Decimal
import inspect
from typing import Callable, Protocol, Sequence
from .calendar import active_gap
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
        # Per closed trade: embedded spread/slippage and cash costs. Measured as the
        # change in cumulative counters since the position opened, so it is exact
        # for flat->open->flat round trips and correct for reversals.
        self.trade_costs: list[dict] = []
        self._entry_marks = (ZERO, ZERO)

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
            self.trade_costs.append(
                {
                    "spread_cost": self.embedded_spread - self._entry_marks[0],
                    "slippage_cost": self.embedded_slippage - self._entry_marks[1],
                    "commission_cost": p.entry_commission + commission,
                    "financing_cost": p.financing,
                }
            )
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
            self._entry_marks = (self.embedded_spread, self.embedded_slippage)
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
    """Common strategy interface used by every backtest.

    ``warmup`` is the number of completed candles in each decision window.
    ``target`` receives only completed candles (oldest first, the newest just
    closed) and returns the desired exposure: ``"BUY"`` (long), ``"SELL"``
    (short) or ``None`` (flat). Position-aware strategies also receive the
    current simulated direction; position-independent strategies may declare
    ``target(candles)`` only. Strategies never see fills, cash or future data.
    """

    name: str
    warmup: int

    def target(
        self, candles: Sequence[Candle], position: str | None = None
    ) -> str | None: ...


def decision_function(strategy) -> Callable[[tuple, str | None], str | None]:
    """Resolve whether a strategy consumes the current position, once per run.

    An explicit ``position_aware`` attribute wins; otherwise a ``target`` that
    accepts a second positional argument is treated as position-aware.
    """
    aware = getattr(strategy, "position_aware", None)
    if aware is None:
        try:
            parameters = inspect.signature(strategy.target).parameters.values()
        except (TypeError, ValueError):
            parameters = ()
        positional = [
            p
            for p in parameters
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        aware = len(positional) >= 2 or any(
            p.kind == p.VAR_POSITIONAL for p in parameters
        )
    if aware:
        return lambda window, position: strategy.target(window, position)
    return lambda window, position: strategy.target(window)


@dataclass(frozen=True)
class ExecutionPolicy:
    """Explicit execution semantics; the two supported models genuinely differ.

    fill: "next_open" executes a completed-close decision at the next observed
        candle open. "observed_close" is reserved for the deprecated one-unit
        quote-buffer adapter in strategies/backtest.py.
    prime_from_warmup: decisions made on warm-up closes may queue an order for
        the first performance open (HMA continuous mode). When False, the first
        decision is the first scored close (research windows).
    reset_on_active_gap: an unexpected gap in the modeled FX session clears the
        indicator window, cancels a pending entry and carries a held position
        (and its pending exit) to the next observed quote.
    liquidate_at_end: a costed synthetic exit at the last completed close ends
        every window flat. When False, open positions remain and are marked.
    """

    name: str
    fill: str = "next_open"
    prime_from_warmup: bool = True
    reset_on_active_gap: bool = False
    liquidate_at_end: bool = False

    def __post_init__(self):
        if self.fill not in ("next_open", "observed_close"):
            raise ValueError("Unknown execution model")


CONTINUOUS = ExecutionPolicy("continuous")
RESEARCH_WINDOW = ExecutionPolicy(
    "research_window",
    prime_from_warmup=False,
    reset_on_active_gap=True,
    liquidate_at_end=True,
)
LEGACY_OBSERVED_CLOSE = ExecutionPolicy("legacy_observed_close", fill="observed_close")


def raw_quotes(candle):
    """Execute against the source candle's own bid/ask (or indicative) prices."""
    return candle


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
    policy: ExecutionPolicy = CONTINUOUS
    # Closed-trade records with signal/fill times and cost decomposition.
    trade_records: list[dict] = field(default_factory=list)
    exposure_seconds: Decimal = ZERO
    cancelled_gap_entries: int = 0
    gap_holding_events: int = 0
    last_candle: Candle | None = None


def simulate(
    candles,
    strategy: Strategy,
    ledger: Ledger,
    *,
    start,
    end,
    policy: ExecutionPolicy = CONTINUOUS,
    quotes: Callable[[Candle], Candle] = raw_quotes,
    warmup_extra: int = 0,
) -> SimulationResult:
    """Completed close -> target -> fill, under one explicit execution policy.

    ``candles`` must already be ordered, validated and limited to warm-up plus
    the performance range. Only candles with ``start <= timestamp`` and
    ``end <= end`` are scored. ``quotes`` maps a source candle to the bid/ask
    used for fills and marks; signals always see the unmodified source candle.
    """
    decide = decision_function(strategy)
    history = deque(maxlen=strategy.warmup)
    curve = [ledger.snapshot(start, ZERO, "initial")]
    decisions, records = [], []
    pending = pending_signal = None
    entry_signal = held_since = None
    previous = last = None
    count = cancelled = gap_holding = 0
    exposure = ZERO
    last_mark = ZERO
    last_quote_time = None

    def execute(target, quote, *, field, stamp, signal, index=None, boundary=False):
        nonlocal entry_signal, held_since, exposure
        before = len(ledger.trades)
        was_flat = ledger.position is None
        ledger.target(target, quote, field=field, stamp=stamp, index=index)
        closed = len(ledger.trades) > before
        if closed:
            t = ledger.trades[-1]
            c = ledger.trade_costs[-1]
            spread, slip = c["spread_cost"], c["slippage_cost"]
            exposure += dec((stamp - held_since).total_seconds())
            records.append(
                dict(
                    asdict(t),
                    entry_signal_time=entry_signal,
                    exit_signal_time=signal,
                    boundary_exit=boundary,
                    midpoint_gross_pnl=t.gross_pnl + spread + slip,
                    spread_cost=spread,
                    slippage_cost=slip,
                    financing_cost=c["financing_cost"],
                    commission_cost=c["commission_cost"],
                    total_cost=spread + slip + t.costs,
                )
            )
            held_since = None
        if ledger.position is not None and (was_flat or closed):
            entry_signal = signal
            held_since = stamp

    for index, candle in enumerate(candles):
        if (
            policy.reset_on_active_gap
            and previous is not None
            and active_gap(previous.end, candle.timestamp)
        ):
            history.clear()
            if ledger.position:
                gap_holding += 1
            elif pending is not None:
                pending = pending_signal = None
                cancelled += 1
        eligible = start <= candle.timestamp and candle.end <= end
        quote = quotes(candle) if eligible else None
        if eligible:
            count += 1
            if policy.fill == "next_open":
                if pending_signal is not None:
                    execute(
                        pending,
                        quote,
                        field="open",
                        stamp=candle.open_time,
                        signal=pending_signal,
                        index=index,
                    )
                else:
                    ledger.accrue_financing(candle.open_time)
            # Mark the actual execution opportunity and completed close only;
            # no intrabar path is guessed.
            curve.append(
                ledger.snapshot(
                    candle.open_time, ledger.liquidation_price(quote, "open"), "open"
                )
            )
            ledger.accrue_financing(candle.end)
        history.append(candle)
        # A decision applies to the next open only. Without a gap reset a fresh
        # decision follows every completed close once the window is full.
        pending_signal = None
        if (
            len(history) >= strategy.warmup
            and index >= strategy.warmup - 1 + warmup_extra
            and (eligible or policy.prime_from_warmup)
        ):
            direction = ledger.position.direction if ledger.position else None
            pending = decide(tuple(history), direction)
            pending_signal = candle.end
            if eligible:
                decisions.append(
                    {"timestamp": candle.end, "target": pending, "index": index}
                )
                if policy.fill == "observed_close":
                    ledger.target(
                        pending, candle, field="close", stamp=candle.end, index=index
                    )
        if eligible:
            last_mark = ledger.liquidation_price(quote, "close")
            last_quote_time = candle.end
            curve.append(ledger.snapshot(candle.end, last_mark, "close"))
            last = candle
        previous = candle

    if policy.liquidate_at_end:
        if last is None:
            raise ValueError("Empty scored window")
        if ledger.position:
            execute(
                None,
                quotes(last),
                field="close",
                stamp=last.end,
                signal=None,
                boundary=True,
            )
        curve.append(ledger.snapshot(end, ZERO, "boundary_flat"))
    elif count:
        ledger.accrue_financing(end)
        final = ledger.snapshot(end, last_mark, "final_mark")
        final["quote_timestamp"] = last_quote_time
        curve.append(final)
    return SimulationResult(
        ledger.config,
        strategy.name,
        start,
        end,
        ledger,
        curve,
        decisions,
        count,
        policy,
        records,
        exposure,
        cancelled,
        gap_holding,
        last,
    )


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
    """Continuous mode: completed close -> target -> next actual open.

    Never liquidates at the final close. observed_close is reserved for the
    deprecated quote-buffer adapter, never the CLI. It uses this same engine and
    ledger while retaining its explicitly optimistic old fills.
    """
    if execution not in ("next_open", "observed_close"):
        raise ValueError("Unknown execution model")
    candles = normalize(candles, completed_before=end)
    if candles and any(c.symbol != "EURUSD" for c in candles):
        raise DataError("Only EURUSD unit contracts and USD accounts are supported")
    return simulate(
        candles,
        strategy,
        Ledger(config),
        start=start,
        end=end,
        policy=CONTINUOUS if execution == "next_open" else LEGACY_OBSERVED_CLOSE,
        warmup_extra=warmup_extra,
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

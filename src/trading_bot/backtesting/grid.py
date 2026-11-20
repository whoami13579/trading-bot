"""Historical fixed-grid simulation: many working limit orders and per-lot accounting.

Historical only. Nothing here imports a broker client, reads credentials or
submits orders; every order below is a simulated record.

Timeline per candle (only candles with ``start <= timestamp`` and
``end <= end`` trade; earlier candles only warm the EMA):

1. Open (``candle.open_time``). A direction change decided at the previous
   close first flattens every open lot of the old grid with market orders at
   this open (longs sell at bid - slippage, shorts buy at ask + slippage). Only
   then, with inventory flat, the new grid activates and seeds its entries
   relative to the signal close. Empty entry levels of the active grid are
   (re)admitted, nearest the reference first, under the position cap and an
   available-funds check at the observed open.
2. Intrabar fills. Working orders created before this candle, or admitted at
   this open, are checked against this candle: buy limits against the ask
   series, sell limits against the bid series (the indicative OHLC when the
   source has no bid/ask).
   - Gap-through: a buy whose ask *open* is at or below its limit fills at the
     ask open; a sell whose bid open is at or above its limit fills at the bid
     open (the better price, never worse than the limit).
   - Touch: otherwise a buy fills at its limit when ask low <= limit; a sell
     fills at its limit when bid high >= limit. No queue position is modeled.
   - Multiple levels in one candle: every touched eligible order fills, each
     at its own price, processed along an assumed path open -> nearer extreme
     -> farther extreme -> close (ties: low first), judged on the midpoint (or
     indicative) candle. Gap fills come first; at the same path point exits
     come before entries, then order id.
   - Orders created by a fill (a lot's exit, or the entry restored after an
     exit) are eligible from the next candle only, so no lot opens and closes
     inside one candle.
3. Close (``candle.end``). The completed close updates the EMA. A direction
   change cancels every working order of the old grid immediately; its lots
   are flattened at the next observed open (step 1).

Funding and exposure:
- Position cap: open lot units + working entry units never exceed
  ``max_position``. Exit orders reduce exposure and are not counted.
- Admission reserves ``level_size * limit / leverage`` for every working entry
  after checking ``equity - used margin - reserved margin - commission``. A
  fill checks again using only information available at that moment (open
  lots marked at the fill price, with the other side estimated from the
  observed opening spread). A failed fill check rejects the order; its level
  is retried at a later open.
- Lot margin is ``level_size * fill price / leverage``: collateral, not cost.

Costs reuse ``SimulationConfig``: commission per fill, linear financing per
unit per calendar day accrued per lot, and slippage on *market* (flatten)
fills only; limit fills execute at their limit or better. The spread is
embedded in bid/ask fills; its estimate charges each fill half of the fill
candle's opening spread (exact for open fills, an estimate for intrabar ones).
Runs never force-liquidate at the end; open lots are marked at the last
observed liquidation-side close.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal

from trading_bot.strategies.grid import Ema, GridStrategy
from .engine import PIP, ZERO, SimulationConfig, Trade, summarize
from .models import Candle, DataError
from .timeframes import normalize

ENTRY, EXIT, MARKET = "entry", "exit", "market"


@dataclass
class GridOrder:
    id: int
    grid_id: int
    role: str  # entry | exit | market (flatten)
    side: str  # BUY | SELL
    level: int | None
    price: Decimal | None  # Limit price; None for market orders.
    size: Decimal
    created_at: datetime
    eligible_index: int  # First candle index on which the order may fill.
    status: str = "working"  # working | filled | cancelled | rejected
    lot_id: int | None = None
    reserved_margin: Decimal = ZERO
    fill_price: Decimal | None = None
    fill_time: datetime | None = None
    fill_type: str | None = None  # gap_open | touch | market_open
    closed_at: datetime | None = None
    reason: str | None = None


@dataclass
class GridLot:
    id: int
    grid_id: int
    direction: str
    level: int
    size: Decimal
    entry_timestamp: datetime
    entry_price: Decimal
    entry_order_id: int
    entry_fill_type: str
    margin: Decimal
    entry_commission: Decimal
    entry_spread: Decimal
    entry_slippage: Decimal
    financing: Decimal
    last_financing_time: datetime
    exit_order_id: int | None = None


@dataclass
class ActiveGrid:
    id: int
    direction: str
    reference: Decimal
    signal_time: datetime
    activated_at: datetime
    entry_levels: tuple


class GridBook:
    """Cash changes only for realized P&L and expensed fees, never for margin."""

    def __init__(self, config: SimulationConfig, strategy: GridStrategy):
        if config.legacy_unfunded:
            raise ValueError("Grid simulation is always funded")
        self.config = config
        self.strategy = strategy
        self.levels = strategy.levels
        self.balance = config.initial_balance
        self.costs = ZERO  # Cash costs: commission + financing.
        self.commission = ZERO
        self.financing = ZERO
        self.embedded_spread = ZERO
        self.embedded_slippage = ZERO
        self.orders: list[GridOrder] = []
        self.working: dict[int, GridOrder] = {}
        self.lots: dict[int, GridLot] = {}
        self.slots: dict[int, tuple[str, int]] = {}  # level -> (entry|lot, id)
        self.trades: list[Trade] = []
        self.trade_records: list[dict] = []
        self.rejections: list[dict] = []
        self.events: list[dict] = []
        self.margin_breaches = 0
        self.grid: ActiveGrid | None = None
        self.grids = 0
        self.funds_rejection_attempts = 0
        self.peak = {
            "open_units": ZERO,
            "open_notional_usd": ZERO,
            "open_lots": 0,
            "committed_units": ZERO,
            "used_margin": ZERO,
        }
        self._reported = set()
        self._lot_ids = 0

    # -- State queries -----------------------------------------------------

    @property
    def commission_rate(self):
        return self.config.commission_per_order or ZERO

    def open_units(self):
        return sum((lot.size for lot in self.lots.values()), ZERO)

    def working_entry_units(self):
        return sum((o.size for o in self.working.values() if o.role == ENTRY), ZERO)

    def committed_units(self):
        return self.open_units() + self.working_entry_units()

    def used_margin(self):
        return sum((lot.margin for lot in self.lots.values()), ZERO)

    def reserved_margin(self):
        return sum((o.reserved_margin for o in self.working.values()), ZERO)

    def unrealized(self, bid, ask):
        total = ZERO
        for lot in self.lots.values():
            if lot.direction == "BUY":
                total += (bid - lot.entry_price) * lot.size
            else:
                total += (lot.entry_price - ask) * lot.size
        return total

    def free_funds(self, bid, ask, *, exclude=ZERO):
        """Equity at (bid, ask) minus lot margin and other entries' reservations."""
        return (
            self.balance
            + self.unrealized(bid, ask)
            - self.used_margin()
            - (self.reserved_margin() - exclude)
        )

    def _margin(self, price):
        return self.strategy.level_size * price / self.config.leverage

    def _track_peaks(self):
        peak = self.peak
        units = self.open_units()
        if units > peak["open_units"]:
            peak["open_units"] = units
            peak["open_lots"] = len(self.lots)
            peak["open_notional_usd"] = sum(
                (lot.size * lot.entry_price for lot in self.lots.values()), ZERO
            )
        peak["committed_units"] = max(peak["committed_units"], self.committed_units())
        peak["used_margin"] = max(peak["used_margin"], self.used_margin())

    # -- Order life cycle --------------------------------------------------

    def _place(self, role, side, level, price, stamp, eligible_index, *, lot_id=None,
               reserve=ZERO):
        order = GridOrder(
            len(self.orders) + 1,
            self.grid.id if self.grid else 0,
            role,
            side,
            level,
            price,
            self.strategy.level_size,
            stamp,
            eligible_index,
            lot_id=lot_id,
            reserved_margin=reserve,
        )
        self.orders.append(order)
        if role != MARKET:
            self.working[order.id] = order
            if role == ENTRY:
                self.slots[level] = (ENTRY, order.id)
        return order

    def _finish(self, order, status, stamp, *, reason=None):
        order.status = status
        order.closed_at = stamp
        order.reason = reason
        order.reserved_margin = ZERO
        self.working.pop(order.id, None)
        if order.role == ENTRY and self.slots.get(order.level) == (ENTRY, order.id):
            del self.slots[order.level]

    def _reject(self, order_or_level, stamp, reason, required, available, *, price):
        grid_id = self.grid.id if self.grid else 0
        level = order_or_level.level if isinstance(order_or_level, GridOrder) else order_or_level
        self.funds_rejection_attempts += 1
        key = (grid_id, level, reason)
        if key in self._reported:
            return  # Retries at later opens are counted, not re-listed.
        self._reported.add(key)
        self.rejections.append(
            {
                "timestamp": stamp,
                "reason": reason,
                "grid_id": grid_id,
                "level": level,
                "price": price,
                "required_margin": required,
                "available_funds": available,
            }
        )

    def activate(self, pending, candle, index):
        if self.lots:
            raise AssertionError("A new grid may only activate while flat")
        self.grids += 1
        levels = self.strategy.entry_levels(pending["direction"], pending["reference"])
        self.grid = ActiveGrid(
            self.grids,
            pending["direction"],
            pending["reference"],
            pending["signal_time"],
            candle.open_time,
            levels,
        )
        self.events.append(
            {
                "action": "ACTIVATE",
                "timestamp": candle.open_time,
                "index": index,
                "grid_id": self.grid.id,
                "direction": self.grid.direction,
                "reference": self.grid.reference,
                "signal_time": self.grid.signal_time,
                "entry_levels": [self.levels[i] for i in levels],
            }
        )

    def deactivate(self, stamp, reason, *, index=None):
        """Cancel every working order of the active grid; lots stay until flattened."""
        if self.grid is None:
            return
        cancelled = 0
        for order in list(self.working.values()):
            self._finish(order, "cancelled", stamp, reason=reason)
            cancelled += 1
        self.events.append(
            {
                "action": "DEACTIVATE",
                "timestamp": stamp,
                "index": index,
                "grid_id": self.grid.id,
                "direction": self.grid.direction,
                "cancelled_orders": cancelled,
                "open_lots": len(self.lots),
                "reason": reason,
            }
        )
        self.grid = None

    def admit(self, candle, index):
        """(Re)place empty entry levels at the observed open, nearest first."""
        grid = self.grid
        stamp = candle.open_time
        bid, ask = candle.quote("bid", "open"), candle.quote("ask", "open")
        size = self.strategy.level_size
        for level in grid.entry_levels:
            if level in self.slots:
                continue
            if self.committed_units() + size > self.strategy.max_position:
                break  # The cap admits nearer levels first; farther ones wait.
            price = self.levels[level]
            reserve = self._margin(price)
            free = self.free_funds(bid, ask) - self.commission_rate
            if free < reserve:
                self._reject(level, stamp, "insufficient_available_funds_on_admission",
                             reserve, free, price=price)
                break
            self._place(ENTRY, grid.direction, level, price, stamp, index, reserve=reserve)
        self._track_peaks()

    # -- Fills ---------------------------------------------------------------

    def accrue_financing(self, stamp):
        rate = self.config.financing_per_unit_day or ZERO
        for lot in self.lots.values():
            seconds = Decimal((stamp - lot.last_financing_time).total_seconds())
            if seconds < 0:
                raise ValueError("Financing time moved backwards")
            charge = lot.size * rate * seconds / 86400
            self.balance -= charge
            self.costs += charge
            self.financing += charge
            lot.financing += charge
            lot.last_financing_time = stamp

    @staticmethod
    def _open_spread(candle):
        return candle.ask.open - candle.bid.open if candle.bid is not None else ZERO

    def _open_lot(self, order, price, candle, index, stamp, fill_type, series):
        spread = self._open_spread(candle)
        bid, ask = (price, price + spread) if series == "bid" else (price - spread, price)
        commission = self.commission_rate
        margin = self._margin(price)
        immediate = (bid - price if order.side == "BUY" else price - ask) * order.size
        free = self.free_funds(bid, ask, exclude=order.reserved_margin) - commission + immediate
        if free < margin:
            self._reject(order, stamp, "insufficient_available_funds_on_fill", margin, free,
                         price=price)
            self._finish(order, "rejected", stamp, reason="insufficient_available_funds")
            return None
        self.balance -= commission
        self.costs += commission
        self.commission += commission
        half = spread / 2 * order.size
        self.embedded_spread += half
        self._lot_ids += 1
        lot = GridLot(
            self._lot_ids,
            order.grid_id,
            order.side,
            order.level,
            order.size,
            stamp,
            price,
            order.id,
            fill_type,
            margin,
            commission,
            half,
            ZERO,
            ZERO,
            stamp,
        )
        order.fill_price, order.fill_time, order.fill_type = price, stamp, fill_type
        order.lot_id = lot.id
        self._finish(order, "filled", stamp)
        self.lots[lot.id] = lot
        self.slots[lot.level] = ("lot", lot.id)
        target = self.strategy.exit_level(lot.direction, lot.level)
        exit_order = self._place(
            EXIT,
            "SELL" if lot.direction == "BUY" else "BUY",
            target,
            self.levels[target],
            stamp,
            index + 1,
            lot_id=lot.id,
        )
        lot.exit_order_id = exit_order.id
        self.events.append(
            {
                "action": "OPEN",
                "timestamp": stamp,
                "index": index,
                "grid_id": lot.grid_id,
                "lot_id": lot.id,
                "direction": lot.direction,
                "level": self.levels[lot.level],
                "price": price,
                "fill_type": fill_type,
            }
        )
        self._track_peaks()
        return lot

    def _close_lot(self, lot, price, stamp, index, *, spread, slip, order, reason,
                   signal_time=None):
        self.accrue_financing(stamp)
        commission = self.commission_rate
        pnl = (price - lot.entry_price) * lot.size * (1 if lot.direction == "BUY" else -1)
        self.balance += pnl - commission
        self.costs += commission
        self.commission += commission
        exit_spread = spread / 2 * lot.size
        exit_slip = slip * lot.size
        self.embedded_spread += exit_spread
        self.embedded_slippage += exit_slip
        expenses = lot.entry_commission + commission + lot.financing
        trade = Trade(
            lot.entry_timestamp,
            stamp,
            lot.direction,
            lot.size,
            lot.entry_price,
            price,
            pnl,
            expenses,
            pnl - expenses,
            self.balance,
        )
        self.trades.append(trade)
        spread_cost = lot.entry_spread + exit_spread
        slippage_cost = lot.entry_slippage + exit_slip
        grid_signal = None
        for event in reversed(self.events):
            if event["action"] == "ACTIVATE" and event["grid_id"] == lot.grid_id:
                grid_signal = event["signal_time"]
                break
        self.trade_records.append(
            dict(
                asdict(trade),
                entry_signal_time=grid_signal,
                exit_signal_time=signal_time,
                boundary_exit=False,
                midpoint_gross_pnl=pnl + spread_cost + slippage_cost,
                spread_cost=spread_cost,
                slippage_cost=slippage_cost,
                financing_cost=lot.financing,
                commission_cost=lot.entry_commission + commission,
                total_cost=spread_cost + slippage_cost + expenses,
                lot_id=lot.id,
                grid_id=lot.grid_id,
                entry_level=self.levels[lot.level],
                exit_level=None if order.role == MARKET else self.levels[order.level],
                entry_fill_type=lot.entry_fill_type,
                exit_fill_type=order.fill_type,
                exit_reason=reason,
                entry_order_id=lot.entry_order_id,
                exit_order_id=order.id,
            )
        )
        self.events.append(
            {
                "action": "CLOSE",
                "timestamp": stamp,
                "index": index,
                "grid_id": lot.grid_id,
                "lot_id": lot.id,
                "direction": lot.direction,
                "price": price,
                "reason": reason,
            }
        )
        del self.lots[lot.id]
        if self.slots.get(lot.level) == ("lot", lot.id):
            del self.slots[lot.level]

    def flatten(self, candle, index, *, signal_time):
        """Market-close every open lot at this observed open (bid/ask -/+ slippage)."""
        stamp = candle.open_time
        slip = (self.config.slippage_pips or ZERO) * PIP
        spread = self._open_spread(candle)
        for lot in sorted(self.lots.values(), key=lambda lot: lot.id):
            if lot.direction == "BUY":
                side, price = "SELL", candle.quote("bid", "open") - slip
            else:
                side, price = "BUY", candle.quote("ask", "open") + slip
            if price <= 0:
                raise ValueError("Slippage produced a nonpositive exit price")
            order = GridOrder(
                len(self.orders) + 1, lot.grid_id, MARKET, side, None, None, lot.size,
                stamp, index, status="filled", lot_id=lot.id, fill_price=price,
                fill_time=stamp, fill_type="market_open", closed_at=stamp,
                reason="direction_change",
            )
            self.orders.append(order)
            self._close_lot(lot, price, stamp, index, spread=spread, slip=slip,
                            order=order, reason="direction_change",
                            signal_time=signal_time)
        if self.lots:
            raise AssertionError("Flatten left open lots")

    def _restore_entry(self, lot, price_on_series, series, candle, index, stamp):
        """Re-place the entry a closed lot came from; eligible next candle."""
        grid = self.grid
        if grid is None or grid.id != lot.grid_id or lot.level in self.slots:
            return
        spread = self._open_spread(candle)
        bid, ask = ((price_on_series, price_on_series + spread) if series == "bid"
                    else (price_on_series - spread, price_on_series))
        price = self.levels[lot.level]
        reserve = self._margin(price)
        free = self.free_funds(bid, ask) - self.commission_rate
        if free < reserve:
            self._reject(lot.level, stamp, "insufficient_available_funds_on_restore",
                         reserve, free, price=price)
            return  # The level stays empty; admission retries it at a later open.
        self._place(ENTRY, grid.direction, lot.level, price, stamp, index + 1,
                    reserve=reserve)

    def fill_candle(self, candle: Candle, index):
        """Fill every eligible working order this candle reaches, in path order."""
        prices = candle.prices
        if candle.bid is not None:
            mid_open = (candle.bid.open + candle.ask.open) / 2
            mid_high = (candle.bid.high + candle.ask.high) / 2
            mid_low = (candle.bid.low + candle.ask.low) / 2
        else:
            mid_open, mid_high, mid_low = prices.open, prices.high, prices.low
        down_first = mid_open - mid_low <= mid_high - mid_open
        fills = []
        for order in self.working.values():
            if order.eligible_index > index:
                continue
            limit = order.price
            if order.side == "BUY":
                q = candle.ask or prices
                if q.open <= limit:
                    fills.append((ZERO, order, q.open, "gap_open", "ask"))
                elif q.low <= limit:
                    distance = (q.open - limit if down_first
                                else (q.high - q.open) + (q.high - limit))
                    fills.append((distance, order, limit, "touch", "ask"))
            else:
                q = candle.bid or prices
                if q.open >= limit:
                    fills.append((ZERO, order, q.open, "gap_open", "bid"))
                elif q.high >= limit:
                    distance = (limit - q.open if not down_first
                                else (q.open - q.low) + (limit - q.low))
                    fills.append((distance, order, limit, "touch", "bid"))
        fills.sort(key=lambda f: (f[0], f[1].role != EXIT, f[1].id))
        for _, order, price, fill_type, series in fills:
            if order.status != "working":
                continue  # Defensive: an order fills at most once.
            stamp = candle.open_time if fill_type == "gap_open" else candle.end
            if order.role == ENTRY:
                self._open_lot(order, price, candle, index, stamp, fill_type, series)
            else:
                lot = self.lots[order.lot_id]
                order.fill_price, order.fill_time, order.fill_type = price, stamp, fill_type
                self._finish(order, "filled", stamp)
                self._close_lot(lot, price, stamp, index, spread=self._open_spread(candle),
                                slip=ZERO, order=order, reason="grid_exit")
                self._restore_entry(lot, price, series, candle, index, stamp)
        self._track_peaks()

    # -- Valuation ---------------------------------------------------------

    def mark(self, bid, ask):
        """Liquidation-side mark: bid while long lots are open, otherwise ask."""
        if any(lot.direction == "BUY" for lot in self.lots.values()):
            return bid
        return ask

    def snapshot(self, stamp, bid, ask, phase):
        unrealized = self.unrealized(bid, ask)
        equity = self.balance + unrealized
        used = self.used_margin()
        reserved = self.reserved_margin()
        if equity - used < 0:
            self.margin_breaches += 1
        return {
            "timestamp": stamp,
            "phase": phase,
            "balance": self.balance,
            "equity": equity,
            "unrealized_pnl": unrealized,
            "mark_price": self.mark(bid, ask),
            "used_margin": used,
            "available_funds": equity - used - reserved,
            "reserved_margin": reserved,
            "open_lots": len(self.lots),
            "open_units": self.open_units(),
            "working_entry_units": self.working_entry_units(),
            "working_orders": len(self.working),
            "direction": self.grid.direction if self.grid else None,
        }

    def check_invariants(self):
        occupied = [o.level for o in self.working.values() if o.role == ENTRY]
        occupied += [lot.level for lot in self.lots.values()]
        if len(occupied) != len(set(occupied)):
            raise AssertionError("Duplicate grid level occupancy")
        if self.committed_units() > self.strategy.max_position:
            raise AssertionError("Position cap exceeded")
        for order in self.working.values():
            if order.role == EXIT and (
                order.lot_id not in self.lots
                or self.lots[order.lot_id].exit_order_id != order.id
            ):
                raise AssertionError("Exit order without its lot")
        for lot in self.lots.values():
            if self.grid is not None and lot.grid_id != self.grid.id:
                raise AssertionError("Lots from an inactive grid remain open")


@dataclass
class GridResult:
    config: SimulationConfig
    strategy: str
    start: datetime
    end: datetime
    ledger: GridBook
    equity_curve: list[dict]
    decisions: list[dict]
    candles: int
    trade_records: list[dict] = field(default_factory=list)
    last_candle: Candle | None = None
    pending: dict | None = None


def simulate_grid(candles, strategy: GridStrategy, config: SimulationConfig, *, start,
                  end) -> GridResult:
    """Run one fixed-grid backtest over ordered, completed candles."""
    candles = normalize(candles, completed_before=end)
    if candles and any(c.symbol != "EURUSD" for c in candles):
        raise DataError("Only EURUSD unit contracts and USD accounts are supported")
    book = GridBook(config, strategy)
    ema = Ema(strategy.ema_period)
    curve = [book.snapshot(start, ZERO, ZERO, "initial")]
    decisions = []
    selected = None  # Latest completed-close direction.
    pending = None  # Activation queued for the next observed open.
    count = 0
    last = None
    for index, candle in enumerate(candles):
        eligible = start <= candle.timestamp and candle.end <= end
        if eligible:
            count += 1
            book.accrue_financing(candle.open_time)
            if pending is not None:
                if book.lots:
                    book.flatten(candle, index, signal_time=pending["signal_time"])
                book.activate(pending, candle, index)
                pending = None
            if book.grid is not None:
                book.admit(candle, index)
            curve.append(book.snapshot(candle.open_time, candle.quote("bid", "open"),
                                       candle.quote("ask", "open"), "open"))
            book.fill_candle(candle, index)
            book.accrue_financing(candle.end)
            book.check_invariants()
        close = strategy.signal_close(candle)
        value = ema.update(close)
        decision = strategy.direction(close, value, selected)
        if decision is not None:
            current = pending["direction"] if pending else (
                book.grid.direction if book.grid else None)
            if decision != current:
                book.deactivate(candle.end, "direction_change", index=index)
                pending = {"direction": decision, "reference": close,
                           "signal_time": candle.end, "ema": value}
            elif pending is not None:
                # Still flat: the latest confirming close becomes the reference.
                pending.update(reference=close, signal_time=candle.end, ema=value)
        selected = decision
        if eligible:
            decisions.append({"timestamp": candle.end, "target": decision, "index": index,
                              "signal_close": close, "ema": value})
            curve.append(book.snapshot(candle.end, candle.quote("bid", "close"),
                                       candle.quote("ask", "close"), "close"))
            last = candle
    if count:
        book.accrue_financing(end)
        final = book.snapshot(end, last.quote("bid", "close"), last.quote("ask", "close"),
                              "final_mark")
        final["quote_timestamp"] = last.end
        curve.append(final)
    return GridResult(config, strategy.name, start, end, book, curve, decisions, count,
                      book.trade_records, last, pending)


def grid_pnl(result: GridResult) -> dict:
    """Same definitions as ``engine.pnl_decomposition``, summed over grid lots."""
    book = result.ledger
    realized = sum((r["midpoint_gross_pnl"] for r in book.trade_records), ZERO)
    final = result.equity_curve[-1]
    unrealized = valuation = entry_legs = ZERO
    if book.lots:
        last = result.last_candle
        mid = GridStrategy.signal_close(last)
        for lot in book.lots.values():
            mark = last.quote("bid" if lot.direction == "BUY" else "ask", "close")
            valuation += abs(mid - mark) * lot.size
            entry_legs += lot.entry_spread + lot.entry_slippage
        unrealized = final["unrealized_pnl"] + valuation + entry_legs
    modeled = book.costs + book.embedded_slippage + book.embedded_spread
    gross = realized + unrealized
    net = final["equity"] - result.config.initial_balance
    if abs(gross - modeled - valuation - net) > Decimal("1e-18"):
        raise AssertionError("Gross/net P&L reconciliation failed")
    return {
        "gross_pnl": gross,
        "realized_gross_pnl": realized,
        "unrealized_gross_pnl": unrealized,
        "modeled_costs": modeled,
        "open_entry_spread_and_slippage": entry_legs,
        "open_valuation_spread": valuation,
        "net_pnl": net,
    }


def grid_statistics(result: GridResult) -> dict:
    book = result.ledger
    final = result.equity_curve[-1]
    units = book.open_units()
    stats = summarize(
        trades=book.trades,
        curve=result.equity_curve,
        initial_balance=result.config.initial_balance,
        balance=book.balance,
        opened=sum(e["action"] == "OPEN" for e in book.events),
        cash_costs=book.costs,
        slippage=book.embedded_slippage,
        spread=book.embedded_spread,
        remaining_positions=len(book.lots),
        remaining_units=units,
        remaining_notional=units * final["mark_price"],
        rejected=len(book.rejections),
        breaches=book.margin_breaches,
    )
    statuses = {}
    for order in book.orders:
        statuses[order.status] = statuses.get(order.status, 0) + 1
    stats.update(
        {
            "commission_costs": book.commission,
            "financing_costs": book.financing,
            "trading_costs": book.costs + book.embedded_slippage + book.embedded_spread,
            "peak_open_units": book.peak["open_units"],
            "peak_open_lots": book.peak["open_lots"],
            "peak_open_notional_usd": book.peak["open_notional_usd"],
            "peak_committed_units": book.peak["committed_units"],
            "peak_used_margin": book.peak["used_margin"],
            "open_lots": len(book.lots),
            "working_orders": len(book.working),
            "working_entry_units": book.working_entry_units(),
            "orders_placed": len(book.orders),
            "orders_filled": statuses.get("filled", 0),
            "orders_cancelled": statuses.get("cancelled", 0),
            "orders_rejected": statuses.get("rejected", 0),
            "funds_rejection_attempts": book.funds_rejection_attempts,
            "grid_activations": book.grids,
            "direction_changes": sum(e["action"] == "DEACTIVATE" for e in book.events),
            "flatten_exits": sum(r["exit_reason"] == "direction_change"
                                 for r in book.trade_records),
            "grid_exits": sum(r["exit_reason"] == "grid_exit" for r in book.trade_records),
        }
    )
    return stats


def ending_inventory(result: GridResult) -> dict:
    book = result.ledger
    pending = result.pending
    return {
        "direction": book.grid.direction if book.grid else None,
        "reference_price": book.grid.reference if book.grid else None,
        "open_lots": [asdict(lot) for lot in sorted(book.lots.values(), key=lambda x: x.id)],
        "open_units": book.open_units(),
        "working_orders": [asdict(o) for o in book.orders if o.status == "working"],
        "pending_activation": None if pending is None else {
            "direction": pending["direction"],
            "reference": pending["reference"],
            "signal_time": pending["signal_time"],
            "note": "decided at the last completed close; would execute at the next open",
        },
    }

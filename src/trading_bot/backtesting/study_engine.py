"""Isolated window execution using the existing Decimal ledger and HMA signal."""

from bisect import bisect_left
from collections import deque
from dataclasses import dataclass, replace, asdict
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .calendar import expected_open
from .engine import Ledger, SimulationConfig, SimulationResult, statistics, PIP, ZERO
from .models import OHLC, dec
from .timeframes import duration, normalize, resample

NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class CostCase:
    name: str
    spread_multiplier: Decimal = Decimal(1)
    slippage_pips: Decimal = Decimal("0.1")
    financing_rate: Decimal = ZERO
    midpoint_spread_pips: Decimal = Decimal(1)


COSTS = (
    CostCase("base"),
    CostCase("high", Decimal(2), Decimal("0.5"), Decimal("0.05")),
    CostCase("financing", financing_rate=Decimal("0.05")),
)


def execution_candle(candle, case):
    """Apply execution costs only; leave signal candles unchanged."""
    bid, ask = [], []
    for field in ("open", "high", "low", "close"):
        mid = ((getattr(candle.bid, field) + getattr(candle.ask, field)) / 2
               if candle.bid else getattr(candle.prices, field))
        spread = ((getattr(candle.ask, field) - getattr(candle.bid, field))
                  if candle.bid else case.midpoint_spread_pips * PIP)
        spread *= case.spread_multiplier
        bid.append(mid - spread / 2)
        ask.append(mid + spread / 2)
    # Expanding separate extrema around their midpoint preserves OHLC ordering.
    for side in (bid, ask):
        side[1] = max(side[1], side[0], side[3])
        side[2] = min(side[2], side[0], side[3])
    return replace(candle, bid=OHLC(*bid), ask=OHLC(*ask))


class RolloverLedger(Ledger):
    def __init__(self, config, case):
        super().__init__(config)
        self.case = case
        self.rollovers = 0

    def accrue_financing(self, stamp):
        p = self.position
        if p is None:
            return
        if stamp < p.last_financing_time:
            raise ValueError("Financing time moved backwards")
        day = p.last_financing_time.astimezone(NY).date()
        last = stamp.astimezone(NY).date()
        while day <= last:
            roll = datetime(day.year, day.month, day.day, 17, tzinfo=NY)
            if day.weekday() < 5 and p.last_financing_time < roll <= stamp:
                # Entry midpoint notional avoids charging financing on slippage.
                weight = 3 if day.weekday() == 2 else 1
                charge = self.entry_mid * p.size * self.case.financing_rate / 360 * weight
                self.balance -= charge
                self.costs += charge
                p.financing += charge
                self.rollovers += 1
            day += timedelta(days=1)
        p.last_financing_time = stamp

    def target(self, direction, candle, **kwargs):
        was_flat = self.position is None
        super().target(direction, candle, **kwargs)
        if was_flat and self.position is not None:
            field = kwargs.get("field", "open")
            self.entry_mid = (candle.quote("ask", field) + candle.quote("bid", field)) / 2


def active_gap(start, end):
    """Missing active 30m source slot; scheduled weekends alone do not reset rules."""
    cursor = start
    while cursor + timedelta(minutes=30) <= end:
        if expected_open(cursor, "dukascopy"):
            return True
        cursor += timedelta(minutes=30)
    return False


def prepare_frames(candles, start, end):
    """Audit finest bars; exclude aggregates with absent expected 30m constituents."""
    raw = tuple(candles)
    series = normalize(raw, completed_before=end)
    source_stamps = {c.timestamp for c in series}
    first = series[0].timestamp
    missing = []
    cursor = first
    while cursor < end:
        if expected_open(cursor, "dukascopy") and cursor not in source_stamps:
            missing.append(cursor)
        cursor += timedelta(minutes=30)
    frames, audits = {}, {}
    for tf in ("MINUTE_30", "HOUR", "HOUR_4"):
        aggregated = resample(series, tf, completed_before=end)
        # An absent active source bar cannot be hidden in an apparently complete H4 bar.
        bad = {int(t.timestamp()) // int(duration(tf).total_seconds()) for t in missing}
        filtered = tuple(c for c in aggregated if
                         int(c.timestamp.timestamp()) // int(duration(tf).total_seconds()) not in bad)
        scored = [c for c in filtered if c.timestamp >= start]
        frames[tf] = filtered
        audits[tf] = {
            "bars": len(scored), "first_start": scored[0].timestamp if scored else None,
            "last_end": scored[-1].end if scored else None, "discarded_incomplete_aggregates": len(aggregated) - len(filtered),
            "unexpected_gap_count": sum(active_gap(a.end, b.timestamp)
                                        for a, b in zip(scored, scored[1:])),
            "too_little_coverage": not scored or (scored[-1].end - scored[0].timestamp).days < 365 * 3 - 7,
        }
    audit = {
        "input_bars": len(raw), "identical_duplicates_removed": len(raw) - len(series),
        "missing_active_30m_bars": len(missing), "missing_timestamps": missing,
        "incomplete_candles_removed": sum(c.end > end for c in raw),
        "sparse_30m_source_candles": sum(c.observations < 30 for c in series)
        if series[0].source == "dukascopy" else None,
        "missing_minute_slots_within_observed_bars": sum(max(0, 30 - c.observations) for c in series)
        if series[0].source == "dukascopy" else None,
        "calendar": "NY Sunday 17:00 to Friday 17:00, DST-aware; holidays flagged as unexpected",
        "timeframes": audits,
    }
    return frames, audit


def simulate(candles, strategy, case, start, end):
    config = SimulationConfig(commission_per_order=0, slippage_pips=case.slippage_pips,
                              financing_per_unit_day=0)
    ledger = RolloverLedger(config, case)
    times = [c.timestamp for c in candles]
    lo, hi = bisect_left(times, start), bisect_left(times, end)
    selected = candles[max(0, lo - strategy.warmup):hi]
    history = deque(maxlen=strategy.warmup)
    pending = None
    pending_signal = None
    entry_signal = None
    entry_costs = None
    curve = [ledger.snapshot(start, ZERO, "initial")]
    trades, decisions = [], []
    previous = None
    exposure_seconds = ZERO
    held_since = None
    cancelled_entries = 0
    gap_holding_events = 0
    last = None

    def execute(target, candle, *, field="open", boundary=False, signal=None):
        nonlocal entry_signal, entry_costs, held_since, exposure_seconds
        before = len(ledger.trades)
        was_flat = ledger.position is None
        costs_before = ledger.embedded_spread, ledger.embedded_slippage
        ledger.target(target, execution_candle(candle, case), field=field)
        stamp = candle.open_time if field == "open" else candle.end
        if len(ledger.trades) > before:
            t = ledger.trades[-1]
            spread = ledger.embedded_spread - entry_costs[0]
            slip = ledger.embedded_slippage - entry_costs[1]
            exposure_seconds += dec((stamp - held_since).total_seconds())
            trades.append(dict(asdict(t), entry_signal_time=entry_signal,
                               exit_signal_time=signal, boundary_exit=boundary,
                               midpoint_gross_pnl=t.gross_pnl + spread + slip,
                               spread_cost=spread, slippage_cost=slip,
                               financing_cost=t.costs, commission_cost=ZERO,
                               total_cost=spread + slip + t.costs))
            held_since = None
        if was_flat and ledger.position:
            entry_signal = signal
            entry_costs = costs_before
            held_since = stamp

    for candle in selected:
        if previous and active_gap(previous.end, candle.timestamp):
            history.clear()
            if ledger.position:
                gap_holding_events += 1
            elif pending is not None:
                pending = None
                pending_signal = None
                cancelled_entries += 1
        eligible = start <= candle.timestamp and candle.end <= end
        if eligible:
            ledger.accrue_financing(candle.open_time)
            if pending_signal is not None:
                execute(pending, candle, signal=pending_signal)
            quote = execution_candle(candle, case)
            curve.append(ledger.snapshot(candle.open_time, ledger.liquidation_price(quote, "open"), "open"))
            ledger.accrue_financing(candle.end)
        history.append(candle)
        pending_signal = None
        if eligible and len(history) >= strategy.warmup:
            direction = ledger.position.direction if ledger.position else None
            pending = (strategy.target(tuple(history), direction) if strategy.name == "Donchian"
                       else strategy.target(tuple(history)))
            pending_signal = candle.end
            decisions.append({"timestamp": candle.end, "target": pending})
        if eligible:
            quote = execution_candle(candle, case)
            curve.append(ledger.snapshot(candle.end, ledger.liquidation_price(quote, "close"), "close"))
            last = candle
        previous = candle
    if last is None:
        raise ValueError("Empty scored window")
    if ledger.position:
        execute(None, last, field="close", boundary=True)
    curve.append(ledger.snapshot(end, ZERO, "boundary_flat"))
    result = SimulationResult(config, strategy.name, start, end, ledger, curve, decisions,
                              sum(start <= c.timestamp < end for c in selected))
    stats = statistics(result)
    stats.update({
        "midpoint_gross_pnl": sum((t["midpoint_gross_pnl"] for t in trades), ZERO),
        "fill_gross_pnl": sum((t["gross_pnl"] for t in trades), ZERO),
        "modeled_costs": ledger.embedded_spread + ledger.embedded_slippage + ledger.costs,
        "financing_cost": ledger.costs, "commission_cost": ZERO,
        "rollover_events": ledger.rollovers,
        "exposure_pct": exposure_seconds / dec((end - start).total_seconds()) * 100,
        "cancelled_gap_entries": cancelled_entries, "gap_holding_events": gap_holding_events,
        "boundary_quote_age_hours": dec((end - last.end).total_seconds()) / 3600,
        "boundary_exits": sum(t["boundary_exit"] for t in trades),
    })
    if abs(stats["midpoint_gross_pnl"] - stats["modeled_costs"] - stats["net_pnl"]) > Decimal("1e-20"):
        raise AssertionError("Cost reconciliation failed")
    return stats, trades, curve

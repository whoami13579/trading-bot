"""Research-window cost cases, rollover financing and snapshot frame preparation.

Simulation itself is the shared engine loop with the RESEARCH_WINDOW policy:
flat start, no warm-up entries, unexpected-gap resets and a costed synthetic
exit at the last completed close of every window.
"""

from bisect import bisect_left
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .calendar import active_gap, expected_open
from .engine import (Ledger, SimulationConfig, RESEARCH_WINDOW, statistics, PIP, ZERO,
                     simulate as run_policy)
from .models import OHLC, dec
from .timeframes import duration, normalize, resample

__all__ = ["CostCase", "COSTS", "execution_candle", "RolloverLedger", "active_gap",
           "prepare_frames", "research_config", "research_quotes", "simulate",
           "simulate_window"]

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


def research_quotes(case):
    """Quote transform for a cost case: signals keep the unmodified source candle."""
    return lambda candle: execution_candle(candle, case)


def research_config(case, *, initial_balance=Decimal(1000), size=Decimal(100),
                    leverage=Decimal(1)):
    """Explicit zero commission assumption; financing comes from the cost case."""
    return SimulationConfig(initial_balance, size, leverage, commission_per_order=0,
                            slippage_pips=case.slippage_pips, financing_per_unit_day=0)


def simulate_window(candles, strategy, case, start, end, *, config=None):
    """One flat-to-flat research window on the shared engine; returns (result, stats)."""
    config = config or research_config(case)
    ledger = RolloverLedger(config, case)
    times = [c.timestamp for c in candles]
    lo, hi = bisect_left(times, start), bisect_left(times, end)
    selected = candles[max(0, lo - strategy.warmup):hi]
    result = run_policy(selected, strategy, ledger, start=start, end=end,
                        policy=RESEARCH_WINDOW, quotes=research_quotes(case))
    trades = result.trade_records
    last = result.last_candle
    stats = statistics(result)
    stats.update({
        "midpoint_gross_pnl": sum((t["midpoint_gross_pnl"] for t in trades), ZERO),
        "fill_gross_pnl": sum((t["gross_pnl"] for t in trades), ZERO),
        "modeled_costs": ledger.embedded_spread + ledger.embedded_slippage + ledger.costs,
        "financing_cost": ledger.costs, "commission_cost": ZERO,
        "rollover_events": ledger.rollovers,
        "exposure_pct": result.exposure_seconds / dec((end - start).total_seconds()) * 100,
        "cancelled_gap_entries": result.cancelled_gap_entries,
        "gap_holding_events": result.gap_holding_events,
        "boundary_quote_age_hours": dec((end - last.end).total_seconds()) / 3600,
        "boundary_exits": sum(t["boundary_exit"] for t in trades),
    })
    if abs(stats["midpoint_gross_pnl"] - stats["modeled_costs"] - stats["net_pnl"]) > Decimal("1e-20"):
        raise AssertionError("Cost reconciliation failed")
    return result, stats


def simulate(candles, strategy, case, start, end):
    """Research-study signature: (stats, trade records, equity curve)."""
    result, stats = simulate_window(candles, strategy, case, start, end)
    return stats, result.trade_records, result.equity_curve

"""Explicit coverage assumptions, not inferred forward-filled market prices."""

from datetime import timedelta
from zoneinfo import ZoneInfo
from .models import DataError, utc
from datetime import datetime
from .timeframes import duration, floor_time


def expected_open(stamp, source):
    if source == "dukascopy":
        local = stamp.astimezone(ZoneInfo("America/New_York"))
        weekday, hour = local.weekday(), local.hour
        return (
            weekday < 4 or (weekday == 4 and hour < 17) or (weekday == 6 and hour >= 17)
        )
    # Yahoo EURUSD weekend edges differ from executable FX sessions. Only demand
    # the conservative core; explicitly disclose excluded edges in every report.
    return stamp.weekday() < 4 or (stamp.weekday() == 4 and stamp.hour < 21)


def audit_coverage(
    candles,
    start,
    end,
    source,
    timeframe,
    *,
    allow_gaps=False,
    provider_buckets_complete=False,
    no_quote_ranges=(),
):
    actual = {c.timestamp for c in candles}
    missing = []
    cursor = floor_time(start, timeframe)
    step = duration(timeframe)
    while cursor + step <= end:
        # Daily/H4 buckets intersecting the open session are required even if they start closed.
        active = any(
            expected_open(cursor + timedelta(minutes=m), source)
            for m in range(0, int(step.total_seconds() / 60), 60)
        )
        if cursor >= start and active and cursor not in actual:
            missing.append(cursor)
        cursor += step
    # Only explicit jumps in a successfully decoded bid/ask packet establish quote
    # absence. A successful HTTP response alone cannot excuse a truncated range.
    ranges = (
        [
            (utc(datetime.fromisoformat(a)), utc(datetime.fromisoformat(b)))
            for a, b in no_quote_ranges
        ]
        if provider_buckets_complete
        else []
    )
    unexplained = [
        stamp
        for stamp in missing
        if not any(a <= stamp and stamp + step <= b for a, b in ranges)
    ]
    if unexplained and not allow_gaps:
        raise DataError(
            f"Incomplete {source} history: {len(unexplained)} unexplained missing active intervals; first {unexplained[0].isoformat()}. Use --allow-data-gaps only to accept this explicitly."
        )
    return {
        "missing_intervals": len(missing),
        "missing_examples": [x.isoformat() for x in missing[:20]],
        "missing_timestamps": [x.isoformat() for x in missing],
        "provider_no_quote_intervals": len(missing) - len(unexplained),
        "unexplained_missing_intervals": len(unexplained),
        "gap_acceptance": "explicitly allowed" if allow_gaps else "strict",
        "calendar": "New York Sunday 17:00 to Friday 17:00; DST-aware"
        if source == "dukascopy"
        else "Yahoo conservative UTC core: Monday–Thursday and Friday before 21:00; weekend edges unverified",
        "forward_filled": False,
    }


def active_gap(start, end, timeframe="MINUTE_30"):
    """True if a whole candle slot of ``timeframe`` in [start, end) should have traded.

    ``start`` is the previous candle's end and ``end`` the next candle's start,
    so every whole ``timeframe`` slot between them is a missing candle. A slot is
    expected if any part of it lies inside the DST-aware New York Sunday 17:00 to
    Friday 17:00 FX week (a 4H slot that starts while closed but contains the
    Sunday open still counts). Scheduled weekend closures alone are not gaps.
    Session boundaries fall on whole New York hours, so probing each slot every
    min(timeframe, 30 minutes) is exact. For MINUTE_30, HOUR and HOUR_4 slots
    aligned to their timeframe the probes are identical to the original 30m scan.
    """
    step = duration(timeframe)
    probe = min(step, timedelta(minutes=30))
    cursor = start
    while cursor + step <= end:
        moment = cursor
        while moment < cursor + step:
            if expected_open(moment, "dukascopy"):
                return True
            moment += probe
        cursor += step
    return False

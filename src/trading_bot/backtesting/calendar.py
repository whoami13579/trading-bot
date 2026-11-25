"""Explicit coverage assumptions, not inferred forward-filled market prices."""

from datetime import timedelta
from zoneinfo import ZoneInfo
from .models import DataError, utc
from datetime import datetime, timezone
from .timeframes import duration, floor_time, resample


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


def slot_expected(start, step):
    """True if any part of the slot [start, start + step) lies in the FX week.

    Uses the DST-aware New York Sunday 17:00 to Friday 17:00 session, probing
    every min(step, 30 minutes) exactly as ``active_gap`` does.
    """
    probe = min(step, timedelta(minutes=30))
    moment = start
    while moment < start + step:
        if expected_open(moment, "dukascopy"):
            return True
        moment += probe
    return False


def completed_groups(candles, timeframe, *, as_of):
    """Constituents of each ``timeframe`` bucket that had completely closed by ``as_of``.

    ``candles`` are completed candles of one finer timeframe, ordered oldest
    first (as the engine supplies them). Buckets are UTC epoch-aligned, as in
    ``timeframes.resample``. A bucket is returned only if it ended at or before
    ``as_of`` and every finer slot inside it that the FX calendar expects to
    trade is present. A bucket still in progress at ``as_of``, a bucket cut off
    by the first candle and a bucket with a missing candle are excluded. Nothing
    is filled, and no candle after ``as_of`` is read.
    """
    if not candles:
        return ()
    step = duration(candles[0].timeframe)
    size = int(duration(timeframe).total_seconds())
    if size < step.total_seconds() or size % int(step.total_seconds()):
        raise DataError("Cannot upsample or use incompatible candle intervals")
    limit = utc(as_of).timestamp()
    groups, present = [], set()
    for candle in candles:
        if candle.end.timestamp() > limit:
            raise DataError("Candle completes after the decision time")
        present.add(candle.timestamp)
        seconds = int(candle.timestamp.timestamp())
        bucket = seconds - seconds % size
        if groups and groups[-1][0] == bucket:
            groups[-1][1].append(candle)
        elif groups and groups[-1][0] > bucket:
            raise DataError("Candles must be ordered oldest first")
        else:
            groups.append((bucket, [candle]))
    result = []
    for bucket, members in groups:
        if bucket + size > limit:
            continue  # Still in progress at the decision time.
        start = datetime.fromtimestamp(bucket, timezone.utc)
        cursor, complete = start, True
        while cursor < start + timedelta(seconds=size):
            if cursor not in present and slot_expected(cursor, step):
                complete = False
                break
            cursor += step
        if complete:
            result.append(tuple(members))
    return tuple(result)


def completed_aggregates(candles, timeframe, *, as_of):
    """OHLC candles of ``completed_groups``: the same aggregation as ``resample``."""
    aggregated = []
    for members in completed_groups(candles, timeframe, as_of=as_of):
        bucket = floor_time(members[0].timestamp, timeframe)
        aggregated.extend(
            c for c in resample(members, timeframe, completed_before=bucket + duration(timeframe))
        )
    return tuple(aggregated)

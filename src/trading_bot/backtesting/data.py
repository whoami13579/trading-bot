"""Provider-neutral warm-up, validated cache snapshots and completeness reports.

Two data sources feed every strategy through the same ``Dataset`` contract:
``load_history`` (Dukascopy/yfinance downloads behind an integrity-checked
cache) and ``load_snapshot`` (an immutable, fingerprinted study snapshot).
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from .calendar import audit_coverage
from .models import Candle, Dataset, DataError, json_value, utc
from .providers import provider
from .timeframes import duration, floor_time, normalize


# Download padding never depends on a strategy's smaller warm-up, so strategies
# sharing a source/symbol/timeframe/range reuse one cached normalized download.
# 48 is the HMA16 window, so existing HMA cache entries remain valid.
SHARED_WARMUP_FLOOR = 48
SNAPSHOT_TIMEFRAMES = ("MINUTE_30", "HOUR", "HOUR_4")


def warmup_start(start, timeframe, candles):
    # Include weekends and a buffer for session edges; actual candle count is checked.
    days = math.ceil(candles * duration(timeframe).total_seconds() / 86400 * 7 / 5) + 3
    return start - timedelta(days=days)


def load_history(
    source, symbol, timeframe, start, end, warmup, cache, *, allow_gaps=False, now=None,
    required_warmup=None,
):
    """Load ``warmup`` completed candles before ``start`` plus the scored range.

    Fewer than ``warmup`` prior candles is an error unless ``required_warmup``
    (default ``warmup``) still holds; the caller then handles the shortfall.
    """
    required = warmup if required_warmup is None else required_warmup
    start, end = utc(start), utc(end)
    now = utc(now or datetime.now(timezone.utc))
    if (
        start >= end
        or end > now
        or floor_time(start, timeframe) != start
        or floor_time(end, timeframe) != end
    ):
        raise DataError(
            "Historical boundaries must be completed, ordered and UTC-timeframe aligned"
        )
    fetch_start = warmup_start(start, timeframe, max(warmup, SHARED_WARMUP_FLOOR))
    key = json.dumps(
        {
            "schema": 3,
            "source": source,
            "symbol": symbol,
            "timeframe": timeframe,
            "start": fetch_start.isoformat(),
            "end": end.isoformat(),
        },
        sort_keys=True,
    )
    saved = cache.get(key)
    if saved is None:
        raw = provider(source, cache, now=now).fetch(
            symbol, timeframe, fetch_start, end
        )
        candles = normalize(raw.candles, completed_before=end)
        metadata = dict(raw.metadata)
    else:
        try:
            candles = normalize(
                [Candle.from_dict(c) for c in saved["candles"]], completed_before=end
            )
            metadata = dict(saved["metadata"])
        except (KeyError, ValueError, TypeError):
            raise DataError("Invalid normalized history cache") from None
    if not candles or any(
        c.source != source
        or c.symbol != symbol
        or c.timeframe != timeframe
        or not fetch_start <= c.timestamp < end
        for c in candles
    ):
        raise DataError(
            "No valid matching history returned for the requested instrument/range"
        )
    raw_candle_dicts = [c.as_dict() for c in candles]
    raw_metadata = dict(metadata)
    prior = [c for c in candles if c.end <= start]
    before = len(prior)
    performance = [c for c in candles if c.timestamp >= start]
    if before < required:
        raise DataError(
            f"Insufficient warm-up: need {required} completed candles, received {before}"
        )
    if not performance:
        raise DataError("No completed candles in the requested performance period")
    # Extra downloaded padding protects against weekends; it is neither used by
    # the indicator nor audited as if it were part of the simulation.
    candles = prior[-warmup:] + performance if warmup else performance
    if "missing_source_active_timestamps" in metadata:
        source_missing = metadata["missing_source_active_timestamps"]
        metadata["downloaded_missing_source_active_intervals"] = len(source_missing)
        metadata["missing_source_active_intervals"] = sum(
            candles[0].timestamp <= datetime.fromisoformat(stamp) < end
            for stamp in source_missing
        )
    if metadata.get("missing_source_active_intervals", 0) and not allow_gaps:
        raise DataError(
            "Incomplete Yahoo source candles inside aggregate intervals; use --allow-data-gaps only to accept this explicitly"
        )
    coverage = audit_coverage(
        candles,
        candles[0].timestamp,
        end,
        source,
        timeframe,
        allow_gaps=allow_gaps,
        provider_buckets_complete=metadata.get("complete_provider_buckets") is True,
        no_quote_ranges=metadata.get("no_quote_ranges", ()),
    )
    metadata.update(coverage)
    source_step = duration(metadata.get("source_resolution", timeframe))
    ratio = int(duration(timeframe) / source_step)
    metadata["sparse_candles"] = sum(c.observations < ratio for c in candles)
    metadata["missing_source_slots_in_existing_candles"] = sum(
        max(0, ratio - c.observations) for c in candles
    )
    metadata["holiday_calendar"] = (
        "Weekends modeled; exceptional holidays/halts require explicit gap acceptance"
    )

    metadata.update(
        {
            "warmup_candles": min(warmup, before),
            "downloaded_warmup_candles": before,
            "performance_candles": len(performance),
            "fetch_start": fetch_start.isoformat(),
            "actual_first": candles[0].timestamp.isoformat(),
            "actual_last_end": candles[-1].end.isoformat(),
            "requested_performance_start": start.isoformat(),
            "requested_performance_end": end.isoformat(),
            "cache_hit": saved is not None,
        }
    )
    # Fingerprint normalized raw observations, independent of cache/download metadata.
    metadata["data_sha256"] = fingerprint(candles)
    if saved is None:
        cache.put(key, {"candles": raw_candle_dicts, "metadata": raw_metadata})
    return Dataset(tuple(candles), start, end, source, symbol, timeframe, metadata)


def fingerprint(candles):
    """SHA256 of normalized candle observations, independent of any metadata."""
    encoded = json.dumps(
        [c.as_dict() for c in candles], default=json_value, sort_keys=True, allow_nan=False
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def load_snapshot(path, symbol, timeframe, start, end, warmup, *, required_warmup=None):
    """Use an immutable study snapshot; never downloads, authenticates or refreshes.

    The snapshot's own fingerprint is verified, then the same audited
    resampling as the research study (missing 30m constituents exclude an
    aggregate; nothing is forward-filled) produces the requested timeframe.
    """
    from .study_data import START, END, read_snapshot
    from .study_engine import prepare_frames

    start, end = utc(start), utc(end)
    required = warmup if required_warmup is None else required_warmup
    if symbol != "EURUSD":
        raise DataError("Study snapshots contain EURUSD only")
    if timeframe not in SNAPSHOT_TIMEFRAMES:
        raise DataError(
            f"Study snapshots are 30m bid/ask; supported timeframes are "
            f"{', '.join(SNAPSHOT_TIMEFRAMES)}. Use --data-source dukascopy for {timeframe}"
        )
    if not START <= start < end <= END:
        raise DataError(
            f"Snapshot backtests require {START.date()} <= start < end <= {END.date()}"
        )
    if any(floor_time(t, timeframe) != t for t in (start, end)):
        raise DataError(f"Start and end must align to completed UTC {timeframe} candles")
    candles, snapshot = read_snapshot(path)
    frames, audit = prepare_frames(candles, start, end)
    bars = frames[timeframe]
    prior = [c for c in bars if c.end <= start]
    performance = [c for c in bars if start <= c.timestamp and c.end <= end]
    label = {"MINUTE_30": "30m", "HOUR": "1H", "HOUR_4": "4H"}[timeframe]
    if len(prior) < required:
        raise DataError(
            f"Insufficient {label} warm-up: need {required} completed bars before "
            f"{start.isoformat()}, found {len(prior)}"
        )
    if not performance:
        raise DataError(
            f"Insufficient data: no completed {label} candles in the requested range"
        )
    used = tuple(prior[-warmup:] + performance) if warmup else tuple(performance)
    coverage = audit["timeframes"][timeframe]
    metadata = {
        "snapshot": str(path),
        "snapshot_metadata": snapshot,
        "snapshot_candles_sha256": snapshot.get("candles_sha256"),
        "quote_model": "historical bid/ask candles" if candles[0].bid
        else "midpoint only; spread assumed by the execution model",
        "source_resolution": candles[0].timeframe,
        "missing_active_30m_bars": audit["missing_active_30m_bars"],
        "missing_timestamps": audit["missing_timestamps"],
        "sparse_30m_source_candles": audit["sparse_30m_source_candles"],
        "missing_minute_slots_within_observed_bars":
            audit["missing_minute_slots_within_observed_bars"],
        "discarded_incomplete_aggregates": coverage["discarded_incomplete_aggregates"],
        "unexpected_gap_events": coverage["unexpected_gap_count"],
        "calendar": audit["calendar"],
        "forward_filled": False,
        "warmup_candles": min(len(prior), warmup),
        "downloaded_warmup_candles": len(prior),
        "performance_candles": len(performance),
        "actual_first": used[0].timestamp.isoformat(),
        "actual_last_end": used[-1].end.isoformat(),
        "requested_performance_start": start.isoformat(),
        "requested_performance_end": end.isoformat(),
        "data_sha256": fingerprint(used),
    }
    source = snapshot.get("source", candles[0].source)
    return Dataset(used, start, end, source, symbol, timeframe, metadata)

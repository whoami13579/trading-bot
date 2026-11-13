"""Provider-neutral warm-up, validated cache snapshots and completeness reports."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from .calendar import audit_coverage
from .models import Candle, Dataset, DataError, json_value, utc
from .providers import provider
from .timeframes import duration, floor_time, normalize


def warmup_start(start, timeframe, candles):
    # Include weekends and a buffer for session edges; actual candle count is checked.
    days = math.ceil(candles * duration(timeframe).total_seconds() / 86400 * 7 / 5) + 3
    return start - timedelta(days=days)


def load_history(
    source, symbol, timeframe, start, end, warmup, cache, *, allow_gaps=False, now=None
):
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
    fetch_start = warmup_start(start, timeframe, warmup)
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
    if before < warmup:
        raise DataError(
            f"Insufficient warm-up: need {warmup} completed candles, received {before}"
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
    candle_dicts = [c.as_dict() for c in candles]
    encoded = json.dumps(
        candle_dicts, default=json_value, sort_keys=True, allow_nan=False
    )
    metadata["data_sha256"] = hashlib.sha256(encoded.encode()).hexdigest()
    if saved is None:
        cache.put(key, {"candles": raw_candle_dicts, "metadata": raw_metadata})
    return Dataset(tuple(candles), start, end, source, symbol, timeframe, metadata)

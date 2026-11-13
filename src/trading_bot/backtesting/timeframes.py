"""UTC epoch-aligned candle aggregation; no generated or forward-filled prices."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from .models import Candle, DataError, OHLC, utc

MINUTES = {
    "MINUTE": 1,
    "MINUTE_5": 5,
    "MINUTE_15": 15,
    "MINUTE_30": 30,
    "HOUR": 60,
    "HOUR_4": 240,
    "DAY": 1440,
}


def duration(timeframe: str) -> timedelta:
    if timeframe not in MINUTES:
        raise DataError("Unsupported timeframe")
    return timedelta(minutes=MINUTES[timeframe])


def floor_time(timestamp: datetime, timeframe: str) -> datetime:
    timestamp = utc(timestamp)
    seconds = int(duration(timeframe).total_seconds())
    return datetime.fromtimestamp(
        int(timestamp.timestamp()) // seconds * seconds, timezone.utc
    )


def normalize(candles, *, completed_before=None):
    unique = {}
    for candle in candles:
        if completed_before is not None and candle.end > utc(completed_before):
            continue
        previous = unique.get(candle.timestamp)
        if previous is not None and previous != candle:
            raise DataError("Conflicting duplicate candle timestamps")
        unique[candle.timestamp] = candle
    result = tuple(unique[k] for k in sorted(unique))
    if result and any(
        (c.symbol, c.timeframe, c.source, bool(c.bid))
        != (
            result[0].symbol,
            result[0].timeframe,
            result[0].source,
            bool(result[0].bid),
        )
        for c in result
    ):
        raise DataError("Mixed candle instruments, sources, timeframes or quote types")
    return result


def aggregate(series):
    return OHLC(
        series[0].open,
        max(x.high for x in series),
        min(x.low for x in series),
        series[-1].close,
    )


def resample(candles, timeframe: str, *, completed_before: datetime):
    candles = normalize(candles, completed_before=completed_before)
    if not candles:
        return ()
    if duration(timeframe) < duration(candles[0].timeframe) or duration(
        timeframe
    ) % duration(candles[0].timeframe):
        raise DataError("Cannot upsample or use incompatible candle intervals")
    groups = defaultdict(list)
    for candle in candles:
        groups[floor_time(candle.timestamp, timeframe)].append(candle)
    result = []
    for stamp, group in sorted(groups.items()):
        if stamp + duration(timeframe) > utc(completed_before):
            continue
        first = group[0]
        result.append(
            Candle(
                stamp,
                aggregate([c.prices for c in group]),
                first.symbol,
                timeframe,
                first.source,
                aggregate([c.bid for c in group]) if first.bid else None,
                aggregate([c.ask for c in group]) if first.ask else None,
                first.open_time,
                sum(c.observations for c in group),
            )
        )
    return tuple(result)

"""Select the completed broker candles a scheduled cycle may use.

A scheduler boundary can be current while the broker's history is stale (market
closed, delayed feed) or ends with the still-forming candle. Only candles that have
completed by the boundary are used, and the most recent of them must be the one
expected for that boundary. Otherwise the cycle is skipped: no signal, no order.

This module must not import broker modules: backtests import the indicators.
"""

from datetime import datetime, timedelta, timezone
import logging
import math

from trading_bot.backtesting.timeframes import MINUTES

_LOGGER = logging.getLogger(__name__)


def _start(row):
    """Capital ``snapshotTimeUTC`` is the interval start in UTC; never local ``snapshotTime``."""
    stamp = datetime.fromisoformat(row["snapshotTimeUTC"])
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)


def _iso(value):
    return value.isoformat(timespec="seconds") if value is not None else "none"


def _skip(reason, timeframe, boundary, latest=None):
    _LOGGER.warning(
        "Scheduled cycle skipped reason=%s timeframe=%s boundary_utc=%s "
        "latest_completed_candle_utc=%s; no signal or order for this boundary",
        reason, timeframe, _iso(boundary), _iso(latest),
    )
    return None


def completed_closes(payload, timeframe: str, boundary: datetime):
    """Ask closes of the candles completed by ``boundary``, or None to skip the cycle.

    ``timeframe`` is the candle resolution, independent of the polling schedule.
    A candle starting at ``s`` completes at ``s + timeframe``. Candles that have not
    completed by the boundary are discarded. The latest completed candle is the
    expected one only if it ended within the last full candle period, which means
    after ``boundary - timeframe``. For UTC-aligned candles this is exactly the candle
    that starts at ``floor(boundary) - timeframe``.
    """
    step = timedelta(minutes=MINUTES[timeframe])
    boundary = boundary.astimezone(timezone.utc)
    try:
        rows = payload["prices"]
        if not isinstance(rows, list):
            raise TypeError
        candles = [(_start(row), row["closePrice"]["ask"]) for row in rows]
    except (KeyError, TypeError, ValueError, AttributeError):
        return _skip("invalid_candle_data", timeframe, boundary)
    if not all(
        isinstance(close, (int, float)) and not isinstance(close, bool) and math.isfinite(close)
        for _, close in candles
    ) or not all(a[0] < b[0] for a, b in zip(candles, candles[1:])):
        return _skip("invalid_candle_data", timeframe, boundary)  # Unordered or duplicate candles.
    completed = [(start, close) for start, close in candles if start + step <= boundary]
    latest = completed[-1][0] if completed else None
    if latest is None or latest + step <= boundary - step:
        return _skip("expected_candle_missing", timeframe, boundary, latest)
    return [close for _, close in completed]


def newest_opens(payload, count: int):
    """Ask opens of the newest ``count`` broker candles, or None to skip the cycle.

    This is the moving-average/SMA input policy, unchanged by the shared execution
    (issue #9): the window ends with the candle that has just started, whose open is a
    known price. It is not a completed-candle selection. Malformed or short history
    skips the cycle instead of deciding on fewer prices.
    """
    try:
        rows = payload["prices"]
        if not isinstance(rows, list):
            raise TypeError
        opens = [row["openPrice"]["ask"] for row in rows[-count:]]
    except (KeyError, TypeError, AttributeError):
        _LOGGER.warning("Scheduled cycle skipped reason=invalid_candle_data; no signal or order")
        return None
    if len(opens) < count or not all(
        isinstance(price, (int, float)) and not isinstance(price, bool) and math.isfinite(price)
        for price in opens
    ):
        _LOGGER.warning("Scheduled cycle skipped reason=insufficient_candle_data candles=%d "
                        "required=%d; no signal or order", len(opens), count)
        return None
    return opens

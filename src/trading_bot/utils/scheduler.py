import time
from datetime import datetime, timedelta, timezone
from collections.abc import Callable

SCHEDULE_TIMEFRAMES = ("MINUTE", "MINUTE_5", "MINUTE_15", "MINUTE_30")


def wait_until_targets(
    target_minutes, *, keepalive: Callable[[], object] | None = None
):
    """Wait for the next candle boundary and return it as an aware UTC datetime.

    Only future boundaries are scheduled, so boundaries missed while the caller was
    paused are skipped rather than replayed. Optionally validate a session every
    five minutes during the wait.
    """
    if (
        not isinstance(target_minutes, (list, tuple))
        or not target_minutes
        or any(
            type(value) is not int or not 0 <= value < 60 for value in target_minutes
        )
    ):
        raise ValueError("scheduler_requires_minutes_between_0_and_59")
    now = datetime.now()
    current_min = now.minute

    # Find the next target minute
    # We look for the first target greater than current_min
    future_targets = [t for t in target_minutes if t > current_min]

    if future_targets:
        # Next target is later this hour
        next_min = min(future_targets)
        target_time = now.replace(minute=next_min, second=0, microsecond=0)
    else:
        # Next target is the first one in the NEXT hour
        next_min = min(target_minutes)
        target_time = (now + timedelta(hours=1)).replace(
            minute=next_min, second=0, microsecond=0
        )

    # Calculate sleep duration
    delay = (target_time - now).total_seconds()
    delay += 2

    print(f"Current time: {now.strftime('%H:%M:%S')}")
    print(
        f"Next run at:  {target_time.strftime('%H:%M:%S')} (In {round(delay, 2)} seconds)"
    )

    boundary = target_time.astimezone(timezone.utc)
    if keepalive is None:
        time.sleep(delay)
        return boundary
    deadline = time.monotonic() + delay
    while (remaining := deadline - time.monotonic()) > 0:
        time.sleep(min(remaining, 300))
        if time.monotonic() < deadline:
            keepalive()  # Read-only session/account validation; failures propagate to the caller.
    return boundary


def schedule_interval_seconds(target_minutes) -> int:
    """Seconds between consecutive boundaries of an evenly spaced minute schedule."""
    if len(target_minutes) < 2:
        return 3600
    return (target_minutes[1] - target_minutes[0]) * 60


def timeframe_to_minutes(timeframe: str) -> list[int]:
    """Supported minute boundaries. Data resolution can be configured separately."""
    intervals = {"MINUTE": 1, "MINUTE_5": 5, "MINUTE_15": 15, "MINUTE_30": 30}
    if timeframe not in intervals:
        raise ValueError("unsupported_scheduler_timeframe")
    return list(range(0, 60, intervals[timeframe]))

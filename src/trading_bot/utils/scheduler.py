import time
from datetime import datetime, timedelta, timezone
from collections.abc import Callable

SCHEDULE_TIMEFRAMES = ("MINUTE", "MINUTE_5", "MINUTE_15", "MINUTE_30")


def wait_until_targets(
    target_minutes,
    *,
    keepalive: Callable[[], object] | None = None,
    poll: Callable[[], float | None] | None = None,
):
    """Wait for the next candle boundary and return it as an aware UTC datetime.

    Only future boundaries are scheduled, so boundaries missed while the caller was
    paused are skipped rather than replayed. Optionally validate a session every
    five minutes during the wait.

    ``poll`` is called as soon as the wait starts, then again after the number of
    seconds it returns, until it returns None. It must perform read-only checks only;
    it never runs a strategy evaluation, and its failures propagate to the caller.
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
    if keepalive is None and poll is None:
        time.sleep(delay)
        return boundary
    started = time.monotonic()
    deadline = started + delay
    next_keepalive = started + 300 if keepalive is not None else None
    next_poll = started if poll is not None else None
    while True:
        now = time.monotonic()
        if now >= deadline:
            return boundary
        if next_poll is not None and now >= next_poll:
            pause = poll()  # Read-only; never evaluates the strategy between boundaries.
            next_poll = None if pause is None else time.monotonic() + max(0.0, pause)
            continue
        wake = min(t for t in (deadline, next_keepalive, next_poll) if t is not None)
        time.sleep(max(0.0, wake - now))
        if next_keepalive is not None and next_keepalive <= (now := time.monotonic()) < deadline:
            keepalive()  # Read-only session/account validation; failures propagate to the caller.
            next_keepalive = time.monotonic() + 300


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

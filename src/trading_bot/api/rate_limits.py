"""Process-wide per-credential limits; broker enforces cross-process/account quotas."""

from collections import defaultdict, deque
import hashlib
import threading
import time

_LOCK = threading.Lock()
_STATE = defaultdict(
    lambda: {"request": None, "login": None, "orders": deque(), "topups": deque()}
)


def throttle(
    api_key: str,
    environment: str,
    method: str,
    path: str,
    account_id: str | None,
    *,
    user_identifier: str | None = None,
):
    # Hash keys so the registry cannot accidentally expose API credentials.
    key = hashlib.sha256(api_key.encode()).digest()
    with _LOCK:
        user_key = hashlib.sha256((user_identifier or api_key).encode()).digest()
        state = _STATE[user_key]
        login_state = _STATE[("login", key)]
        now = time.monotonic()
        delay = (
            max(0, 0.11 - (now - state["request"]))
            if state["request"] is not None
            else 0
        )
        login = method == "post" and path == "/api/v1/session"
        if login and login_state["login"] is not None:
            delay = max(delay, 1.05 - (now - login_state["login"]))
        if delay:
            time.sleep(delay)
        now = time.monotonic()
        queue = None
        if (
            environment == "demo"
            and method == "post"
            and path in ("/api/v1/positions", "/api/v1/workingorders")
        ):
            queue, period, maximum = (
                _STATE[(user_key, environment, account_id)]["orders"],
                3600,
                1000,
            )
        elif method == "post" and path == "/api/v1/accounts/topUp":
            queue = _STATE[(user_key, environment, account_id)]["topups"]
            period, maximum = 86400, 100
        if queue is not None:
            while queue and now - queue[0] >= period:
                queue.popleft()
            if len(queue) >= maximum:
                raise PermissionError("local_broker_quota_exhausted")
            queue.append(
                now
            )  # Count attempts conservatively; never retry a rejected mutation.
        state["request"] = now
        if login:
            login_state["login"] = now

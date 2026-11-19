"""Bounded retries for read-only GET requests only.

Retries never apply to login, logout or any trade-changing request (POST, PUT,
DELETE). A trade-changing request whose outcome is unknown must be reconciled,
not repeated. Classification uses the fixed reason codes from
``network_failure_reason`` and HTTP status codes, never exception text.
"""

from dataclasses import dataclass, field
import math
import random
import time
from collections.abc import Callable

# Failures where no usable HTTP response arrived, and a later attempt may succeed.
# TLS/certificate failures and unclassified request errors are deliberately absent:
# they can indicate interception or a programming error rather than an outage.
TRANSIENT_TRANSPORT_REASONS = frozenset(
    {
        "dns_resolution_failed",
        "connection_failed",
        "connection_timeout",
        "read_timeout",
        "network_timeout",
        "proxy_connection_failed",
    }
)
# Broker/gateway statuses that are safe to retry for a GET. 401/403 are
# authentication results and are never retried here.
RETRYABLE_READ_STATUSES = frozenset({429, 502, 503, 504})
TRANSIENT_REASONS = TRANSIENT_TRANSPORT_REASONS | frozenset(
    f"http_{status}" for status in RETRYABLE_READ_STATUSES
)

MAX_CONFIGURABLE_RETRIES = 10
MAX_CONFIGURABLE_DELAY = 600.0


def is_transient(reason) -> bool:
    """Whether a fixed failure reason describes a recoverable outage."""
    return isinstance(reason, str) and reason in TRANSIENT_REASONS


def bounded_seconds(value, name, *, minimum=0.0, maximum=MAX_CONFIGURABLE_DELAY):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid_{name}")
    value = float(value)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"invalid_{name}")
    return value


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with symmetric jitter: about 2s, 4s, 8s by default.

    ``max_retries`` counts additional attempts after the first request, so the
    default sends a read at most four times. The per-request bound is fixed;
    a read is never retried indefinitely.
    """

    max_retries: int = 3
    base_delay: float = 2.0
    max_delay: float = 30.0
    jitter: float = 0.2
    rng: random.Random | None = field(default=None, compare=False, repr=False)
    # None resolves time.sleep at call time.
    sleep: Callable[[float], object] | None = field(
        default=None, compare=False, repr=False
    )

    def __post_init__(self):
        if (
            type(self.max_retries) is not int
            or not 0 <= self.max_retries <= MAX_CONFIGURABLE_RETRIES
        ):
            raise ValueError("invalid_max_retries")
        base = bounded_seconds(self.base_delay, "retry_base_delay")
        ceiling = bounded_seconds(self.max_delay, "retry_max_delay")
        if ceiling < base:
            raise ValueError("invalid_retry_max_delay")
        jitter = bounded_seconds(self.jitter, "retry_jitter", maximum=0.99)
        object.__setattr__(self, "base_delay", base)
        object.__setattr__(self, "max_delay", ceiling)
        object.__setattr__(self, "jitter", jitter)

    def delay(self, retry_number: int) -> float:
        """Delay before retry ``retry_number`` (1-based), capped at ``max_delay``."""
        if type(retry_number) is not int or retry_number < 1:
            raise ValueError("invalid_retry_number")
        nominal = min(self.max_delay, self.base_delay * 2 ** (retry_number - 1))
        rng = self.rng if self.rng is not None else random
        jittered = nominal * (1 + rng.uniform(-self.jitter, self.jitter))
        return max(0.0, min(self.max_delay, jittered))

    def wait(self, seconds: float) -> None:
        (self.sleep if self.sleep is not None else time.sleep)(seconds)

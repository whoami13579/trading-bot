"""Pause scheduled trading during network outages and resume only when it is safe.

Safety model
------------
* A stopped ``TradingApi`` client is never revived. Recovery builds a fresh DEMO
  client that must probe connectivity, log in, rediscover the account and match
  the originally pinned account before broker state is reconciled.
* An uncertain trade-changing request (open, close, update, working order) is never
  recovered automatically. The bot stops and requires manual reconciliation.
* Authentication, permission, identity and malformed-response failures stop: they
  are not outages and repeated logins could lock an account.
* LIVE never resumes automatically: a new LIVE session requires the full preflight.
* Missed candle boundaries are never replayed and a boundary is executed at most once.

Log messages contain only fixed reason codes, counts and durations: never
credentials, tokens, account or deal identifiers, URLs or exception text.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import random
import signal
import sys
import threading
import time
from collections.abc import Callable

from trading_bot.api.network_errors import TradingUnavailable
from trading_bot.api.retry import is_transient, bounded_seconds

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryPolicy:
    """How a paused DEMO strategy checks for recovered connectivity.

    ``max_outage`` of 0 waits indefinitely, suitable for unattended operation.
    """

    enabled: bool = True
    interval: float = 60.0
    jitter: float = 0.1
    max_outage: float = 0.0
    rng: random.Random | None = field(default=None, compare=False, repr=False)

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("invalid_recovery_enabled")
        object.__setattr__(
            self, "interval", bounded_seconds(self.interval, "recovery_interval", minimum=1.0, maximum=3600.0)
        )
        object.__setattr__(self, "jitter", bounded_seconds(self.jitter, "recovery_jitter", maximum=0.99))
        object.__setattr__(
            self, "max_outage", bounded_seconds(self.max_outage, "max_outage", maximum=30 * 86400.0)
        )

    def next_delay(self) -> float:
        rng = self.rng if self.rng is not None else random
        return self.interval * (1 + rng.uniform(-self.jitter, self.jitter))


class ShutdownRequested(KeyboardInterrupt):
    """SIGINT/SIGTERM. A KeyboardInterrupt so in-flight requests are marked interrupted."""

    def __init__(self, signum=signal.SIGINT):
        super().__init__(signum)
        self.signum = signum

    @property
    def exit_code(self) -> int:
        return 128 + int(self.signum)


class GracefulShutdown:
    """Translate SIGINT/SIGTERM into ShutdownRequested at a safe point.

    A signal received while an order request is in flight is deferred until that
    single request returns, so stopping never itself creates an uncertain order.
    A second signal during the request forces an immediate interrupt.
    """

    SIGNALS = (signal.SIGINT, signal.SIGTERM)

    def __init__(self):
        self.requested = None
        self._critical = 0
        self._previous = {}

    def __enter__(self):
        if threading.current_thread() is threading.main_thread():
            for signum in self.SIGNALS:
                self._previous[signum] = signal.signal(signum, self._handle)
        return self

    def __exit__(self, *exc_info):
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)
        self._previous.clear()
        return False

    def _handle(self, signum, frame):
        if self._critical and self.requested is None:
            self.requested = signum
            _LOGGER.warning(
                "Shutdown requested signal=%s; deferred until the in-flight order "
                "request returns (repeat to force)", signal.Signals(signum).name,
            )
            return
        self.requested = signum
        raise ShutdownRequested(signum)

    def check(self):
        if self.requested is not None:
            raise ShutdownRequested(self.requested)

    @contextmanager
    def order_request(self):
        self.check()
        self._critical += 1
        try:
            yield
        finally:
            self._critical -= 1
        self.check()


def _utc_now():
    return datetime.now(timezone.utc)


class CycleGuard:
    """Execute each scheduled boundary at most once, and only while it is current.

    A cycle is stale once more than ``max_delay`` seconds, or one full schedule
    interval, have elapsed since its boundary. Stale cycles calculate no signal and
    submit no order; the strategy simply waits for the next future boundary.
    """

    def __init__(self, interval_seconds, max_delay_seconds, *, clock: Callable[[], datetime] | None = None):
        self.interval = bounded_seconds(interval_seconds, "schedule_interval", minimum=1.0, maximum=86400.0)
        self.max_delay = bounded_seconds(max_delay_seconds, "max_signal_delay", minimum=1.0, maximum=86400.0)
        self.clock = clock
        self.last = None
        self.current = None

    def _now(self):
        return (self.clock if self.clock is not None else _utc_now)()

    def _boundary(self, boundary):
        if isinstance(boundary, datetime):
            return boundary if boundary.tzinfo is not None else boundary.astimezone(timezone.utc)
        return self._now()  # A scheduler without boundary information starts now.

    def start(self, boundary) -> bool:
        boundary = self._boundary(boundary)
        if self.last is not None and boundary <= self.last:
            self.current = None
            _LOGGER.warning(
                "Scheduled cycle skipped reason=boundary_already_executed "
                "boundary_utc=%s", boundary.astimezone(timezone.utc).isoformat(timespec="seconds"),
            )
            return False
        self.last = self.current = boundary
        return self.fresh("cycle_start")

    def fresh(self, stage: str) -> bool:
        if self.current is None:
            return False
        age = (self._now() - self.current).total_seconds()
        if age > min(self.max_delay, self.interval):
            _LOGGER.warning(
                "Scheduled cycle skipped reason=stale_boundary stage=%s boundary_utc=%s "
                "age_seconds=%.1f max_delay_seconds=%.1f; no signal or order for this boundary",
                stage, self.current.astimezone(timezone.utc).isoformat(timespec="seconds"),
                age, min(self.max_delay, self.interval),
            )
            self.current = None
            return False
        return True


def _invalid():
    raise TradingUnavailable("invalid_api_response")


def reconcile_broker_state(api, symbol: str) -> dict:
    """Read and validate open positions and working orders on the recovered session.

    The strategy derives every decision from these broker records at its next
    cycle, so nothing is replayed from memory. Only counts are logged.
    """
    payload, status = api.getAllPositions()
    if status != 200 or not isinstance(payload, dict) or not isinstance(payload.get("positions"), list):
        _invalid()
    positions = payload["positions"]
    for row in positions:
        if not (
            isinstance(row, dict)
            and isinstance(row.get("market"), dict)
            and isinstance(row["market"].get("epic"), str)
            and isinstance(row.get("position"), dict)
            and isinstance(row["position"].get("dealId"), str)
        ):
            _invalid()
    working = api.getAllWorkingOrders()
    if not isinstance(working, dict) or not isinstance(working.get("workingOrders"), list):
        _invalid()
    orders = working["workingOrders"]
    for row in orders:
        if not (isinstance(row, dict) and isinstance(row.get("workingOrderData"), dict)):
            _invalid()
    summary = {
        "positions": len(positions),
        "symbol_positions": sum(row["market"]["epic"] == symbol for row in positions),
        "working_orders": len(orders),
        "symbol_working_orders": sum(
            row["workingOrderData"].get("epic") == symbol for row in orders
        ),
    }
    _LOGGER.info(
        "Broker state reconciled positions=%d symbol_positions=%d working_orders=%d "
        "symbol_working_orders=%d", *summary.values(),
    )
    if summary["symbol_working_orders"]:
        _LOGGER.warning(
            "Working orders exist for the strategy symbol; the strategy never creates, "
            "changes or cancels working orders. Review them on Capital.com."
        )
    return summary


def _uncertain(exc, api) -> bool:
    return getattr(exc, "outcome_uncertain", False) is True or (
        getattr(api, "_mutation_outcome_uncertain", False) is True
    )


class NetworkSupervisor:
    """Decide whether a stopped client may be replaced, and wait until it can be."""

    def __init__(
        self,
        policy: RecoveryPolicy,
        *,
        environment: str,
        symbol: str,
        reconcile: Callable = reconcile_broker_state,
        sleep: Callable[[float], object] | None = None,
        monotonic: Callable[[], float] | None = None,
        shutdown: GracefulShutdown | None = None,
    ):
        self.policy = policy
        self.environment = environment
        self.symbol = symbol
        self.reconcile = reconcile
        self._sleep = sleep
        self._monotonic = monotonic
        self.shutdown = shutdown
        self.outages = 0

    def sleep(self, seconds):
        (self._sleep if self._sleep is not None else time.sleep)(seconds)

    def monotonic(self):
        return (self._monotonic if self._monotonic is not None else time.monotonic)()

    def _require_recoverable(self, exc, api=None):
        reason = getattr(exc, "reason", None)
        if _uncertain(exc, api):
            _LOGGER.critical(
                "Trading stopped reason=%s environment=%s: a trade-changing request has an "
                "uncertain outcome. It is never resubmitted automatically. Manual "
                "reconciliation of positions and working orders is required before restart.",
                reason, self.environment.upper(),
            )
            raise exc
        if not is_transient(reason):
            _LOGGER.error(
                "Trading stopped reason=%s environment=%s: unrecoverable error; "
                "no automatic retry", reason, self.environment.upper(),
            )
            raise exc
        if not self.policy.enabled:
            _LOGGER.error(
                "Trading stopped reason=%s environment=%s: network recovery is disabled",
                reason, self.environment.upper(),
            )
            raise exc
        if self.environment != "demo":
            _LOGGER.error(
                "Trading stopped reason=%s environment=LIVE: LIVE trading never resumes "
                "automatically; restart the strategy to run the full LIVE preflight",
                reason,
            )
            raise exc

    def _wait_until(self, exc, attempt_once: Callable, *, phase: str):
        reason = exc.reason
        self.outages += 1
        started = self.monotonic()
        _LOGGER.warning(
            "Trading paused reason=%s environment=%s phase=%s recovery_interval_seconds=%.0f "
            "max_outage_seconds=%s; no signals or orders while paused",
            reason, self.environment.upper(), phase, self.policy.interval,
            f"{self.policy.max_outage:.0f}" if self.policy.max_outage else "unlimited",
        )
        attempt = 0
        while True:
            elapsed = self.monotonic() - started
            if self.policy.max_outage and elapsed >= self.policy.max_outage:
                _LOGGER.error(
                    "Trading stopped reason=%s environment=%s: outage lasted %.0f seconds, "
                    "exceeding max_outage_seconds=%.0f", reason, self.environment.upper(),
                    elapsed, self.policy.max_outage,
                )
                raise exc
            delay = self.policy.next_delay()
            if self.policy.max_outage:
                delay = min(delay, max(0.0, self.policy.max_outage - elapsed))
            if self.shutdown is not None:
                self.shutdown.check()
            self.sleep(delay)
            attempt += 1
            try:
                result = attempt_once()
            except TradingUnavailable as failure:
                if _uncertain(failure, None) or not is_transient(failure.reason):
                    _LOGGER.error(
                        "Recovery stopped reason=%s environment=%s attempt=%d: not a "
                        "network outage; no further automatic attempts",
                        failure.reason, self.environment.upper(), attempt,
                    )
                    raise failure
                reason = failure.reason
                _LOGGER.warning(
                    "Connectivity not yet recovered reason=%s attempt=%d outage_seconds=%.0f",
                    reason, attempt, self.monotonic() - started,
                )
                continue
            _LOGGER.info(
                "Connectivity recovered environment=%s attempt=%d outage_seconds=%.0f",
                self.environment.upper(), attempt, self.monotonic() - started,
            )
            return result

    def start(self, factory: Callable):
        """Create the initial client, waiting out a transient outage before any trade."""
        try:
            return factory()
        except TradingUnavailable as exc:
            self._require_recoverable(exc)
            api = self._wait_until(exc, factory, phase="startup")
            _LOGGER.info("Trading started after startup outage; waiting for the next scheduled boundary")
            return api

    def recover(self, exc: TradingUnavailable, api):
        """Return a verified replacement client, or re-raise ``exc`` to stop trading."""
        self._require_recoverable(exc, api)

        def attempt():
            client = api.recovered_client()
            _LOGGER.info(
                "Session reauthenticated environment=%s; account identity and environment "
                "match the original session", self.environment.upper(),
            )
            self.reconcile(client, self.symbol)
            return client

        client = self._wait_until(exc, attempt, phase="scheduled")
        _LOGGER.info(
            "Scheduled trading resumed environment=%s; missed boundaries are skipped and "
            "the next future boundary is used", self.environment.upper(),
        )
        return client


class _CurrentStderrHandler(logging.StreamHandler):
    """Write to the current sys.stderr, including when it is redirected."""

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, value):
        pass


def configure_runtime_logging():
    """Show INFO runtime events on stderr unless the application configured logging."""
    package = logging.getLogger("trading_bot")
    if logging.getLogger().handlers or package.handlers:
        return
    handler = _CurrentStderrHandler()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%SZ"
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    package.addHandler(handler)
    package.setLevel(logging.INFO)

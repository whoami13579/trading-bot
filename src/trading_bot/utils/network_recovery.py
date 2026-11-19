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
* A DEMO order acknowledged with HTTP 200 stays pending, across client replacement,
  until the broker confirms it and the position list reflects it. No further
  strategy order is sent meanwhile. It is polled between candle boundaries and,
  during an outage, the pause is bounded by its deadline: an overdue or
  inconsistent order stops trading for manual reconciliation.

Log messages contain only fixed reason codes, counts and durations: never
credentials, tokens, account or deal identifiers, URLs or exception text.
"""

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import logging
import random
import re
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


def broker_positions(api) -> list:
    """Every open position on the account, validated; malformed data stops trading."""
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
    return positions


def reconcile_broker_state(api, symbol: str) -> dict:
    """Read and validate open positions and working orders on the recovered session.

    The strategy derives every decision from these broker records at its next
    cycle, so nothing is replayed from memory. Only counts are logged.
    """
    positions = broker_positions(api)
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


_DEAL_REFERENCE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _same_size(value, expected) -> bool:
    try:
        return Decimal(str(value)) == Decimal(str(expected))
    except (InvalidOperation, ValueError):
        return False


class PendingOrders:
    """DEMO strategy orders acknowledged with HTTP 200 but not yet confirmed and reconciled.

    The broker can acknowledge an order before its confirmation is available or the
    position list reflects it. Deciding from that list too early could send the same
    order twice, so while any entry is unresolved no strategy order is allowed.

    Entries are held by the strategy, not by a client, so they survive client
    replacement after an outage. They are kept in memory only: a process restart loses
    them, and every exit with entries left warns that manual reconciliation is
    required. Only the read-only confirmation and positions endpoints are used; an
    order is never resubmitted.

    Every entry has a monotonic deadline ``timeout`` seconds after its acknowledgement,
    enforced independently of the candle schedule: ``poll`` runs during the scheduler
    wait, and the wait, read retries and outage pauses are all bounded by it.
    """

    def __init__(self, symbol, *, enabled=True, timeout=300.0, poll_interval=10.0,
                 monotonic=None, sleep=None):
        self.symbol = symbol
        self.enabled = enabled
        self.timeout = bounded_seconds(timeout, "order_confirmation_timeout", minimum=1.0, maximum=86400.0)
        self.poll_interval = bounded_seconds(
            poll_interval, "order_confirmation_poll_interval", minimum=0.1, maximum=self.timeout)
        self._monotonic = monotonic
        self._sleep = sleep
        self.entries = []

    def __len__(self):
        return len(self.entries)

    def monotonic(self):
        return (self._monotonic if self._monotonic is not None else time.monotonic)()

    def sleep(self, seconds):
        (self._sleep if self._sleep is not None else time.sleep)(seconds)

    def record_open(self, result, *, direction, size):
        self._record(result, operation="open", direction=direction, size=size)

    def record_close(self, result, *, deal_id):
        self._record(result, operation="close", deal_id=deal_id)

    def _record(self, result, **order):
        if not self.enabled:
            return  # LIVE confirms and verifies every order inside its execution gate.
        reference = result.get("dealReference") if isinstance(result, dict) else None
        if not isinstance(reference, str) or not _DEAL_REFERENCE.fullmatch(reference):
            reference = None  # Unconfirmable: the next resolution stops for manual review.
        self.entries.append({**order, "reference": reference, "since": self.monotonic(),
                             "announced": False})
        _LOGGER.info(
            "Order acknowledged operation=%s; no further strategy order until the broker "
            "confirms it and positions reflect it", order["operation"],
        )

    def _fail(self, reason, entry, detail):
        _LOGGER.critical(
            "Trading stopped reason=%s operation=%s detail=%s: a submitted order could not be "
            "reconciled with its broker confirmation and positions. It is never resubmitted "
            "automatically. Manual reconciliation of positions and working orders is "
            "required before restart.", reason, entry["operation"], detail,
        )
        raise TradingUnavailable(reason, mutation=True)

    def _opened_deal(self, confirmation, entry):
        affected = confirmation.get("affectedDeals")
        if not (
            confirmation.get("epic") == self.symbol
            and confirmation.get("direction") == entry["direction"]
            and _same_size(confirmation.get("size"), entry["size"])
            and isinstance(affected, list)
            and len(affected) == 1
            and isinstance(affected[0], dict)
            and affected[0].get("status") == "OPENED"
            and isinstance(affected[0].get("dealId"), str)
            and affected[0]["dealId"]
        ):
            self._fail("order_confirmation_mismatch", entry, "confirmation_differs_from_order")
        return affected[0]["dealId"]

    def _reconciled(self, api, entry, positions):
        """True when confirmed and reflected in positions; False while still pending."""
        if entry["reference"] is None:
            self._fail("order_confirmation_mismatch", entry, "missing_deal_reference")
        confirmation = api.getPositionOrderConfirmation(entry["reference"])
        if confirmation is None:
            return False  # HTTP 404: the broker has not published the confirmation yet.
        if not isinstance(confirmation, dict):
            self._fail("order_confirmation_mismatch", entry, "invalid_confirmation")
        status = confirmation.get("dealStatus")
        if status in ("PENDING", "UNKNOWN"):
            return False
        if status != "ACCEPTED" or confirmation.get("dealReference") != entry["reference"]:
            self._fail("order_confirmation_mismatch", entry, "confirmation_not_accepted")
        if not positions:
            positions.append(broker_positions(api))
        rows = positions[0]
        if entry["operation"] == "open":
            deal_id = self._opened_deal(confirmation, entry)
            return any(
                row["position"]["dealId"] == deal_id and row["market"]["epic"] == self.symbol
                for row in rows
            )
        return not any(row["position"]["dealId"] == entry["deal_id"] for row in rows)

    def deadline(self):
        """Monotonic time by which every pending entry must be resolved, or None."""
        if not self.entries:
            return None
        return min(entry["since"] for entry in self.entries) + self.timeout

    def pollable(self) -> bool:
        """Whether a confirmation can be looked up between candle boundaries."""
        return any(entry["reference"] is not None for entry in self.entries)

    def bounded_reads(self, api):
        """Limit the client's read-retry backoff to the remaining confirmation time."""
        deadline = self.deadline()
        if deadline is None or getattr(type(api), "read_retry_deadline", None) is None:
            return nullcontext()
        return api.read_retry_deadline(deadline)

    def stop_if_overdue(self):
        """Stop for manual reconciliation once the confirmation deadline is reached."""
        deadline = self.deadline()
        if deadline is not None and self.monotonic() >= deadline:
            self._fail("order_confirmation_unresolved", self.entries[0], "confirmation_timeout")

    def _check(self, api):
        """One read-only pass; drops every entry that is confirmed and reconciled."""
        positions = []  # Fetched at most once per pass, only after a confirmation.
        remaining = []
        with self.bounded_reads(api):
            for entry in self.entries:
                if self._reconciled(api, entry, positions):
                    _LOGGER.info(
                        "Order confirmed operation=%s; broker positions reflect it", entry["operation"]
                    )
                else:
                    remaining.append(entry)
        self.entries = remaining

    def resolve(self, api, *, attempts=1, delay=1.0) -> bool:
        """Return True once nothing is pending; False while confirmation is still pending.

        Raises TradingUnavailable (outcome uncertain) when a confirmation is rejected,
        malformed or inconsistent, or still unresolved after ``timeout`` seconds.
        Waits between attempts never extend past that deadline.
        """
        for attempt in range(attempts):
            if not self.entries:
                return True
            if attempt:
                remaining = self.deadline() - self.monotonic()
                if remaining <= 0:
                    break
                self.sleep(min(delay, remaining))
            self._check(api)
        if not self.entries:
            return True
        self.stop_if_overdue()
        _LOGGER.warning(
            "Scheduled cycle skipped reason=order_confirmation_pending pending_orders=%d "
            "age_seconds=%.0f timeout_seconds=%.0f; no signal or order until confirmed",
            len(self.entries), self.monotonic() - (self.deadline() - self.timeout), self.timeout,
        )
        return False

    def poll(self, api):
        """Between candle boundaries: check once; return seconds until the next check.

        Returns None once nothing is pending (or nothing can be looked up). Never
        evaluates a signal or sends an order; raises like ``resolve`` when overdue or
        inconsistent.
        """
        if not self.pollable():
            return None
        self._check(api)
        if not self.entries:
            return None
        self.stop_if_overdue()
        for entry in self.entries:
            if not entry["announced"]:
                entry["announced"] = True
                _LOGGER.warning(
                    "Order confirmation pending operation=%s; rechecking every %.0f seconds "
                    "for up to timeout_seconds=%.0f; no signal or order until confirmed",
                    entry["operation"], self.poll_interval, self.timeout,
                )
        return min(self.poll_interval, max(0.0, self.deadline() - self.monotonic()))


def warn_unconfirmed(pending):
    if pending is not None and len(pending):
        _LOGGER.critical(
            "%d acknowledged order(s) are not yet confirmed by the broker. They are not "
            "tracked across a restart: reconcile positions on Capital.com before restarting.",
            len(pending),
        )


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
        pending: PendingOrders | None = None,
    ):
        self.policy = policy
        self.environment = environment
        self.symbol = symbol
        self.reconcile = reconcile
        self._sleep = sleep
        self._monotonic = monotonic
        self.shutdown = shutdown
        self.pending = pending
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

    def _stop_at_deadline(self, exc, reason, started, deadline):
        now = self.monotonic()
        if deadline is not None and now >= deadline:
            _LOGGER.error(
                "Trading stopped reason=%s environment=%s: outage lasted %.0f seconds, "
                "reaching max_outage_seconds=%.0f; trading is not resumed", reason,
                self.environment.upper(), now - started, self.policy.max_outage,
            )
            raise exc

    def _wait_until(self, exc, attempt_once: Callable, *, phase: str):
        reason = exc.reason
        self.outages += 1
        started = self.monotonic()
        # A monotonic deadline: no probe starts, and no recovery resumes, at or after it.
        deadline = started + self.policy.max_outage if self.policy.max_outage else None
        _LOGGER.warning(
            "Trading paused reason=%s environment=%s phase=%s recovery_interval_seconds=%.0f "
            "max_outage_seconds=%s; no signals or orders while paused",
            reason, self.environment.upper(), phase, self.policy.interval,
            f"{self.policy.max_outage:.0f}" if self.policy.max_outage else "unlimited",
        )
        attempt = 0
        while True:
            self._stop_at_deadline(exc, reason, started, deadline)
            if self.pending is not None:
                # Unreachable confirmations cannot wait out an outage: stop at their deadline.
                self.pending.stop_if_overdue()
            delay = self.policy.next_delay()
            if deadline is not None:
                delay = min(delay, deadline - self.monotonic())
            confirm_by = self.pending.deadline() if self.pending is not None else None
            if confirm_by is not None:
                delay = min(delay, max(0.0, confirm_by - self.monotonic()))
            if self.shutdown is not None:
                self.shutdown.check()
            self.sleep(delay)
            self._stop_at_deadline(exc, reason, started, deadline)
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
            # A recovery that returns at or after the deadline is discarded, not resumed.
            self._stop_at_deadline(exc, reason, started, deadline)
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
        """Return a verified replacement client, or re-raise to stop trading.

        While orders await confirmation the pause ends at their deadline: one last
        check is attempted then, and trading stops if they are still unresolved.
        """
        self._require_recoverable(exc, api)

        def attempt():
            client = api.recovered_client()
            _LOGGER.info(
                "Session reauthenticated environment=%s; account identity and environment "
                "match the original session", self.environment.upper(),
            )
            if self.pending is None:
                self.reconcile(client, self.symbol)
                return client
            with self.pending.bounded_reads(client):
                self.reconcile(client, self.symbol)
                # Unconfirmed orders survive the client replacement and are rechecked on
                # the new session; inconsistent or overdue ones stop recovery here.
                self.pending.resolve(client)
            return client

        client = self._wait_until(exc, attempt, phase="scheduled")
        _LOGGER.info(
            "Scheduled trading resumed environment=%s pending_orders=%d; missed boundaries are "
            "skipped and the next future boundary is used", self.environment.upper(),
            len(self.pending) if self.pending is not None else 0,
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

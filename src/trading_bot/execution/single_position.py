"""Shared online execution for every scheduled single-position strategy.

A strategy supplies only what is specific to it: a market-data read bound to the
cycle boundary and a pure decision. This module owns the execution lifecycle and
reuses the existing mechanisms instead of re-implementing them:

* ``utils.scheduler.wait_until_targets``: UTC candle-boundary scheduling; missed
  boundaries are skipped, never replayed; session keepalive while waiting.
* ``utils.network_recovery.CycleGuard``: each boundary runs at most once and never
  once stale (``--max-signal-delay``); rechecked before the signal, immediately
  before every order and, in LIVE, by the gate immediately before the send.
* ``utils.network_recovery.PendingOrders``: a DEMO order acknowledged with HTTP 200
  is pending until the broker confirms it and the position list reflects it; no
  decision is made meanwhile, and an overdue or inconsistent order stops trading.
* ``utils.network_recovery.NetworkSupervisor``: DEMO pause/resume with a verified
  new client and broker reconciliation; uncertain orders, authentication failures
  and LIVE never resume automatically.
* ``utils.network_recovery.GracefulShutdown``: SIGINT/SIGTERM are deferred while
  the single order request is in flight. Stopping never closes positions.
* ``risk.live_execution.LiveExecution``: the LIVE gate (account, fees, limits,
  broker stop, lock, durable journal, synchronous confirmation), bound to the
  cycle deadline. Every LIVE order goes through it via the client.

Decision interface
------------------
Each cycle, ``strategy.signal(api, boundary)`` reads market data and returns any
strategy-specific value, or None when the signal is unavailable (for example a
missing completed candle): the cycle is then skipped. ``strategy.target(signal,
side)`` is pure: given that value and the verified side of the strategy-owned
position (``"BUY"``, ``"SELL"`` or None when flat) it returns one of:

* ``BUY`` / ``SELL``: hold a position on that side;
* ``FLAT``: hold no position (close an owned one);
* ``HOLD``: keep whatever is verified (no order).

Ownership
---------
Only positions this process opened, as confirmed by the broker, are owned, and
only owned positions are ever closed. Ownership is never inferred from the
instrument or the direction. DEMO ownership comes from the confirmation of each
order (``PendingOrders``) and is held in memory: after a restart nothing is owned,
so earlier positions are never adopted or closed. LIVE ownership is the gate's
journal-backed record. While any position the process does not own is open on
the instrument, no entry is sent: the account would no longer hold exactly the
strategy's position, and on a netting account the order could change it.

Single position
---------------
At most one owned position. A requested change from one side directly to the
other (a reversal) is never sent as an opposite order: it stops trading, with
the owned position left open (issue #10 decides the confirmed reversal).
"""

from collections.abc import Callable
from dataclasses import dataclass
import logging

from trading_bot.api.network_errors import TradingUnavailable
from trading_bot.config import strategy_defaults
from trading_bot.config.trading_config import network_policies
from trading_bot.utils.colors import colors
from trading_bot.utils.network_recovery import (
    CycleGuard,
    GracefulShutdown,
    NetworkSupervisor,
    PendingOrders,
    ShutdownRequested,
    configure_runtime_logging,
    warn_unconfirmed,
)
from trading_bot.utils.scheduler import schedule_interval_seconds

_LOGGER = logging.getLogger(__name__)

BUY, SELL, FLAT, HOLD = "BUY", "SELL", "FLAT", "HOLD"
SIDES = (BUY, SELL)
TARGETS = (BUY, SELL, FLAT, HOLD)


class ExecutionHalted(SystemExit):
    """Fail-closed stop. A SystemExit, so no broad ``except Exception`` continues."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(
            f"{reason}: trading stopped; no further order is sent and none is retried. "
            "Open positions are not closed: reconcile them on Capital.com before restarting."
        )


@dataclass(frozen=True)
class SinglePositionStrategy:
    """What one strategy contributes to the shared execution lifecycle."""

    key: str
    symbol: str
    size: float
    schedule: list  # Minute boundaries for wait_until_targets.
    signal: Callable  # (api, boundary) -> strategy value, or None when unavailable.
    target: Callable  # (value, verified side or None) -> BUY, SELL, FLAT or HOLD.
    probes: tuple = ()  # (timeframe, count) history reads of --read-only cycles.
    days: int = 7  # 5: wait while the FX market is closed.

    def __post_init__(self):
        if not self.schedule:
            raise ValueError("schedule_required")
        if any(not (isinstance(timeframe, str) and type(count) is int and count > 0)
               for timeframe, count in self.probes):
            raise ValueError("invalid_read_only_probe")


@dataclass(frozen=True)
class Verified:
    """Broker positions as seen by this cycle."""

    side: str | None  # Direction of the owned position; None when none is owned.
    deal_id: str | None
    unowned: int  # Positions on the instrument this process does not own.


class Ownership:
    """Deal IDs this process verifiably opened: the only positions it may close."""

    def __init__(self):
        self.gate = None  # LIVE: the armed execution gate, whose record is used.
        self._deals = {}  # DEMO: deal ID -> direction, from broker confirmations.

    def deals(self) -> dict:
        if self.gate is None:
            return dict(self._deals)
        owned = getattr(self.gate, "owned", None)
        if not isinstance(owned, dict):
            raise ExecutionHalted("live_ownership_record_unavailable")
        return {deal: entry["direction"] for deal, entry in owned.items()}

    def reconciled(self, entry):
        """``PendingOrders`` callback: a DEMO order confirmed and reflected in positions."""
        if entry["operation"] == "open":
            self._deals[entry["opened_deal_id"]] = entry["direction"]
            _LOGGER.info("Position ownership recorded: opened by this process and confirmed "
                         "by the broker")
        else:
            self._deals.pop(entry["deal_id"], None)

    def release(self, deal_id):
        self._deals.pop(deal_id, None)


class SinglePositionExecution:
    def __init__(self, strategy: SinglePositionStrategy, args, *, connect: Callable, wait: Callable):
        self.strategy = strategy
        self.args = args
        self.connect = connect  # (**create_trading_api keywords) -> authenticated client.
        self.wait = wait  # wait_until_targets-compatible scheduler.
        self.live = args.real is True
        self.read_only = getattr(args, "read_only", False) is True
        self.retry_policy, recovery_policy = network_policies(args)
        self.shutdown = GracefulShutdown()
        self.ownership = Ownership()
        # Held here, not by a client, so unconfirmed DEMO orders survive client replacement.
        self.pending = PendingOrders(
            strategy.symbol, enabled=not self.live,
            timeout=strategy_defaults.ORDER_CONFIRMATION_TIMEOUT,
            poll_interval=strategy_defaults.ORDER_CONFIRMATION_POLL_INTERVAL,
            on_reconciled=self.ownership.reconciled,
        )
        self.supervisor = NetworkSupervisor(
            recovery_policy, environment="live" if self.live else "demo",
            symbol=strategy.symbol, shutdown=self.shutdown, pending=self.pending,
        )
        self.guard = CycleGuard(schedule_interval_seconds(strategy.schedule), args.max_signal_delay)
        self.api = None  # The client in use, including after a recovery.
        self.desired = None  # Side requested by the latest decision; None for flat.
        self.verified = None  # Latest Verified broker state.
        self.halted = None
        self._unowned_reported = 0

    # --- lifecycle ---------------------------------------------------------

    def run(self):
        configure_runtime_logging()
        try:
            with self.shutdown:
                # A transient DEMO startup outage waits before any trade.
                if self.read_only:
                    self.api = self.supervisor.start(lambda: self.connect(order_execution=False))
                    self.api.forbid_mutations()  # Refused by the client before any transport.
                else:
                    self.api = self.supervisor.start(self.connect)
                self.api.configure_read_retries(self.retry_policy)
                if self.live:
                    gate = getattr(self.api, "_live_execution", None)
                    if gate is not None:
                        # The gate reads account and market state before sending; it rechecks
                        # this cycle's deadline immediately before the order itself.
                        gate.bind_cycle(lambda: self.guard.fresh("live_send"))
                        self.ownership.gate = gate
                self._loop()
            self._report("Scheduled cycles complete")
        except ShutdownRequested as stop:
            self._report_shutdown()
            raise SystemExit(stop.exit_code) from None
        except KeyboardInterrupt:
            self._report_shutdown()
            raise SystemExit(130) from None
        finally:
            # Every exit, including normal --cycles completion and any stop: acknowledged
            # orders still awaiting confirmation must be reconciled before a restart.
            warn_unconfirmed(self.pending)

    def _pending_poll(self):
        return {"poll": lambda: self.pending.poll(self.api)} if self.pending.pollable() else {}

    def _loop(self):
        completed = 0
        while self.args.cycles is None or completed < self.args.cycles:
            try:
                # Pending confirmations are polled (reads only) while waiting, so their
                # deadline does not depend on the candle schedule.
                boundary = self.wait(self.strategy.schedule, keepalive=self.api.prepare_trading_cycle,
                                     **self._pending_poll())
                completed += 1
                if self.strategy.days == 5 and not self.api.is_market_open():
                    # Confirmation checks and their deadline stay active over the weekend.
                    # The boundary that found the market closed is stale by reopening.
                    self.api.wait_until_open(**self._pending_poll())
                # Each boundary runs at most once, and never after it has become stale.
                if not self.guard.start(boundary):
                    continue
                renewed = self.api.prepare_trading_cycle()
                if self.read_only:
                    self._probe(completed, renewed)
                    continue
                self._cycle()
            except TradingUnavailable as unavailable:
                # A verified replacement client, or a re-raise to stop: uncertain orders,
                # authentication/identity failures and LIVE never resume.
                self.api = self.supervisor.recover(unavailable, self.api)
            except Exception as error:
                if self.read_only:
                    raise SystemExit("DEMO read-only cycle failed; no order was submitted.") from None
                print(f"{colors.WARNING}Error in main loop: {error}{colors.ENDC}")

    def _probe(self, cycle, renewed):
        """--read-only: session and history reads only; no signal, position read or order."""
        for timeframe, count in self.strategy.probes:
            history, code = self.api.getHistoricalPrices(self.strategy.symbol, timeframe, count)
            print(f"DEMO read-only cycle {cycle}: {timeframe} history HTTP {code}; "
                  f"{len(history['prices'])} prices; session {'renewed' if renewed else 'validated'}")

    def _cycle(self):
        api = self.api
        # Positions are only trustworthy once every acknowledged order is reflected.
        if not self.pending.resolve(api):
            return
        if not self.guard.fresh("signal"):
            return
        value = self.strategy.signal(api, self.guard.current)
        if value is None:
            return  # Signal unavailable for this boundary: no decision, no order.
        verified = self.verified = self._verify(api)
        self._transition(api, verified, self.strategy.target(value, verified.side))

    # --- broker state ------------------------------------------------------

    def _verify(self, api) -> Verified:
        """The owned position from the broker's list; never from requested orders."""
        symbol = self.strategy.symbol
        listed = api.getAllPositionsList()  # LIVE: the client lists gate-owned positions only.
        owned = self.ownership.deals()
        listed_ids = {position.dealId for position in listed}
        for deal_id in [deal for deal in owned if deal not in listed_ids]:
            if self.live:
                # The journal still records it, so the gate refuses every later entry.
                self._halt("live_owned_position_closed_outside_strategy",
                           "an owned LIVE position is no longer listed (broker stop, manual close "
                           "or margin closure); the LIVE journal must be reconciled")
            self.ownership.release(deal_id)
            _LOGGER.warning(
                "Owned position no longer listed reason=closed_outside_strategy: closed by a broker "
                "stop, manually or by the broker; ownership released, strategy is flat"
            )
        mine = [position for position in listed if position.dealId in owned]
        if len(mine) > 1 or any(position.epic != symbol for position in mine):
            self._halt("owned_positions_inconsistent",
                       "more than one owned position, or one on another instrument")
        unowned = sum(position.epic == symbol and position.dealId not in owned for position in listed)
        if unowned != self._unowned_reported:
            self._unowned_reported = unowned
            if unowned:
                _LOGGER.warning(
                    "Positions not owned by this process positions=%d on the strategy instrument: "
                    "never adopted or closed (manual, another strategy or an earlier run); no entry "
                    "while they are open", unowned,
                )
        if not mine:
            return Verified(None, None, unowned)
        deal_id = mine[0].dealId
        return Verified(owned[deal_id], deal_id, unowned)

    # --- transitions -------------------------------------------------------

    def _transition(self, api, verified, target):
        if target not in TARGETS:
            self._halt("invalid_strategy_target", "the strategy returned an unknown target")
        desired = verified.side if target == HOLD else None if target == FLAT else target
        self.desired = desired
        if desired == verified.side:
            return  # Already there: no order.
        if desired is None:
            self._close(api, verified.deal_id)
        elif verified.side is None:
            if verified.unowned:
                _LOGGER.warning(
                    "Entry blocked reason=unowned_position_on_instrument positions=%d side=%s: "
                    "no order; reconcile positions on Capital.com", verified.unowned, desired,
                )
                return
            self._open(api, desired)
        else:
            # Never an opposite order over an open position: depending on the account it
            # closes, hedges or is refused. A confirmed close-then-open is issue #10.
            self._halt("reversal_requires_confirmed_close",
                       f"the strategy requested {verified.side} to {desired}; the single-position "
                       "engine does not reverse; the owned position stays open")

    def _mutate(self, request):
        """Send one order request; any outcome other than a clean return is uncertain."""
        try:
            with self.shutdown.order_request():
                return request()
        except (TradingUnavailable, KeyboardInterrupt):
            raise  # Already classified: the client flags uncertain mutations itself.
        except Exception:
            self._halt("order_outcome_uncertain", "the order request failed unexpectedly")

    def _open(self, api, side):
        if not self.pending.resolve(api) or not self.guard.fresh("order"):
            return
        symbol, size = self.strategy.symbol, self.strategy.size

        def request():
            result, code = api.createPosition(symbol, side, size)
            if code == 200:  # Recorded before a deferred shutdown can take effect.
                self.pending.record_open(result, direction=side, size=size)
            return result, code

        result, code = self._mutate(request)
        if code is None:
            return  # LIVE gate: the cycle expired during its checks; nothing was sent.
        if code != 200:
            self._halt("order_response_unexpected", f"entry answered HTTP {code}")
        color = colors.BLUE if side == BUY else colors.RED
        print(f">>> {color}{side}{colors.ENDC} order submitted")

    def _close(self, api, deal_id):
        # The close needs every earlier order confirmed and a still-current boundary.
        if not self.pending.resolve(api, attempts=3) or not self.guard.fresh("order"):
            return

        def request():
            result, code = api.closePosition(deal_id)
            if code == 200:  # Recorded before a deferred shutdown can take effect.
                self.pending.record_close(result, deal_id=deal_id)
            return result, code

        result, code = self._mutate(request)
        if code is None:
            return  # LIVE gate: the cycle expired during its checks; nothing was sent.
        if code != 200:
            self._halt("order_response_unexpected", f"close answered HTTP {code}")
        print(f">>> {colors.YELLOW}close position{colors.ENDC}")

    # --- stops and reports -------------------------------------------------

    def status(self) -> dict:
        """In-memory execution state; no broker request."""
        entries = list(self.pending.entries)
        try:
            owned = tuple(self.ownership.deals())
        except ExecutionHalted:
            owned = ()
        uncertain = getattr(self.api, "_mutation_outcome_uncertain", False) is True
        pending_entries = sum(entry["operation"] == "open" for entry in entries)
        pending_exits = sum(entry["operation"] == "close" for entry in entries)
        phase = ("halted" if self.halted or uncertain else "entry_pending" if pending_entries
                 else "exit_pending" if pending_exits else "open" if owned else "flat")
        return {"phase": phase, "desired": self.desired,
                "verified": self.verified.side if self.verified else None,
                "owned_positions": len(owned), "pending_entries": pending_entries,
                "pending_exits": pending_exits, "outcome_uncertain": uncertain}

    def _exposure_note(self, state):
        note = (f"strategy_owned_positions={state['owned_positions']} "
                f"pending_orders={state['pending_entries'] + state['pending_exits']}")
        if state["owned_positions"] and not self.live:
            note += ("; DEMO ownership is not kept across a restart: a restarted strategy never "
                     "adopts or closes these positions")
        return note

    def _halt(self, reason, detail):
        self.halted = reason
        _LOGGER.critical(
            "Trading stopped reason=%s strategy=%s environment=%s: %s. No order is retried and "
            "open positions are not closed (%s). Reconcile positions on Capital.com before "
            "restarting.", reason, self.strategy.key, "LIVE" if self.live else "DEMO", detail,
            self._exposure_note(self.status()),
        )
        raise ExecutionHalted(reason)

    def _report_shutdown(self):
        self._report("Shutdown complete")

    def _report(self, event):
        state = self.status()
        _LOGGER.warning(
            "%s: no further signals or orders. Open positions are not closed (%s)%s",
            event, self._exposure_note(state),
            "; an order request was interrupted and its outcome is uncertain: reconcile on "
            "Capital.com before restarting" if state["outcome_uncertain"] else "",
        )


def run(strategy: SinglePositionStrategy, args, *, connect: Callable, wait: Callable):
    """Run ``strategy`` until ``--cycles`` or a stop; see the module documentation."""
    return SinglePositionExecution(strategy, args, connect=connect, wait=wait).run()

"""Shared single-position execution (issue #9): order lifecycle, broker-confirmed
ownership, scheduling, recovery, shutdown and read-only safety.

Offline only: a method-level fake DEMO broker, a deterministic clock and the real
CycleGuard, PendingOrders, NetworkSupervisor and GracefulShutdown.
"""

import argparse
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import logging
import signal
import unittest
from unittest.mock import patch

from trading_bot.api.client import TradingApi
from trading_bot.api.models import TradingPosition
from trading_bot.api.network_errors import TradingUnavailable
from trading_bot.config.trading_config import add_execution_arguments, add_trading_arguments
from trading_bot.execution import single_position as engine
from trading_bot.execution.single_position import (
    BUY, FLAT, HOLD, SELL, ExecutionHalted, SinglePositionExecution, SinglePositionStrategy,
)
from trading_bot.utils.candles import completed_closes

BOUNDARY = datetime(2026, 11, 17, 22, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=1)  # Boundaries one minute apart: inside the 300 s confirmation deadline.


def minutes(count):
    return [BOUNDARY + i * STEP for i in range(count)]


class Clock:
    def __init__(self):
        self.now = BOUNDARY - timedelta(minutes=10)
        self.mono = 1000.0

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.mono += seconds

    def at(self, moment):
        self.advance((moment - self.now).total_seconds())


class Broker:
    """A DEMO account: positions, confirmations and order calls. ``mode`` decides what an
    opposite market order would do (the engine must never send one over a position)."""

    def __init__(self, clock, mode="hedging"):
        self.clock, self.mode = clock, mode
        self.positions = []  # (epic, deal_id, direction, size)
        self.confirmations = {}
        self.orders = []  # ("open", direction) / ("close", deal_id)
        self.calls = []
        self.confirm_status = "ACCEPTED"
        self.lag = False  # Confirmations unpublished and changes not yet listed.
        self.unlisted_opens = False  # New positions confirmed but not yet listed.
        self.hidden = []
        self.mutation_error = None
        self.unavailable = None  # Raised by the next read, then cleared.
        self.status = 200
        self.tamper = None  # Rewrites a confirmation before it is served.
        self.forbidden = False
        self.recoveries = 0
        self._mutation_outcome_uncertain = False
        self.hook = None  # Called with the method name before it is served.

    def _call(self, name):
        self.calls.append(name)
        if self.hook is not None:
            self.hook(name)
        if self.unavailable is not None and name in ("prepare_trading_cycle", "getAllPositionsList"):
            reason, self.unavailable = self.unavailable, None
            raise TradingUnavailable(reason)

    def add(self, deal_id, direction="BUY", epic="EURUSD", size=100):
        self.positions.append((epic, deal_id, direction, size))

    def listed(self):
        return [p for p in self.positions if p[1] not in self.hidden]

    # Session and scheduling.
    def prepare_trading_cycle(self):
        self._call("prepare_trading_cycle")
        return False

    def is_market_open(self):
        return True

    def configure_read_retries(self, policy):
        self.retry_policy = policy

    def forbid_mutations(self):
        self.forbidden = True

    def recovered_client(self):
        self.recoveries += 1
        return self

    def getAllWorkingOrders(self):
        return {"workingOrders": []}

    # Reads.
    def getHistoricalPrices(self, symbol, timeframe, count):
        self._call("getHistoricalPrices")
        return {"prices": [{}] * count}, 200

    def getAllPositions(self):
        self._call("getAllPositions")
        return {"positions": [{"market": {"epic": e}, "position": {"dealId": d, "direction": s, "size": z}}
                              for e, d, s, z in self.listed()]}, 200

    def getAllPositionsList(self):
        self._call("getAllPositionsList")
        return [TradingPosition(epic=e, dealId=d) for e, d, _, _ in self.listed()]

    def getPositionOrderConfirmation(self, reference):
        self._call("getPositionOrderConfirmation")
        if self.lag or reference not in self.confirmations:
            return None
        value = {**self.confirmations[reference], "dealStatus": self.confirm_status}
        return self.tamper(value) if self.tamper else value

    # Mutations.
    def _mutate(self, kind, value):
        self._call(kind)
        if self.forbidden:
            raise PermissionError("read_only_mode_forbids_mutations")
        self.orders.append((kind, value))
        if self.mutation_error is not None:
            raise self.mutation_error
        return f"ref-{len(self.orders)}"

    def createPosition(self, epic, direction, size):
        reference = self._mutate("open", direction)
        deal_id = f"deal-{len(self.orders)}"
        opposite = [p for p in self.positions if p[0] == epic and p[2] != direction]
        if self.mode == "netting" and opposite:
            self.positions.remove(opposite[0])  # Netting: the opposite order reduces it.
            affected = [{"dealId": opposite[0][1], "status": "FULLY_CLOSED"}]
        else:
            self.add(deal_id, direction, epic, size)
            affected = [{"dealId": deal_id, "status": "OPENED"}]
        if self.lag or self.unlisted_opens:
            self.hidden.append(deal_id)
        self.confirmations[reference] = {"dealReference": reference, "epic": epic, "direction": direction,
                                         "size": size, "affectedDeals": affected}
        return {"dealReference": reference}, self.status

    def closePosition(self, deal_id):
        reference = self._mutate("close", deal_id)
        closed = [p for p in self.positions if p[1] == deal_id]
        self.positions = [p for p in self.positions if p[1] != deal_id]
        if self.lag:
            self.positions.extend(closed)  # Still listed until released.
            self.closing = closed
        self.confirmations[reference] = {"dealReference": reference, "epic": "EURUSD",
                                         "affectedDeals": [{"dealId": deal_id, "status": "FULLY_CLOSED"}]}
        return {"dealReference": reference}, self.status

    def release(self):
        self.lag = False
        self.hidden.clear()
        for closed in getattr(self, "closing", []):
            if closed in self.positions:
                self.positions.remove(closed)
        self.closing = []


def arguments(*flags):
    parser = argparse.ArgumentParser()
    add_trading_arguments(parser)
    add_execution_arguments(parser)
    return parser.parse_args(["--network-retry-jitter", "0", *flags])


class Harness:
    """Runs the real engine for ``signals`` (one per boundary) against the fake broker."""

    def __init__(self, case, signals, *, broker=None, flags=(), target=None, on_boundary=None,
                 boundaries=None, signal_hook=None, days=7):
        self.case = case
        self.clock = Clock()
        self.broker = broker or Broker(self.clock)
        self.broker.clock = self.clock
        self.signals = list(signals)
        self.boundaries = boundaries or [BOUNDARY + i * STEP for i in range(len(self.signals))]
        self.on_boundary = on_boundary
        self.signal_hook = signal_hook
        self.days = days
        self.seen = []  # (signal value, verified side) per decision.
        self.decisions = 0
        self.connects = []
        self.flags = ("--cycles", str(len(self.boundaries)), *flags)
        self.target = target or self.side_target

    @staticmethod
    def side_target(value, side):
        return value

    def signal(self, api, boundary):
        value = self.signals[self.decisions]
        self.decisions += 1
        if self.signal_hook is not None:
            self.signal_hook(self)
        return value

    def target_of(self, value, side):
        self.seen.append((value, side))
        return self.target(value, side)

    def wait(self, schedule, *, keepalive=None, poll=None):
        index = len(self.waits)
        self.waits.append(bool(poll))
        if poll is not None:
            poll()  # One read-only confirmation check while waiting.
        boundary = self.boundaries[index]
        self.clock.at(boundary + timedelta(seconds=2))
        if self.on_boundary is not None:
            self.on_boundary(self, index)
        return boundary

    def connect(self, **options):
        self.connects.append(options)
        return self.broker

    def build(self, *extra):
        self.waits = []
        self.args = arguments(*self.flags, *extra)
        strategy = SinglePositionStrategy(
            key="test", symbol="EURUSD", size=100, schedule=[0, 30], signal=self.signal,
            target=self.target_of, probes=(("MINUTE_30", 48),), days=self.days)
        return SinglePositionExecution(strategy, self.args, connect=self.connect, wait=self.wait)

    def run(self, *extra, expect=None):
        self.execution = self.build(*extra)
        with ExitStack() as stack:
            stack.enter_context(patch("time.monotonic", side_effect=lambda: self.clock.mono))
            stack.enter_context(patch("time.sleep", side_effect=self.clock.advance))
            stack.enter_context(patch("trading_bot.utils.network_recovery._utc_now",
                                      side_effect=lambda: self.clock.now))
            stack.enter_context(patch("random.uniform", return_value=0.0))
            self.stdout = stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            logs = stack.enter_context(self.case.assertLogs("trading_bot", level="INFO"))
            logging.getLogger("trading_bot.tests").info("run start")
            self.raised = None
            if expect is None:
                self.execution.run()
            else:
                with self.case.assertRaises(expect) as caught:
                    self.execution.run()
                self.raised = caught.exception
        self.output = "\n".join(logs.output)
        return self.raised

    @property
    def orders(self):
        return self.broker.orders


class DecisionInterfaceTests(unittest.TestCase):
    def test_targets_distinguish_flat_hold_and_an_unavailable_signal(self):
        cases = (
            # signals per boundary -> orders
            ([BUY, BUY, HOLD, FLAT], [("open", BUY), ("close", "deal-1")]),
            ([BUY, None, None, HOLD], [("open", BUY)]),  # None: no decision, the owned position stays.
            ([FLAT, HOLD, FLAT, SELL], [("open", SELL)]),  # Flat stays flat without orders.
        )
        for signals, orders in cases:
            with self.subTest(signals=signals):
                run = Harness(self, signals)
                run.run()
                self.assertEqual(run.orders, orders)
        # An unavailable signal never reads positions or reaches the decision.
        run = Harness(self, [None, None])
        run.run()
        self.assertEqual(run.seen, [])
        self.assertNotIn("getAllPositionsList", run.broker.calls)

    def test_the_decision_receives_the_verified_owned_side(self):
        run = Harness(self, [BUY, HOLD, FLAT, HOLD])
        run.run()
        self.assertEqual([side for _, side in run.seen], [None, BUY, BUY, None])

    def test_an_unknown_target_stops_without_an_order(self):
        run = Harness(self, ["LONG"])
        stopped = run.run(expect=ExecutionHalted)
        self.assertEqual(stopped.reason, "invalid_strategy_target")
        self.assertEqual(run.orders, [])

    def test_strategy_definition_is_validated(self):
        with self.assertRaises(ValueError):
            SinglePositionStrategy("x", "EURUSD", 100, [], lambda *a: None, lambda *a: HOLD)
        with self.assertRaises(ValueError):
            SinglePositionStrategy("x", "EURUSD", 100, [0], lambda *a: None, lambda *a: HOLD,
                                   probes=(("MINUTE", 0),))


class OrderLifecycleTests(unittest.TestCase):
    def test_entry_and_exit_are_confirmed_before_the_state_advances(self):
        phases = []

        def record(run, index):
            phases.append(run.execution.status()["phase"])

        run = Harness(self, [BUY, HOLD, FLAT, HOLD], on_boundary=record)
        run.run()
        self.assertEqual(run.orders, [("open", BUY), ("close", "deal-1")])
        # Each acknowledgement was confirmed (polled while waiting) before the next boundary.
        self.assertEqual(phases, ["flat", "open", "open", "flat"])
        self.assertIn("Order confirmed operation=open", run.output)
        self.assertIn("Position ownership recorded", run.output)
        self.assertIn("Order confirmed operation=close", run.output)
        self.assertEqual(run.execution.status()["owned_positions"], 0)

    def test_pending_entry_and_exit_phases_while_unconfirmed(self):
        def lag_and_record(run, index):
            if index == 1:
                run.phase_after_entry = run.execution.status()["phase"]

        broker = Broker(None)
        broker.lag = True
        run = Harness(self, [BUY, BUY], broker=broker, on_boundary=lag_and_record)
        run.run()
        self.assertEqual(run.phase_after_entry, "entry_pending")
        self.assertEqual(run.orders, [("open", BUY)])  # The second boundary sends nothing.
        self.assertIn("reason=order_confirmation_pending", run.output)
        self.assertIn("1 acknowledged order(s) are not yet confirmed", run.output)

    def test_rejected_entry_and_exit_stop_without_a_retry(self):
        for signals, rejected in (([BUY], "open"), ([BUY, FLAT], "close")):
            with self.subTest(rejected=rejected):
                def reject(run, index, _rejected=rejected):
                    if _rejected == "open" or index == 1:
                        run.broker.confirm_status = "REJECTED"

                run = Harness(self, signals + [HOLD], on_boundary=reject)
                stopped = run.run(expect=TradingUnavailable)
                self.assertEqual(stopped.reason, "order_confirmation_mismatch")
                self.assertTrue(stopped.outcome_uncertain)
                self.assertEqual([kind for kind, _ in run.orders][-1], rejected)
                self.assertEqual(len(run.orders), len(signals))  # Never resubmitted.
                self.assertIn("Manual reconciliation of positions and working orders", run.output)

    def test_delayed_confirmation_and_listing_never_duplicate_the_entry(self):
        def release(run, index):
            if index == 2:
                run.broker.release()

        broker = Broker(None)
        broker.lag = True
        run = Harness(self, [BUY, BUY, BUY, BUY], broker=broker, on_boundary=release)
        run.run()
        self.assertEqual(run.orders, [("open", BUY)])
        self.assertEqual(run.decisions, 3)  # Boundary 1 waited for the confirmation.
        self.assertEqual(run.output.count("reason=order_confirmation_pending"), 1)

    def test_confirmed_but_not_yet_listed_is_still_pending(self):
        def list_it(run, index):
            if index == 2:
                run.broker.hidden.clear()

        broker = Broker(None)
        broker.unlisted_opens = True  # Accepted at once, listed only at boundary 2.
        run = Harness(self, [BUY, BUY, BUY], broker=broker, on_boundary=list_it)
        run.run()
        self.assertEqual(run.orders, [("open", BUY)])
        self.assertEqual(run.decisions, 2)
        self.assertIn("reason=order_confirmation_pending", run.output)
        self.assertEqual(run.execution.ownership.deals(), {"deal-1": BUY})

    def test_missing_confirmation_stops_at_the_deadline(self):
        broker = Broker(None)
        broker.lag = True
        boundaries = [BOUNDARY + i * STEP for i in range(12)]
        run = Harness(self, [BUY] * 12, broker=broker, boundaries=boundaries)
        stopped = run.run(expect=TradingUnavailable)
        self.assertEqual(stopped.reason, "order_confirmation_unresolved")
        self.assertEqual(run.orders, [("open", BUY)])

    def test_repeated_confirmations_record_ownership_once(self):
        def list_it(run, index):
            if index == 3:
                run.broker.hidden.clear()

        broker = Broker(None)
        broker.unlisted_opens = True
        run = Harness(self, [BUY] * 5, broker=broker, on_boundary=list_it)
        run.run()
        self.assertGreaterEqual(broker.calls.count("getPositionOrderConfirmation"), 4)
        self.assertEqual(run.output.count("Position ownership recorded"), 1)
        self.assertEqual(run.orders, [("open", BUY)])

    def test_confirmation_details_that_differ_from_the_order_stop_trading(self):
        tampering = {
            "direction": lambda v: {**v, "direction": SELL},
            "size": lambda v: {**v, "size": 200},
            "instrument": lambda v: {**v, "epic": "GBPUSD"},
            "affected_deals": lambda v: {**v, "affectedDeals": v["affectedDeals"] * 2},
            "reference": lambda v: {**v, "dealReference": "other"},
        }
        for name, tamper in tampering.items():
            with self.subTest(detail=name):
                run = Harness(self, [BUY, BUY])
                run.broker.tamper = tamper
                stopped = run.run(expect=TradingUnavailable)
                self.assertEqual(stopped.reason, "order_confirmation_mismatch")
                self.assertEqual(run.orders, [("open", BUY)])
                self.assertEqual(run.execution.ownership.deals(), {})  # Never adopted.

    def test_uncertain_order_outcomes_stop_without_recovery_or_resubmission(self):
        outcomes = {
            "transport": TradingUnavailable("read_timeout", mutation=True),
            "unexpected": ValueError("malformed reply"),
        }
        for name, error in outcomes.items():
            with self.subTest(outcome=name):
                run = Harness(self, [BUY, BUY, BUY])
                run.broker.mutation_error = error
                stopped = run.run(expect=(TradingUnavailable, ExecutionHalted))
                self.assertEqual(run.orders, [("open", BUY)])
                self.assertEqual(run.broker.recoveries, 0)
                if name == "unexpected":
                    self.assertEqual(stopped.reason, "order_outcome_uncertain")

    def test_an_unexpected_success_status_is_uncertain_and_stops(self):
        run = Harness(self, [BUY, BUY])
        run.broker.status = 202
        stopped = run.run(expect=ExecutionHalted)
        self.assertEqual(stopped.reason, "order_response_unexpected")
        self.assertEqual(run.orders, [("open", BUY)])

    def test_reversal_is_never_sent_as_an_opposite_order(self):
        for mode in ("netting", "hedging"):
            with self.subTest(mode=mode):
                run = Harness(self, [BUY, SELL, BUY], broker=Broker(None, mode))
                stopped = run.run(expect=ExecutionHalted)
                self.assertEqual(stopped.reason, "reversal_requires_confirmed_close")
                self.assertEqual(run.orders, [("open", BUY)])  # No opposite order, no close.
                self.assertEqual([p[2] for p in run.broker.positions], [BUY])  # Still open.
                self.assertIn("strategy_owned_positions=1", run.output)


class OwnershipTests(unittest.TestCase):
    def test_unrelated_positions_on_the_instrument_are_never_closed_or_adopted(self):
        for direction in (BUY, SELL):
            with self.subTest(manual=direction):
                run = Harness(self, [FLAT, BUY, SELL, FLAT])
                run.broker.add("manual-1", direction)
                run.run()
                self.assertEqual(run.orders, [])
                self.assertEqual([p[1] for p in run.broker.positions], ["manual-1"])
                self.assertEqual([side for _, side in run.seen], [None] * 4)
                self.assertIn("Positions not owned by this process positions=1", run.output)
                self.assertEqual(run.output.count("Entry blocked reason=unowned_position_on_instrument"), 2)

    def test_positions_on_other_instruments_do_not_block_entries(self):
        run = Harness(self, [BUY, FLAT])
        run.broker.add("other-1", BUY, epic="GBPUSD")
        run.run()
        self.assertEqual(run.orders, [("open", BUY), ("close", "deal-1")])
        self.assertEqual([p[1] for p in run.broker.positions], ["other-1"])

    def test_only_the_owned_position_is_closed_among_several_on_the_instrument(self):
        def manual_trade(run, index):
            if index == 1:
                run.broker.add("manual-1", BUY)  # Opened by hand after the strategy's entry.

        run = Harness(self, [BUY, HOLD, FLAT, BUY], on_boundary=manual_trade)
        run.run()
        self.assertEqual(run.orders, [("open", BUY), ("close", "deal-1")])
        self.assertEqual([p[1] for p in run.broker.positions], ["manual-1"])
        self.assertIn("Entry blocked", run.output)  # Boundary 3: the manual position blocks.

    def test_ownership_is_never_inferred_from_a_matching_instrument_and_direction(self):
        owned_before_listing = []

        def lookalike(run, index):
            if index == 1:
                run.broker.add("manual-1", BUY)  # Same instrument, side and size as the order.
                owned_before_listing.append(run.execution.ownership.deals())
            if index == 2:
                run.broker.hidden.clear()  # The strategy's own deal is listed at last.

        broker = Broker(None)
        broker.unlisted_opens = True  # Accepted, but the strategy's deal is not yet listed.
        # Signals are consumed per decision: boundary 1 waits for the confirmation.
        run = Harness(self, [BUY, FLAT], broker=broker, on_boundary=lookalike, boundaries=minutes(3))
        run.run()
        self.assertEqual(owned_before_listing, [{}])  # The lookalike was never adopted.
        self.assertEqual(run.orders, [("open", BUY), ("close", "deal-1")])
        self.assertEqual([p[1] for p in run.broker.positions], ["manual-1"])

    def test_broker_side_closure_releases_ownership_and_allows_a_new_entry(self):
        def stop_out(run, index):
            if index == 2:
                run.broker.positions = [p for p in run.broker.positions if p[1] != "deal-1"]

        run = Harness(self, [BUY, HOLD, BUY, BUY], on_boundary=stop_out)
        run.run()
        self.assertEqual(run.orders, [("open", BUY), ("open", BUY)])
        self.assertIn("reason=closed_outside_strategy", run.output)
        self.assertEqual(run.execution.ownership.deals(), {"deal-2": BUY})

    def test_restart_never_adopts_a_position_from_an_earlier_run(self):
        first = Harness(self, [BUY, HOLD])
        first.run()
        self.assertIn("Scheduled cycles complete: no further signals or orders. Open positions are "
                      "not closed (strategy_owned_positions=1 pending_orders=0; DEMO ownership is "
                      "not kept across a restart", first.output)
        restarted = Harness(self, [FLAT, BUY, SELL], broker=first.broker)
        restarted.run()
        self.assertEqual(first.broker.orders, [("open", BUY)])  # Nothing closed or added.
        self.assertEqual(len(first.broker.positions), 1)
        self.assertEqual([side for _, side in restarted.seen], [None, None, None])
        self.assertIn("Entry blocked", restarted.output)

    def test_netting_and_hedging_accounts_see_only_explicit_closes(self):
        for mode in ("netting", "hedging"):
            with self.subTest(mode=mode):
                run = Harness(self, [BUY, FLAT, SELL, FLAT], broker=Broker(None, mode))
                run.run()
                self.assertEqual(run.orders, [("open", BUY), ("close", "deal-1"),
                                              ("open", SELL), ("close", "deal-3")])
                self.assertEqual(run.broker.positions, [])

    def test_an_opposite_unowned_position_on_a_netting_account_blocks_the_entry(self):
        run = Harness(self, [BUY], broker=Broker(None, "netting"))
        run.broker.add("manual-1", SELL)
        run.run()
        self.assertEqual(run.orders, [])  # A BUY would have closed the manual SELL.
        self.assertEqual([p[1] for p in run.broker.positions], ["manual-1"])


class Gate:
    """The parts of the LIVE gate the engine reads: its journal-backed ownership."""

    def __init__(self):
        self.owned = {}
        self.window = None

    def bind_cycle(self, still_current):
        self.window = still_current


class LiveOwnershipTests(unittest.TestCase):
    def test_live_ownership_is_the_gates_record(self):
        broker = Broker(None)
        broker._live_execution = gate = Gate()

        def gate_open(epic, direction, size):
            broker.orders.append(("open", direction))
            broker.add("live-1", direction)
            gate.owned["live-1"] = {"direction": direction}  # Recorded by the gate.
            return {"dealReference": "r"}, 200

        def gate_close(deal_id):
            broker.orders.append(("close", deal_id))
            broker.positions.clear()
            del gate.owned[deal_id]
            return {"dealReference": "r"}, 200

        broker.createPosition, broker.closePosition = gate_open, gate_close
        run = Harness(self, [BUY, HOLD, FLAT], broker=broker)
        run.run("--REAL")
        self.assertEqual(run.orders, [("open", BUY), ("close", "live-1")])
        self.assertEqual([side for _, side in run.seen], [None, BUY, BUY])
        self.assertIsNotNone(gate.window)  # Bound to the cycle deadline.
        self.assertEqual(len(run.execution.pending), 0)  # The gate confirms synchronously.

    def test_live_owned_position_closed_outside_the_strategy_stops(self):
        broker = Broker(None)
        broker._live_execution = gate = Gate()
        gate.owned["live-1"] = {"direction": BUY}  # Journal says owned; the broker no longer lists it.
        run = Harness(self, [BUY], broker=broker)
        stopped = run.run("--REAL", expect=ExecutionHalted)
        self.assertEqual(stopped.reason, "live_owned_position_closed_outside_strategy")
        self.assertEqual(run.orders, [])
        self.assertEqual(gate.owned, {"live-1": {"direction": BUY}})  # Never edited by the engine.


class SchedulingTests(unittest.TestCase):
    def test_a_boundary_runs_at_most_once(self):
        run = Harness(self, [HOLD, HOLD], boundaries=[BOUNDARY, BOUNDARY])
        run.run()
        self.assertEqual(run.decisions, 1)
        self.assertIn("reason=boundary_already_executed", run.output)

    def test_a_stale_boundary_calculates_no_signal(self):
        def late(run, index):
            run.clock.advance(200)  # The cycle starts 202 s after its boundary.

        run = Harness(self, [BUY], on_boundary=late)
        run.run()
        self.assertEqual(run.decisions, 0)
        self.assertEqual(run.orders, [])
        self.assertIn("reason=stale_boundary stage=cycle_start", run.output)

    def test_the_deadline_is_rechecked_immediately_before_the_order(self):
        for delay, sent in ((100, True), (130, False)):
            with self.subTest(delay=delay):
                run = Harness(self, [BUY], signal_hook=lambda r, d=delay: r.clock.advance(d))
                run.run("--max-signal-delay", "120")
                self.assertEqual(run.orders, [("open", BUY)] if sent else [])
                self.assertEqual("reason=stale_boundary stage=order" in run.output, not sent)

    def test_a_stale_signal_does_not_become_valid_after_recovery(self):
        def outage(run, index):
            if index == 0:
                run.broker.unavailable = "dns_resolution_failed"

        run = Harness(self, [BUY, BUY], on_boundary=outage)
        run.run()
        # Boundary 0 failed during session validation; it is never replayed after recovery.
        self.assertEqual(run.broker.recoveries, 1)
        self.assertEqual(run.decisions, 1)
        self.assertEqual(run.orders, [("open", BUY)])

    def test_confirmation_polling_runs_only_while_orders_are_pending(self):
        run = Harness(self, [BUY, HOLD, HOLD])
        run.run()
        self.assertEqual(run.waits, [False, True, False])

    def test_market_closed_waits_only_with_five_trading_days(self):
        for days, waited in ((7, False), (5, True)):
            with self.subTest(days=days):
                run = Harness(self, [HOLD], days=days)
                run.broker.is_market_open = lambda: False
                run.broker.wait_until_open = lambda **kwargs: run.broker.calls.append("market-wait")
                run.run()
                self.assertEqual("market-wait" in run.broker.calls, waited)


class MultiTimeframeTests(unittest.TestCase):
    """Higher-timeframe candles are used only once fully closed by the cycle boundary."""

    ORIGIN = datetime(2026, 11, 1, tzinfo=timezone.utc)

    @classmethod
    def history(cls, minutes, newest_start, count):
        """Broker rows ending with the candle starting at ``newest_start`` (still forming).
        Each close is the candle's start in hours since ORIGIN, to identify it."""
        step = timedelta(minutes=minutes)
        starts = [newest_start - (count - 1 - i) * step for i in range(count)]
        return {"prices": [{"snapshotTimeUTC": start.replace(tzinfo=None).isoformat(),
                            "closePrice": {"ask": (start - cls.ORIGIN).total_seconds() / 3600}}
                           for start in starts]}

    def hours(self, moment):
        return (moment - self.ORIGIN).total_seconds() / 3600

    def test_a_four_hour_candle_is_used_only_from_the_boundary_at_which_it_completes(self):
        sixteen = datetime(2026, 11, 17, 16, 0, tzinfo=timezone.utc)  # The 16:00-20:00 candle.
        # 19:30: the 16:00 candle is still forming; the newest usable one is 12:00-16:00.
        closes = completed_closes(self.history(240, sixteen, 14), "HOUR_4",
                                  datetime(2026, 11, 17, 19, 30, tzinfo=timezone.utc))
        self.assertEqual(closes[-1], self.hours(sixteen - timedelta(hours=4)))
        self.assertNotIn(self.hours(sixteen), closes)
        # 20:00: it has just completed and is used; the 20:00 candle that started is not.
        twenty = sixteen + timedelta(hours=4)
        closes = completed_closes(self.history(240, twenty, 14), "HOUR_4", twenty)
        self.assertEqual(closes[-1], self.hours(sixteen))
        self.assertNotIn(self.hours(twenty), closes)

    def test_lower_and_higher_timeframes_align_to_the_same_boundary(self):
        boundary = datetime(2026, 11, 17, 20, 0, tzinfo=timezone.utc)
        thirty = completed_closes(self.history(30, boundary, 10), "MINUTE_30", boundary)
        four = completed_closes(self.history(240, boundary, 10), "HOUR_4", boundary)
        self.assertEqual((thirty[-1], four[-1]), (self.hours(boundary) - 0.5, self.hours(boundary) - 4))
        # The expected 4H candle (20:00-24:00) is missing at midnight: skip the cycle.
        self.assertIsNone(completed_closes(self.history(240, boundary - timedelta(hours=4), 10),
                                           "HOUR_4", boundary + timedelta(hours=4)))


class RecoveryTests(unittest.TestCase):
    def test_demo_outage_resumes_with_ownership_and_pending_orders_intact(self):
        def outage(run, index):
            if index == 1:
                run.broker.unavailable = "connection_failed"

        broker = Broker(None)
        run = Harness(self, [BUY, FLAT], broker=broker, on_boundary=outage, boundaries=minutes(3))
        run.run()
        self.assertEqual(broker.recoveries, 1)
        self.assertIn("Scheduled trading resumed", run.output)
        self.assertEqual(run.orders, [("open", BUY), ("close", "deal-1")])  # No duplicate.

    def test_unconfirmed_entry_survives_recovery_and_is_never_resent(self):
        def outage_then_release(run, index):
            if index == 1:
                run.broker.unavailable = "dns_resolution_failed"
            if index == 2:
                run.broker.release()

        broker = Broker(None)
        broker.lag = True
        run = Harness(self, [BUY, BUY, BUY, BUY], broker=broker, on_boundary=outage_then_release)
        run.run()
        self.assertEqual(run.orders, [("open", BUY)])
        self.assertIn("pending_orders=1", run.output)

    def test_non_transient_failures_and_live_never_resume(self):
        for flags, reason in (((), "authentication_failed_http_401"), (("--REAL",), "dns_resolution_failed")):
            with self.subTest(reason=reason, live=bool(flags)):
                def fail(run, index, _reason=reason):
                    run.broker.unavailable = _reason

                run = Harness(self, [BUY], on_boundary=fail)
                stopped = run.run(*flags, expect=TradingUnavailable)
                self.assertEqual(stopped.reason, reason)
                self.assertEqual(run.broker.recoveries, 0)
                self.assertEqual(run.orders, [])


class ShutdownTests(unittest.TestCase):
    def test_shutdown_while_waiting_reports_exposure_and_sends_nothing(self):
        def stop(run, index):
            if index == 2:
                signal.raise_signal(signal.SIGTERM)

        run = Harness(self, [BUY, HOLD, FLAT], on_boundary=stop)
        stopped = run.run(expect=SystemExit)
        self.assertEqual(stopped.code, 143)
        self.assertEqual(run.orders, [("open", BUY)])  # The FLAT decision never ran.
        self.assertIn("Shutdown complete: no further signals or orders. Open positions are not "
                      "closed (strategy_owned_positions=1 pending_orders=0", run.output)

    def test_shutdown_during_an_order_request_is_deferred_and_the_order_recorded(self):
        broker = Broker(None)
        broker.lag = True
        broker.hook = lambda name: name == "open" and signal.raise_signal(signal.SIGTERM)
        run = Harness(self, [BUY, BUY], broker=broker)
        stopped = run.run(expect=SystemExit)
        self.assertEqual(stopped.code, 143)
        self.assertEqual(run.orders, [("open", BUY)])
        self.assertIn("deferred until the in-flight order request returns", run.output)
        self.assertIn("pending_orders=1", run.output)
        self.assertIn("1 acknowledged order(s) are not yet confirmed", run.output)

    def test_shutdown_during_a_read_stops_before_any_decision(self):
        broker = Broker(None)
        broker.hook = lambda name: name == "prepare_trading_cycle" and signal.raise_signal(signal.SIGINT)
        run = Harness(self, [BUY], broker=broker)
        stopped = run.run(expect=SystemExit)
        self.assertEqual(stopped.code, 130)
        self.assertEqual((run.decisions, run.orders), (0, []))

    def test_forced_interrupt_during_an_order_is_reported_as_uncertain(self):
        broker = Broker(None)

        def interrupted(name):
            if name == "open":
                broker._mutation_outcome_uncertain = True
                raise KeyboardInterrupt

        broker.hook = interrupted
        run = Harness(self, [BUY], broker=broker)
        stopped = run.run(expect=SystemExit)
        self.assertEqual(stopped.code, 130)
        self.assertIn("its outcome is uncertain", run.output)


class ReadOnlyTests(unittest.TestCase):
    def test_read_only_cycles_never_read_positions_decide_or_order(self):
        run = Harness(self, [BUY, FLAT])
        run.run("--read-only")
        self.assertTrue(run.broker.forbidden)
        self.assertEqual(run.connects, [{"order_execution": False}])
        self.assertEqual(run.decisions, 0)
        self.assertEqual(run.orders, [])
        for name in ("getAllPositionsList", "getAllPositions", "open", "close"):
            self.assertNotIn(name, run.broker.calls)
        self.assertEqual(run.broker.calls.count("getHistoricalPrices"), 2)
        self.assertIn("DEMO read-only cycle 2: MINUTE_30 history HTTP 200; 48 prices", run.stdout.getvalue())

    def test_the_client_refuses_mutations_after_read_only_is_set(self):
        api = TradingApi("key", "login", "password", environment="demo", auto_authenticate=False)
        api.cst, api.x_security_token = "cst", "token"
        api.forbid_mutations()
        with patch("trading_bot.api.client.requests.post") as post, \
                patch("trading_bot.api.client.requests.delete") as delete, \
                patch("trading_bot.api.rate_limits.throttle"):
            for call in (lambda: api.createPosition("EURUSD", BUY, 100), lambda: api.closePosition("d1")):
                with self.assertRaises(PermissionError):
                    call()
        post.assert_not_called()
        delete.assert_not_called()

    def test_read_only_requires_demo(self):
        parser = argparse.ArgumentParser()
        add_trading_arguments(parser)
        add_execution_arguments(parser)
        from trading_bot.config.trading_config import check_execution_arguments
        for flags in (["--read-only", "--REAL"], ["--cycles", "0"],
                      ["--network-retry-delay", "10", "--network-retry-max-delay", "5"]):
            with self.subTest(flags=flags), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                check_execution_arguments(parser, parser.parse_args(flags))


class ModuleBoundaryTests(unittest.TestCase):
    def test_engine_constants_and_public_surface(self):
        self.assertEqual(engine.TARGETS, (BUY, SELL, FLAT, HOLD))
        self.assertTrue(issubclass(ExecutionHalted, SystemExit))  # Never swallowed by a loop.


if __name__ == "__main__":
    unittest.main()

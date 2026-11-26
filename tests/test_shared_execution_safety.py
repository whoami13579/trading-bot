"""Cross-strategy regressions for the shared single-position execution (issue #9).

Every order-capable strategy runs the same engine; none keeps its own loop, order
submission or scheduling; LIVE closes of owned positions stay possible at the
entry limit; DEMO recovery applies to every strategy. Offline only.
"""

import ast
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime
import io
import logging
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from test_execution_assumptions import LiveGateCase
from test_network_recovery import Broker, FakeClock, assert_redacted, broker_patches
from trading_bot.strategies import ema_hma, hma, registry
from trading_bot.tools import trade

ROOT = Path(__file__).resolve().parents[1]
MODULES = {"moving-average": "moving_average", "sma": "sma", "hma": "hma",
           "ema-hma": "ema_hma", "multiple-hma": "multiple_hma"}
EXPECTED = {
    # key: (schedule, read-only history probes, trading days)
    "moving-average": (list(range(60)), (("MINUTE", 20),), 7),
    "sma": (list(range(60)), (("MINUTE", 21),), 7),
    "hma": ([0, 30], (("MINUTE_30", 48),), 7),
    "ema-hma": (list(range(0, 60, 5)), (("MINUTE_5", 200), ("MINUTE_5", 48)), 7),
    "multiple-hma": ([0, 30], (("HOUR_4", 210), ("MINUTE_30", 60)), 5),
}


class SharedDispatchTests(unittest.TestCase):
    def test_every_order_capable_strategy_runs_the_shared_engine(self):
        self.assertEqual({s.key for s in registry.with_mode(registry.DEMO)}, set(EXPECTED))
        for key, (schedule, probes, days) in EXPECTED.items():
            with self.subTest(strategy=key), \
                    patch("trading_bot.execution.single_position.run") as run, \
                    redirect_stdout(io.StringIO()):
                self.assertIsNone(trade.main(["--strategy", key, "--symbol", "AUDUSD", "--size", "200"]))
                (strategy, args), kwargs = run.call_args
                self.assertEqual((strategy.key, strategy.symbol, strategy.size), (key, "AUDUSD", 200))
                self.assertEqual((strategy.schedule, strategy.probes, strategy.days), (schedule, probes, days))
                self.assertFalse(args.real)  # DEMO unless --REAL/--real.
                module = __import__(f"trading_bot.strategies.{MODULES[key]}", fromlist=["x"])
                with patch.object(module, "create_trading_api", return_value="client") as factory:
                    self.assertEqual(kwargs["connect"](), "client")
                    self.assertEqual(kwargs["connect"](order_execution=False), "client")
                self.assertEqual(factory.call_args_list[0].kwargs, {"symbol": "AUDUSD", "size": 200})
                self.assertIs(factory.call_args_list[0].args[0], args)
                self.assertEqual(factory.call_args_list[1].kwargs["order_execution"], False)

    def test_live_selection_reaches_the_engine_only_with_the_exact_flag(self):
        for flag in ("--REAL", "--real"):
            with self.subTest(flag=flag), patch("trading_bot.execution.single_position.run") as run, \
                    redirect_stdout(io.StringIO()):
                trade.main(["--strategy", "ema-hma", flag])
                self.assertTrue(run.call_args.args[1].real)

    def test_strategy_modules_never_submit_orders_read_positions_or_schedule_themselves(self):
        forbidden = {"createPosition", "closePosition", "getAllPositionsList", "getAllPositions",
                     "sleep", "PendingOrders", "NetworkSupervisor", "CycleGuard", "GracefulShutdown"}
        for name in MODULES.values():
            tree = ast.parse((ROOT / f"src/trading_bot/strategies/{name}.py").read_text())
            with self.subTest(module=name):
                used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
                    n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
                self.assertEqual(used & forbidden, set())
                self.assertFalse(any(isinstance(n, ast.While) for n in ast.walk(tree)))


class LiveExitAtEntryLimitTests(unittest.TestCase):
    def test_owned_position_is_closed_through_the_gate_after_the_opening_order_limit(self):
        case = LiveGateCase("setUp")
        case.setUp()
        try:
            case.gate.args.max_orders = 1
            case.gate.arm()
            case.api.prepare_trading_cycle = Mock(return_value=False)
            with patch("sys.argv", ["hma.py", "--REAL", "--cycles", "2"]), \
                    patch.object(hma, "create_trading_api", return_value=case.api), \
                    patch.object(hma, "wait_until_targets"), \
                    patch.object(hma, "calculate_hma_result", side_effect=["BUY", "SELL"]), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                hma.main()
            self.assertEqual([c.args[0] for c in case.send.call_args_list], ["post", "delete"])
            self.assertEqual(case.gate.open_count, 1)  # The limit was reached by the entry.
            self.assertEqual((case.gate.owned, case.positions), ({}, []))
            self.assertFalse(case.gate.failed)
        finally:
            case.doCleanups()


class DemoRecoveryForEveryStrategyTests(unittest.TestCase):
    """The real client, scheduler and supervisor: EMA-HMA used to stop on an outage."""

    START = datetime(2026, 11, 17, 21, 50, 0)

    def at(self, hour, minute, day=17):
        return datetime(2026, 11, day, hour, minute)

    def test_ema_hma_outage_pauses_recovers_and_never_duplicates_the_entry(self):
        clock = FakeClock(self.START)
        broker = Broker(clock)
        broker.outages.append((self.at(22, 20), self.at(23, 10)))
        stdout, stderr = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            broker_patches(stack, broker, clock)
            stack.enter_context(patch("sys.argv", [
                "ema_hma.py", "--network-retry-jitter", "0", "--cycles", "3", "--time_frame", "MINUTE_30",
                "--long_period", "40", "--short_period", "10", "--hma_period", "8"]))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            logs = stack.enter_context(self.assertLogs("trading_bot", level="INFO"))
            logging.getLogger("trading_bot.tests").info("scenario start")
            ema_hma.main()
        output = "\n".join(logs.output)
        assert_redacted(self, output, stdout.getvalue(), stderr.getvalue())
        boundaries = sorted({t.replace(second=0) for t in broker.times("get", "/api/v1/prices/EURUSD")})
        # 22:30 and 23:00 fell inside the outage and are never replayed.
        self.assertEqual(boundaries, [self.at(22, 0), self.at(23, 30), self.at(0, 0, day=18)])
        self.assertEqual(broker.count("post", "/api/v1/positions"), 1)
        self.assertEqual(len(broker.positions), 1)
        for event in ("Trading paused reason=dns_resolution_failed", "Session reauthenticated",
                      "Broker state reconciled positions=1 symbol_positions=1",
                      "Scheduled trading resumed", "Order confirmed operation=open",
                      "Position ownership recorded"):
            self.assertIn(event, output)


if __name__ == "__main__":
    unittest.main()

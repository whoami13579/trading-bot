"""tb-trade / tb-backtest: registry, dispatch, safety and legacy equivalence.

Offline only. Order-capable paths stop at a mocked startup (create_trading_api)
or at the real LIVE preflight with the broker client replaced; installed
console scripts run in child processes whose sitecustomize blocks sockets,
credential/private-state files and, where stated, broker module imports.
"""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from decimal import Decimal
import importlib
from importlib.metadata import distribution
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from unittest.mock import Mock, patch

from trading_bot.backtesting import cli as backtest_cli
from trading_bot.backtesting.adapters import ADAPTERS
from trading_bot.strategies import registry
from trading_bot.strategies.registry import BACKTEST, DEMO, LIVE, Strategy
from trading_bot.tools import trade

from test_donchian_cli import fixture as donchian_snapshot
from test_grid_backtesting import GRID_ARGS, oscillating_snapshot

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(sysconfig.get_path("scripts"))
TRADABLE = ("moving-average", "sma", "hma", "ema-hma", "multiple-hma")
BACKTEST_ONLY = ("donchian", "grid")
MODULES = {"moving-average": "moving_average", "sma": "sma", "hma": "hma",
           "ema-hma": "ema_hma", "multiple-hma": "multiple_hma"}
# Modules no help, listing or rejected tb-trade command may import.
BROKER_PREFIXES = ("trading_bot.api", "trading_bot.risk", "trading_bot.config.trading_config",
                   "trading_bot.utils.network_recovery",
                   *(f"trading_bot.strategies.{m}" for m in MODULES.values()))
# Strategy-specific options of each existing command, with non-default values.
SPECIFIC = {
    "moving-average": ["--account", "ACC1", "--max-margin", "3", "--stop-distance", "0.002"],
    "sma": ["--max-exposure", "90", "--max-orders", "2", "--fee-profile", "fees.json"],
    "hma": ["--time-frame", "MINUTE_5", "--hma-period", "20", "--direction", "SELL",
            "--days", "5", "--cycles", "2", "--network-retries", "1", "--max-outage", "60",
            "--no-network-recovery"],
    "ema-hma": ["--long_period", "100", "--short_period", "10", "--hma_period", "12",
                "--time_frame", "MINUTE_15", "--days", "5"],
    "multiple-hma": ["--long_term", "HOUR", "--short_term", "MINUTE_15", "--hma_period", "10",
                     "--days", "7"],
}
TOOL_ALIASES = ("check-offline", "check-environment", "demo-readonly", "diagnose-demo-auth",
                "demo-roundtrip", "live-preflight", "ping")


class StartupReached(BaseException):
    """Raised by the mocked broker startup; BaseException so no strategy loop swallows it."""


def console_scripts():
    return {e.name: e for e in distribution("trading-bot").entry_points
            if e.group == "console_scripts"}


def run_main(argv):
    """tb-trade in-process: (exit code, stdout, stderr)."""
    with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
        try:
            code = trade.main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


# Child-process guard, installed as sitecustomize so it also covers installed
# console-script executables. Any violation ends the process with status 91.
SITECUSTOMIZE = r'''
import json, os, sys
from pathlib import Path

def _violation(what):
    sys.stderr.write(f"GUARD VIOLATION: {what}\n")
    sys.stderr.flush()
    os._exit(91)

_forbidden = tuple(p for p in os.environ.get("TB_FORBID_IMPORTS", "").split(",") if p)

if os.environ.get("TB_FAKE_STARTUP"):
    # Replace broker startup before any strategy binds it: record what the
    # strategy would have authenticated with, then stop.
    import trading_bot.config.trading_config as config

    def create_trading_api(args, *, symbol=None, size=None, order_execution=True):
        record = {k: str(v) for k, v in sorted(vars(args).items())}
        print(json.dumps({"args": record, "symbol": symbol, "size": size,
                          "order_execution": order_execution}, sort_keys=True))
        sys.stdout.flush()
        os._exit(97)

    config.create_trading_api = create_trading_api

def _guard(event, args):
    # Connections and name resolution; urllib3's import-time IPv6 probe binds ::1 only.
    if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
                 "socket.gethostbyaddr", "socket.sendto"}:
        _violation(f"network {event}")
    if event == "import" and _forbidden and str(args[0]).startswith(_forbidden):
        _violation(f"import {args[0]}")
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(args[0].decode() if isinstance(args[0], bytes) else args[0])
        if (path.name == ".env" or path.name.startswith(".env.") and not path.name.endswith(".example")
                or any(p in (".demo-integration", ".live-integration") for p in path.parts)):
            _violation(f"private file {path.name}")

sys.addaudithook(_guard)
'''


class GuardedChild:
    """Runs installed console scripts with the sitecustomize guard."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        (self.tmp / "guard").mkdir()
        (self.tmp / "guard/sitecustomize.py").write_text(SITECUSTOMIZE)
        (self.tmp / "work").mkdir()

    def tearDown(self):
        self._dir.cleanup()

    def command(self, name, *args, forbid=(), fake_startup=False, timeout=60):
        executable = SCRIPTS / name
        self.assertTrue(executable.is_file(), f"installed console script missing: {executable}")
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        env.update(PYTHONPATH=str(self.tmp / "guard"), TB_FORBID_IMPORTS=",".join(forbid),
                   PYTHONDONTWRITEBYTECODE="1")
        if fake_startup:
            env["TB_FAKE_STARTUP"] = "1"
        result = subprocess.run([str(executable), *args], cwd=self.tmp / "work", env=env,
                                capture_output=True, text=True, timeout=timeout)
        self.assertNotEqual(result.returncode, 91, result.stderr)
        return result


class RegistryTests(unittest.TestCase):
    def test_capability_matrix_is_exactly_the_implemented_modes(self):
        expected = {key: {DEMO, LIVE, BACKTEST} for key in TRADABLE}
        expected.update(donchian={BACKTEST}, grid={BACKTEST})
        self.assertEqual({k: set(s.modes) for k, s in registry.STRATEGIES.items()}, expected)
        self.assertEqual(tuple(s.key for s in registry.with_mode(DEMO)), TRADABLE)
        self.assertEqual(tuple(s.key for s in registry.with_mode(LIVE)), TRADABLE)

    def test_backtest_capability_is_exactly_the_backtesting_adapters(self):
        backtestable = {s.key: s.backtest_adapter for s in registry.with_mode(BACKTEST)}
        self.assertEqual(set(backtestable.values()), set(ADAPTERS))
        self.assertEqual(backtestable, {key: key for key in ADAPTERS})
        for strategy in registry.STRATEGIES.values():
            if not strategy.supports(BACKTEST):
                self.assertIsNone(strategy.backtest_adapter)

    def test_trading_entries_are_the_legacy_console_scripts_and_resolve_to_the_same_main(self):
        scripts = console_scripts()
        for key in TRADABLE:
            strategy = registry.get(key)
            with self.subTest(strategy=key):
                module = importlib.import_module(f"trading_bot.strategies.{MODULES[key]}")
                if key == "hma":  # hma.trade = hma.main with --backtest help hidden.
                    self.assertEqual(strategy.trading_entry, "trading_bot.strategies.hma:trade")
                    self.assertIs(strategy.load_trading_entry(), module.trade)
                else:
                    self.assertEqual(scripts[f"trading-bot-{key}"].value, strategy.trading_entry)
                    self.assertIs(strategy.load_trading_entry(), module.main)
                self.assertEqual(scripts[f"trading-bot-{key}"].value,
                                 f"trading_bot.strategies.{MODULES[key]}:main")
                self.assertIn(f"trading-bot-{key}", strategy.legacy_commands)
        legacy_strategy_scripts = {n for n, e in scripts.items()
                                   if e.value.startswith("trading_bot.strategies.")}
        self.assertEqual(legacy_strategy_scripts, {f"trading-bot-{k}" for k in TRADABLE})

    def test_backtest_only_strategies_have_no_trading_entry(self):
        for key in BACKTEST_ONLY:
            strategy = registry.get(key)
            with self.subTest(strategy=key):
                self.assertFalse(strategy.tradable)
                self.assertIsNone(strategy.trading_entry)
                with self.assertRaises(LookupError):
                    strategy.load_trading_entry()

    def test_inconsistent_or_unsafe_registrations_are_refused(self):
        entry = "trading_bot.strategies.hma:main"
        bad = (
            dict(modes={LIVE}, trading_entry=entry),  # LIVE without DEMO
            dict(modes={DEMO}),  # trading mode without an entry point
            dict(modes={BACKTEST}, trading_entry=entry, backtest_adapter="hma"),  # entry, no mode
            dict(modes={BACKTEST}),  # backtest without an adapter
            dict(modes={DEMO, BACKTEST}, trading_entry=entry),
            dict(modes={DEMO}, trading_entry="os:system"),  # outside trading_bot.strategies
            dict(modes={DEMO}, trading_entry="trading_bot.strategies.hma:main()"),
            dict(modes=set()),
            dict(modes={"paper"}),
        )
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Strategy("x", "x", **kwargs)
        demo_only = Strategy("x", "x", {DEMO}, trading_entry=entry)
        self.assertEqual((demo_only.supports(DEMO), demo_only.supports(LIVE)), (True, False))

    def test_unknown_keys_are_not_resolved_dynamically(self):
        for key in ("trading_bot.strategies.hma", "hma.py", "HMA", "ema_hma", "", "os"):
            with self.subTest(key=key), self.assertRaises(KeyError):
                registry.get(key)

    def test_registry_import_loads_no_strategy_backtesting_or_broker_code(self):
        child = subprocess.run(
            [sys.executable, "-I", "-c",
             "import sys, trading_bot.strategies.registry as r; r.capability_table();"
             "print(sorted(m for m in sys.modules if m.startswith('trading_bot')))"],
            capture_output=True, text=True, timeout=30, cwd=tempfile.gettempdir())
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout.replace("'", '"')),
                         ["trading_bot", "trading_bot.strategies", "trading_bot.strategies.registry"])


class TradeCliTests(unittest.TestCase):
    def setUp(self):
        # No test in this class may reach a strategy unless it patches this itself.
        self.load = patch.object(Strategy, "load_trading_entry",
                                 side_effect=AssertionError("strategy entry must not load"))
        self.load.start()
        self.addCleanup(self.load.stop)

    def assertUsageError(self, argv, *fragments):
        code, out, err = run_main(argv)
        self.assertEqual(code, 2, (argv, out, err))
        self.assertEqual(out, "")
        self.assertIn("usage: tb-trade", err)
        for fragment in fragments:
            self.assertIn(fragment, err)
        return err

    def test_global_help_lists_strategies_and_modes(self):
        for argv in (["--help"], ["-h"], ["--help", "--REAL"]):
            code, out, err = run_main(argv)
            self.assertEqual((code, err), (0, ""))
            self.assertIn("usage: tb-trade --strategy STRATEGY [--REAL | --real]", out)
            self.assertIn(registry.capability_table(), out)
            self.assertIn("tb-backtest --strategy NAME", out)

    def test_list_strategies_prints_the_complete_capability_matrix(self):
        code, out, err = run_main(["--list-strategies"])
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0].split()[:4], ["Strategy", "DEMO", "LIVE", "Backtest"])
        rows = {line.split()[0]: line.split()[1:4] for line in lines[1:8]}
        expected = {key: ["yes", "yes", "yes"] for key in TRADABLE}
        expected.update(donchian=["no", "no", "yes"], grid=["no", "no", "yes"])
        self.assertEqual(rows, expected)
        for argv in (["--list-strategies", "--strategy", "hma"], ["--list-strategies", "--REAL"],
                     ["--list-strategies", "--help"]):
            with self.subTest(argv=argv):
                self.assertUsageError(argv, "--list-strategies takes no other arguments")

    def test_strategy_selection_is_required_explicit_and_single(self):
        self.assertUsageError([], "--strategy is required")
        self.assertUsageError(["--REAL"], "--strategy is required")
        self.assertUsageError(["--symbol", "EURUSD"], "--strategy is required")
        self.assertUsageError(["hma"], "--strategy is required")
        self.assertUsageError(["--strategy"], "expected one argument")
        for argv in (["--strategy", "hma", "--strategy", "sma"],
                     ["--strategy", "hma", "--strategy", "hma"],
                     ["--strategy=hma", "--strategy", "sma"],
                     ["--strategy", "hma", "--REAL", "--strategy=grid"]):
            with self.subTest(argv=argv):
                self.assertUsageError(argv, "--strategy may be given only once")
        for argv in (["--strat", "hma"], ["--strategies", "hma"]):  # No abbreviations.
            with self.subTest(argv=argv):
                self.assertUsageError(argv, "--strategy is required")

    def test_unknown_strategies_are_rejected(self):
        for key in ("nonexistent", "HMA", "ema_hma", "trading_bot.strategies.hma", "os:system", ""):
            with self.subTest(key=key):
                self.assertUsageError(["--strategy", key], "unknown strategy")

    def test_backtest_only_strategies_never_reach_a_trading_path(self):
        for key in BACKTEST_ONLY:
            for extra in ([], ["--REAL"], ["--real"], ["--help"], ["--read-only"]):
                with self.subTest(strategy=key, extra=extra):
                    self.assertUsageError(["--strategy", key, *extra],
                                          "historical backtesting only",
                                          f"tb-backtest --strategy {key}")

    def test_unsupported_live_mode_is_rejected_before_loading_the_strategy(self):
        demo_only = Strategy("paper-only", "x", {DEMO}, trading_entry="trading_bot.strategies.hma:main")
        with patch.dict(registry.STRATEGIES, {"paper-only": demo_only}):
            for flag in ("--REAL", "--real"):
                with self.subTest(flag=flag):
                    self.assertUsageError(["--strategy", "paper-only", flag],
                                          "does not support LIVE trading")

    def test_backtesting_options_are_rejected_with_a_pointer_to_tb_backtest(self):
        options = (["--backtest"], ["--start", "2025-01-01"], ["--end=2025-02-01"],
                   ["--data-source", "dukascopy"], ["--lookback-days", "5"],
                   ["--initial-balance", "5000"], ["--cache-dir", "c"], ["--output-dir", "o"],
                   ["--refresh-data"], ["--allow-data-gaps"], ["--leverage", "2"],
                   ["--commission-per-order", "1"], ["--slippage-pips", "1"],
                   ["--financing-usd-per-unit-day", "1"], ["--snapshot", "s"],
                   ["--execution-model", "grid"], ["--cost-case", "base"],
                   ["--allow-holdout-overlap"], ["--grid-lower", "1.1"], ["--grid-lookback", "9"],
                   ["--level-size", "5"], ["--ema-period", "5"], ["--entry-lookback", "3"])
        for key in TRADABLE:
            for option in options:
                with self.subTest(strategy=key, option=option):
                    err = self.assertUsageError(["--strategy", key, *option],
                                                option[0].split("=")[0],
                                                "not accepted by tb-trade")
                    if key == "hma":
                        self.assertIn("use: tb-backtest --strategy hma", err)

    def test_other_environment_selectors_are_rejected(self):
        for option in (["--environment", "live"], ["--environment=demo"], ["--live"], ["--LIVE"],
                       ["--demo"], ["--DEMO"]):
            for real in ([], ["--REAL"]):
                with self.subTest(option=option, real=real):
                    self.assertUsageError(["--strategy", "hma", *real, *option],
                                          "LIVE is selected only with --REAL or --real")

    def test_dispatch_forwards_remaining_arguments_once_and_restores_argv(self):
        for key in TRADABLE:
            for extra in ([], ["--REAL"], ["--real", "--size", "200"], SPECIFIC[key]):
                seen = []
                entry = Mock(side_effect=lambda: seen.append(list(sys.argv)) or 7)
                before = list(sys.argv)
                with self.subTest(strategy=key, extra=extra), \
                        patch.object(Strategy, "load_trading_entry", return_value=entry) as load:
                    code, out, err = run_main(["--strategy", key, *extra])
                    self.assertEqual(code, 7)  # The strategy's return value is the exit status.
                    entry.assert_called_once_with()
                    load.assert_called_once_with()
                    self.assertEqual(seen, [[f"tb-trade --strategy {key}", *extra]])
                    self.assertEqual(sys.argv, before)

    def test_strategy_position_and_equals_form_are_equivalent(self):
        for argv in (["--strategy", "sma", "--size", "5"], ["--strategy=sma", "--size", "5"],
                     ["--size", "5", "--strategy", "sma"]):
            entry = Mock(return_value=None)
            with self.subTest(argv=argv), patch.object(Strategy, "load_trading_entry", return_value=entry), \
                    patch.object(sys, "argv", ["x"]):
                def check():
                    self.assertEqual(sys.argv[1:], ["--size", "5"])
                entry.side_effect = check
                self.assertEqual(run_main(argv)[0], None)
                entry.assert_called_once_with()

    def test_strategy_exceptions_and_exit_codes_propagate_unchanged(self):
        for raised, expected in ((SystemExit(130), 130), (SystemExit("message"), "message")):
            with self.subTest(raised=raised), \
                    patch.object(Strategy, "load_trading_entry", return_value=Mock(side_effect=raised)):
                self.assertEqual(run_main(["--strategy", "hma"])[0], expected)
        with patch.object(Strategy, "load_trading_entry",
                          return_value=Mock(side_effect=KeyboardInterrupt)), \
                self.assertRaises(KeyboardInterrupt):
            trade.main(["--strategy", "hma"])


class ForwardingEquivalenceTests(unittest.TestCase):
    """Each legacy strategy command and tb-trade reach the same startup with the same arguments."""

    def startup(self, key, run):
        module = importlib.import_module(f"trading_bot.strategies.{MODULES[key]}")
        with ExitStack() as stack:
            factory = stack.enter_context(
                patch.object(module, "create_trading_api", side_effect=StartupReached))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(patch.object(sys, "argv", list(sys.argv)))
            with self.assertRaises(StartupReached):
                run()
        factory.assert_called_once()
        return vars(factory.call_args.args[0]), factory.call_args.kwargs

    def test_legacy_entry_point_and_tb_trade_parse_identically(self):
        scripts = console_scripts()
        for key in TRADABLE:
            legacy_main = scripts[f"trading-bot-{key}"].load()
            for extra in ([], ["--REAL"], ["--real"], ["--symbol", "AUDUSD", "--size", "200"],
                          SPECIFIC[key], ["--REAL", *SPECIFIC[key]]):
                with self.subTest(strategy=key, extra=extra):
                    def legacy():
                        sys.argv = [f"trading-bot-{key}", *extra]
                        legacy_main()
                    expected = self.startup(key, legacy)
                    actual = self.startup(key, lambda: trade.main(["--strategy", key, *extra]))
                    self.assertEqual(actual, expected)
                    self.assertIs(actual[0]["real"], "--REAL" in extra or "--real" in extra)

    def test_strategy_defaults_are_unchanged(self):
        for key in TRADABLE:
            with self.subTest(strategy=key):
                args, kwargs = self.startup(key, lambda: trade.main(["--strategy", key]))
                self.assertEqual(kwargs, {"symbol": "EURUSD", "size": 100})
                self.assertEqual((args["real"], args["account"]), (False, None))
        args, _ = self.startup("hma", lambda: trade.main(["--strategy", "hma"]))
        for name in ("hma_period", "time_frame", "days", "direction", "cycles", "backtest"):
            self.assertIn(args[name], (None, False), name)  # Resolved to defaults inside hma.
        self.assertIs(args["network_recovery"], True)

    def test_strategy_parsers_still_validate_and_reject_unknown_arguments(self):
        bad = {"hma": (["--time-frame", "HOUR"], ["--cycles", "0"], ["--read-only", "--REAL"],
                       ["--size", "0"], ["--bogus"], ["--RE"], ["--REAL=1"], ["extra"]),
               "sma": (["--size", "nan"], ["--rea"], ["--fast_period", "5"]),
               "ema-hma": (["--long_period", "x"], ["--long-period-x", "5"]),
               "multiple-hma": (["--days", "x"],), "moving-average": (["--max-orders", "x"],)}
        for key, cases in bad.items():
            module = importlib.import_module(f"trading_bot.strategies.{MODULES[key]}")
            for extra in cases:
                with self.subTest(strategy=key, extra=extra), \
                        patch.object(module, "create_trading_api", side_effect=AssertionError) as api:
                    code, _, err = run_main(["--strategy", key, *extra])
                    self.assertEqual(code, 2, err)
                    self.assertIn(f"tb-trade --strategy {key}: error", err)
                    api.assert_not_called()

    def test_strategy_help_is_the_strategy_parser_help(self):
        scripts = console_scripts()
        for key in TRADABLE:
            with self.subTest(strategy=key):
                with patch.object(sys, "argv", [f"trading-bot-{key}", "--help"]), \
                        redirect_stdout(io.StringIO()) as legacy, self.assertRaises(SystemExit) as done:
                    scripts[f"trading-bot-{key}"].load()()
                self.assertEqual(done.exception.code, 0)
                code, out, _ = run_main(["--strategy", key, "--help"])
                self.assertEqual(code, 0)
                self.assertTrue(out.startswith(f"usage: tb-trade --strategy {key} "))
                options = lambda text: text[text.index("\noptions:"):]
                if key != "hma":
                    self.assertEqual(options(out), options(legacy.getvalue()))
                    continue
                # tb-trade refuses HMA's --backtest options, so its help omits exactly them.
                from trading_bot.backtesting.cli import SIMULATION_OPTIONS
                hidden = {"--backtest", *SIMULATION_OPTIONS}
                for option in hidden:
                    self.assertIn(option, legacy.getvalue())
                    self.assertNotIn(option, out)
                for line in options(out).splitlines():
                    self.assertIn(line, options(legacy.getvalue()))


class SafetyGateTests(unittest.TestCase):
    """tb-trade reaches the existing authentication and LIVE gates, never around them."""

    def test_demo_reaches_real_startup_selecting_only_the_demo_host(self):
        from trading_bot.config.trading_config import ConfigurationError

        for key in TRADABLE:
            client = Mock()
            client.createNewSession.return_value = None
            client._unavailable = None
            with self.subTest(strategy=key), ExitStack() as stack:
                api = stack.enter_context(patch("trading_bot.api.client.TradingApi", return_value=client))
                gate = stack.enter_context(patch("trading_bot.risk.live_execution.LiveExecution"))
                stack.enter_context(redirect_stdout(io.StringIO()))
                stack.enter_context(redirect_stderr(io.StringIO()))
                with self.assertRaises(ConfigurationError) as failure:
                    trade.main(["--strategy", key])
                self.assertEqual(str(failure.exception), "demo_authentication_failed_no_fallback")
                api.assert_called_once_with(environment="demo", account=None, auto_authenticate=False)
                gate.assert_not_called()
                gate.validate_options.assert_not_called()

    def test_live_runs_the_existing_preflight_before_any_authentication(self):
        for key in TRADABLE:
            for flag in ("--REAL", "--real"):
                with self.subTest(strategy=key, flag=flag), ExitStack() as stack:
                    api = stack.enter_context(patch("trading_bot.api.client.TradingApi",
                                                    side_effect=AssertionError("no auth")))
                    validate = stack.enter_context(patch(
                        "trading_bot.risk.live_execution.LiveExecution.validate_options",
                        side_effect=StartupReached))
                    out = stack.enter_context(redirect_stdout(io.StringIO()))
                    with self.assertRaises(StartupReached):
                        trade.main(["--strategy", key, flag, "--size", "50"])
                    args, symbol, size = validate.call_args.args
                    self.assertIs(args.real, True)
                    self.assertEqual((symbol, size), ("EURUSD", 50))
                    self.assertIn("LIVE TRADING — REAL MONEY AT RISK", out.getvalue())
                    api.assert_not_called()

    def test_invalid_live_limits_stop_before_authentication(self):
        from trading_bot.risk.live_risk import IntegrationError

        for key in TRADABLE:
            with self.subTest(strategy=key), \
                    patch("trading_bot.api.client.TradingApi", side_effect=AssertionError) as api, \
                    redirect_stdout(io.StringIO()), self.assertRaises(IntegrationError):
                trade.main(["--strategy", key, "--REAL", "--max-margin", "0"])
            api.assert_not_called()

    def test_tb_trade_runs_the_identical_strategy_function(self):
        # The same function object as trading-bot-<key> (hma: its main through
        # hma.trade), so the existing network recovery, uncertain-order, journal and
        # shutdown tests cover tb-trade too.
        scripts = console_scripts()
        for key in TRADABLE:
            with self.subTest(strategy=key):
                if key == "hma":
                    from trading_bot.strategies import hma
                    with patch.object(hma, "main", return_value=None) as main:
                        registry.get(key).load_trading_entry()()
                    main.assert_called_once_with(simulation_help=False)
                    self.assertIs(scripts["trading-bot-hma"].load(), hma.main)
                    continue
                self.assertIs(registry.get(key).load_trading_entry(),
                              scripts[f"trading-bot-{key}"].load())

    def test_tb_trade_has_no_broker_or_order_code_of_its_own(self):
        source = (ROOT / "src/trading_bot/tools/trade.py").read_text()
        registry_source = (ROOT / "src/trading_bot/strategies/registry.py").read_text()
        for text in (source, registry_source):
            for forbidden in ("trading_bot.api", "trading_bot.risk", "create_trading_api",
                              "createPosition", "closePosition", "load_credentials", "retry"):
                self.assertNotIn(forbidden, text)


class TradeIsolationTests(GuardedChild, unittest.TestCase):
    """Installed tb-trade: offline commands import no strategy or broker module."""

    def test_help_listing_and_rejections_load_no_strategy_or_broker_module(self):
        cases = {
            ("--help",): 0, ("--list-strategies",): 0, (): 2,
            ("--strategy", "donchian"): 2, ("--strategy", "grid"): 2,
            ("--strategy", "grid", "--REAL"): 2, ("--strategy", "grid", "--real"): 2,
            ("--strategy", "nonexistent"): 2, ("--strategy", "hma", "--strategy", "sma"): 2,
            ("--strategy", "hma", "--backtest"): 2, ("--strategy", "hma", "--REAL", "--start", "x"): 2,
            ("--strategy", "hma", "--environment", "live"): 2,
        }
        for args, expected in cases.items():
            with self.subTest(args=args):
                result = self.command("tb-trade", *args, forbid=BROKER_PREFIXES)
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_strategy_help_is_offline_and_unauthenticated(self):
        for key in TRADABLE:
            with self.subTest(strategy=key):
                # Some strategy modules import the client module at top level (no
                # side effects); the guard still forbids sockets, .env and LIVE code.
                result = self.command("tb-trade", "--strategy", key, "--help",
                                      forbid=("trading_bot.risk",))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"usage: tb-trade --strategy {key}", result.stdout)
                self.assertIn("--REAL", result.stdout)

    def test_installed_legacy_command_and_tb_trade_reach_identical_startup(self):
        cases = [(key, extra) for key in TRADABLE
                 for extra in ([], ["--REAL", "--symbol", "AUDUSD", "--size", "200"], SPECIFIC[key])]
        for key, extra in cases:
            with self.subTest(strategy=key, extra=extra):
                legacy = self.command(f"trading-bot-{key}", *extra, fake_startup=True,
                                      forbid=("trading_bot.risk",))
                unified = self.command("tb-trade", "--strategy", key, *extra, fake_startup=True,
                                       forbid=("trading_bot.risk",))
                self.assertEqual(legacy.returncode, 97, legacy.stderr)
                self.assertEqual(unified.returncode, 97, unified.stderr)
                expected = json.loads(legacy.stdout.splitlines()[-1])
                self.assertEqual(json.loads(unified.stdout.splitlines()[-1]), expected)
                self.assertEqual(expected["args"]["real"], str("--REAL" in extra))


class BacktestCliTests(unittest.TestCase):
    def invoke(self, argv):
        with ExitStack() as stack:
            fetch = stack.enter_context(patch("trading_bot.backtesting.runner.load_history"))
            snap = stack.enter_context(patch("trading_bot.backtesting.runner.load_snapshot"))
            out = stack.enter_context(redirect_stdout(io.StringIO()))
            err = stack.enter_context(redirect_stderr(io.StringIO()))
            try:
                code = backtest_cli.main(argv)
            except SystemExit as exc:
                code = exc.code
        fetch.assert_not_called()
        snap.assert_not_called()
        return code, out.getvalue(), err.getvalue()

    def test_tb_backtest_is_the_existing_backtest_command(self):
        scripts = console_scripts()
        self.assertEqual(scripts["tb-backtest"].value, scripts["trading-bot-backtest"].value)
        self.assertIs(scripts["tb-backtest"].load(), backtest_cli.main)

    def test_list_strategies(self):
        code, out, err = self.invoke(["--list-strategies"])
        self.assertEqual((code, err), (0, ""))
        rows = {line.split()[0]: line.split()[1:4] for line in out.splitlines()[1:8]}
        self.assertEqual(rows, {"moving-average": ["MINUTE", "dukascopy", "continuous"],
                                "sma": ["MINUTE", "dukascopy", "continuous"],
                                "hma": ["MINUTE_30", "dukascopy", "continuous"],
                                "ema-hma": ["MINUTE_5", "dukascopy", "continuous"],
                                "multiple-hma": ["MINUTE_30", "dukascopy", "continuous"],
                                "donchian": ["HOUR", "snapshot", "research"],
                                "grid": ["HOUR", "dukascopy", "grid"]})
        self.assertNotIn("No historical backtest", out)
        code, _, err = self.invoke(["--list-strategies", "--strategy", "grid"])
        self.assertEqual(code, 2)
        self.assertIn("--list-strategies takes no other arguments", err)

    def test_default_strategy_is_still_hma(self):
        args = backtest_cli.build_parser().parse_args([])
        self.assertEqual(args.strategy, "hma")

    def test_repeated_or_unknown_strategy_is_rejected_before_data_access(self):
        for argv in (["--strategy", "hma", "--strategy", "grid"], ["--strategy=grid", "--strategy=grid"]):
            with self.subTest(argv=argv):
                code, _, err = self.invoke(argv)
                self.assertEqual(code, 2)
                self.assertIn("--strategy may be given only once", err)
        trading_only = Strategy("paper-only", "x", {DEMO}, trading_entry="trading_bot.strategies.hma:main")
        with patch.dict(registry.STRATEGIES, {"paper-only": trading_only}):
            code, _, err = self.invoke(["--strategy", "paper-only"])
        self.assertEqual(code, 2)
        self.assertIn("tb-trade --strategy paper-only", err)
        code, _, err = self.invoke(["--strategy", "nonexistent"])
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)

    def test_trading_arguments_are_rejected_before_data_access(self):
        for strategy in ("hma", "donchian", "grid"):
            for option in (["--REAL"], ["--real"], ["--account", "x"], ["--account=x"], ["--read-only"],
                           ["--cycles", "1"], ["--days", "5"], ["--schedule-time-frame", "MINUTE_5"],
                           ["--max-margin", "1"], ["--max-exposure", "1"], ["--max-charges", "1"],
                           ["--max-loss", "1"], ["--max-orders", "1"], ["--stop-distance", "1"],
                           ["--fee-profile", "f"]):
                with self.subTest(strategy=strategy, option=option):
                    code, _, err = self.invoke(["--strategy", strategy, *option])
                    self.assertEqual(code, 2)
                    self.assertIn("DEMO/LIVE trading uses tb-trade", err)
            for option in (["--network-retries", "2"], ["--no-network-recovery"], ["--environment", "live"]):
                with self.subTest(strategy=strategy, option=option):
                    code, _, err = self.invoke(["--strategy", strategy, *option])
                    self.assertEqual(code, 2)
                    self.assertIn("unrecognized arguments", err)


class BacktestEquivalenceTests(GuardedChild, unittest.TestCase):
    """Installed trading-bot-backtest and tb-backtest: identical, broker-free, reproducible."""

    BROKER = ("trading_bot.api", "trading_bot.risk", "trading_bot.config.trading_config",
              "trading_bot.utils.network_recovery")

    def compare(self, *args):
        reports, outputs = [], []
        for name in ("trading-bot-backtest", "tb-backtest"):
            # A fresh directory per run: glob order is filesystem-dependent.
            output = self.tmp / "work" / f"out-{len(list((self.tmp / 'work').glob('out-*')))}"
            result = self.command(name, *args, "--output-dir", str(output), forbid=self.BROKER)
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append([line for line in result.stdout.splitlines()
                            if not line.startswith("Results:")])
            found = list(output.glob("*/report.json"))
            self.assertEqual(len(found), 1, found)
            reports.append(json.loads(found[0].read_text()))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(reports[0], reports[1])  # Including the reproducibility hashes.
        self.assertEqual(len(reports[0]["reproducibility"]["configuration_sha256"]), 64)
        return reports[0], "\n".join(outputs[0])

    def test_help_and_listing(self):
        for name in ("tb-backtest", "trading-bot-backtest"):
            for args in (("--help",), ("--list-strategies",)):
                with self.subTest(command=name, args=args):
                    result = self.command(name, *args, forbid=self.BROKER)
                    self.assertEqual(result.returncode, 0, result.stderr)
        for args in (("--REAL",), ("--strategy", "grid", "--real"), ("--account", "a"),
                     ("--strategy", "sma", "--cycles", "1"), ("--strategy", "nonexistent")):
            with self.subTest(args=args):
                result = self.command("tb-backtest", *args, forbid=self.BROKER)
                self.assertEqual(result.returncode, 2, result.stderr)

    def test_hma_and_donchian(self):
        snapshot = self.tmp / "work/.cache/donchian/eurusd-30m.json.gz"
        donchian_snapshot(snapshot)
        window = ("--start", "2023-11-01", "--end", "2023-11-02")
        report, _ = self.compare("--strategy", "donchian", *window)
        self.assertEqual(report["strategy_key"], "donchian")
        self.assertEqual(Decimal(report["statistics"]["net_pnl"]), Decimal("0.428"))  # Pinned elsewhere too.
        report, _ = self.compare("--strategy", "hma", "--data-source", "snapshot",
                                 "--execution-model", "research", "--hma-period", "4", *window)
        self.assertEqual(report["strategy_key"], "hma")

    def test_grid_manual_and_automatic_bounds(self):
        oscillating_snapshot(self.tmp / "work/grid.json.gz")
        source = ("--data-source", "snapshot", "--snapshot", "grid.json.gz")
        manual, text = self.compare(*GRID_ARGS, *source, "--start", "2023-11-01", "--end", "2023-11-02")
        self.assertEqual(manual["grid"]["bounds"]["mode"], "manual")
        self.assertGreater(manual["statistics"]["grid_exits"], 0)
        self.assertIn("All orders are simulated", text)
        automatic, _ = self.compare("--strategy", "grid", "--grid-lookback", "6", "--grid-intervals", "4",
                                    "--ema-period", "4", "--max-position", "300", *source,
                                    "--start", "2023-11-01T08:00Z", "--end", "2023-11-02")
        bounds = automatic["grid"]["bounds"]
        self.assertEqual((bounds["mode"], bounds["lookback_candles"], bounds["lower"], bounds["upper"]),
                         ("automatic", 6, "1.1000", "1.1012"))


class ConsoleScriptTests(GuardedChild, unittest.TestCase):
    def test_preferred_and_legacy_commands_are_installed_with_matching_targets(self):
        scripts = console_scripts()
        expected_tb = {"tb-trade", "tb-backtest", *(f"tb-{tool}" for tool in TOOL_ALIASES)}
        legacy = {f"trading-bot-{name}" for name in (*TRADABLE, "backtest", *TOOL_ALIASES)}
        self.assertEqual(set(scripts), expected_tb | legacy)
        self.assertEqual(scripts["tb-trade"].value, "trading_bot.tools.trade:main")
        for tool in (*TOOL_ALIASES, "backtest"):
            with self.subTest(tool=tool):
                self.assertEqual(scripts[f"tb-{tool}"].value, scripts[f"trading-bot-{tool}"].value)
        for name in scripts:
            with self.subTest(executable=name):
                self.assertTrue((SCRIPTS / name).is_file(), name)

    def test_every_installed_executable_help_is_offline(self):
        for name in sorted(console_scripts()):
            with self.subTest(command=name):
                result = self.command(name, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)


if __name__ == "__main__":
    unittest.main()

"""Installed src layout and legacy compatibility, with I/O guarded in children."""
import ast
import importlib
from importlib.metadata import distribution
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from trading_bot.config.paths import ROOT

# Every former implementation path remains importable, including test.py's imports.
LEGACY = {
    "TradingApi": "api.client", "tradingPosition": "api.models",
    "main": "strategies.moving_average", "test_strategy": "tools.backtest",
    "sma": "strategies.sma", "hma": "strategies.hma", "ema_hma": "strategies.ema_hma",
    "multiple_hma": "strategies.multiple_hma", "indicators": "strategies.indicators",
    "colors": "utils.colors", "scheduler": "utils.scheduler", "ping_service": "tools.ping_service",
    "test_api_key": "tools.demo_roundtrip", "test_live_api": "tools.live_preflight",
    **{f"scripts.{name}": f"risk.{name}" for name in (
        "live_risk", "live_execution", "strategy_costs", "trade_reconciliation")},
    **{f"scripts.{name}": f"config.{name}" for name in ("trading_config", "strategy_defaults")},
    "scripts.network_errors": "api.network_errors",
    **{f"scripts.{name}": f"tools.{name}" for name in (
        "demo_readonly", "diagnose_demo_auth", "check_environment")},
    "scripts.test_offline": "tools.check_offline", "scripts.integration_roundtrip": "tools.roundtrip",
}

GUARD = '''
import sys
from pathlib import Path
from unittest.mock import patch

def forbidden(*args, **kwargs):
    raise AssertionError("Account/network/credential access forbidden")

def audit(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname"}:
        forbidden()
    if event == "open" and isinstance(args[0], (str, bytes)):
        name = Path(args[0].decode() if isinstance(args[0], bytes) else args[0]).name
        if name == ".env" or name.startswith(".env.") and not name.endswith(".example"):
            forbidden()
sys.addaudithook(audit)
import requests
import dotenv
from trading_bot.api.client import TradingApi
'''


class PackageLayoutTests(unittest.TestCase):
    def child(self, code, *args):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-I", "-c", GUARD + code, *args],
                                    cwd=directory, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_all_legacy_imports_share_the_canonical_module_and_patch_points(self):
        for legacy, target in LEGACY.items():
            with self.subTest(module=legacy):
                self.assertIs(importlib.import_module(legacy), importlib.import_module("trading_bot." + target))

    def test_all_package_modules_import_outside_checkout_with_no_account_access(self):
        self.child('''
import importlib
import pkgutil
import trading_bot
with patch.object(TradingApi, "__init__", forbidden), patch.object(requests.sessions.Session, "send", forbidden), patch.object(dotenv, "dotenv_values", forbidden), patch("time.sleep", forbidden):
    for module in pkgutil.walk_packages(trading_bot.__path__, "trading_bot."):
        importlib.import_module(module.name)
from trading_bot.config.paths import ROOT
assert str(ROOT) == sys.argv[1]
''', str(ROOT))

    def test_untouched_test_py_imports_safely_without_running_main(self):
        self.child('''
import importlib.util
sys.path.insert(0, sys.argv[1])  # test.py deliberately retains its checkout-only legacy imports.
with patch.object(TradingApi, "__init__", forbidden), patch.object(requests.sessions.Session, "send", forbidden):
    spec = importlib.util.spec_from_file_location("legacy_trade_entry", Path(sys.argv[1]) / "test.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.main)
''', str(ROOT))

    def test_all_installed_help_commands_exit_before_authentication(self):
        self.child('''
from importlib.metadata import distribution
with patch.object(TradingApi, "__init__", forbidden), patch.object(requests.sessions.Session, "send", forbidden), patch.object(dotenv, "dotenv_values", forbidden), patch("time.sleep", forbidden):
    for entry in distribution("trading-bot").entry_points:
        if entry.group != "console_scripts":
            continue
        sys.argv = [entry.name, "--help"]
        try:
            entry.load()()
        except SystemExit as exc:
            assert exc.code == 0
        else:
            raise AssertionError(entry.name)
''')

    def test_old_and_new_script_help_work_outside_checkout(self):
        self.child('''
import runpy
checkout = Path(sys.argv[1])
with patch.object(TradingApi, "__init__", forbidden), patch.object(requests.sessions.Session, "send", forbidden):
    for relative in ("test.py", "test_api_key.py", "test_live_api.py", "test_strategy.py", "main.py", "scripts/demo_roundtrip.py", "scripts/live_preflight.py", "scripts/backtest.py"):
        sys.path.insert(0, str(checkout))
        sys.argv = [str(checkout / relative), "--help"]
        try:
            runpy.run_path(sys.argv[0], run_name="__main__")
        except SystemExit as exc:
            assert exc.code == 0
        else:
            raise AssertionError(relative)
''', str(ROOT))

    def test_production_package_never_imports_checkout_compatibility_modules(self):
        forbidden_modules = {name.split('.')[0] for name in LEGACY} | {"scripts"}
        for path in (ROOT / "src/trading_bot").rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                names = ([node.module] if isinstance(node, ast.ImportFrom) and node.level == 0
                         else [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
                for name in names:
                    if name:
                        self.assertNotIn(name.split('.')[0], forbidden_modules, str(path))

    def test_packaging_discovers_only_src_and_has_all_console_commands(self):
        self.assertEqual(len([entry for entry in distribution("trading-bot").entry_points
                              if entry.group == "console_scripts"]), 13)
        self.assertTrue((ROOT / "src/trading_bot/api/client.py").is_file())

    def test_fixed_credentials_path_ignores_working_directory(self):
        self.child('''
from trading_bot.config.trading_config import ROOT as config_root
assert str(config_root) == sys.argv[1]
assert config_root != Path.cwd()
''', str(ROOT))

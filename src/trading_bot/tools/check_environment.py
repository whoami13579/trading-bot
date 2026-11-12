"""Offline environment checks; never authenticate or run a strategy loop."""

import ast
from contextlib import redirect_stdout
import importlib
from importlib.metadata import distribution, version
import inspect
import io
from pathlib import Path
import py_compile
import runpy
import sys
import tomllib
from unittest.mock import patch


from trading_bot.config.paths import ROOT
CLI_SCRIPTS = ("main", "sma", "test", "hma", "multiple_hma", "ema_hma",
               "ping_service", "test_strategy", "test_api_key", "test_live_api")
# These files are compiled and inspected as text, never imported or executed.
STATIC_ONLY = {"main", "test"}


def forbidden(*args, **kwargs):
    raise RuntimeError("Offline check blocked API construction, I/O, or waiting")


def block_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname"}:
        forbidden()


def main(argv=None):
    import argparse
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Run this check with the project's Python 3.12 environment")
    sys.addaudithook(block_network)
    sys.path.insert(0, str(ROOT))

    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = [p for p in lock["package"] if "registry" in p["source"]]
    for package in packages:
        if version(package["name"]) != package["version"]:
            raise RuntimeError(f"Installed version differs from lock: {package['name']}")
    for module in ("requests", "dotenv", "Crypto.Cipher.AES", "certifi",
                   "charset_normalizer", "idna", "urllib3"):
        importlib.import_module(module)
    print(f"PASS: Python {sys.version.split()[0]}, {len(packages)} locked dependencies and imports")

    sources = (sorted(ROOT.glob("*.py")) + sorted((ROOT / "scripts").glob("*.py"))
               + sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "tests").rglob("*.py")))
    for path in sources:
        py_compile.compile(str(path), doraise=True)
    print(f"PASS: compiled {len(sources)} Python files (no entry points executed)")

    import dotenv
    import requests
    from trading_bot.api.client import TradingApi

    signature = inspect.signature(TradingApi)
    constructor_calls = 0
    for path in sources:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "TradingApi"
                    and not any(isinstance(arg, ast.Starred) for arg in node.args)
                    and all(keyword.arg is not None for keyword in node.keywords)):
                # Bind dummy values without evaluating expressions or constructing an API.
                signature.bind(*([None] * len(node.args)), **{k.arg: None for k in node.keywords})
                constructor_calls += 1
        if path.parent == ROOT and path.stem in STATIC_ONLY:
            if any(isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) for node in tree.body):
                raise RuntimeError(f"Unguarded top-level call in {path.name}")
    print(f"PASS: {constructor_calls} TradingApi call signatures; main.py/test.py inspected statically only")

    with patch.object(TradingApi, "__init__", forbidden), \
         patch.object(requests.sessions.Session, "request", forbidden), \
         patch.object(dotenv, "load_dotenv", forbidden), \
         patch("time.sleep", forbidden):
        modules = [p.stem for p in ROOT.glob("*.py") if p.stem not in STATIC_ONLY]
        modules += [".".join(p.relative_to(ROOT / "src").with_suffix("").parts)
                    for p in (ROOT / "src/trading_bot").rglob("*.py") if p.name != "__init__.py"]
        for module in sorted(modules):
            importlib.import_module(module)
        print(f"PASS: imported {len(modules)} project modules with API, network, dotenv loading and sleep blocked")

        from trading_bot.strategies.indicators import calculate_ema, calculate_hma, calculate_sma, calculate_wma
        from trading_bot.utils.scheduler import timeframe_to_minutes

        prices = [10.0] * 60
        assert calculate_sma(prices, 16) == 10.0
        assert calculate_wma(prices, 16) == 10.0
        assert calculate_ema(prices, 16)[-1] == 10.0
        assert calculate_hma(prices, 16)[-1] == 10.0
        assert timeframe_to_minutes("MINUTE_15") == [0, 15, 30, 45]
        print("PASS: indicator and scheduling smoke checks using synthetic data")

        from trading_bot.strategies.sma import get_buffer_prices
        from trading_bot.tools.backtest import TestStrategy
        from unittest.mock import Mock

        api = Mock()
        api.getHistoricalPrices.return_value = ({"prices": [{"openPrice": {"ask": 10.0}}]}, 200)
        assert get_buffer_prices(api, 1) == [10.0]
        assert TestStrategy(api).get_price(1) == [10.0]
        print("PASS: legacy historical-price readers handle (payload, status) using a fake API")

        original_argv = sys.argv[:]
        try:
            for script in CLI_SCRIPTS:
                sys.argv = [f"{script}.py", "--help"]
                output = io.StringIO()
                with redirect_stdout(output):
                    try:
                        runpy.run_path(str(ROOT / f"{script}.py"), run_name="__main__")
                    except SystemExit as exc:
                        if exc.code != 0:
                            raise
                    else:
                        raise RuntimeError(f"{script}.py --help did not exit during argument parsing")
                assert "usage:" in output.getvalue()
                print(f"PASS: {script}.py --help exited before configuration or API access")
            for path in sorted((ROOT / "scripts").glob("*.py")):
                if path.stem not in {"check_environment", "check_offline", "test_offline", "demo_readonly",
                                     "diagnose_demo_auth", "demo_roundtrip", "live_preflight",
                                     "ping_service", "backtest"}:
                    continue
                sys.argv = [str(path), "--help"]
                with redirect_stdout(io.StringIO()) as output:
                    try:
                        runpy.run_path(str(path), run_name="__main__")
                    except SystemExit as exc:
                        assert exc.code == 0
                    else:
                        raise RuntimeError(f"{path.name} --help did not exit")
                assert "usage:" in output.getvalue()
                print(f"PASS: scripts/{path.name} --help")
            for entry in distribution("trading-bot").entry_points:
                if entry.group != "console_scripts":
                    continue
                sys.argv = [entry.name, "--help"]
                with redirect_stdout(io.StringIO()) as output:
                    try:
                        entry.load()()
                    except SystemExit as exc:
                        assert exc.code == 0
                    else:
                        raise RuntimeError(f"{entry.name} --help did not exit")
                assert "usage:" in output.getvalue()
                print(f"PASS: installed {entry.name} --help")
        finally:
            sys.argv = original_argv

    print("All offline checks passed; no accounts contacted or trades performed.")


if __name__ == "__main__":
    main()

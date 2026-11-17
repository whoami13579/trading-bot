"""Exercise the direct Donchian entry point with real offline snapshot fixtures."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from trading_bot.backtesting.models import Candle, OHLC
from trading_bot.backtesting.study_data import START, END, FETCH_START, write_snapshot

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src/trading_bot/strategies/donchian.py"

# A fresh interpreter must not acquire prices, authenticate, or read private state.
GUARD = """
import sys
from pathlib import Path
def guard(event, args):
    if event.startswith('socket.') and event != 'socket.__new__':
        raise AssertionError('Network forbidden for cached backtest')
    if event == 'import' and args[0].startswith(('trading_bot.api', 'trading_bot.risk')):
        raise AssertionError('Broker modules forbidden for cached backtest')
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(args[0].decode() if isinstance(args[0], bytes) else args[0])
        if path.name.startswith('.env') or any(p in ('.demo-integration', '.live-integration') for p in path.parts):
            raise AssertionError('Credentials/private state forbidden')
sys.addaudithook(guard)
import runpy
script = sys.argv.pop(1)
sys.argv[0] = script
runpy.run_path(script, run_name='__main__')
"""


def candle(stamp, mid):
    sides = [OHLC(*(mid + shift for _ in range(4)))
             for shift in (Decimal("-.0001"), Decimal(".0001"))]
    return Candle(stamp, sides[1], "EURUSD", "MINUTE_30", "fixture", *sides,
                  observations=30)


def fixture(path, *, flat=False, warmup=True):
    # Match the immutable study snapshot contract, while keeping the scored test
    # interval short. Outer sentinel candles are outside the scored day.
    candles = [candle(FETCH_START, Decimal("1.1"))]
    if warmup:
        candles.extend(candle(START - timedelta(minutes=30*i), Decimal("1.1"))
                       for i in range(48, 0, -1))
    candles.extend(candle(START + timedelta(minutes=30*i),
                          Decimal("1.1") + (0 if flat else Decimal(".0001")*i))
                   for i in range(48))
    candles.append(candle(END - timedelta(days=2), Decimal("1.1")))
    write_snapshot(path, candles, {"source": "fixture"})


class DonchianCliTests(unittest.TestCase):
    def report_fields(self, output):
        self.assertEqual(output.splitlines()[0], "========== BACKTEST REPORT ==========")
        self.assertTrue(output.endswith("=====================================\n"))
        return {line[:24].strip().removesuffix(":"): line[25:]
                for line in output.splitlines()
                if line[:24].rstrip().endswith(":")}

    def invoke(self, cwd, *args):
        return subprocess.run(
            [sys.executable, "-c", GUARD, str(SCRIPT), *args],
            cwd=cwd, capture_output=True, text=True, timeout=20,
        )

    def test_direct_backtest_uses_default_cache_and_prints_one_real_run(self):
        with tempfile.TemporaryDirectory() as td:
            snapshot = Path(td) / ".cache/donchian/eurusd-30m.json.gz"
            fixture(snapshot)
            result = self.invoke(td, "--backtest", "--start", "2023-11-01", "--end", "2023-11-02")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.count("========== BACKTEST REPORT =========="), 1)
        fields = self.report_fields(result.stdout)
        expected = {
            "Strategy": "Donchian (long / short), 20/10 bars",
            "Symbol": "EURUSD", "Size": "100 EUR", "Environment": "DEMO (simulated only)",
            "Timeframe": "HOUR (1H)", "Data source": "fixture (cached historical bid/ask)",
            "Period": "2023-11-01T00:00:00+00:00 to 2023-11-02T00:00:00+00:00 (UTC, end exclusive)",
            "Candles": "24", "Total closed trades": "1",
            "Initial balance": "$1,000.0000", "Final balance": "$1,000.4280",
            "Final equity": "$1,000.4280", "Available funds": "$1,000.4280",
            "Gross P&L": "$0.4500", "Modeled costs": "$0.0220", "Net P&L": "$0.4280",
            "Total return": "0.0428%", "Win rate": "100.0000%",
            "Max drawdown": "$0.0210 / 0.0021%",
        }
        for name, value in expected.items():
            self.assertEqual(fields[name], value, name)
        # Shared meanings retain HMA's field order even with Donchian additions.
        names = list(fields)
        common_order = ["Strategy", "Symbol", "Data source", "Timeframe", "Historical days",
                        "Period", "Candles", "Initial balance", "Final balance", "Final equity",
                        "Net P&L", "Total return", "Total closed trades", "Winning trades",
                        "Losing trades", "Win rate", "Profit factor", "Max drawdown",
                        "Realized cash P&L", "Unrealized P&L", "Available funds"]
        self.assertEqual(sorted(common_order, key=names.index), common_order)
        self.assertIn("All orders are simulated", result.stdout)
        self.assertIn("Base costs: spread + 0.1 pip/leg slippage; commission assumed zero; historical financing omitted.", result.stdout)

    def test_missing_default_snapshot_is_nonzero_and_actionable_on_stderr(self):
        with tempfile.TemporaryDirectory() as td:
            result = self.invoke(td, "--backtest")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Donchian backtest failed", result.stderr)
        self.assertIn(".cache/donchian/eurusd-30m.json.gz", result.stderr)
        self.assertIn("EURUSD MINUTE_30 gzip JSON study snapshot", result.stderr)
        self.assertIn("uv run -m trading_bot.backtesting.study_data --source capital-demo", result.stderr)
        self.assertIn("--source dukascopy", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_zero_trades_still_prints_success_summary(self):
        with tempfile.TemporaryDirectory() as td:
            snapshot = Path(td) / "flat.json.gz"
            fixture(snapshot, flat=True)
            result = self.invoke(td, "--backtest", "--snapshot", str(snapshot),
                                 "--start", "2023-11-01", "--end", "2023-11-02")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        fields = self.report_fields(result.stdout)
        self.assertEqual(fields["Total closed trades"], "0")
        for name in ("Gross P&L", "Net P&L", "Modeled costs"):
            self.assertEqual(fields[name], "$0.0000")
        self.assertEqual(fields["Max drawdown"], "$0.0000 / 0.0000%")
        self.assertEqual(fields["Win rate"], "unavailable")
        self.assertEqual(fields["Profit factor"], "undefined_no_profits_or_losses")

    def test_insufficient_warmup_is_nonzero_not_zero_trade_success(self):
        with tempfile.TemporaryDirectory() as td:
            snapshot = Path(td) / "insufficient.json.gz"
            fixture(snapshot, warmup=False)
            result = self.invoke(td, "--backtest", "--snapshot", str(snapshot),
                                 "--start", "2023-11-01", "--end", "2023-11-02")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Insufficient 1H warm-up: need 20 completed bars", result.stderr)
        self.assertIn("Prepare it with:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_help_documents_options_defaults_and_simulated_execution(self):
        with tempfile.TemporaryDirectory() as td:
            result = self.invoke(td, "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        for value in ("--backtest", "--snapshot", "--start", "--end", "EURUSD",
                      "100 EUR", "DEMO", "1H", "20/10", "0.1 pip", "2023-11-01",
                      "2026-11-01", ".cache/donchian/eurusd-30m.json.gz"):
            self.assertIn(value, result.stdout)


if __name__ == "__main__":
    unittest.main()

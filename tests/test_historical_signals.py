import json
import math
from pathlib import Path
from statistics import fmean
import unittest
from unittest.mock import Mock

from indicators import calculate_ema, calculate_hma, calculate_hma_result, calculate_multiple, calculate_two_ema_result
from sma import calculate_sma
from reference_indicators import ema, hma_tail, multiple_signal

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class HistoricalSignalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = json.loads((FIXTURES / "eurusd_ecb_daily.json").read_text())
        cls.values = [row["usd_per_eur"] for row in fixture["observations"]]

    def test_archived_prices_are_finite_and_chronological(self):
        rows = json.loads((FIXTURES / "eurusd_ecb_daily.json").read_text())["observations"]
        self.assertGreaterEqual(len(rows), 210)
        self.assertEqual([r["date"] for r in rows], sorted({r["date"] for r in rows}))
        self.assertTrue(all(math.isfinite(p) and p > 0 for p in self.values))

    def test_sma_fast_and_slow_on_historical_windows(self):
        for end in range(210, len(self.values) + 1):
            values = self.values[:end]
            self.assertAlmostEqual(calculate_sma(values, 9), fmean(values[-9:]))
            self.assertAlmostEqual(calculate_sma(values, 21), fmean(values[-21:]))
            actual = calculate_sma(values, 9) - calculate_sma(values, 21)
            expected = fmean(values[-9:]) - fmean(values[-21:])
            self.assertEqual(actual > 0, expected > 0)

    def test_ema_signal_on_historical_windows(self):
        for end in range(210, len(self.values) + 1):
            values = self.values[end - 200:end]
            api = Mock(getHistoricalPricesList=Mock(return_value=values))
            self.assertAlmostEqual(calculate_ema(values, 20)[-1], ema(values, 20))
            self.assertEqual(calculate_two_ema_result(api, "EURUSD", "DAY", 200, 20),
                             "SELL" if ema(values, 20) < ema(values, 200) else "BUY")

    def test_hma_signal_on_historical_windows(self):
        for end in range(210, len(self.values) + 1):
            values = self.values[end - 48:end]
            api = Mock(getHistoricalPricesList=Mock(return_value=values))
            previous, current = hma_tail(values, 16)
            self.assertAlmostEqual(calculate_hma(values, 16)[-1], current)
            self.assertEqual(calculate_hma_result(api, "EURUSD", "DAY", 16),
                             "BUY" if previous < current else "SELL")

    def test_multiple_hma_agreement_on_historical_windows(self):
        for end in range(210, len(self.values) + 1):
            long_values = self.values[end - 210:end]
            short_values = self.values[end - 60:end]
            long_api = Mock(getHistoricalPricesList=Mock(return_value=long_values))
            short_api = Mock(getHistoricalPricesList=Mock(return_value=short_values))
            long_signal = calculate_multiple(long_api, "EURUSD", "DAY")
            short_signal = calculate_hma_result(short_api, "EURUSD", "DAY", 20)
            previous, current = hma_tail(short_values, 20)
            expected_short = "BUY" if previous < current else "SELL"
            expected_long = multiple_signal(long_values)
            self.assertEqual((long_signal, short_signal), (expected_long, expected_short))
            self.assertEqual(long_signal == short_signal, expected_long == expected_short)

    @unittest.skipUnless((FIXTURES / "eurusd_demo_prices.json").exists(), "Capital.com demo history requires successful demo authentication")
    def test_capital_demo_historical_snapshots(self):
        snapshots = json.loads((FIXTURES / "eurusd_demo_prices.json").read_text())["prices"]
        self.assertEqual(set(snapshots), {"MINUTE", "MINUTE_5", "MINUTE_30", "HOUR_4"})
        for resolution, snapshot in snapshots.items():
            values = snapshot["close"]
            self.assertGreaterEqual(len(values), 210)
            api = Mock(getHistoricalPricesList=Mock(return_value=values))
            self.assertEqual(calculate_multiple(api, "EURUSD", resolution), multiple_signal(values))
            self.assertAlmostEqual(calculate_ema(values, 20)[-1], ema(values, 20))
            self.assertAlmostEqual(calculate_hma(values, 16)[-1], hma_tail(values, 16)[-1])

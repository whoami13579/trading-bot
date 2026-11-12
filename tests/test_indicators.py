import math
from statistics import fmean
import unittest
from unittest.mock import Mock, patch

import indicators
from sma import calculate_sma as strategy_sma
from reference_indicators import ema, hma_tail, multiple_signal, wma


class IndicatorTests(unittest.TestCase):
    def setUp(self):
        self.values = [100 + 0.04 * i + math.sin(i / 7) for i in range(250)]

    def test_sma_and_wma_match_independent_reference(self):
        for period in (9, 16, 21, 200):
            with self.subTest(period=period):
                self.assertAlmostEqual(indicators.calculate_sma(self.values, period), fmean(self.values[-period:]))
                self.assertAlmostEqual(strategy_sma(self.values, period), fmean(self.values[-period:]))
                self.assertAlmostEqual(indicators.calculate_wma(self.values, period), wma(self.values, period))

    def test_ema_matches_seeded_reference(self):
        for period in (10, 20, 200):
            with self.subTest(period=period):
                self.assertAlmostEqual(indicators.calculate_ema(self.values, period)[-1], ema(self.values, period))

    def test_hma_latest_values_match_independent_reference(self):
        for period in (9, 16, 20, 27):
            with self.subTest(period=period):
                actual = indicators.calculate_hma(self.values, period)[-2:]
                for result, expected in zip(actual, hma_tail(self.values, period)):
                    self.assertAlmostEqual(result, expected)

    def test_trend_signals_for_rising_and_falling_prices(self):
        for direction, values in (("BUY", list(range(1, 251))), ("SELL", list(range(250, 0, -1)))):
            with self.subTest(direction=direction):
                api = Mock(getHistoricalPricesList=Mock(return_value=values))
                self.assertEqual(indicators.calculate_hma_result(api, "EURUSD", "MINUTE_30", 16), direction)
                self.assertEqual(indicators.calculate_two_ema_result(api, "EURUSD", "MINUTE_5", 200, 20), direction)
                self.assertEqual(indicators.calculate_multiple(api, "EURUSD", "HOUR_4"), direction)
                self.assertEqual("BUY" if strategy_sma(values, 9) > strategy_sma(values, 21) else "SELL", direction)

    def test_multiple_signal_matches_reference_votes(self):
        api = Mock(getHistoricalPricesList=Mock(return_value=self.values))
        self.assertEqual(indicators.calculate_multiple(api, "EURUSD", "HOUR_4"), multiple_signal(self.values))

    def test_multiple_neutral_threshold(self):
        api = Mock(getHistoricalPricesList=Mock(return_value=[100.0] * 210))
        with patch.object(indicators, "calculate_sma", side_effect=[99] * 3 + [101] * 3), \
             patch.object(indicators, "calculate_ema", side_effect=[[99]] * 3 + [[101]] * 3), \
             patch.object(indicators, "calculate_hma", return_value=[1, 2]):
            self.assertEqual(indicators.calculate_multiple(api, "EURUSD", "HOUR_4"), "Neutral")

    def test_invalid_and_insufficient_inputs_fail_clearly(self):
        cases = [(indicators.calculate_sma, ([], 9)),
                 (indicators.calculate_sma, ([1, 2], 0)),
                 (indicators.calculate_ema, ([1, 2], 20)),
                 (indicators.calculate_ema, ([1, 2], 0)),
                 (indicators.calculate_hma, ([1, 2], 1))]
        for function, arguments in cases:
            with self.subTest(function=function.__name__, arguments=arguments):
                with self.assertRaises(ValueError):
                    function(*arguments)

    def test_signal_helpers_reject_short_history(self):
        api = Mock(getHistoricalPricesList=Mock(return_value=[]))
        for function, extra in ((indicators.calculate_hma_result, (16,)),
                                (indicators.calculate_two_ema_result, (200, 20)),
                                (indicators.calculate_multiple, ())):
            with self.subTest(function=function.__name__):
                with self.assertRaises(ValueError):
                    function(api, "EURUSD", "MINUTE_30", *extra)

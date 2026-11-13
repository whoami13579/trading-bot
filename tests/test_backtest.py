"""Pure simulation regressions; no account credentials or network required."""
import io
import math
import unittest
from unittest.mock import Mock, patch

from trading_bot.strategies import backtest
from trading_bot.strategies.indicators import calculate_hma
from trading_bot.tools import backtest as command


class PortfolioTests(unittest.TestCase):
    def test_long_profit_and_short_profit_have_correct_signs(self):
        portfolio = backtest.Portfolio()
        portfolio.buy(10)
        portfolio.sell(13)
        self.assertEqual(portfolio.balance, 3)
        portfolio.buy(9)
        self.assertEqual(portfolio.balance, 7)
        self.assertEqual([t.pnl for t in portfolio.trades], [3, 4])
        self.assertEqual(portfolio.direction, "BUY")

    def test_long_loss_and_short_loss_have_correct_signs(self):
        portfolio = backtest.Portfolio()
        portfolio.buy(13)
        portfolio.sell(10)
        portfolio.buy(12)
        self.assertEqual([t.pnl for t in portfolio.trades], [-3, -2])
        self.assertEqual(portfolio.balance, -5)

    def test_repeated_signals_hold_original_entry(self):
        portfolio = backtest.Portfolio()
        portfolio.sell(10)
        portfolio.sell(9)
        portfolio.sell(8)
        self.assertEqual((portfolio.direction, portfolio.price, portfolio.balance), ("SELL", 10, 0))
        self.assertEqual(portfolio.trades, [])
        self.assertEqual(portfolio.unrealized(8), 2)

    def test_reversal_closes_exactly_one_prior_side(self):
        portfolio = backtest.Portfolio()
        portfolio.buy(10)
        portfolio.sell(12)
        self.assertEqual(len(portfolio.trades), 1)
        self.assertEqual((portfolio.direction, portfolio.price), ("SELL", 12))
        self.assertEqual(portfolio.unrealized(11), 1)

    def test_invalid_inputs_do_not_change_position(self):
        portfolio = backtest.Portfolio()
        for price in (0, -1, math.inf, math.nan):
            with self.subTest(price=price), self.assertRaises(ValueError):
                portfolio.buy(price)
        with self.assertRaises(ValueError):
            portfolio.target("unknown", 10)
        self.assertIsNone(portfolio.direction)


class HmaBacktestTests(unittest.TestCase):
    def test_downtrend_enters_sell_once_and_reports_open_profit_separately(self):
        result = backtest.run_hma_backtest([200 - i for i in range(100)], 10)
        self.assertEqual([s[1] for s in result.signals], ["SELL"])
        self.assertEqual(result.signals[0][0], 30)
        self.assertEqual(result.realized_pnl, 0)
        self.assertGreater(result.unrealized_pnl, 0)
        self.assertEqual(result.trades, ())
        self.assertEqual(result.open_direction, "SELL")

    def test_uptrend_enters_buy_once(self):
        result = backtest.run_hma_backtest(range(1, 101), 10)
        self.assertEqual([s[1] for s in result.signals], ["BUY"])
        self.assertEqual(result.unrealized_pnl, 69)

    def test_flat_history_and_insufficient_warmup_do_not_open_positions(self):
        for prices in ([], [1], [1] * 100, list(range(1, 31))):
            with self.subTest(length=len(prices)):
                result = backtest.run_hma_backtest(prices, 10)
                self.assertEqual(result.signals, ())
                self.assertIsNone(result.open_direction)
                self.assertEqual((result.realized_pnl, result.unrealized_pnl), (0, 0))

    def test_reversals_realize_only_completed_positions(self):
        prices = [100 + i for i in range(60)] + [159 - i for i in range(1, 60)]
        result = backtest.run_hma_backtest(prices, 10)
        self.assertEqual([s[1] for s in result.signals], ["BUY", "SELL"])
        self.assertEqual(len(result.trades), 1)
        self.assertGreater(result.trades[0].pnl, 0)
        self.assertEqual(result.realized_pnl, sum(t.pnl for t in result.trades))
        self.assertGreater(result.unrealized_pnl, 0)

    def test_future_prices_never_change_past_signals_or_closed_trades(self):
        prices = [100 + 10 * math.sin(i / 6) for i in range(130)]
        full = backtest.run_hma_backtest(prices, 8)
        for end in range(25, len(prices)):
            prefix = backtest.run_hma_backtest(iter(prices[:end]), 8)
            signals = tuple(s for s in full.signals if s[0] < end)
            self.assertEqual(prefix.signals, signals)
            self.assertEqual(prefix.trades, full.trades[:max(0, len(signals) - 1)])

    def test_indicator_is_shared_with_live_strategies(self):
        self.assertIs(backtest.calculate_hma, calculate_hma)

    def test_each_indicator_window_contains_only_observed_prices(self):
        prices = list(range(1, 41))
        original = calculate_hma
        windows = []
        def record(window, period):
            windows.append(tuple(window))
            return original(window, period)
        with patch.object(backtest, "calculate_hma", side_effect=record):
            backtest.run_hma_backtest(prices, 10)
        self.assertEqual(windows, [tuple(prices[i-29:i+1]) for i in range(30, 40)])

    def test_invalid_period_or_price_fails_without_api_calls(self):
        for period in (0, 1, -1, 2.5, True):
            with self.subTest(period=period), self.assertRaises(ValueError):
                backtest.run_hma_backtest([1] * 20, period)
        with self.assertRaises(ValueError):
            backtest.run_hma_backtest([1, float("nan")], 10)


class BacktestCommandTests(unittest.TestCase):
    def test_cli_rejects_broker_options_without_authentication(self):
        with patch("trading_bot.config.trading_config.create_trading_api") as create, \
             patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            command.main(["--REAL", "--account", "synthetic-account"])
        create.assert_not_called()

    def test_failed_history_is_rejected_instead_of_simulated(self):
        api = Mock()
        api.getHistoricalPrices.return_value = ({"prices": []}, 401)
        with self.assertRaises(ValueError):
            backtest.TestStrategy(api)
        api.createPosition.assert_not_called()

    def test_legacy_history_buffer_consumes_only_requested_prices(self):
        api = Mock()
        api.getHistoricalPrices.return_value = ({"prices": [{"openPrice": {"ask": p}} for p in (1, 2, 3)]}, 200)
        buffer = backtest.TestStrategy(api)
        self.assertIsNone(buffer.get_price(4))
        self.assertEqual(buffer.get_price(2), [1, 2])
        self.assertEqual(buffer.get_price(1), [3])
        self.assertIsNone(buffer.get_price(1))

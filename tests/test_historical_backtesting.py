"""Independent deterministic accounting and provider-contract regression cases."""

from contextlib import ExitStack, redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io, json, math
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from trading_bot.backtesting.models import Candle, OHLC, Dataset, DataError, dec
from trading_bot.backtesting.timeframes import (
    MINUTES,
    duration,
    floor_time,
    normalize,
    resample,
)
from trading_bot.backtesting.calendar import audit_coverage, expected_open
from trading_bot.backtesting.cache import Cache
from trading_bot.backtesting.data import load_history
from trading_bot.backtesting.engine import Ledger, SimulationConfig, run, statistics
from trading_bot.backtesting.strategy import HmaStrategy
from trading_bot.backtesting.providers.dukascopy import decode, DukascopyProvider
from trading_bot.backtesting.providers.yahoo import YahooProvider
from trading_bot.strategies.indicators import hma_signal, calculate_hma_result
from trading_bot.strategies import hma

BASE = datetime(2026, 10, 12, tzinfo=timezone.utc)


def candle(
    i,
    price="1.1",
    *,
    bid=None,
    ask=None,
    timeframe="MINUTE",
    stamp=None,
    source="dukascopy",
):
    stamp = stamp or BASE + i * duration(timeframe)

    def prices(p):
        return OHLC(p, p, p, p)

    return Candle(
        stamp,
        prices(ask if ask is not None else price),
        "EURUSD",
        timeframe,
        source,
        prices(bid) if bid is not None else None,
        prices(ask) if ask is not None else None,
    )


class DataNormalizationTests(unittest.TestCase):
    def test_all_timeframes_are_utc_epoch_aligned(self):
        for name, minutes in MINUTES.items():
            self.assertEqual(duration(name), timedelta(minutes=minutes))
            stamp = BASE + timedelta(hours=13, minutes=51, seconds=24)
            aligned = floor_time(stamp, name)
            self.assertLessEqual(aligned, stamp)
            self.assertLess(stamp, aligned + duration(name))
            self.assertEqual(int(aligned.timestamp()) % (minutes * 60), 0)

    def test_normalizes_timezone_order_and_identical_duplicates(self):
        rows = [candle(2), candle(0), candle(1), candle(1)]
        self.assertEqual(
            [c.timestamp for c in normalize(rows)],
            [BASE + timedelta(minutes=i) for i in range(3)],
        )
        with self.assertRaises(DataError):
            normalize([candle(1, "1.1"), candle(1, "1.2")])
        with self.assertRaises(DataError):
            candle(0, stamp=datetime(2026, 10, 12))
        self.assertEqual(
            candle(0, stamp=BASE.astimezone(timezone(timedelta(hours=8)))), candle(0)
        )

    def test_rejects_invalid_ohlc_quotes_and_alignment(self):
        for values in (
            (1, 0, 1, 1),
            (1, 2, 1.5, 1),
            (0, 1, 0, 1),
            (1, "NaN", 1, 1),
            (1, 1, 1, -1),
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                OHLC(*values)
        with self.assertRaises(DataError):
            candle(0, bid="1.2", ask="1.1")
        with self.assertRaises(DataError):
            candle(0, stamp=BASE + timedelta(seconds=30))

    def test_bid_and_ask_aggregate_separately(self):
        rows = [
            Candle(
                BASE + timedelta(minutes=i),
                OHLC(2, 3, 1, 2),
                "EURUSD",
                "MINUTE",
                "dukascopy",
                OHLC(1 + i, 2 + i, dec(".5") + i, dec("1.5") + i),
                OHLC(dec("1.1") + i, dec("2.1") + i, dec(".6") + i, dec("1.6") + i),
            )
            for i in range(5)
        ]
        result = resample(
            rows, "MINUTE_5", completed_before=BASE + timedelta(minutes=5)
        )
        self.assertEqual(result[0].bid, OHLC(1, 6, ".5", "5.5"))
        self.assertEqual(result[0].ask, OHLC("1.1", "6.1", ".6", "5.6"))
        self.assertEqual(result[0].observations, 5)

    def test_incomplete_candles_are_not_used_or_invented(self):
        rows = [candle(i) for i in range(9)]
        self.assertEqual(
            len(
                resample(rows, "MINUTE_5", completed_before=BASE + timedelta(minutes=9))
            ),
            1,
        )
        sparse = resample(
            [candle(1), candle(4)],
            "MINUTE_5",
            completed_before=BASE + timedelta(minutes=5),
        )
        self.assertEqual(sparse[0].open_time, BASE + timedelta(minutes=1))
        self.assertEqual(sparse[0].observations, 2)
        with self.assertRaises(DataError):
            resample(
                [candle(0, timeframe="HOUR")],
                "MINUTE",
                completed_before=BASE + timedelta(days=1),
            )

    def test_dst_aware_weekend_and_unknown_gaps_are_distinguished(self):
        self.assertTrue(
            expected_open(datetime(2026, 10, 18, 21, tzinfo=timezone.utc), "dukascopy")
        )
        self.assertFalse(
            expected_open(datetime(2026, 11, 8, 21, tzinfo=timezone.utc), "dukascopy")
        )
        self.assertTrue(
            expected_open(datetime(2026, 11, 8, 22, tzinfo=timezone.utc), "dukascopy")
        )
        with self.assertRaises(DataError):
            audit_coverage(
                [candle(0)], BASE, BASE + timedelta(minutes=3), "dukascopy", "MINUTE"
            )
        result = audit_coverage(
            [candle(0)],
            BASE,
            BASE + timedelta(minutes=3),
            "dukascopy",
            "MINUTE",
            allow_gaps=True,
        )
        self.assertEqual(result["missing_intervals"], 2)
        self.assertFalse(result["forward_filled"])

    def test_cache_integrity_and_refresh_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Cache(directory)
            cache.put("key", {"value": 1})
            self.assertEqual(cache.get("key"), {"value": 1})
            outer = json.loads(cache.path("key").read_text())
            outer["payload"] = '{"value":2}'
            cache.path("key").write_text(json.dumps(outer))
            with self.assertRaises(DataError):
                cache.get("key")
            self.assertIsNone(Cache(directory, refresh=True).get("key"))
        with self.assertRaises(ValueError):
            Cache(".demo-integration/data")

    def test_only_declared_quote_gaps_can_pass_strict_coverage(self):
        rows = [candle(0), candle(3)]
        known = [(candle(1).timestamp.isoformat(), candle(3).timestamp.isoformat())]
        result = audit_coverage(
            rows,
            BASE,
            candle(3).end,
            "dukascopy",
            "MINUTE",
            provider_buckets_complete=True,
            no_quote_ranges=known,
        )
        self.assertEqual(result["provider_no_quote_intervals"], 2)
        self.assertEqual(result["unexplained_missing_intervals"], 0)
        with self.assertRaises(DataError):
            audit_coverage(
                rows,
                BASE,
                candle(3).end,
                "dukascopy",
                "MINUTE",
                provider_buckets_complete=True,
            )

    def test_four_hour_and_daily_resampling_does_not_fill_weekends(self):
        friday = datetime(2026, 10, 16, 20, tzinfo=timezone.utc)
        sunday = datetime(2026, 10, 18, 21, tzinfo=timezone.utc)
        rows = [
            candle(0, timeframe="HOUR", stamp=friday),
            candle(0, timeframe="HOUR", stamp=sunday),
        ]
        for timeframe in ("HOUR_4", "DAY"):
            result = resample(
                rows, timeframe, completed_before=sunday + timedelta(days=1)
            )
            self.assertEqual(len(result), 2)
            self.assertEqual(result[1].open_time, sunday)
            self.assertEqual(result[1].timestamp, floor_time(sunday, timeframe))


class AccountingTests(unittest.TestCase):
    def test_independent_long_and_short_bid_ask_examples(self):
        # BUY 100 EUR at 1.1002, sell at 1.1010 -> USD 0.08.
        ledger = Ledger(SimulationConfig(1000, 100))
        ledger.target("BUY", candle(0, bid="1.1000", ask="1.1002"))
        self.assertEqual(ledger.balance, 1000)
        self.assertEqual(ledger.unrealized("1.1000"), Decimal("-.02"))
        self.assertEqual(ledger.position.margin, Decimal("110.0200"))
        ledger.target(None, candle(1, bid="1.1010", ask="1.1012"))
        self.assertEqual(ledger.balance, Decimal("1000.0800"))
        self.assertEqual(ledger.trades[0].net_pnl, Decimal(".08"))
        # SELL at bid 1.1000, cover at ask 1.0992 -> USD 0.08.
        ledger = Ledger(SimulationConfig(1000, 100))
        ledger.target("SELL", candle(0, bid="1.1000", ask="1.1002"))
        ledger.target(None, candle(1, bid="1.0990", ask="1.0992"))
        self.assertEqual(ledger.balance, Decimal("1000.0800"))

    def test_spread_is_not_subtracted_twice(self):
        ledger = Ledger(SimulationConfig(1000, 100))
        quote = candle(0, bid="1.1000", ask="1.1002")
        ledger.target("BUY", quote)
        ledger.target(None, candle(1, bid="1.1000", ask="1.1002"))
        self.assertEqual(ledger.trades[0].gross_pnl, Decimal("-.02"))
        self.assertEqual(ledger.costs, 0)
        self.assertEqual(ledger.balance, Decimal("999.98"))

    def test_commission_slippage_and_financing_accounting(self):
        ledger = Ledger(
            SimulationConfig(
                1000,
                100,
                commission_per_order=dec(".01"),
                slippage_pips=1,
                financing_per_unit_day=dec(".001"),
            )
        )
        ledger.target("BUY", candle(0, bid="1.1", ask="1.1002"))
        self.assertEqual(ledger.balance, Decimal("999.99"))
        # One day: executed entry 1.1003, exit 1.1009, gross .06; costs .02 + .10 = .12.
        ledger.target(None, candle(1440, bid="1.1010", ask="1.1012"))
        self.assertEqual(ledger.trades[0].gross_pnl, Decimal(".06"))
        self.assertEqual(ledger.trades[0].costs, Decimal(".12"))
        self.assertEqual(ledger.balance, Decimal("999.94"))
        self.assertEqual(ledger.embedded_slippage, Decimal(".02"))

    def test_funding_is_collateral_not_an_expense_and_insufficient_funds_reject(self):
        ledger = Ledger(SimulationConfig(100, 100))
        ledger.target("BUY", candle(0, bid="1.1", ask="1.1002"))
        self.assertIsNone(ledger.position)
        self.assertEqual(ledger.balance, 100)
        self.assertEqual(len(ledger.rejections), 1)
        leveraged = Ledger(SimulationConfig(100, 100, leverage=10))
        leveraged.target("BUY", candle(0, bid="1.1", ask="1.1002"))
        self.assertEqual(leveraged.balance, 100)
        self.assertEqual(leveraged.position.margin, Decimal("11.002"))

    def test_repeated_targets_hold_and_reversal_uses_correct_exit_side(self):
        ledger = Ledger(SimulationConfig())
        ledger.target("BUY", candle(0, bid="1.1", ask="1.1002"))
        ledger.target("BUY", candle(1, bid="1.2", ask="1.2002"))
        self.assertEqual(ledger.position.entry_price, Decimal("1.1002"))
        ledger.target("SELL", candle(2, bid="1.3", ask="1.3002"))
        self.assertEqual(len(ledger.trades), 1)
        self.assertEqual(ledger.position.entry_price, Decimal("1.3"))

    def test_invalid_account_assumptions(self):
        for kwargs in (
            {"initial_balance": 0},
            {"size": -1},
            {"leverage": ".5"},
            {"commission_per_order": -1},
            {"size": "NaN"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                SimulationConfig(**kwargs)


class ExecutionAndHmaTests(unittest.TestCase):
    def test_archived_reference_price_fixture_reproduces_identical_results(self):
        # Existing public ECB reference rates exercise reproducibility, not an
        # assertion that reference observations are executable daily OHLC.
        observations = json.loads(
            (Path(__file__).parent / "fixtures/eurusd_ecb_daily.json").read_text()
        )["observations"]
        rows = tuple(
            candle(
                0,
                row["usd_per_eur"],
                timeframe="DAY",
                stamp=datetime.fromisoformat(row["date"]).replace(tzinfo=timezone.utc),
                source="ecb-reference-fixture",
            )
            for row in observations
        )
        results = [
            run(
                rows,
                HmaStrategy(),
                SimulationConfig(),
                start=rows[48].timestamp,
                end=rows[-1].end,
            )
            for _ in range(2)
        ]
        self.assertEqual(statistics(results[0]), statistics(results[1]))
        self.assertEqual(results[0].ledger.trades, results[1].ledger.trades)
        self.assertGreater(len(results[0].ledger.trades), 0)
        self.assertEqual(results[0].candles, len(rows) - 48)

    def test_next_open_execution_never_uses_decision_close(self):
        class Rule:
            name = "fixture"
            warmup = 1

            def target(self, history):
                return "BUY" if history[-1].prices.close < 2 else None

        rows = [candle(0, 1), candle(1, 2), candle(2, 3)]
        result = run(
            rows, Rule(), SimulationConfig(1000, 100), start=BASE, end=rows[-1].end
        )
        self.assertEqual(result.ledger.trades[0].entry_timestamp, rows[1].timestamp)
        self.assertEqual(result.ledger.trades[0].entry_price, 2)
        self.assertEqual(result.ledger.trades[0].exit_price, 3)
        self.assertEqual(result.ledger.balance, 1100)

    def test_warmup_primes_first_opportunity_without_trading_in_warmup(self):
        rows = [candle(i, dec("1.0") + dec(i) / 10000) for i in range(60)]
        strategy = HmaStrategy(4, "BUY")
        start = rows[20].timestamp
        result = run(rows, strategy, SimulationConfig(), start=start, end=rows[-1].end)
        self.assertTrue(result.ledger.position)
        self.assertEqual(result.ledger.position.entry_timestamp, start)
        self.assertEqual(result.candles, 40)
        self.assertTrue(all(x["timestamp"] >= start for x in result.equity_curve))

    def test_hma_matches_live_flat_and_both_direction_modes(self):
        flat = [1.1] * 48
        self.assertEqual(hma_signal(flat, 16), "SELL")
        api = Mock(getHistoricalPricesList=Mock(return_value=flat))
        self.assertEqual(calculate_hma_result(api, "EURUSD", "MINUTE_30", 16), "SELL")
        rows = [candle(i, "1.1") for i in range(48)]
        self.assertIsNone(HmaStrategy(16, "BUY").target(rows))
        self.assertEqual(HmaStrategy(16, "SELL").target(rows), "SELL")
        for side in ("BUY", "SELL"):
            strategy = HmaStrategy(4, side)
            rows = [candle(i, 1 + math.sin(i / 6) / 100) for i in range(80)]
            result = run(
                rows,
                strategy,
                SimulationConfig(),
                start=rows[20].timestamp,
                end=rows[-1].end,
            )
            self.assertTrue(all(t.direction == side for t in result.ledger.trades))

    def test_future_data_does_not_change_prior_signals_or_fills(self):
        rows = [candle(i, 1 + math.sin(i / 5) / 100) for i in range(100)]
        strategy = HmaStrategy(4, "BUY")
        full = run(
            rows,
            strategy,
            SimulationConfig(),
            start=rows[20].timestamp,
            end=rows[-1].end,
        )
        for end in range(30, 100, 7):
            prefix = run(
                rows[:end],
                strategy,
                SimulationConfig(),
                start=rows[20].timestamp,
                end=rows[end - 1].end,
            )
            self.assertEqual(
                prefix.decisions,
                [d for d in full.decisions if d["timestamp"] <= rows[end - 1].end],
            )
            self.assertEqual(
                prefix.ledger.trades,
                [t for t in full.ledger.trades if t.exit_timestamp < rows[end - 1].end],
            )

    def test_open_final_position_balance_equity_and_drawdown(self):
        class Rule:
            name = "fixture"
            warmup = 1

            def target(self, history):
                return "BUY"

        rows = [candle(0, 1), candle(1, 1), candle(2, "1.2"), candle(3, "1.1")]
        result = run(
            rows, Rule(), SimulationConfig(1000, 100), start=BASE, end=rows[-1].end
        )
        stats = statistics(result)
        self.assertEqual(stats["final_balance"], 1000)
        self.assertEqual(stats["final_equity"], 1010)
        self.assertEqual(stats["unrealized_pnl"], 10)
        self.assertEqual(stats["max_drawdown"], 10)
        self.assertEqual(stats["remaining_positions"], 1)
        self.assertEqual(stats["total_trades"], 0)
        self.assertIsNone(stats["win_rate_pct"])

    def test_financing_extends_to_end_without_manufacturing_quotes(self):
        class Rule:
            name = "fixture"
            warmup = 1

            def target(self, history):
                return "BUY"

        rows = [candle(0), candle(1)]
        end = BASE + timedelta(days=1)
        result = run(
            rows,
            Rule(),
            SimulationConfig(1000, 100, financing_per_unit_day=dec(".01")),
            start=BASE,
            end=end,
        )
        self.assertEqual(result.equity_curve[-1]["timestamp"], end)
        self.assertEqual(result.equity_curve[-1]["quote_timestamp"], rows[-1].end)
        self.assertEqual(result.ledger.position.entry_timestamp, rows[1].timestamp)

    def test_statistics_from_independent_cash_examples(self):
        class Rule:
            name = "fixture"
            warmup = 1

            def target(self, history):
                return "BUY" if len(history) else None

        result = run([], Rule(), SimulationConfig(), start=BASE, end=BASE)
        from trading_bot.backtesting.engine import Trade

        result.ledger.trades = [
            Trade(BASE, BASE, "BUY", dec(100), dec(1), dec(1), p, dec(0), p, dec(1000))
            for p in (dec(2), dec(-1), dec(3))
        ]
        stats = statistics(result)
        self.assertEqual(
            (stats["total_trades"], stats["winning_trades"], stats["losing_trades"]),
            (3, 2, 1),
        )
        self.assertEqual(stats["profit_factor"], 5)
        self.assertAlmostEqual(float(stats["win_rate_pct"]), 200 / 3)


def delta_payload():
    return {
        "timestamp": int(BASE.timestamp() * 1000),
        "multiplier": 0.00001,
        "shift": 60000,
        "open": 1.1000000000000001,
        "high": 1.10002,
        "low": 1.1,
        "close": 1.10001,
        "times": [0, 1],
        "opens": [0, 1],
        "highs": [0, 1],
        "lows": [0, 1],
        "closes": [0, 1],
        "volumes": [3, 4],
    }


class ProviderContractTests(unittest.TestCase):
    def test_dukascopy_delta_schema_including_float_base_rounding(self):
        rows = decode(delta_payload(), "MINUTE")
        self.assertEqual(
            rows[0], (BASE, OHLC("1.10000", "1.10002", "1.10000", "1.10001"))
        )
        self.assertEqual(
            rows[1],
            (
                BASE + timedelta(minutes=1),
                OHLC("1.10001", "1.10003", "1.10001", "1.10002"),
            ),
        )
        broken = delta_payload()
        broken["opens"] = [0]
        with self.assertRaises(DataError):
            decode(broken, "MINUTE")
        broken = delta_payload()
        broken["times"] = [0, -1]
        with self.assertRaises(DataError):
            decode(broken, "MINUTE")

    def test_dukascopy_http_errors_are_fatal_and_not_empty_or_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            for status in (202, 403, 429, 500):
                session = Mock()
                session.get.return_value = Mock(status_code=status)
                source = DukascopyProvider(
                    Cache(root), session=session, now=BASE + timedelta(days=2)
                )
                with (
                    self.subTest(status=status),
                    self.assertRaisesRegex(DataError, f"HTTP {status}"),
                ):
                    source.fetch("EURUSD", "MINUTE", BASE, BASE + timedelta(days=1))
                session.get.assert_called_once()

    def test_dukascopy_uses_candles_with_real_bid_ask_and_cache(self):
        payload = delta_payload()
        ask = delta_payload()
        for key in ("open", "high", "low", "close"):
            ask[key] += 0.0002
        session = Mock()
        session.get.side_effect = [
            Mock(status_code=200, json=Mock(return_value=payload)),
            Mock(status_code=200, json=Mock(return_value=ask)),
        ]
        with (
            tempfile.TemporaryDirectory() as root,
            patch("trading_bot.backtesting.providers.dukascopy.time.sleep"),
        ):
            source = DukascopyProvider(
                Cache(root), session=session, now=BASE + timedelta(days=2)
            )
            result = source.fetch("EURUSD", "MINUTE", BASE, BASE + timedelta(minutes=2))
            self.assertEqual(len(result.candles), 2)
            self.assertEqual(
                result.candles[0].ask.open - result.candles[0].bid.open,
                Decimal(".0002"),
            )
            self.assertEqual(result.candles[0].prices, result.candles[0].ask)
            again = DukascopyProvider(
                Cache(root), session=session, now=BASE + timedelta(days=2)
            ).fetch("EURUSD", "MINUTE", BASE, BASE + timedelta(minutes=2))
            self.assertEqual(again.candles, result.candles)
            self.assertEqual(session.get.call_count, 2)
            for call in session.get.call_args_list:
                self.assertIn("/candles/minute/EUR-USD/", call.args[0])
                self.assertNotIn("ticks", call.args[0])
                self.assertIs(call.kwargs["allow_redirects"], False)

    def test_dukascopy_missing_ask_quotes_are_rejected(self):
        session = Mock()
        ask = delta_payload()
        for key in ("times", "opens", "highs", "lows", "closes", "volumes"):
            ask[key] = ask[key][:1]
        session.get.side_effect = [
            Mock(status_code=200, json=Mock(return_value=delta_payload())),
            Mock(status_code=200, json=Mock(return_value=ask)),
        ]
        with (
            tempfile.TemporaryDirectory() as root,
            patch("trading_bot.backtesting.providers.dukascopy.time.sleep"),
            self.assertRaisesRegex(DataError, "timestamp sets"),
        ):
            DukascopyProvider(
                Cache(root), session=session, now=BASE + timedelta(days=2)
            ).fetch("EURUSD", "MINUTE", BASE, BASE + timedelta(minutes=2))

    def test_yahoo_chunking_timezone_and_no_spread(self):
        import pandas as pd

        frames = []
        start = BASE
        stop = start + timedelta(days=31)
        for stamp in (start, start + timedelta(days=30)):
            frames.append(
                pd.DataFrame(
                    {"Open": [1.1], "High": [1.2], "Low": [1.0], "Close": [1.15]},
                    index=pd.DatetimeIndex([stamp]),
                )
            )
        ticker = Mock()
        ticker.history.side_effect = frames
        with tempfile.TemporaryDirectory() as root:
            result = YahooProvider(
                Cache(root), now=stop, ticker_factory=Mock(return_value=ticker)
            ).fetch("EURUSD", "MINUTE_5", start, stop)
        self.assertEqual(ticker.history.call_count, 2)
        self.assertEqual(len(result.candles), 2)
        self.assertIsNone(result.candles[0].bid)
        self.assertIn("unavailable", result.metadata["quote_model"])
        for call in ticker.history.call_args_list:
            self.assertFalse(call.kwargs["auto_adjust"])
            self.assertFalse(call.kwargs["repair"])
            self.assertEqual(call.kwargs["interval"], "5m")

    def test_yahoo_retention_applies_to_entire_warmup_range(self):
        ticker = Mock()
        with tempfile.TemporaryDirectory() as root:
            provider = YahooProvider(
                Cache(root), now=BASE + timedelta(days=61), ticker_factory=ticker
            )
            with self.assertRaisesRegex(DataError, "including warm-up"):
                provider.fetch("EURUSD", "MINUTE_5", BASE, BASE + timedelta(days=30))
            ticker.assert_not_called()

    def test_yahoo_empty_or_invalid_chunks_fail(self):
        import pandas as pd

        ticker = Mock()
        ticker.history.return_value = pd.DataFrame()
        with tempfile.TemporaryDirectory() as root, self.assertRaises(DataError):
            YahooProvider(
                Cache(root),
                now=BASE + timedelta(days=1),
                ticker_factory=Mock(return_value=ticker),
            ).fetch("EURUSD", "HOUR", BASE, BASE + timedelta(days=1))
        ticker.history.return_value = pd.DataFrame(
            {"Close": [1.1]}, index=pd.DatetimeIndex([BASE])
        )
        with (
            tempfile.TemporaryDirectory() as root,
            self.assertRaisesRegex(DataError, "schema"),
        ):
            YahooProvider(
                Cache(root),
                now=BASE + timedelta(days=1),
                ticker_factory=Mock(return_value=ticker),
            ).fetch("EURUSD", "HOUR", BASE, BASE + timedelta(days=1))
        ticker.history.side_effect = RuntimeError("network failure")
        with (
            tempfile.TemporaryDirectory() as root,
            self.assertRaisesRegex(DataError, "RuntimeError"),
        ):
            YahooProvider(
                Cache(root),
                now=BASE + timedelta(days=1),
                ticker_factory=Mock(return_value=ticker),
            ).fetch("EURUSD", "HOUR", BASE, BASE + timedelta(days=1))

    def test_data_service_validates_warmup_and_cache_without_hiding_gaps(self):
        start = BASE + timedelta(days=1)
        end = start + timedelta(minutes=20)
        rows = tuple(candle(i, 1 + i / 100000) for i in range(1420, 1460) if i != 1455)
        fake = Mock()
        fake.fetch.return_value = Dataset(
            rows,
            BASE,
            end,
            "dukascopy",
            "EURUSD",
            "MINUTE",
            {"source_resolution": "MINUTE"},
        )
        with (
            tempfile.TemporaryDirectory() as root,
            patch("trading_bot.backtesting.data.provider", return_value=fake),
        ):
            result = load_history(
                "dukascopy",
                "EURUSD",
                "MINUTE",
                start,
                end,
                12,
                Cache(root),
                allow_gaps=True,
                now=end,
            )
            again = load_history(
                "dukascopy",
                "EURUSD",
                "MINUTE",
                start,
                end,
                12,
                Cache(root),
                allow_gaps=True,
                now=end,
            )
            self.assertEqual(result.candles, again.candles)
            fake.fetch.assert_called_once()
            self.assertTrue(again.metadata["cache_hit"])
            self.assertEqual(again.metadata["warmup_candles"], 12)
            self.assertEqual(again.metadata["downloaded_warmup_candles"], 20)
            with self.assertRaises(DataError):
                load_history(
                    "dukascopy",
                    "EURUSD",
                    "MINUTE",
                    start,
                    end,
                    12,
                    Cache(root),
                    now=end,
                )
        fake.fetch.return_value = Dataset(
            rows[-20:], start, end, "dukascopy", "EURUSD", "MINUTE", {}
        )
        with (
            tempfile.TemporaryDirectory() as root,
            patch("trading_bot.backtesting.data.provider", return_value=fake),
            self.assertRaisesRegex(DataError, "warm-up"),
        ):
            load_history(
                "dukascopy",
                "EURUSD",
                "MINUTE",
                start,
                end,
                12,
                Cache(root),
                allow_gaps=True,
                now=end,
            )

    def test_source_gaps_are_checked_only_in_used_history_even_inside_full_buckets(
        self,
    ):
        start = BASE + timedelta(hours=1)
        end = start + timedelta(minutes=5)
        rows = tuple(candle(i, source="yfinance") for i in range(55, 65))
        source = Mock()
        metadata = {
            "source_resolution": "MINUTE",
            "missing_source_active_intervals": 1,
            "missing_source_active_timestamps": [BASE.isoformat()],
        }
        source.fetch.return_value = Dataset(
            rows, BASE, end, "yfinance", "EURUSD", "MINUTE", metadata
        )
        with (
            tempfile.TemporaryDirectory() as root,
            patch("trading_bot.backtesting.data.provider", return_value=source),
        ):
            result = load_history(
                "yfinance", "EURUSD", "MINUTE", start, end, 3, Cache(root), now=end
            )
            self.assertEqual(result.metadata["missing_source_active_intervals"], 0)
        # Source data can be incomplete even if a target bucket still exists.
        metadata["missing_source_active_timestamps"] = [start.isoformat()]
        with (
            tempfile.TemporaryDirectory() as root,
            patch("trading_bot.backtesting.data.provider", return_value=source),
            self.assertRaisesRegex(DataError, "source candles"),
        ):
            load_history(
                "yfinance", "EURUSD", "MINUTE", start, end, 3, Cache(root), now=end
            )


class CliAndIsolationTests(unittest.TestCase):
    def test_requested_cli_defaults_and_overrides_never_touch_broker(self):
        cases = [
            [],
            ["--time-frame", "MINUTE_5"],
            ["--data-source", "yfinance"],
            ["--initial-balance", "5000", "--size", "200"],
            ["--time_frame", "HOUR_4"],
        ]
        for options in cases:
            with self.subTest(options=options), ExitStack() as stack:
                stack.enter_context(
                    patch("sys.argv", ["hma.py", "--backtest", *options])
                )
                authentication = stack.enter_context(
                    patch.object(
                        hma,
                        "create_trading_api",
                        side_effect=AssertionError("Broker authentication forbidden"),
                    )
                )
                stack.enter_context(
                    patch(
                        "trading_bot.config.trading_config.load_credentials",
                        side_effect=AssertionError("Credentials forbidden"),
                    )
                )
                stack.enter_context(
                    patch(
                        "trading_bot.risk.live_execution.LiveExecution.arm",
                        side_effect=AssertionError("Gate forbidden"),
                    )
                )

                def capture(args, parser):
                    self.assertTrue(args.backtest)
                    self.assertEqual(args.symbol, "EURUSD")
                    self.assertEqual(args.lookback_days, 30)
                    self.assertEqual(
                        args.data_source,
                        "yfinance" if "yfinance" in options else "dukascopy",
                    )
                    self.assertEqual(
                        args.initial_balance, 5000 if "5000" in options else 1000
                    )
                    self.assertEqual(args.size, 200 if "200" in options else 100)
                    return 0

                dispatch = stack.enter_context(
                    patch("trading_bot.backtesting.cli.execute", side_effect=capture)
                )
                hma.main()
                dispatch.assert_called_once()
                authentication.assert_not_called()

    def test_conflicting_cli_options_rejected_before_providers_or_broker(self):
        for options in (
            ["--backtest", "--REAL"],
            ["--backtest", "--account", "x"],
            ["--backtest", "--read-only"],
            ["--backtest", "--max-margin", "5"],
            ["--backtest", "--cycles", "2"],
            ["--data-source", "yfinance"],
        ):
            with (
                self.subTest(options=options),
                patch("sys.argv", ["hma.py", *options]),
                patch.object(hma, "create_trading_api") as auth,
                patch("trading_bot.backtesting.cli.load_history") as fetch,
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                hma.main()
            auth.assert_not_called()
            fetch.assert_not_called()

    def test_full_cli_uses_real_engine_and_saves_reproducible_artifacts(self):
        from trading_bot.backtesting.data import warmup_start

        start = BASE + timedelta(days=5)
        end = start + timedelta(days=1)
        fetch_start = warmup_start(start, "MINUTE_30", 48)
        rows = tuple(
            candle(
                i,
                1 + math.sin(i / 5) / 100,
                timeframe="MINUTE_30",
                stamp=fetch_start + i * timedelta(minutes=30),
            )
            for i in range(int((end - fetch_start).total_seconds() / 1800))
        )
        source = Mock()
        source.fetch.return_value = Dataset(
            rows,
            fetch_start,
            end,
            "dukascopy",
            "EURUSD",
            "MINUTE_30",
            {"source_resolution": "MINUTE_30"},
        )
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            stack.enter_context(
                patch(
                    "sys.argv",
                    [
                        "hma.py",
                        "--backtest",
                        "--start",
                        start.isoformat(),
                        "--end",
                        end.isoformat(),
                        "--cache-dir",
                        root + "/cache",
                        "--output-dir",
                        root + "/results",
                    ],
                )
            )
            stack.enter_context(
                patch("trading_bot.backtesting.data.provider", return_value=source)
            )
            stack.enter_context(
                patch.object(
                    hma,
                    "create_trading_api",
                    side_effect=AssertionError("Broker forbidden"),
                )
            )
            stack.enter_context(
                patch(
                    "trading_bot.config.trading_config.load_credentials",
                    side_effect=AssertionError("Credentials forbidden"),
                )
            )
            stack.enter_context(redirect_stdout(io.StringIO()))
            hma.main()
            files = list((Path(root) / "results").glob("*/report.json"))
            self.assertEqual(len(files), 1)
            report = json.loads(files[0].read_text())
            self.assertEqual(report["candles"], 48)
            self.assertEqual(report["statistics"]["initial_balance"], "1000")
            self.assertIn("commission_per_order", report["unmodeled_costs"])
            self.assertIsNone(report["total_cost_estimate"])
            self.assertTrue((files[0].parent / "trades.csv").exists())
            self.assertTrue((files[0].parent / "equity.html").exists())
            first = report["statistics"]
            hma.main()
            reports = [
                json.loads(p.read_text())
                for p in (Path(root) / "results").glob("*/report.json")
            ]
            self.assertTrue(all(r["statistics"] == first for r in reports))
            source.fetch.assert_called_once()
            # A fresh interpreter catches accidental import-time credential reads,
            # broker construction, and alternate HTTP stacks missed by mocks.
            code = """
import sys
from pathlib import Path
def guard(event, args):
    if event.startswith("socket.") and event != "socket.__new__":
        raise AssertionError("Network forbidden for cached simulation")
    if event == "import" and args[0].startswith(("trading_bot.api", "trading_bot.risk")):
        raise AssertionError("Broker modules forbidden")
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(args[0].decode() if isinstance(args[0], bytes) else args[0])
        if path.name.startswith(".env") or any(p in (".demo-integration", ".live-integration") for p in path.parts):
            raise AssertionError("Credentials/private state forbidden")
sys.addaudithook(guard)
import runpy
runpy.run_module("trading_bot.strategies.hma", run_name="__main__")
assert not any(n.startswith(("trading_bot.api", "trading_bot.risk")) for n in sys.modules)
"""
            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    code,
                    "--backtest",
                    "--start",
                    start.isoformat(),
                    "--end",
                    end.isoformat(),
                    "--cache-dir",
                    root + "/cache",
                    "--output-dir",
                    root + "/isolated-results",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertIn("BACKTEST REPORT", child.stdout)

    def test_invalid_parameters_fail_before_historical_download(self):
        for options in (
            ["--hma_period", "0"],
            ["--lookback-days", "0"],
            ["--initial-balance", "0"],
            ["--leverage", ".5"],
            ["--commission-per-order", "-1"],
            ["--direction", "BAD"],
        ):
            with (
                self.subTest(options=options),
                patch("sys.argv", ["hma.py", "--backtest", *options]),
                patch("trading_bot.backtesting.cli.load_history") as fetch,
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                hma.main()
            fetch.assert_not_called()

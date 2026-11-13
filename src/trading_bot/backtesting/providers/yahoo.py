"""yfinance historical OHLC; no invented executable bid/ask or historical spread."""

from datetime import datetime, timedelta, timezone
import math
from ..calendar import audit_coverage
from ..models import Candle, Dataset, DataError, OHLC, utc
from ..timeframes import duration, normalize, resample


class YahooProvider:
    def __init__(self, cache, *, now=None, ticker_factory=None):
        self.cache = cache
        self.now = utc(now or datetime.now(timezone.utc))
        self.ticker_factory = ticker_factory

    def fetch(self, symbol, timeframe, start, end):
        if symbol != "EURUSD":
            raise DataError("Yahoo contract mapping supports EURUSD (EURUSD=X) only")
        start, end = utc(start), utc(end)
        # Request 15m for 30m and perform our own UTC aggregation instead of yfinance's
        # exchange-local 30m workaround. H4/day aggregate unadjusted hourly data where applicable.
        interval, resolution, retention, chunk = {
            "MINUTE": ("1m", "MINUTE", 30, 7),
            "MINUTE_5": ("5m", "MINUTE_5", 60, 30),
            "MINUTE_15": ("15m", "MINUTE_15", 60, 30),
            "MINUTE_30": ("15m", "MINUTE_15", 60, 30),
            "HOUR": ("1h", "HOUR", 730, 60),
            "HOUR_4": ("1h", "HOUR", 730, 60),
            "DAY": ("1h", "HOUR", 730, 60),
        }[timeframe]
        if retention is not None and start < self.now - timedelta(days=retention):
            raise DataError(
                f"Yahoo {interval} history is retained for about {retention} days, including warm-up; shorten lookback or select Dukascopy"
            )
        if self.ticker_factory is None:
            import yfinance as yf

            yf.set_tz_cache_location(str(self.cache.root / "yfinance-metadata"))
            ticker = yf.Ticker("EURUSD=X")
        else:
            ticker = self.ticker_factory("EURUSD=X")
        result = []
        cursor = start
        chunks = 0
        empty_rows = 0
        while cursor < end:
            stop = min(end, cursor + timedelta(days=chunk))
            try:
                frame = ticker.history(
                    start=cursor,
                    end=stop,
                    interval=interval,
                    auto_adjust=False,
                    actions=False,
                    repair=False,
                    keepna=True,
                    timeout=30,
                    raise_errors=True,
                )
            except Exception as exc:
                raise DataError(
                    f"yfinance historical download failed ({type(exc).__name__}); no other provider was tried"
                ) from None
            if frame.empty:
                raise DataError(
                    "Yahoo returned an empty historical chunk; unavailable period or interval"
                )
            if any(
                name not in frame.columns for name in ("Open", "High", "Low", "Close")
            ):
                raise DataError("Yahoo returned an incomplete OHLC schema")
            if getattr(frame.index, "tz", None) is None:
                raise DataError(
                    "Yahoo returned naive timestamps; exchange timezone unknown"
                )
            frame = frame.tz_convert("UTC")
            for index, row in frame.iterrows():
                prices = [row[k] for k in ("Open", "High", "Low", "Close")]
                if all(math.isnan(float(p)) for p in prices):
                    empty_rows += 1
                    continue
                stamp = index.to_pydatetime()
                if cursor <= stamp and stamp + duration(resolution) <= stop:
                    result.append(
                        Candle(
                            stamp,
                            OHLC(row["Open"], row["High"], row["Low"], row["Close"]),
                            symbol,
                            resolution,
                            "yfinance",
                        )
                    )
            cursor = stop
            chunks += 1
        source_rows = normalize(result, completed_before=end)
        source_audit = audit_coverage(
            source_rows, start, end, "yfinance", resolution, allow_gaps=True
        )
        candles = resample(source_rows, timeframe, completed_before=end)
        return Dataset(
            candles,
            start,
            end,
            "yfinance",
            symbol,
            timeframe,
            {
                "quote_model": "indicative OHLC only; executable bid/ask and spread unavailable",
                "complete_provider_buckets": False,
                "source_resolution": resolution,
                "chunks": chunks,
                "yahoo_symbol": "EURUSD=X",
                "provider_interval": interval,
                "empty_quote_rows_removed": empty_rows,
                "missing_source_active_intervals": source_audit["missing_intervals"],
                "missing_source_active_timestamps": source_audit["missing_timestamps"],
            },
        )

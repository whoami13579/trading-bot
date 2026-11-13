"""Dukascopy delta-encoded candle API, following maintained dukascopy-node's schema.

https://github.com/Leo4815162342/dukascopy-node/tree/master/src/data-normaliser
No ticks, synthetic flat candles, provider fallback or Capital.com connectivity.
"""

from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP
import time
import requests
from ..models import Candle, Dataset, DataError, OHLC, dec, utc
from ..timeframes import duration, normalize, resample

BASE = "https://jetta.dukascopy.com/v1/candles"
HEADERS = {
    "Referer": "https://widgets.dukascopy.com/en/historical-data-export",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
}


def decode(payload, timeframe):
    """Decode integer deltas; unlike upstream normalise(), do not manufacture gap fills."""
    try:
        times = payload["times"]
        if not isinstance(times, list):
            raise ValueError()
        columns = [payload[k] for k in ("opens", "highs", "lows", "closes", "volumes")]
        if any(not isinstance(c, list) or len(c) != len(times) for c in columns):
            raise ValueError()
        multiplier = dec(payload["multiplier"])
        stamp = dec(payload["timestamp"])
        shift = dec(payload["shift"])
        if multiplier <= 0 or shift != duration(timeframe).total_seconds() * 1000:
            raise ValueError()
        if not times:
            return []
        units = [
            (dec(payload[k]) / multiplier).to_integral_value(rounding=ROUND_HALF_UP)
            for k in ("open", "high", "low", "close")
        ]
        rows = []
        for i, delta in enumerate(times):
            if type(delta) is not int or delta < 0 or (i > 0 and delta == 0):
                raise ValueError()
            stamp += delta * shift
            for j in range(4):
                change = dec(columns[j][i])
                if change != change.to_integral_value():
                    raise ValueError()
                units[j] += change
            o, h, l, c = [u * multiplier for u in units]
            if dec(columns[4][i]) < 0:
                raise ValueError()
            rows.append(
                (
                    datetime.fromtimestamp(float(stamp / 1000), timezone.utc),
                    OHLC(o, h, l, c),
                )
            )
        return rows
    except (KeyError, TypeError, ValueError, OverflowError):
        raise DataError("Invalid Dukascopy candle schema or delta encoding") from None


class DukascopyProvider:
    def __init__(self, cache, *, session=None, now=None):
        self.cache = cache
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.now = utc(now or datetime.now(timezone.utc))
        self.requests = 0

    def _bucket(self, side, source, stamp, active):
        base = f"{BASE}/{source}/EUR-USD/{side}"
        if active:
            url = base + f"?from={int(stamp.timestamp() * 1000)}"
        elif source == "minute":
            url = base + f"/{stamp.year}/{stamp.month}/{stamp.day}"
        else:
            url = base + f"/{stamp.year}/{stamp.month}"
        # Cache immutable completed buckets; active buckets are fetched fresh unless
        # the whole requested normalized snapshot is already cached by the data service.
        value = None if active else self.cache.get(url)
        if value is None:
            if self.requests:
                time.sleep(0.15)
            try:
                response = self.session.get(
                    url, headers=HEADERS, timeout=(5, 30), allow_redirects=False
                )
            except requests.RequestException:
                raise DataError(
                    "Dukascopy network failure; no other provider was tried"
                ) from None
            self.requests += 1
            if response.status_code != 200:
                raise DataError(
                    f"Dukascopy HTTP {response.status_code}: historical export unavailable/rate-limited or access challenged; no fallback or empty-data success"
                )
            try:
                value = response.json()
            except ValueError:
                raise DataError("Dukascopy returned non-JSON historical data") from None
            decode(value, "MINUTE" if source == "minute" else "HOUR")
            if not active:
                self.cache.put(url, value)
        return value

    def fetch(self, symbol, timeframe, start, end):
        if symbol != "EURUSD":
            raise DataError("Dukascopy unit/price mapping is verified only for EURUSD")
        start, end = utc(start), utc(end)
        source = "minute" if duration(timeframe) < timedelta(hours=1) else "hour"
        resolution = "MINUTE" if source == "minute" else "HOUR"
        cursor = start.replace(hour=0, minute=0, second=0, microsecond=0)
        if source == "hour":
            cursor = cursor.replace(day=1)
        all_rows = []
        no_quote_ranges = []
        buckets = 0
        while cursor < end:
            next_bucket = (
                cursor + timedelta(days=1)
                if source == "minute"
                else (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
            )
            if source == "minute" and cursor.weekday() == 5:
                cursor = next_bucket
                continue
            active = cursor <= self.now < next_bucket
            sides = [
                {
                    t: p
                    for t, p in decode(
                        self._bucket(side, source, cursor, active), resolution
                    )
                    if start <= t and t + duration(resolution) <= end
                }
                for side in ("BID", "ASK")
            ]
            if set(sides[0]) != set(sides[1]):
                raise DataError(
                    "Dukascopy bid/ask timestamp sets differ; incomplete quote pairs"
                )
            if not sides[0] and source == "minute" and cursor.weekday() < 5:
                raise DataError("Dukascopy returned an empty active historical bucket")
            stamps = sorted(sides[0])
            source_step = duration(resolution)
            for previous, following in zip(stamps, stamps[1:]):
                if following > previous + source_step:
                    no_quote_ranges.append(
                        ((previous + source_step).isoformat(), following.isoformat())
                    )
            for stamp, bid in sides[0].items():
                if start <= stamp and stamp + duration(resolution) <= end:
                    ask = sides[1][stamp]
                    all_rows.append(
                        Candle(stamp, ask, symbol, resolution, "dukascopy", bid, ask)
                    )
            buckets += 1
            cursor = next_bucket
        candles = resample(
            normalize(all_rows, completed_before=end), timeframe, completed_before=end
        )
        return Dataset(
            candles,
            start,
            end,
            "dukascopy",
            symbol,
            timeframe,
            {
                "quote_model": "historical bid/ask candles; signal uses ask close",
                "complete_provider_buckets": True,
                "buckets": buckets,
                "source_resolution": resolution,
                "provider_interface": BASE,
                "download_requests": self.requests,
                "sparse_records": "explicit delta gaps represent no supplied quote, never filled",
                "no_quote_ranges": no_quote_ranges,
            },
        )

"""Deterministic, exact-Decimal synthetic candles shared by regression tests.

The series is generated from integer arithmetic only (no floats, no randomness
from the platform), so pinned results in fixtures/backtest_regression.json stay
valid on every machine. It contains a scheduled weekend closure, an unexpected
active-session gap and varying bid/ask spreads.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json

from trading_bot.backtesting.models import Candle, OHLC, json_value
from trading_bot.backtesting.calendar import expected_open

ORIGIN = datetime(2026, 9, 1, tzinfo=timezone.utc)  # Tuesday
PIP = Decimal("0.00001")  # Price units are tenths of a pip.


def _walk(count, seed=20261117):
    state = seed
    level = 110000  # 1.10000
    for _ in range(count):
        state = (state * 1103515245 + 12345) % 2**31
        level += (state >> 8) % 61 - 30 + (8 if (state >> 4) % 7 == 0 else 0)
        yield level, state


def hourly_candles(hours=24 * 30, *, missing=(datetime(2026, 9, 16, 13, tzinfo=timezone.utc),)):
    """Hourly EURUSD bid/ask candles inside the modeled FX week only."""
    rows = []
    previous = None
    for i, (level, state) in enumerate(_walk(hours)):
        stamp = ORIGIN + timedelta(hours=i)
        if not expected_open(stamp, "dukascopy") or stamp in missing:
            continue
        spread = 6 + state % 9  # 0.6 to 1.4 pips
        open_ = previous if previous is not None else level
        high = max(open_, level) + state % 23
        low = min(open_, level) - (state >> 3) % 19
        mid = [Decimal(v) * PIP for v in (open_, high, low, level)]
        half = Decimal(spread) * PIP / 2
        bid = OHLC(*(m - half for m in mid))
        ask = OHLC(*(m + half for m in mid))
        rows.append(Candle(stamp, ask, "EURUSD", "HOUR", "dukascopy", bid, ask))
        previous = level
    return tuple(rows)


def fingerprint(candles):
    encoded = json.dumps([c.as_dict() for c in candles], default=json_value, sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def canonical(value):
    """Exact Decimal/timestamp strings, the same encoding as saved reports."""
    return json.loads(json.dumps(value, default=json_value, sort_keys=True))

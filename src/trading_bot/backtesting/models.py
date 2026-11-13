"""UTC candle and reproducible historical dataset contracts."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


class DataError(ValueError):
    """Provider unavailable, malformed, insufficient or incomplete history."""


def dec(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("A finite numeric value is required") from None
    if not result.is_finite():
        raise ValueError("A finite numeric value is required")
    return result


def utc(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise DataError("Timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class OHLC:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self):
        for name in ("open", "high", "low", "close"):
            object.__setattr__(self, name, dec(getattr(self, name)))
        if (
            min(self.open, self.high, self.low, self.close) <= 0
            or self.low > min(self.open, self.close)
            or self.high < max(self.open, self.close)
            or self.low > self.high
        ):
            raise DataError("Invalid positive OHLC prices")


@dataclass(frozen=True)
class Candle:
    timestamp: datetime  # UTC start of the interval, not its closing time.
    prices: (
        OHLC  # Ask for Dukascopy (matches the live HMA input); indicative for Yahoo.
    )
    symbol: str
    timeframe: str
    source: str
    bid: OHLC | None = None
    ask: OHLC | None = None
    open_time: datetime | None = (
        None  # First actual observation, including sparse buckets.
    )
    observations: int = 1

    def __post_init__(self):
        from .timeframes import duration, floor_time

        object.__setattr__(self, "timestamp", utc(self.timestamp))
        object.__setattr__(self, "open_time", utc(self.open_time or self.timestamp))
        if floor_time(self.timestamp, self.timeframe) != self.timestamp:
            raise DataError("Candle is not aligned to its UTC timeframe")
        if (
            not self.timestamp
            <= self.open_time
            < self.timestamp + duration(self.timeframe)
        ):
            raise DataError("Candle first observation is outside its interval")
        if (self.bid is None) != (self.ask is None):
            raise DataError("Bid and ask must both be present or both absent")
        if self.bid and any(
            getattr(self.bid, k) > getattr(self.ask, k)
            for k in ("open", "high", "low", "close")
        ):
            raise DataError("Crossed bid/ask candles")
        if self.observations <= 0:
            raise DataError("Candles need actual observations")

    @property
    def end(self):
        from .timeframes import duration

        return self.timestamp + duration(self.timeframe)

    def quote(self, side: str, field: str) -> Decimal:
        series = self.ask if side == "ask" else self.bid
        return getattr(series or self.prices, field)

    def as_dict(self):
        from dataclasses import asdict

        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        value = dict(value)
        for key in ("timestamp", "open_time"):
            if value.get(key):
                value[key] = datetime.fromisoformat(value[key])
        for key in ("prices", "bid", "ask"):
            if value.get(key) is not None:
                value[key] = OHLC(**value[key])
        return cls(**value)


@dataclass(frozen=True)
class Dataset:
    candles: tuple[Candle, ...]
    requested_start: datetime
    requested_end: datetime
    source: str
    symbol: str
    timeframe: str
    metadata: dict[str, Any] = field(default_factory=dict)


def json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError("Unsupported report value")

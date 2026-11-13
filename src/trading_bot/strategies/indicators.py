from __future__ import annotations
import math
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from trading_bot.api.client import TradingApi
from typing import Any, List

def _validate_period(period: int, minimum: int = 1) -> None:
    if not isinstance(period, int) or isinstance(period, bool) or period < minimum:
        raise ValueError(f"Period must be an integer of at least {minimum}")

def calculate_wma(prices: list[float], period: int) -> float:
    """Calculates the Weighted Moving Average (WMA)."""
    _validate_period(period)
    if len(prices) < period:
        return 0.0

    subset = prices[-period:]
    weight_sum = sum(range(1, period + 1))
    weighted_val = sum(p * (i + 1) for i, p in enumerate(subset))
    return weighted_val / weight_sum

def calculate_hma(prices: list[float], period: int) -> list[float]:
    """
    Calculates the Hull Moving Average sequence.
    Formula: WMA(2*WMA(n/2) - WMA(n), sqrt(n))
    """
    _validate_period(period, minimum=2)
    half_period = period // 2
    sqrt_period = int(math.sqrt(period))

    # We need a series of 'raw' HMA values to calculate the final WMA smoothing
    raw_hma: list[float] = []

    # Generate the raw HMA series
    # We need enough history to calculate the WMAs
    for i in range(len(prices)):
        if i < period:
            continue

        current_slice = prices[: i + 1]
        wma_half = calculate_wma(current_slice, half_period)
        wma_full = calculate_wma(current_slice, period)

        raw_val = (2 * wma_half) - wma_full
        raw_hma.append(raw_val)

    # Final smoothing: WMA of the raw_hma series over sqrt(period)
    final_hma: list[float] = []
    for j in range(len(raw_hma)):
        if j < sqrt_period:
            continue
        final_hma.append(calculate_wma(raw_hma[: j + 1], sqrt_period))

    return final_hma


def calculate_sma(prices, period)->float:
    """Calculates Simple Moving Average for a specific window."""
    _validate_period(period)
    if len(prices) < period:
        raise ValueError("Not enough history for SMA")
    return sum(prices[-period:]) / period


def calculate_ema(prices, span, smoothing=2)->list[float]:
    _validate_period(span)
    if len(prices) < span:
        raise ValueError("Not enough history for EMA")

    # Calculate the multiplier
    multiplier = smoothing / (1 + span)

    # Start with the SMA for the first EMA value
    ema = sum(prices[:span]) / span
    ema_values: list = [None] * (span - 1) + [ema]

    # Calculate EMA for the remaining prices
    for price in prices[span:]:
        ema = (price - ema) * multiplier + ema
        ema_values.append(ema)

    return ema_values

def calculate_multiple(tradingApi: TradingApi, symbol: str, time_frame: str)->str:
    buy_or_sell = 0
    prices = tradingApi.getHistoricalPricesList(symbol, time_frame, 210)
    periods = [10, 20, 30, 50, 100, 200]
    if len(prices) < max(periods):
        raise ValueError("Not enough history for the multiple-indicator signal")

    for period in periods:
        sma_value = calculate_sma(prices, period)
        if prices[-1] < sma_value:
            buy_or_sell -= 1
        else:
            buy_or_sell += 1

    for period in periods:
        ema_values = calculate_ema(prices, period)
        if prices[-1] < ema_values[-1]:
            buy_or_sell -= 1
        else:
            buy_or_sell += 1

    hma_period = 9 * 3
    hma_values = calculate_hma(prices, hma_period)
    if len(hma_values) < 2:
        raise ValueError("Not enough history for an HMA slope signal")
    if hma_values[-2] < hma_values[-1]:
        buy_or_sell += 1
    else:
        buy_or_sell -= 1

    if 1 < buy_or_sell:
        return "BUY"
    elif buy_or_sell < -1:
        return "SELL"
    else:
        return "Neutral"

def hma_signal(prices: list[float], hma_period: int) -> str:
    """The live HMA slope rule; a flat slope is SELL, preserving existing behavior."""
    hma_values = calculate_hma(prices, hma_period)
    if len(hma_values) < 2:
        raise ValueError("Not enough history for an HMA slope signal")
    return "BUY" if hma_values[-2] < hma_values[-1] else "SELL"


def calculate_hma_result(tradingApi: TradingApi, symbol: str, time_frame: str, hma_period: int)->str:
    prices = tradingApi.getHistoricalPricesList(symbol, time_frame, hma_period * 3)
    return hma_signal(prices, hma_period)


def calculate_two_ema_result(tradingApi: TradingApi, symbol: str, time_frame: str, long_period: int, short_period: int)->str:
    prices = tradingApi.getHistoricalPricesList(symbol, time_frame, long_period)
    long_period_ema_values = calculate_ema(prices, long_period)
    short_period_ema_values = calculate_ema(prices, short_period)

    long_period_ema = long_period_ema_values[-1]
    short_period_ema = short_period_ema_values[-1]

    if short_period_ema < long_period_ema:
        return "SELL"
    else:
        return "BUY"

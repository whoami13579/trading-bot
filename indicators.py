import math

def calculate_wma(prices: list[float], period: int) -> float:
    """Calculates the Weighted Moving Average (WMA)."""
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
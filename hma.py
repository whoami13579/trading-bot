import os
import time
import math
from typing import List, Optional, Dict, Any
from dotenv import find_dotenv, load_dotenv
from TradingBot import TradingBot
import time
from datetime import datetime, timedelta, timezone

# --- Configuration ---
SYMBOL: str = "EURUSD"
HMA_PERIOD: int = 20  # Length of the Hull window
QUANTITY: int = 100
SLEEP_TIME: int = 60 * 30
TIMEFRAME = "MINUTE_30"


def calculate_wma(prices: List[float], period: int) -> float:
    """Calculates the Weighted Moving Average (WMA)."""
    if len(prices) < period:
        return 0.0

    subset = prices[-period:]
    weight_sum = sum(range(1, period + 1))
    weighted_val = sum(p * (i + 1) for i, p in enumerate(subset))
    return weighted_val / weight_sum


def calculate_hma(prices: List[float], period: int) -> List[float]:
    """
    Calculates the Hull Moving Average sequence.
    Formula: WMA(2*WMA(n/2) - WMA(n), sqrt(n))
    """
    half_period = period // 2
    sqrt_period = int(math.sqrt(period))

    # We need a series of 'raw' HMA values to calculate the final WMA smoothing
    raw_hma: List[float] = []

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
    final_hma: List[float] = []
    for j in range(len(raw_hma)):
        if j < sqrt_period:
            continue
        final_hma.append(calculate_wma(raw_hma[: j + 1], sqrt_period))

    return final_hma


def wait_until_targets(target_minutes):
    now = datetime.now()
    current_min = now.minute

    # Find the next target minute
    # We look for the first target greater than current_min
    future_targets = [t for t in target_minutes if t > current_min]

    if future_targets:
        # Next target is later this hour
        next_min = min(future_targets)
        target_time = now.replace(minute=next_min, second=0, microsecond=0)
    else:
        # Next target is the first one in the NEXT hour
        next_min = min(target_minutes)
        target_time = (now + timedelta(hours=1)).replace(
            minute=next_min, second=0, microsecond=0
        )

    # Calculate sleep duration
    delay = (target_time - now).total_seconds()
    delay += 2

    print(f"Current time: {now.strftime('%H:%M:%S')}")
    print(
        f"Next run at:  {target_time.strftime('%H:%M:%S')} (In {round(delay, 2)} seconds)"
    )

    # Standard time.sleep can be slightly inaccurate over long periods,
    # but for 5-30 minute windows, it works perfectly.
    time.sleep(delay)


def timeframe_to_minutes(timeframe: str) -> list[int]:
    match timeframe:
        case "MINUTE_30":
            return [0, 30]

def is_market_open():
    """
    Checks if the current UTC time is between 
    Sunday 21:00 and Friday 22:00.
    """
    now = datetime.now(timezone.utc)
    weekday = now.weekday()  # Monday is 0, Sunday is 6
    hour = now.hour

    # Case 1: Sunday after 22:00
    if weekday == 6 and hour >= 22:
        return True
    
    # Case 2: Monday (0) through Thursday (3) - Always open
    if 0 <= weekday <= 3:
        return True
    
    # Case 3: Friday before 22:00
    if weekday == 4 and hour < 22:
        return True

    # Otherwise, we are in the Friday night - Sunday evening gap
    return False

def wait_until_open(check_interval=60):
    """
    Pauses execution until the market open conditions are met.
    """
    while not is_market_open():
        print(f"[{datetime.now(timezone.utc)}] Market closed. Waiting...")
        time.sleep(check_interval)
    
    print("Market is now OPEN!")

def main() -> None:
    # 1. Environment & Auth
    load_dotenv(find_dotenv())
    tradingBot = TradingBot(
        os.getenv("X-CAP-API-KEY", ""),
        os.getenv("identifier", ""),
        os.getenv("password", ""),
        os.getenv("CST", ""),
        os.getenv("X_SECURITY_TOKEN", ""),
    )

    times = timeframe_to_minutes(TIMEFRAME)
    wait_until_targets(times)
    if not is_market_open():
        wait_until_open()

    # 2. Pre-load sufficient history
    # HMA needs more data than the period itself to stabilize (usually 2x-3x)
    history_count: int = HMA_PERIOD * 3
    print(f"Fetching {history_count} candles for HMA warmup...")

    result: Dict[str, Any] = tradingBot.getHistoricalPrices(
        SYMBOL, TIMEFRAME, history_count
    )
    prices: List[float] = [item["openPrice"]["ask"] for item in result["prices"]]

    current_status: Optional[str] = None

    # 3. Execution Loop
    while True:
        try:
            if not is_market_open():
                wait_until_open()

            # Update latest price
            latest_res: Dict[str, Any] = tradingBot.getHistoricalPrices(
                SYMBOL, TIMEFRAME, 1
            )
            new_price: float = latest_res["prices"][0]["openPrice"]["ask"]

            prices.pop(0)
            prices.append(new_price)

            # Calculate HMA Sequence
            hma_values = calculate_hma(prices, HMA_PERIOD)

            if len(hma_values) < 2:
                print("Calculating HMA...")
                continue

            current_hma = hma_values[-1]
            prev_hma = hma_values[-2]

            print(f"Price: {new_price:.5f} | HMA: {current_hma:.5f}")

            # 4. Slope Logic (Trend Following)
            if current_hma > prev_hma and current_status != "BUY":
                print(">>> HMA Slope UP - Entering BUY")
                res, code = tradingBot.createPosition(SYMBOL, "BUY", QUANTITY)
                if code == 200:
                    current_status = "BUY"

            elif current_hma < prev_hma and current_status != "SELL":
                print(">>> HMA Slope DOWN - Entering SELL")
                res, code = tradingBot.createPosition(SYMBOL, "SELL", QUANTITY)
                if code == 200:
                    current_status = "SELL"

        except Exception as e:
            print(f"Error in main loop: {e}")

        time.sleep(SLEEP_TIME)


if __name__ == "__main__":
    main()

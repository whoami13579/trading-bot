import os
import time
import math
from typing import List, Optional, Dict, Any
from dotenv import find_dotenv, load_dotenv
from TradingBot import TradingBot

# --- Configuration ---
SYMBOL: str = "EURUSD"
HMA_PERIOD: int = 20  # Length of the Hull window
QUANTITY: int = 100
SLEEP_TIME: int = 60

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
            
        current_slice = prices[:i+1]
        wma_half = calculate_wma(current_slice, half_period)
        wma_full = calculate_wma(current_slice, period)
        
        raw_val = (2 * wma_half) - wma_full
        raw_hma.append(raw_val)
    
    # Final smoothing: WMA of the raw_hma series over sqrt(period)
    final_hma: List[float] = []
    for j in range(len(raw_hma)):
        if j < sqrt_period:
            continue
        final_hma.append(calculate_wma(raw_hma[:j+1], sqrt_period))
        
    return final_hma

def main() -> None:
    # 1. Environment & Auth
    load_dotenv(find_dotenv())
    tradingBot = TradingBot(
        os.getenv('X-CAP-API-KEY', ''),
        os.getenv("identifier", ''),
        os.getenv("password", ''),
        os.getenv('CST', ''),
        os.getenv('X_SECURITY_TOKEN', '')
    )

    # 2. Pre-load sufficient history
    # HMA needs more data than the period itself to stabilize (usually 2x-3x)
    history_count: int = HMA_PERIOD * 3
    print(f"Fetching {history_count} candles for HMA warmup...")
    
    result: Dict[str, Any] = tradingBot.getHistoricalPrices(SYMBOL, "MINUTE", history_count)
    prices: List[float] = [item['openPrice']['ask'] for item in result['prices']]
    
    current_status: Optional[str] = None

    # 3. Execution Loop
    while True:
        try:
            # Update latest price
            latest_res: Dict[str, Any] = tradingBot.getHistoricalPrices(SYMBOL, "MINUTE", 1)
            new_price: float = latest_res['prices'][0]['openPrice']['ask']
            
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
                if code == 200: current_status = "BUY"

            elif current_hma < prev_hma and current_status != "SELL":
                print(">>> HMA Slope DOWN - Entering SELL")
                res, code = tradingBot.createPosition(SYMBOL, "SELL", QUANTITY)
                if code == 200: current_status = "SELL"

        except Exception as e:
            print(f"Error in main loop: {e}")

        time.sleep(SLEEP_TIME)

if __name__ == "__main__":
    main()
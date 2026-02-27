import os
import time
import math
from typing import List, Optional, Dict, Any
from dotenv import find_dotenv, load_dotenv
from TradingBot import TradingBot

# --- Configuration ---
SYMBOL: str = "AUDUSD"
HMA_PERIOD: int = 25  # Length of the Hull window
TIME = "MINUTE_30"

'''
data: 1000
SYMBOL: EURUSD
HMA_PERIOD: 20  # Length of the Hull window

MINUTE:       -0.0014699999999974178   (16.66 hr)
MINUTE_5:      0.0019599999999986295   (3.47 d)
MINUTE_15:     0.009969999999999368    (10.41 d)
MINUTE_30:     0.03276999999999819     (20.83 d)
HOUR:          0.0018000000000042427
HOUR_4:        0.002920000000002032
'''

'''
data: 1000
SYMBOL: AUDUSD
HMA_PERIOD: 20  # Length of the Hull window

MINUTE:       -0.002389999999998671   (16.66 hr)
MINUTE_5:     -0.0008399999999993968  (3.47 d)
MINUTE_15:    -0.005670000000000397   (10.41 d)
MINUTE_30:     0.008459999999999912    (20.83 d)
HOUR:         -0.009869999999998713
HOUR_4:       -0.02316000000000029
'''

'''
data: 1000
SYMBOL: AUDUSD
HMA_PERIOD: 15  # Length of the Hull window

MINUTE:       -0.0027999999999990255  (16.66 hr)
MINUTE_5:     -0.0010099999999994003  (3.47 d)
MINUTE_15:     0.006010000000000515   (10.41 d)
MINUTE_30:     0.009039999999999715   (20.83 d)
HOUR:         -0.010589999999998767
HOUR_4:       -0.02725999999999973
'''

'''
data: 1000
SYMBOL: AUDUSD
HMA_PERIOD: 25  # Length of the Hull window

MINUTE:       -0.0028199999999992675  (16.66 hr)
MINUTE_5:     -0.0009799999999995368  (3.47 d)
MINUTE_15:     0.006980000000000652   (10.41 d)
MINUTE_30:     0.00800999999999985   (20.83 d)
HOUR:         -0.009459999999998359
HOUR_4:       -0.02999999999999947
'''

class TestStrategy:
    def __init__(self, tb: TradingBot):
        self.price = 0
        self.balance = 0
        result: Dict[str, Any] = tb.getHistoricalPrices(SYMBOL, TIME, 1000)
        self.all_prices: List[float] = [item['openPrice']['ask'] for item in result['prices']]

    def buy(self, price):
        if self.price == 0:
            self.price = price
        else:
            self.balance += (self.price - price)
            self.price = 0

    def sell(self, price):
        if self.price == 0:
            self.price = price
        else:
            self.balance += (price - self.price)
            self.price = 0
    
    def get_price(self, n)->list[float] | None:
        if len(self.all_prices) < n:
            return None

        res = self.all_prices[:n]
        del self.all_prices[:n]

        return res

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

    ts = TestStrategy(tradingBot)

    # 2. Pre-load sufficient history
    # HMA needs more data than the period itself to stabilize (usually 2x-3x)
    history_count: int = HMA_PERIOD * 3
    print(f"Fetching {history_count} candles for HMA warmup...")
    
    # result: Dict[str, Any] = tradingBot.getHistoricalPrices(SYMBOL, "MINUTE", history_count)
    # prices: List[float] = [item['openPrice']['ask'] for item in result['prices']]
    prices = ts.get_price(history_count)
    
    current_status: Optional[str] = None

    # 3. Execution Loop
    while True:
        try:
            # Update latest price
            # latest_res: Dict[str, Any] = tradingBot.getHistoricalPrices(SYMBOL, "MINUTE", 1)
            # new_price: float = latest_res['prices'][0]['openPrice']['ask']
            new_price = ts.get_price(1)
            if new_price is None:
                break
            new_price = new_price[0]
            
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
                ts.buy(new_price)

            elif current_hma < prev_hma and current_status != "SELL":
                print(">>> HMA Slope DOWN - Entering SELL")
                ts.buy(new_price)

        except Exception as e:
            print(f"Error in main loop: {e}")
            break
    
    print(ts.balance)


if __name__ == "__main__":
    main()
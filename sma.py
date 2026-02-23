import os
import time
from dotenv import find_dotenv, load_dotenv
from TradingBot import TradingBot

# Configuration Constants
SYMBOL = "EURUSD"
FAST_PERIOD = 9
SLOW_PERIOD = 21
QUANTITY = 100
SLEEP_TIME = 60

def get_buffer_prices(tradingBot, count):
    """Fetches initial historical data to populate the moving averages."""
    try:
        result = tradingBot.getHistoricalPrices(SYMBOL, "MINUTE", count)
        return [item['openPrice']['ask'] for item in result['prices']]
    except Exception as e:
        print(f"Error initializing prices: {e}")
        return []

def calculate_sma(prices, period):
    """Calculates Simple Moving Average for a specific window."""
    return sum(prices[-period:]) / period

def main():
    # 1. Environment Setup
    load_dotenv(find_dotenv())
    tradingBot = TradingBot(
        os.getenv('X-CAP-API-KEY'),
        os.getenv("identifier"),
        os.getenv("password"),
        os.getenv('CST'),
        os.getenv('X_SECURITY_TOKEN')
    )

    # 2. Initial Data Load
    # We need at least SLOW_PERIOD prices to start calculating averages
    prices = get_buffer_prices(tradingBot, SLOW_PERIOD)
    current_status = None # Tracks if we are currently "BUY" or "SELL"

    print(f"Bot Started. Initial Prices: {prices[-5:]}")

    # 3. Main Loop
    while True:
        try:
            # Fetch latest single price
            result = tradingBot.getHistoricalPrices(SYMBOL, "MINUTE", 1)
            new_price = result['prices'][0]['openPrice']['ask']
            
            # Update price list (Rolling window)
            prices.pop(0)
            prices.append(new_price)

            # Calculate Averages
            fast_sma = calculate_sma(prices, FAST_PERIOD)
            slow_sma = calculate_sma(prices, SLOW_PERIOD)

            print(f"Price: {new_price:.5f} | Fast: {fast_sma:.5f} | Slow: {slow_sma:.5f}")

            # 4. Strategy Logic: Crossover
            # BUY if Fast crosses ABOVE Slow
            if fast_sma > slow_sma and current_status != "BUY":
                print(">>> TREND UP: Executing BUY")
                # Note: You should call a 'closePosition' function here if your API requires it
                res, code = tradingBot.createPosition(SYMBOL, "BUY", QUANTITY)
                if code == 200:
                    current_status = "BUY"
                else:
                    print(f"Trade Failed: {res}")

            # SELL if Fast crosses BELOW Slow
            elif fast_sma < slow_sma and current_status != "SELL":
                print(">>> TREND DOWN: Executing SELL")
                res, code = tradingBot.createPosition(SYMBOL, "SELL", QUANTITY)
                if code == 200:
                    current_status = "SELL"
                else:
                    print(f"Trade Failed: {res}")

        except Exception as e:
            print(f"Loop Error: {e}")

        time.sleep(SLEEP_TIME)

if __name__ == "__main__":
    main()
import os
import time
from typing import List, Optional, Dict, Any
from dotenv import find_dotenv, load_dotenv
from TradingBot import TradingBot
import time
from datetime import datetime, timedelta
from indicators import calculate_hma
from colors import colors
import argparse

# --- Configuration ---
DEFAULT_SYMBOL: str = "EURUSD"
DEFAULT_HMA_PERIOD: int = 20  # Length of the Hull window
DEFAULT_QUANTITY: int = 100
DEFAULT_SLEEP_TIME: int = 60 * 30
DEFAULT_TIMEFRAME = "MINUTE_30"
DEFAULT_DAYS = 5


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



def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--size", type=float)
    parser.add_argument("--symbol", type=str)
    parser.add_argument("--hma_period", type=str)
    parser.add_argument("--time_frame", type=str)
    parser.add_argument("--days", type=int)

    args = parser.parse_args()
    if args.size:
        QUANTITY: int = args.size
    else:
        QUANTITY: int = DEFAULT_QUANTITY
    
    if args.symbol:
        SYMBOL: str = args.symbol
    else:
        SYMBOL: str = DEFAULT_SYMBOL
    
    if args.hma_period:
        HMA_PERIOD: str = args.hma_period
    else:
        HMA_PERIOD: str = DEFAULT_HMA_PERIOD

    if args.time_frame:
        TIMEFRAME: str = args.time_frame
    else:
        TIMEFRAME: str = DEFAULT_TIMEFRAME

    if args.days:
        DAYS: str = args.days
    else:
        DAYS: str = DEFAULT_DAYS

    # 1. Environment & Auth
    load_dotenv(find_dotenv())
    tradingBot = TradingBot(
        os.getenv("X-CAP-API-KEY", ""),
        os.getenv("identifier", ""),
        os.getenv("password", ""),
        os.getenv("CST", ""),
        os.getenv("X_SECURITY_TOKEN", ""),
        args.real,
    )

    TIMES = timeframe_to_minutes(TIMEFRAME)

    # 2. Pre-load sufficient history
    # HMA needs more data than the period itself to stabilize (usually 2x-3x)
    history_count: int = HMA_PERIOD * 3
    prices: List[float] = None

    current_status: Optional[str] = None

    # 3. Execution Loop
    while True:
        try:
            wait_until_targets(TIMES)

            if DAYS == 7:
                pass
            elif DAYS == 5:
                if not tradingBot.is_market_open():
                    tradingBot.wait_until_open()

            # Update latest price
            prices = tradingBot.getHistoricalPricesList(SYMBOL, TIMEFRAME, history_count)

            # Calculate HMA Sequence
            hma_values = calculate_hma(prices, HMA_PERIOD)

            if len(hma_values) < 2:
                print("Calculating HMA...")
                continue

            current_hma = hma_values[-1]
            prev_hma = hma_values[-2]

            print(f"Price: {prices[-1]:.5f} | HMA: {current_hma:.5f}")

            # 4. Slope Logic (Trend Following)
            if current_hma > prev_hma and current_status != "BUY":
                print(f">>> HMA Slope UP - Entering {colors.BLUE}BUY{colors.ENDC}")
                res, code = tradingBot.createPosition(SYMBOL, "BUY", QUANTITY)
                if code == 200:
                    current_status = "BUY"

            elif current_hma < prev_hma and current_status != "SELL":
                print(f">>> HMA Slope DOWN - Entering {colors.RED}SELL{colors.ENDC}")
                res, code = tradingBot.createPosition(SYMBOL, "SELL", QUANTITY)
                if code == 200:
                    current_status = "SELL"

        except Exception as e:
            print(f"{colors.WARNING}Error in main loop: {e}{colors.ENDC}")



if __name__ == "__main__":
    main()

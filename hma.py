import os
import time
from typing import List, Optional, Dict, Any
from dotenv import find_dotenv, load_dotenv
from TradingBot import TradingBot
import time
from datetime import datetime, timedelta
from indicators import calculate_hma_result
from colors import colors
import argparse

# --- Configuration ---
DEFAULT_SYMBOL: str = "EURUSD"
DEFAULT_HMA_PERIOD: int = 20  # Length of the Hull window
DEFAULT_QUANTITY: int = 100
DEFAULT_TIMEFRAME = "MINUTE_30"
DEFAULT_DAYS = 7
DEFAULT_DIRECTION = "BUY"


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
    parser.add_argument("--direction", type=str)

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

    if args.direction:
        DIRECTION: str = args.direction
    else:
        DIRECTION: str = DEFAULT_DIRECTION

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

    # 3. Execution Loop
    while True:
        try:
            wait_until_targets(TIMES)
            tradingBot.load_keys()

            if DAYS == 7:
                pass
            elif DAYS == 5:
                if not tradingBot.is_market_open():
                    tradingBot.wait_until_open()

            result = calculate_hma_result(tradingBot, SYMBOL, TIMEFRAME, HMA_PERIOD)

            if result != DIRECTION:
                all_positions = tradingBot.getAllPositionsList()
                targets = [pos for pos in all_positions if pos.epic == SYMBOL]
                if 0 < len(targets):
                    for target in targets:
                        result, code = tradingBot.closePosition(target.dealId)
                    
                    if code == 200:
                        print(f">>> {colors.YELLOW}close position{colors.ENDC}")
                    else:
                        print(f"failed to close position ({result})")
            elif result == DIRECTION:
                all_positions = tradingBot.getAllPositionsList()
                targets = [pos for pos in all_positions if pos.epic == SYMBOL]
                if len(targets) == 0:
                    result, code = tradingBot.createPosition(SYMBOL, result, QUANTITY)
                    
                    if code == 200:
                        if result == "BUY":
                            print(f">>> {colors.BLUE}BUY{colors.ENDC}")
                        else:
                            print(f">>> {colors.RED}SELL{colors.ENDC}")
                    else:
                        print(f"failed to create {result} position ({result})")

        except Exception as e:
            print(f"{colors.WARNING}Error in main loop: {e}{colors.ENDC}")



if __name__ == "__main__":
    main()

import os
import time
from dotenv import find_dotenv, load_dotenv
from TradingApi import TradingApi
import time
from datetime import datetime, timedelta
from indicators import calculate_multiple, calculate_hma_result
from colors import colors
import argparse
import traceback
from scheduler import wait_until_targets, timeframe_to_minutes

# --- Configuration ---
DEFAULT_SYMBOL: str = "EURUSD"
DEFAULT_HMA_PERIOD: int = 20  # Length of the Hull window
DEFAULT_QUANTITY: int = 100
DEFAULT_SLEEP_TIME: int = 60 * 30
DEFAULT_SHORT_TERM = "MINUTE_30"
DEFAULT_LONG_TERM = "HOUR_4"
DEFAULT_DAYS = 5




def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--size", type=float)
    parser.add_argument("--symbol", type=str)
    parser.add_argument("--long_term", type=str)
    parser.add_argument("--hma_period", type=int)
    parser.add_argument("--short_term", type=str)
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
        HMA_PERIOD: int = args.hma_period
    else:
        HMA_PERIOD: int = DEFAULT_HMA_PERIOD

    if args.short_term:
        SHORT_TERM: str = args.short_term
    else:
        SHORT_TERM: str = DEFAULT_SHORT_TERM

    if args.days:
        DAYS: str = args.days
    else:
        DAYS: str = DEFAULT_DAYS

    if args.long_term:
        LONG_TERM: str = args.long_term
    else:
        LONG_TERM: str = DEFAULT_LONG_TERM

    # 1. Environment & Auth
    load_dotenv(find_dotenv())
    tradingApi = TradingApi(
        os.getenv("X-CAP-API-KEY", ""),
        os.getenv("identifier", ""),
        os.getenv("password", ""),
        args.real,
    )

    TIMES = timeframe_to_minutes(SHORT_TERM)

    long_term_result = ""
    short_term_result = ""

    # 3. Execution Loop
    while True:
        try:
            wait_until_targets(TIMES)
            tradingApi.load_keys()

            if DAYS == 7:
                pass
            elif DAYS == 5:
                if not tradingApi.is_market_open():
                    tradingApi.wait_until_open()

            long_term_result = calculate_multiple(tradingApi, SYMBOL, LONG_TERM)
            short_term_result = calculate_hma_result(
                tradingApi, SYMBOL, SHORT_TERM, HMA_PERIOD
            )

            if short_term_result != long_term_result:
                all_positions = tradingApi.getAllPositionsList()
                targets = [pos for pos in all_positions if pos.epic == SYMBOL]
                if 0 < len(targets):
                    for target in targets:
                        result, code = tradingApi.closePosition(target.dealId)

                    if code == 200:
                        print(f">>> {colors.YELLOW}close position{colors.ENDC}")
                    else:
                        print(f"failed to close position ({result})")
            elif short_term_result == long_term_result:
                all_positions = tradingApi.getAllPositionsList()
                targets = [pos for pos in all_positions if pos.epic == SYMBOL]
                if len(targets) == 0:
                    result, code = tradingApi.createPosition(
                        SYMBOL, short_term_result, QUANTITY
                    )

                    if code == 200:
                        if short_term_result == "BUY":
                            print(f">>> {colors.BLUE}BUY{colors.ENDC}")
                        else:
                            print(f">>> {colors.RED}SELL{colors.ENDC}")
                    else:
                        print(f"failed to create {short_term_result} position ({result})")

        except Exception as e:
            print(f"{colors.WARNING}#####{colors.ENDC}")
            print(f"{colors.WARNING}Error in main loop: {e}{colors.ENDC}")
            print("")
            print(traceback.format_exc())
            print(f"{colors.WARNING}#####{colors.ENDC}")



if __name__ == "__main__":
    main()

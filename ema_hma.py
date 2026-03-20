import os
from dotenv import find_dotenv, load_dotenv
from TradingBot import TradingBot
from indicators import calculate_hma_result, calculate_two_ema_result
from colors import colors
import argparse
import traceback
from scheduler import wait_until_targets, timeframe_to_minutes

# --- Configuration ---
DEFAULT_SYMBOL: str = "EURUSD"
DEFAULT_HMA_PERIOD: int = 16  # Length of the Hull window
DEFAULT_QUANTITY: int = 100
DEFAULT_DAYS = 7
DEFAULT_LONG_PERIOD = 200
DEFAULT_SHORT_PERIOD = 20
DEFAULT_TIME_FRAME = "MINUTE_5"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--size", type=float)
    parser.add_argument("--symbol", type=str)
    parser.add_argument("--long_period", type=int)
    parser.add_argument("--short_period", type=int)
    parser.add_argument("--hma_period", type=int)
    parser.add_argument("--days", type=int)
    parser.add_argument("--time_frame", type=str)

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

    if args.days:
        DAYS: int = args.days
    else:
        DAYS: int = DEFAULT_DAYS

    if args.long_period:
        LONG_PERIOD: int = args.long_period
    else:
        LONG_PERIOD: int = DEFAULT_LONG_PERIOD

    if args.short_period:
        SHORT_PERIOD: int = args.short_period
    else:
        SHORT_PERIOD: int = DEFAULT_SHORT_PERIOD
    
    if args.time_frame:
        TIME_FRAME: str = args.time_frame
    else:
        TIME_FRAME: str = DEFAULT_TIME_FRAME

    # 1. Environment & Auth
    load_dotenv(find_dotenv())
    tradingBot = TradingBot(
        os.getenv("X-CAP-API-KEY", ""),
        os.getenv("identifier", ""),
        os.getenv("password", ""),
        args.real,
    )

    TIMES = timeframe_to_minutes(TIME_FRAME)

    ema_result = ""
    hma_result = ""

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

            ema_result = calculate_two_ema_result(tradingBot, SYMBOL, TIME_FRAME, LONG_PERIOD, SHORT_PERIOD)
            hma_result = calculate_hma_result(tradingBot, SYMBOL, TIME_FRAME, HMA_PERIOD)

            if hma_result != ema_result:
                all_positions = tradingBot.getAllPositionsList()
                targets = [pos for pos in all_positions if pos.epic == SYMBOL]
                if 0 < len(targets):
                    for target in targets:
                        result, code = tradingBot.closePosition(target.dealId)
                    
                    if code == 200:
                        print(f">>> {colors.YELLOW}close position{colors.ENDC}")
                    else:
                        print(f"failed to close position ({result})")
            elif hma_result == ema_result:
                all_positions = tradingBot.getAllPositionsList()
                targets = [pos for pos in all_positions if pos.epic == SYMBOL]
                if len(targets) == 0:
                    result, code = tradingBot.createPosition(SYMBOL, hma_result, QUANTITY)
                    
                    if code == 200:
                        if hma_result == "BUY":
                            print(f">>> {colors.BLUE}BUY{colors.ENDC}")
                        else:
                            print(f">>> {colors.RED}SELL{colors.ENDC}")
                    else:
                        print(f"failed to create {hma_result} position ({result})")

        except Exception as e:
            print(f"{colors.WARNING}#####{colors.ENDC}")
            print(f"{colors.WARNING}Error in main loop: {e}{colors.ENDC}")
            print("")
            print(traceback.format_exc())
            print(f"{colors.WARNING}#####{colors.ENDC}")



if __name__ == "__main__":
    main()

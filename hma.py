import os
from dotenv import find_dotenv, load_dotenv
from TradingApi import TradingApi
from indicators import calculate_hma_result
from colors import colors
import argparse
from scheduler import wait_until_targets, timeframe_to_minutes

# --- Configuration ---
DEFAULT_SYMBOL: str = "EURUSD"
DEFAULT_HMA_PERIOD: int = 20  # Length of the Hull window
DEFAULT_QUANTITY: int = 100
DEFAULT_TIMEFRAME = "MINUTE_30"
DEFAULT_DAYS = 7
DEFAULT_DIRECTION = "BUY"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--size", type=float)
    parser.add_argument("--symbol", type=str)
    parser.add_argument("--hma_period", type=int)
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
        HMA_PERIOD: int = args.hma_period
    else:
        HMA_PERIOD: int = DEFAULT_HMA_PERIOD

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
    tradingApi = TradingApi(
        os.getenv("X-CAP-API-KEY", ""),
        os.getenv("identifier", ""),
        os.getenv("password", ""),
        args.real,
    )

    TIMES = timeframe_to_minutes(TIMEFRAME)

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

            hma_result = calculate_hma_result(tradingApi, SYMBOL, TIMEFRAME, HMA_PERIOD)

            if hma_result != DIRECTION:
                all_positions = tradingApi.getAllPositionsList()
                targets = [pos for pos in all_positions if pos.epic == SYMBOL]
                if 0 < len(targets):
                    for target in targets:
                        result, code = tradingApi.closePosition(target.dealId)
                        if code != 200:
                            print(f"failed to close position ({result})")
                    
                    if code == 200:
                        print(f">>> {colors.YELLOW}close position{colors.ENDC}")
                    else:
                        print(f"failed to close position ({result})")
            elif hma_result == DIRECTION:
                all_positions = tradingApi.getAllPositionsList()
                targets = [pos for pos in all_positions if pos.epic == SYMBOL]
                if len(targets) == 0:
                    result, code = tradingApi.createPosition(SYMBOL, hma_result, QUANTITY)
                    
                    if code == 200:
                        if result == "BUY":
                            print(f">>> {colors.BLUE}BUY{colors.ENDC}")
                        else:
                            print(f">>> {colors.RED}SELL{colors.ENDC}")
                    else:
                        print(f"failed to create {hma_result} position ({result})")

        except Exception as e:
            print(f"{colors.WARNING}Error in main loop: {e}{colors.ENDC}")



if __name__ == "__main__":
    main()

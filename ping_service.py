import os
from dotenv import find_dotenv, load_dotenv
from TradingBot import TradingBot
from colors import colors
from scheduler import wait_until_targets


def main() -> None:
    # 1. Environment & Auth
    load_dotenv(find_dotenv())
    tradingBot = TradingBot(
        os.getenv("X-CAP-API-KEY", ""),
        os.getenv("identifier", ""),
        os.getenv("password", ""),
    )

    times = []
    for i in range(4, 60, 5):
        times.append(i)
    
    while True:
        try:
            wait_until_targets(times)
            tradingBot.pingService()
            tradingBot.write_keys()
        except Exception as e:
            print("#####")
            print(f"{colors.WARNING}Error in main loop: {e}{colors.ENDC}")
            print("#####")



if __name__ == "__main__":
    main()

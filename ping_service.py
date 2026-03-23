import os
from dotenv import find_dotenv, load_dotenv
from TradingApi import TradingApi
from colors import colors
from scheduler import wait_until_targets
import argparse


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true")

    args = parser.parse_args()
    # 1. Environment & Auth
    load_dotenv(find_dotenv())
    tradingApi = TradingApi(
        os.getenv("X-CAP-API-KEY", ""),
        os.getenv("identifier", ""),
        os.getenv("password", ""),
        args.real,
    )

    times = []
    for i in range(4, 60, 5):
        times.append(i)
    
    while True:
        try:
            wait_until_targets(times)
            tradingApi.pingService()
            tradingApi.write_keys()
        except Exception as e:
            print("#####")
            print(f"{colors.WARNING}Error in main loop: {e}{colors.ENDC}")
            print("#####")



if __name__ == "__main__":
    main()

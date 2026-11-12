from trading_bot.api.client import TradingApi
from trading_bot.config.trading_config import add_environment_arguments, create_trading_api
from trading_bot.utils.colors import colors
from trading_bot.utils.scheduler import wait_until_targets
import argparse


def main() -> None:
    parser = argparse.ArgumentParser()
    add_environment_arguments(parser)

    args = parser.parse_args()
    # 1. Environment & Auth
    tradingApi = create_trading_api(args, order_execution=False)

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

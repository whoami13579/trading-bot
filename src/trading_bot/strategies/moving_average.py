import argparse
from trading_bot.api.client import TradingApi
from trading_bot.config.trading_config import add_trading_arguments, create_trading_api
import time

SYMBOL = "EURUSD"
FAST_PERIOD = 9
SLOW_PERIOD = 21
QUANTITY = 100
SLEEP_TIME = 60


def update_prices(prices: list, tradingApi: TradingApi, symbol=SYMBOL):
    tradingApi.prepare_trading_cycle()
    prices.pop(0)

    result, _ = tradingApi.getHistoricalPrices(symbol, "MINUTE", 1)
    price = result['prices'][0]['openPrice']['ask']
    prices.append(price)


def price_average(prices: list) -> float:
    return sum(prices) / len(prices)


def main():
    parser = argparse.ArgumentParser()
    add_trading_arguments(parser)
    args = parser.parse_args()
    SYMBOL, QUANTITY = args.symbol, args.size
    tradingApi = create_trading_api(args, symbol=SYMBOL, size=QUANTITY)
    result, _ = tradingApi.getHistoricalPrices(SYMBOL, "MINUTE", 20)

    prices = []
    for item in result['prices']:
        prices.append(item['openPrice']['ask'])

    print(prices)
    print("----------")

    if prices[-1] < price_average(prices):
        while prices[-1] < price_average(prices):
            update_prices(prices, tradingApi, SYMBOL)
            print(prices)
            print("----------")
            time.sleep(SLEEP_TIME)
    else:
        while prices[-1] > price_average(prices):
            update_prices(prices, tradingApi, SYMBOL)
            print(prices)
            print("----------")
            time.sleep(SLEEP_TIME)

    status = ""
    while True:
        update_prices(prices, tradingApi, SYMBOL)
        print(prices)
        print("----------")

        if prices[-1] > price_average(prices) and status != "BUY":
            result, statuscode = tradingApi.createPosition(SYMBOL, "BUY", QUANTITY)
            if statuscode == 200:
                print("Buy")
                status = "BUY"
            else:
                print(result)
            print("----------")

        if prices[-1] < price_average(prices) and status != "SELL":
            result, statuscode = tradingApi.createPosition(SYMBOL, "SELL", QUANTITY)
            if statuscode == 200:
                print("SELL")
                status = "SELL"
            else:
                print(result)
            print("----------")

        time.sleep(SLEEP_TIME)

if __name__ == "__main__":
    main()

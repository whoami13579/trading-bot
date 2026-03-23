import os
from dotenv import find_dotenv, load_dotenv
from TradingApi import TradingApi
import time

SYMBOL = "EURUSD"
FAST_PERIOD = 9
SLOW_PERIOD = 21
QUANTITY = 100
SLEEP_TIME = 60


def update_prices(prices: list, tradingApi: TradingApi):
    prices.pop(0)

    result = tradingApi.getHistoricalPrices(SYMBOL, "MINUTE", 1)
    price = result['prices'][0]['openPrice']['ask']
    prices.append(price)


def price_average(prices: list) -> float:
    return sum(prices) / len(prices)


def main():
    dotenv_path = find_dotenv()
    if dotenv_path:
        load_dotenv(dotenv_path)
    else:
        print("Can't find .env")
        exit()

    IDENTIFIER = os.getenv("identifier")
    PASSWORD = os.getenv("password")
    X_CAP_API_KEY = os.getenv('X-CAP-API-KEY')
    CST = os.getenv('CST')
    X_SECURITY_TOKEN = os.getenv('X_SECURITY_TOKEN')


    tradingApi = TradingApi(X_CAP_API_KEY, IDENTIFIER, PASSWORD, CST, X_SECURITY_TOKEN)
    result = tradingApi.getHistoricalPrices(SYMBOL, "MINUTE", 20)

    prices = []
    for item in result['prices']:
        prices.append(item['openPrice']['ask'])

    print(prices)
    print("----------")

    if prices[-1] < price_average(prices):
        while prices[-1] < price_average(prices):
            update_prices(prices, tradingApi)
            print(prices)
            print("----------")
            time.sleep(SLEEP_TIME)
    else:
        while prices[-1] > price_average(prices):
            update_prices(prices, tradingApi)
            print(prices)
            print("----------")
            time.sleep(SLEEP_TIME)

    status = ""
    while True:
        update_prices(prices, tradingApi)
        print(prices)
        print("----------")

        if prices[-1] > price_average(prices) and status != "BUY":
            result, statuscode = tradingApi.createPosition(SYMBOL, "BUY", 100)
            if statuscode == 200:
                print("Buy")
                status = "BUY"
            else:
                print(result)
            print("----------")

        if prices[-1] < price_average(prices) and status != "SELL":
            result, statuscode = tradingApi.createPosition(SYMBOL, "SELL", 100)
            if statuscode == 200:
                print("SELL")
                status = "SELL"
            else:
                print(result)
            print("----------")

        time.sleep(SLEEP_TIME)

main()
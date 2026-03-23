import os
from dotenv import find_dotenv, load_dotenv
from TradingApi import TradingApi
from indicators import calculate_hma, calculate_sma
import json

# --- Configuration ---
SYMBOL: str = "AUDUSD"
HMA_PERIOD: int = 9  # Length of the Hull window
# TIMEFRAME = "MINUTE_30"
TIMEFRAME = "MINUTE_30"


def main() -> None:
    # 1. Environment & Auth
    load_dotenv(find_dotenv())
    tradingApi = TradingApi(
        os.getenv("X-CAP-API-KEY", ""),
        os.getenv("identifier", ""),
        os.getenv("password", ""),
        os.getenv("CST", ""),
        os.getenv("X_SECURITY_TOKEN", ""),
        real_account=True
    )
    # result, _ = tradingApi.createPosition(SYMBOL, "SELL", 100)
    # dealReference = result["dealReference"]
    # result = tradingApi.getPositionOrderConfirmation(dealReference)
    # dealId = result["affectedDeals"][0]["dealId"]
    # time.sleep(5)
    # res = tradingApi.closePosition(dealId)

    # print(dealId)
    # print(res)

    # results = tradingApi.getAllPositionsList()
    # for position in results:
    #     print(position)

    # tradingApi.closePosition('0015421d-0055-311e-0000-000081fa0c09')
    results, code = tradingApi.createPosition("EURUSD", "SELL", 100)
    # results, code = tradingApi.getHistoricalPrices("EURUSD", "HOUR", 100)
    print(results)
    print(code)




if __name__ == "__main__":
    main()

import os
from dotenv import find_dotenv, load_dotenv
from TradingBot import TradingBot
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
    tradingBot = TradingBot(
        os.getenv("X-CAP-API-KEY", ""),
        os.getenv("identifier", ""),
        os.getenv("password", ""),
        os.getenv("CST", ""),
        os.getenv("X_SECURITY_TOKEN", ""),
    )
    # result, _ = tradingBot.createPosition(SYMBOL, "SELL", 100)
    # dealReference = result["dealReference"]
    # result = tradingBot.getPositionOrderConfirmation(dealReference)
    # dealId = result["affectedDeals"][0]["dealId"]
    # time.sleep(5)
    # res = tradingBot.closePosition(dealId)

    # print(dealId)
    # print(res)

    # results = tradingBot.getAllPositionsList()
    # for position in results:
    #     print(position)

    # tradingBot.closePosition('0015421d-0055-311e-0000-000081fa0c09')
    results = tradingBot.getHistoricalPricesList("EURUSD", "HOUR_4", 210)
    print(results)




if __name__ == "__main__":
    main()

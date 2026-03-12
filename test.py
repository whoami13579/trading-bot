import os
from dotenv import find_dotenv, load_dotenv
from TradingBot import TradingBot
from indicators import calculate_hma, calculate_sma

# --- Configuration ---
SYMBOL: str = "EURUSD"
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

    # 2. Pre-load sufficient history
    # HMA needs more data than the period itself to stabilize (usually 2x-3x)
    history_count: int = HMA_PERIOD * 3
    print(f"Fetching {history_count} candles for HMA warmup...")

    result: dict[str, any] = tradingBot.getHistoricalPrices(
        SYMBOL, TIMEFRAME, history_count
    )
    # print(result)
    # prices: list[float] = [item["openPrice"]["ask"] for item in result["prices"]]
    prices: list[float] = [item["closePrice"]["ask"] for item in result["prices"]]


    # Calculate HMA Sequence
    hma_values = calculate_hma(prices, HMA_PERIOD)
    print(hma_values[-1])

    calculate_sma(prices, 10)



if __name__ == "__main__":
    main()


'''
1.1623218888
1.1626053703
'''
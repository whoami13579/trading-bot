import os
from dotenv import find_dotenv, load_dotenv
from TradingBot import TradingBot

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


tradingBot = TradingBot(X_CAP_API_KEY, IDENTIFIER, PASSWORD, CST, X_SECURITY_TOKEN)
result = tradingBot.getAllWatchlists()
print(result)

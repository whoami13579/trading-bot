import requests
from dotenv import find_dotenv, set_key
from datetime import datetime, timezone
import time
from typing import Any, List
from tradingPosition import TradingPosition
from dotenv import find_dotenv, load_dotenv
import os


class TradingBot:
    def __init__(self, x_cap_api_key: str, identifier: str, password: str, cst, x_security_token: str, real_account: bool = False):
        REAL_BASE_URL = "https://api-capital.backend-capital.com"
        DEMO_BASE_URL = "https://demo-api-capital.backend-capital.com"
        if real_account:
            self.BASE_URL = REAL_BASE_URL
        else:
            self.BASE_URL = DEMO_BASE_URL

        self.BASE_URL = "https://demo-api-capital.backend-capital.com"
        self.x_cap_api_key = x_cap_api_key
        self.identifier = identifier
        self.password = password
        self.cst = cst
        self.x_security_token = x_security_token
        self.encryptionkey = ""

        self.pingService()

    def getcstAndXSecurityToken(self) -> list[str] | None:
        # Login Headers
        headers = {
            "X-CAP-API-KEY": self.x_cap_api_key,
            "Content-Type": "application/json",
        }

        # Login Body
        payload = {"identifier": self.identifier, "password": self.password}

        response = requests.post(
            f"{self.BASE_URL}/api/v1/session", json=payload, headers=headers
        )

        if response.status_code == 200:
            # THE TOKENS ARE IN THE HEADERS
            cst_token = response.headers.get("cst")
            security_token = response.headers.get("X-SECURITY-TOKEN")

            return (cst_token, security_token)
        else:
            print(f"Failed to login: {response.status_code} - {response.text}")
            return None

    def getServerTime(self) -> str | None:
        res = requests.get(f"{self.BASE_URL}/api/v1/time")
        if res.status_code == 200:
            return res.json()

        return None

    def pingService(self) -> str | None:
        payload = ""
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "cst": self.cst}
        res = requests.get(
            f"{self.BASE_URL}/api/v1/ping", json=payload, headers=headers
        )
        if res.status_code == 200:
            return res.json()


        # get cst and x_security_token, then ping service again
        cstAndXSecurityToken = self.getcstAndXSecurityToken()
        if cstAndXSecurityToken:
            self.cst = cstAndXSecurityToken[0]
            self.x_security_token = cstAndXSecurityToken[1]

            dotenv_path = find_dotenv()
            if dotenv_path:
                set_key(dotenv_path, "cst", self.cst)
                set_key(dotenv_path, "x_security_token", self.x_security_token)

        payload = ""
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "cst": self.cst}
        res = requests.get(
            f"{self.BASE_URL}/api/v1/ping", json=payload, headers=headers
        )

        if res.status_code == 200:
            return res.json()

        print("ping service failed error: ", end='')
        print(res.json)

        return None

    def getencryptionkey(self) -> dict | None:
        payload = ""
        headers = {"X-CAP-API-KEY": self.x_cap_api_key}
        res = requests.get(
            f"{self.BASE_URL}/api/v1/session/encryptionkey",
            json=payload,
            headers=headers,
        )
        if res.status_code == 200:
            dotenv_path = find_dotenv()
            if dotenv_path:
                self.encryptionkey = res.json().get("encryptionkey")
                set_key(dotenv_path, "encryptionkey", self.encryptionkey)

            return res.json()

        return None

    def getSessionDetails(self) -> dict | None:
        payload = ""
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "cst": self.cst}
        res = requests.get(
            f"{self.BASE_URL}/api/v1/session", json=payload, headers=headers
        )
        if res.status_code == 200:
            return res.json()

        return None

    def createNewSession(self) -> dict | None:
        payload = {"identifier": self.identifier, "password": self.password}
        headers = {
            "X-CAP-API-KEY": self.x_cap_api_key,
            "Content-Type": "application/json",
        }
        res = requests.post(
            f"{self.BASE_URL}/api/v1/session", json=payload, headers=headers
        )
        if res.status_code == 200:
            return res.json()

        return None

    def switchesActiveAccount(self):
        pass

    def logOutOfTheCurrentSession(self):
        pass

    def getAllAccounts(self) -> dict | None:
        payload = ""
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "cst": self.cst}
        res = requests.get(
            f"{self.BASE_URL}/api/v1/accounts", json=payload, headers=headers
        )
        if res.status_code == 200:
            return res.json()

        return None

    def getAccountPreferences(self) -> dict | None:
        payload = ""
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "cst": self.cst}
        res = requests.get(
            f"{self.BASE_URL}/api/v1/accounts/preferences",
            json=payload,
            headers=headers,
        )
        if res.status_code == 200:
            return res.json()

        return None

    def updateAccountPreferences(self):
        pass

    def getAccountActivityHistory(self) -> dict | None:
        payload = ""
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "cst": self.cst}
        res = requests.get(
            f"{self.BASE_URL}/api/v1/history/activity",
            json=payload,
            headers=headers,
        )
        if res.status_code == 200:
            return res.json()

        return None

    def getAccountTransactionsHistory(self) -> dict | None:
        payload = ""
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "cst": self.cst}
        res = requests.get(
            f"{self.BASE_URL}/api/v1/history/transactions",
            json=payload,
            headers=headers,
        )
        if res.status_code == 200:
            return res.json()

        return None

    def adjustBalanceOfDemoAccount(self) -> dict | None:
        return None

    def getPositionOrderConfirmation(self, dealReference: str) -> dict | None:
        payload = ""
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "cst": self.cst}
        res = requests.get(
            f"{self.BASE_URL}/api/v1/confirms/{dealReference}",
            json=payload,
            headers=headers,
        )
        if res.status_code == 200:
            return res.json()

        return None

    def getAllPositions(self) -> dict | None:
        payload = ""
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "cst": self.cst}
        res = requests.get(
            f"{self.BASE_URL}/api/v1/positions", json=payload, headers=headers
        )

        return res.json(), res.status_code
    
    def getAllPositionsList(self) -> list[TradingPosition]:
        result, code = self.getAllPositions()

        # List comprehension to create the objects
        trading_positions_list = [
            TradingPosition(
                epic=item['market']['epic'], 
                dealId=item['position']['dealId']
            ) 
            for item in result['positions']
        ]

        return trading_positions_list

    def createPosition(
        self,
        epic: str,
        direction: str,
        size: int,
        guaranteedStop: bool = False,
        stopLevel: int = 0,
        profitLevel: int = 0,
        trailingStop: bool = 0,
        stopDistance: int = 0,
        stopAmount: int = 0,
        profitDistance: int = 0,
        profitAmount: int = 0,
    ) -> tuple[dict, int]| None:
        payload = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "guaranteedStop": guaranteedStop,
            # "stopLevel": stopLevel,
            # "profitLevel": profitLevel,
        }
        headers = {
            "X-SECURITY-TOKEN": self.x_security_token,
            "cst": self.cst,
            "Content-Type": "application/json",
        }
        res = requests.post(
            f"{self.BASE_URL}/api/v1/positions", json=payload, headers=headers
        )
        # if res.status_code == 200:
        #     return res.json()

        return res.json(), res.status_code

    def getsinglePosition(self, dealId) -> dict | None:
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.x_security_token,
            "cst": self.cst,
        }
        res = requests.get(
            f"{self.BASE_URL}/api/v1/positions/{dealId}", json=payload, headers=headers
        )
        if res.status_code == 200:
            return res.json()

        return None
    
    def updatePosition(self) -> dict | None:
        return None
    
    def closePosition(self, dealId: str) -> dict | None:
        payload = ''
        headers = {
        'X-SECURITY-TOKEN': self.x_security_token,
        'cst': self.cst
        }
        res = requests.delete(f"{self.BASE_URL}/api/v1/positions/{dealId}", json=payload, headers=headers)

        return res.json(), res.status_code

    def getAllWorkingOrders(self) -> dict | None:
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.x_security_token,
            "cst": self.cst,
        }
        res = requests.get(
            f"{self.BASE_URL}/api/v1/workingorders", json=payload, headers=headers
        )
        if res.status_code == 200:
            return res.json()

        return None
    
    def createWorkingOrder(self) -> dict | None:
        return None
    
    def updateWorkingOrder(self) -> dict | None:
        return None

    def deleteWorkingOrder(self) -> dict | None:
        return None
    
    def getAllTopLevelMarketCategories(self) -> dict | None:
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.x_security_token,
            "cst": self.cst,
        }
        res = requests.get(
            f"{self.BASE_URL}/api/v1/marketnavigation", json=payload, headers=headers
        )
        if res.status_code == 200:
            return res.json()

        return None

    def getAllCategorySubNodes(self, nodeId) -> dict | None:
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.x_security_token,
            "cst": self.cst,
        }
        res = requests.get(
            f"{self.BASE_URL}/api/v1/marketnavigation/{nodeId}", json=payload, headers=headers
        )
        if res.status_code == 200:
            return res.json()

        return None

    def getMarketsDetails(self, epics: str) -> dict | None:
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.x_security_token,
            "cst": self.cst,
        }
        res = requests.get(
            f"{self.BASE_URL}/api/v1/markets?epics={epics}", json=payload, headers=headers
        )
        if res.status_code == 200:
            return res.json()

        return None

    def getSingleMarketDetails(self, epic: str) -> dict | None:
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.x_security_token,
            "cst": self.cst,
        }
        res = requests.get(
            f"{self.BASE_URL}/api/v1/markets/{epic}", json=payload, headers=headers
        )
        if res.status_code == 200:
            return res.json()

        return None
    
    def getHistoricalPrices(self, epic: str, resolution: str, max: int) -> dict | None:
        '''
        Docstring for getHistoricalPrices
        
        :param self: Description
        :param epic: Possible values: EURUSD
        :type epic: str
        :param resolution: Possible values are MINUTE, MINUTE_5, MINUTE_15, MINUTE_30, HOUR, HOUR_4, DAY, WEEK
        :type resolution: str
        :param max: from 1 to 1000
        :type max: int
        :return: Description
        :rtype: dict | None
        '''
        self.load_keys()
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.x_security_token,
            "cst": self.cst,
        }
        res = requests.get(
            f"{self.BASE_URL}/api/v1/prices/{epic}?resolution={resolution}&max={max}", json=payload, headers=headers
        )

        return res.json(), res.status_code

    def getHistoricalPricesList(self, symbol: str, time_frame: str, period: int) -> list[float]:
        result, code = self.getHistoricalPrices(
            symbol, time_frame, period
        )

        if code != 200:
            print(result)

        prices: List[float] = [item["closePrice"]["ask"] for item in result["prices"]]
        return prices
    
    def getClientSentimentForMarkets(self) -> dict | None:
        return None

    def getClientSentimentForMarket(self) -> dict | None:
        return None
    
    def getAllWatchlists(self) -> dict | None:
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.x_security_token,
            "cst": self.cst,
        }
        res = requests.get(
            f"{self.BASE_URL}/api/v1/watchlists", json=payload, headers=headers
        )
        if res.status_code == 200:
            return res.json()

        return None
    
    def createWatchlist(self) -> dict | None:
        return None
    
    def getSingleWatchlist(self) -> dict | None:
        return None

    def addMarketToWatchlist(self) -> dict | None:
        return None
    
    def deleteWatchlist(self) -> dict | None:
        return None
    
    def removeMarketFromWatchlist(self) -> dict | None:
        return None
    

    def is_market_open(self):
        """
        Checks if the current UTC time is between 
        Sunday 21:00 and Friday 22:00.
        """
        now = datetime.now(timezone.utc)
        weekday = now.weekday()  # Monday is 0, Sunday is 6
        hour = now.hour

        # Case 1: Sunday after 22:00
        if weekday == 6 and hour >= 22:
            return True
        
        # Case 2: Monday (0) through Thursday (3) - Always open
        if 0 <= weekday <= 3:
            return True
        
        # Case 3: Friday before 22:00
        if weekday == 4 and hour < 22:
            return True

        # Otherwise, we are in the Friday night - Sunday evening gap
        return False


    def wait_until_open(self, check_interval=60):
        """
        Pauses execution until the market open conditions are met.
        """
        while not self.is_market_open():
            print(f"[{datetime.now(timezone.utc)}] Market closed. Waiting...")
            time.sleep(check_interval)
        
        print("Market is now OPEN!")

    def load_keys(self):
        load_dotenv(find_dotenv())
        self.x_cap_api_key = os.getenv("X-CAP-API-KEY", ""),
        self.identifier = os.getenv("identifier", ""),
        self.password = os.getenv("password", ""),
        self.cst = os.getenv("cst", ""),
        self.x_security_token = os.getenv("x_security_token", ""),

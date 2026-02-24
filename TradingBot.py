import http.client
import requests
from dotenv import find_dotenv, set_key


class TradingBot:
    def __init__(self, x_cap_api_key, identifier, password, cst, x_security_token):
        # self.BASE_URL = "https://api-capital.backend-capital.com"
        self.DEMO_BASE_URL = "https://demo-api-capital.backend-capital.com"
        self.X_CAP_API_KEY = x_cap_api_key
        self.IDENTIFIER = identifier
        self.PASSWORD = password
        self.CST = cst
        self.X_SECURITY_TOKEN = x_security_token
        self.ENCRYPTIONKEY = ""

        self.pingService()

    def getCstAndXSecurityToken(self) -> list[str] | None:
        # Login Headers
        headers = {
            "X-CAP-API-KEY": self.X_CAP_API_KEY,
            "Content-Type": "application/json",
        }

        # Login Body
        payload = {"identifier": self.IDENTIFIER, "password": self.PASSWORD}

        response = requests.post(
            f"{self.DEMO_BASE_URL}/api/v1/session", json=payload, headers=headers
        )

        if response.status_code == 200:
            # THE TOKENS ARE IN THE HEADERS
            cst_token = response.headers.get("CST")
            security_token = response.headers.get("X-SECURITY-TOKEN")

            return (cst_token, security_token)
        else:
            print(f"Failed to login: {response.status_code} - {response.text}")
            return None

    def getServerTime(self) -> str | None:
        res = requests.get(f"{self.DEMO_BASE_URL}/api/v1/time")
        if res.status_code == 200:
            return res.json()

        return None

    def pingService(self) -> str | None:
        payload = ""
        headers = {"X-SECURITY-TOKEN": self.X_SECURITY_TOKEN, "CST": self.CST}
        res = requests.get(
            f"{self.DEMO_BASE_URL}/api/v1/ping", json=payload, headers=headers
        )
        if res.status_code == 200:
            return res.json()


        cstAndXSecurityToken = self.getCstAndXSecurityToken()
        if cstAndXSecurityToken:
            self.CST = cstAndXSecurityToken[0]
            self.X_SECURITY_TOKEN = cstAndXSecurityToken[1]

            dotenv_path = find_dotenv()
            if dotenv_path:
                set_key(dotenv_path, "CST", self.CST)
                set_key(dotenv_path, "X_SECURITY_TOKEN", self.X_SECURITY_TOKEN)

        if res.status_code == 200:
            return res.json()

        print("ping service failed error: ", end='')
        print(res.json)

        return None

    def getEncryptionKey(self) -> dict | None:
        payload = ""
        headers = {"X-CAP-API-KEY": self.X_CAP_API_KEY}
        res = requests.get(
            f"{self.DEMO_BASE_URL}/api/v1/session/encryptionKey",
            json=payload,
            headers=headers,
        )
        if res.status_code == 200:
            dotenv_path = find_dotenv()
            if dotenv_path:
                self.ENCRYPTIONKEY = res.json().get("encryptionKey")
                set_key(dotenv_path, "ENCRYPTIONKEY", self.ENCRYPTIONKEY)

            return res.json()

        return None

    def getSessionDetails(self) -> dict | None:
        payload = ""
        headers = {"X-SECURITY-TOKEN": self.X_SECURITY_TOKEN, "CST": self.CST}
        res = requests.get(
            f"{self.DEMO_BASE_URL}/api/v1/session", json=payload, headers=headers
        )
        if res.status_code == 200:
            return res.json()

        return None

    def createNewSession(self) -> dict | None:
        payload = {"identifier": self.IDENTIFIER, "password": self.PASSWORD}
        headers = {
            "X-CAP-API-KEY": self.X_CAP_API_KEY,
            "Content-Type": "application/json",
        }
        res = requests.post(
            f"{self.DEMO_BASE_URL}/api/v1/session", json=payload, headers=headers
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
        headers = {"X-SECURITY-TOKEN": self.X_SECURITY_TOKEN, "CST": self.CST}
        res = requests.get(
            f"{self.DEMO_BASE_URL}/api/v1/accounts", json=payload, headers=headers
        )
        if res.status_code == 200:
            return res.json()

        return None

    def getAccountPreferences(self) -> dict | None:
        payload = ""
        headers = {"X-SECURITY-TOKEN": self.X_SECURITY_TOKEN, "CST": self.CST}
        res = requests.get(
            f"{self.DEMO_BASE_URL}/api/v1/accounts/preferences",
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
        headers = {"X-SECURITY-TOKEN": self.X_SECURITY_TOKEN, "CST": self.CST}
        res = requests.get(
            f"{self.DEMO_BASE_URL}/api/v1/history/activity",
            json=payload,
            headers=headers,
        )
        if res.status_code == 200:
            return res.json()

        return None

    def getAccountTransactionsHistory(self) -> dict | None:
        payload = ""
        headers = {"X-SECURITY-TOKEN": self.X_SECURITY_TOKEN, "CST": self.CST}
        res = requests.get(
            f"{self.DEMO_BASE_URL}/api/v1/history/transactions",
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
        headers = {"X-SECURITY-TOKEN": self.X_SECURITY_TOKEN, "CST": self.CST}
        res = requests.get(
            f"{self.DEMO_BASE_URL}/api/v1/confirms/{dealReference}",
            json=payload,
            headers=headers,
        )
        if res.status_code == 200:
            return res.json()

        return None

    def getAllPositions(self) -> dict | None:
        payload = ""
        headers = {"X-SECURITY-TOKEN": self.X_SECURITY_TOKEN, "CST": self.CST}
        res = requests.get(
            f"{self.DEMO_BASE_URL}/api/v1/positions", json=payload, headers=headers
        )
        if res.status_code == 200:
            return res.json()

        return None

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
        self.pingService()
        payload = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "guaranteedStop": guaranteedStop,
            # "stopLevel": stopLevel,
            # "profitLevel": profitLevel,
        }
        headers = {
            "X-SECURITY-TOKEN": self.X_SECURITY_TOKEN,
            "CST": self.CST,
            "Content-Type": "application/json",
        }
        res = requests.post(
            f"{self.DEMO_BASE_URL}/api/v1/positions", json=payload, headers=headers
        )
        # if res.status_code == 200:
        #     return res.json()

        return res.json(), res.status_code

    def getsinglePosition(self, dealId) -> dict | None:
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.X_SECURITY_TOKEN,
            "CST": self.CST,
        }
        res = requests.get(
            f"{self.DEMO_BASE_URL}/api/v1/positions/{dealId}", json=payload, headers=headers
        )
        if res.status_code == 200:
            return res.json()

        return None
    
    def updatePosition(self) -> dict | None:
        return None
    
    def closePosition(self) -> dict | None:
        return None

    def getAllWorkingOrders(self) -> dict | None:
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.X_SECURITY_TOKEN,
            "CST": self.CST,
        }
        res = requests.get(
            f"{self.DEMO_BASE_URL}/api/v1/workingorders", json=payload, headers=headers
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
            "X-SECURITY-TOKEN": self.X_SECURITY_TOKEN,
            "CST": self.CST,
        }
        res = requests.get(
            f"{self.DEMO_BASE_URL}/api/v1/marketnavigation", json=payload, headers=headers
        )
        if res.status_code == 200:
            return res.json()

        return None

    def getAllCategorySubNodes(self, nodeId) -> dict | None:
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.X_SECURITY_TOKEN,
            "CST": self.CST,
        }
        res = requests.get(
            f"{self.DEMO_BASE_URL}/api/v1/marketnavigation/{nodeId}", json=payload, headers=headers
        )
        if res.status_code == 200:
            return res.json()

        return None

    def getMarketsDetails(self, epics: str) -> dict | None:
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.X_SECURITY_TOKEN,
            "CST": self.CST,
        }
        res = requests.get(
            f"{self.DEMO_BASE_URL}/api/v1/markets?epics={epics}", json=payload, headers=headers
        )
        if res.status_code == 200:
            return res.json()

        return None

    def getSingleMarketDetails(self, epic: str) -> dict | None:
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.X_SECURITY_TOKEN,
            "CST": self.CST,
        }
        res = requests.get(
            f"{self.DEMO_BASE_URL}/api/v1/markets/{epic}", json=payload, headers=headers
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
        self.pingService()
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.X_SECURITY_TOKEN,
            "CST": self.CST,
        }
        res = requests.get(
            f"{self.DEMO_BASE_URL}/api/v1/prices/{epic}?resolution={resolution}&max={max}", json=payload, headers=headers
        )
        if res.status_code == 200:
            return res.json()

        return None
    
    def getClientSentimentForMarkets(self) -> dict | None:
        return None

    def getClientSentimentForMarket(self) -> dict | None:
        return None
    
    def getAllWatchlists(self) -> dict | None:
        payload = ''
        headers = {
            "X-SECURITY-TOKEN": self.X_SECURITY_TOKEN,
            "CST": self.CST,
        }
        res = requests.get(
            f"{self.DEMO_BASE_URL}/api/v1/watchlists", json=payload, headers=headers
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
    


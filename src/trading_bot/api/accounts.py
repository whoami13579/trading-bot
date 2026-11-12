"""Account discovery, explicit preference edits, and history filters."""

import time
from trading_bot.api.validation import boolean, dates, integer, number, text

TRANSACTION_TYPES = frozenset(
    (
        "INACTIVITY_FEE RESERVE VOID UNRESERVE WRITE_OFF_OR_CREDIT CREDIT_FACILITY "
        "FX_COMMISSION COMPLAINT_SETTLEMENT DEPOSIT WITHDRAWAL REFUND WITHDRAWAL_MONEY_BACK TRADE SWAP "
        "TRADE_COMMISSION TRADE_COMMISSION_GSL NEGATIVE_BALANCE_PROTECTION TRADE_CORRECTION CHARGEBACK "
        "ADJUSTMENT BONUS TRANSFER CORPORATE_ACTION CONVERSION REBATE TRADE_SLIPPAGE_PROTECTION"
    ).split()
)


class AccountsAPI:
    def getAllAccounts(self) -> dict:
        return self._api_call("get", "/accounts")

    def getAccountPreferences(self) -> dict:
        return self._api_call("get", "/accounts/preferences")

    def updateAccountPreferences(
        self, *, leverages=None, hedgingMode=None, authorization=None
    ) -> dict:
        payload = {}
        if leverages is not None:
            if not isinstance(leverages, dict) or not leverages:
                raise ValueError("explicit_leverages_required")
            current = self.getAccountPreferences()["leverages"]
            for asset, value in leverages.items():
                integer(value, 1, 100000)
                if asset not in current or value not in current[asset]["available"]:
                    raise ValueError("leverage_not_available")
            payload["leverages"] = dict(leverages)
        if hedgingMode is not None:
            payload["hedgingMode"] = boolean(hedgingMode)
        if not payload:
            raise ValueError("explicit_preference_change_required")
        self._authorize(authorization, "preferences")
        return self._api_call("put", "/accounts/preferences", payload=payload)

    def getAccountActivityHistory(
        self,
        *,
        from_=None,
        to=None,
        lastPeriod=None,
        detailed=None,
        dealId=None,
        filter=None,
    ) -> dict:
        params = dates(from_, to, max_days=1)
        if lastPeriod is not None:
            integer(lastPeriod, 1, min(86400, int(time.time())))
        if detailed is not None:
            boolean(detailed)
        if dealId is not None:
            text(dealId)
        if filter is not None:
            text(
                filter
            )  # FIQL is passed verbatim through query encoding; broker validates its grammar.
        params.update(
            lastPeriod=lastPeriod, detailed=detailed, dealId=dealId, filter=filter
        )
        return self._api_call("get", "/history/activity", params=params)

    def getAccountTransactionsHistory(
        self, *, from_=None, to=None, lastPeriod=None, type=None
    ) -> dict:
        params = dates(from_, to)
        if lastPeriod is not None:
            integer(lastPeriod, 1, int(time.time()))
        if type is not None and type not in TRANSACTION_TYPES:
            raise ValueError("invalid_transaction_type")
        params.update(lastPeriod=lastPeriod, type=type)
        return self._api_call("get", "/history/transactions", params=params)

    def adjustBalanceOfDemoAccount(self, amount: float, *, authorization=None) -> dict:
        number(amount, positive=False)
        if not -400000 <= amount <= 400000:
            raise ValueError("demo_balance_adjustment_out_of_range")
        account = self._authorize(authorization, "demo-balance")
        if float(account["balance"]["balance"]) + amount > 100000:
            raise ValueError("demo_balance_cap_exceeded")
        return self._api_call("post", "/accounts/topUp", payload={"amount": amount})

"""Market navigation, search and time-bounded prices."""

from trading_bot.api.validation import RESOLUTIONS, dates, integer, items, segment, text


class MarketsAPI:
    def getAllTopLevelMarketCategories(self) -> dict:
        return self._api_call("get", "/marketnavigation")

    def getAllCategorySubNodes(self, nodeId: str, limit: int | None = None) -> dict:
        if limit is not None:
            integer(limit, 1, 500)
        return self._api_call(
            "get", "/marketnavigation/" + segment(nodeId), params={"limit": limit}
        )

    def getMarketsDetails(self, epics=None, *, searchTerm: str | None = None) -> dict:
        params = {}
        if epics is not None:
            params["epics"] = ",".join(items(epics, maximum=50))
        if searchTerm is not None:
            params["searchTerm"] = text(searchTerm)
        return self._api_call("get", "/markets", params=params)

    def getAllMarkets(self) -> dict:
        return self.getMarketsDetails()

    def searchMarkets(self, searchTerm: str) -> dict:
        return self.getMarketsDetails(searchTerm=searchTerm)

    def getSingleMarketDetails(self, epic: str) -> dict:
        return self._api_call("get", "/markets/" + segment(epic))

    def getHistoricalPrices(
        self,
        epic: str,
        resolution: str | None = None,
        max: int | None = None,
        *,
        from_: str | None = None,
        to: str | None = None,
    ) -> tuple[dict, int]:
        if resolution is not None and resolution not in RESOLUTIONS:
            raise ValueError("unsupported_price_resolution")
        if max is not None:
            integer(max, 1, 1000)
        params = dates(from_, to)
        params.update(resolution=resolution, max=max)
        return self._api_call(
            "get", "/prices/" + segment(epic), params=params, pair=True
        )

    def getClientSentimentForMarkets(self, marketIds=None) -> dict:
        return self._api_call(
            "get",
            "/clientsentiment",
            params={
                "marketIds": ",".join(items(marketIds))
                if marketIds is not None
                else None
            },
        )

    def getClientSentimentForMarket(self, marketId: str) -> dict:
        return self._api_call("get", "/clientsentiment/" + segment(marketId))

"""Watchlist edits use their own explicit authorization, separate from trading."""

from trading_bot.api.validation import items, segment, text


class WatchlistsAPI:
    def getAllWatchlists(self) -> dict:
        return self._api_call("get", "/watchlists")

    def getSingleWatchlist(self, watchlistId: str) -> dict:
        return self._api_call("get", "/watchlists/" + segment(watchlistId))

    def createWatchlist(self, name: str, epics=None, *, authorization=None) -> dict:
        text(name)
        if len(name) > 20:
            raise ValueError("watchlist_name_too_long")
        payload = {"name": name}
        if epics is not None:
            payload["epics"] = [] if epics == [] else items(epics)
        self._authorize(authorization, "watchlist")
        return self._api_call("post", "/watchlists", payload=payload)

    def addMarketToWatchlist(
        self, watchlistId: str, epic: str, *, authorization=None
    ) -> dict:
        path = "/watchlists/" + segment(watchlistId)
        text(epic)
        self._authorize(authorization, "watchlist")
        return self._api_call("put", path, payload={"epic": epic})

    def deleteWatchlist(self, watchlistId: str, *, authorization=None) -> dict:
        path = "/watchlists/" + segment(watchlistId)
        self._authorize(authorization, "watchlist")
        return self._api_call("delete", path)

    def removeMarketFromWatchlist(
        self, watchlistId: str, epic: str, *, authorization=None
    ) -> dict:
        path = "/watchlists/" + segment(watchlistId) + "/" + segment(epic)
        self._authorize(authorization, "watchlist")
        return self._api_call("delete", path)

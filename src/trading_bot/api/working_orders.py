"""Working orders return submission references, never assumed fills."""

from trading_bot.api.validation import date, number, segment, stops, text


class WorkingOrdersAPI:
    def getAllWorkingOrders(self) -> dict:
        return self._api_call("get", "/workingorders")

    def createWorkingOrder(
        self,
        epic: str,
        direction: str,
        size: float,
        level: float,
        type: str,
        *,
        goodTillDate: str | None = None,
        authorization=None,
        **stop_settings,
    ) -> dict:
        text(epic)
        if direction not in ("BUY", "SELL") or type not in ("LIMIT", "STOP"):
            raise ValueError("invalid_order_direction_or_type")
        payload = {
            "epic": epic,
            "direction": direction,
            "size": number(size),
            "level": number(level, positive=False),
            "type": type,
            **stops(stop_settings),
        }
        if goodTillDate is not None:
            date(goodTillDate)
            payload["goodTillDate"] = goodTillDate
        self._authorize(authorization, "working-order")
        return self._api_call("post", "/workingorders", payload=payload)

    def updateWorkingOrder(
        self,
        dealId: str,
        *,
        level=None,
        goodTillDate=None,
        authorization=None,
        **stop_settings,
    ) -> dict:
        path = "/workingorders/" + segment(dealId)
        payload = dict(stops(stop_settings))
        if level is not None:
            payload["level"] = number(level, positive=False)
        if goodTillDate is not None:
            date(goodTillDate)
            payload["goodTillDate"] = goodTillDate
        if not payload:
            raise ValueError("order_update_required")
        self._authorize(authorization, "working-order", resource_id=dealId)
        return self._api_call("put", path, payload=payload)

    def deleteWorkingOrder(self, dealId: str, *, authorization=None) -> dict:
        path = "/workingorders/" + segment(dealId)
        self._authorize(authorization, "working-order", resource_id=dealId)
        return self._api_call("delete", path)

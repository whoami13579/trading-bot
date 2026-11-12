"""Positions retain legacy response pairs and the existing LIVE execution gate."""

from trading_bot.api.models import TradingPosition
from trading_bot.api.validation import boolean, number, segment, stops, text


class PositionsAPI:
    def getPositionOrderConfirmation(self, dealReference: str) -> dict | None:
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "CST": self.cst}
        res = self._request(
            "get",
            f"{self.BASE_URL}/api/v1/confirms/{segment(dealReference)}",
            headers=headers,
            timeout=20,
            allow_redirects=False,
        )
        if res.status_code == 200:
            return res.json()

        return None

    def getAllPositions(self) -> dict | None:
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "CST": self.cst}
        res = self._request(
            "get",
            f"{self.BASE_URL}/api/v1/positions",
            headers=headers,
            timeout=20,
            allow_redirects=False,
        )

        return res.json(), res.status_code

    def getAllPositionsList(self) -> list[TradingPosition]:
        result, code = self.getAllPositions()
        if code != 200:
            raise RuntimeError("positions_discovery_failed")

        # List comprehension to create the objects
        trading_positions_list = [
            TradingPosition(
                epic=item["market"]["epic"], dealId=item["position"]["dealId"]
            )
            for item in result["positions"]
            if self.environment != "live"
            or (
                self._live_execution is not None
                and item["position"]["dealId"] in self._live_execution.owned
            )
        ]

        return trading_positions_list

    def createPosition(
        self,
        epic: str,
        direction: str,
        size: float,
        guaranteedStop: bool = False,
        stopLevel: float | None = None,
        profitLevel: float | None = None,
        trailingStop: bool | None = None,
        stopDistance: float | None = None,
        stopAmount: float | None = None,
        profitDistance: float | None = None,
        profitAmount: float | None = None,
    ) -> tuple[dict, int] | None:
        self._require_available()
        if self.environment == "live":
            self.verify_account_environment()
            if self._live_execution is None:
                from trading_bot.risk.live_execution import LiveSafetyStop

                raise LiveSafetyStop(
                    "live_execution_not_armed_strategy_preflight_required"
                )
        text(epic)
        number(size)
        boolean(guaranteedStop)
        if direction not in ("BUY", "SELL"):
            raise ValueError("invalid_position_direction")
        payload = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "guaranteedStop": guaranteedStop,
        }
        for name, value in (
            ("stopLevel", stopLevel),
            ("profitLevel", profitLevel),
            ("trailingStop", trailingStop),
            ("stopDistance", stopDistance),
            ("stopAmount", stopAmount),
            ("profitDistance", profitDistance),
            ("profitAmount", profitAmount),
        ):
            if value is not None:
                payload[name] = value
        stops(
            {k: v for k, v in payload.items() if k not in ("epic", "direction", "size")}
        )
        if self.environment == "live":
            payload.setdefault(
                "stopDistance", float(self._live_execution.args.stop_distance)
            )
            return self._live_execution.execute(
                "open",
                lambda: self._gated_send(lambda: self._post_position(payload)),
                payload=payload,
            )
        return self._post_position(payload)

    def _post_position(self, payload):
        headers = {
            "X-SECURITY-TOKEN": self.x_security_token,
            "CST": self.cst,
            "Content-Type": "application/json",
        }
        res = self._request(
            "post",
            f"{self.BASE_URL}/api/v1/positions",
            json=payload,
            headers=headers,
            timeout=20,
            allow_redirects=False,
        )

        if res.status_code != 200:
            print(f"createPosition: HTTP {res.status_code}")

        return res.json(), res.status_code

    def getsinglePosition(self, dealId) -> dict | None:
        headers = {
            "X-SECURITY-TOKEN": self.x_security_token,
            "CST": self.cst,
        }
        res = self._request(
            "get",
            f"{self.BASE_URL}/api/v1/positions/{segment(dealId)}",
            headers=headers,
            timeout=20,
            allow_redirects=False,
        )
        if res.status_code == 200:
            return res.json()

        return None

    def updatePosition(
        self, dealId: str, *, authorization=None, **stop_settings
    ) -> dict:
        path = "/positions/" + segment(dealId)
        payload = stops(stop_settings)
        if not payload:
            raise ValueError("position_update_required")
        self._authorize(authorization, "position-update", resource_id=dealId)
        return self._api_call("put", path, payload=payload)

    def closePosition(self, dealId: str) -> tuple[dict, int]:
        self._require_available()
        if self.environment == "live":
            self.verify_account_environment()
            if self._live_execution is None:
                from trading_bot.risk.live_execution import LiveSafetyStop

                raise LiveSafetyStop(
                    "live_execution_not_armed_strategy_preflight_required"
                )
            return self._live_execution.execute(
                "close",
                lambda: self._gated_send(lambda: self._delete_position(dealId)),
                deal_id=dealId,
            )
        return self._delete_position(dealId)

    def _delete_position(self, dealId):
        headers = {"X-SECURITY-TOKEN": self.x_security_token, "CST": self.cst}
        res = self._request(
            "delete",
            f"{self.BASE_URL}/api/v1/positions/{segment(dealId)}",
            headers=headers,
            timeout=20,
            allow_redirects=False,
        )

        if res.status_code != 200:
            print(f"closePosition: HTTP {res.status_code}")
        return res.json(), res.status_code

"""Fail-closed LIVE strategy execution. Authentication and reads never arm orders."""

import atexit
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time

from scripts.live_risk import (IntegrationError, check_limits, dec, fingerprint,
                               require, risk_estimate)
from scripts.strategy_costs import strategy_costs
from scripts.trading_config import ROOT, LIVE_URL


class LiveSafetyStop(SystemExit):
    """Exit even when legacy strategy loops catch Exception and continue."""


class LiveExecution:
    @staticmethod
    def validate_options(args, symbol, size):
        require(args.real is True, "explicit_live_strategy_opt_in_required")
        # Preserve the existing verified contract/fee scope; never guess another mapping.
        require(symbol == "EURUSD" and dec(size) == 100,
                "verified_live_mapping_requires_eurusd_size_100")
        for name in ("max_margin", "max_exposure", "max_charges", "max_loss", "stop_distance"):
            require(getattr(args, name, None) is not None and dec(getattr(args, name)) > 0,
                    "live_requires_positive_margin_exposure_charges_loss_and_stop_limits")
        require(type(args.max_orders) is int and args.max_orders > 0, "live_requires_positive_max_orders")

    def __init__(self, api, args, symbol, size):
        self.api, self.args, self.symbol, self.size = api, args, symbol, dec(size)
        self.account_id = None
        self.tokens = (api.cst, api.x_security_token)
        self.armed = False
        self.failed = False
        self.owned = {}
        self.open_count = 0
        self.charges = Decimal("0")
        self.initial_equity = None
        self.peak_equity = None
        self.lock = None
        self.path = None
        self.pending = False

    def _positions(self):
        payload, status = self.api.getAllPositions()
        require(status == 200 and isinstance(payload, dict) and isinstance(payload.get("positions"), list),
                "live_positions_unavailable")
        return payload["positions"]

    def _account(self):
        require(self.api.environment == "live" and self.api.BASE_URL == LIVE_URL, "live_endpoint_changed")
        require((self.api.cst, self.api.x_security_token) == self.tokens and all(self.tokens),
                "live_session_changed_requires_new_preflight")
        account = self.api.verify_account_environment()
        if self.account_id is not None:
            require(account["accountId"] == self.account_id, "live_active_account_changed")
        return account

    def _snapshot(self, direction):
        account = self._account()
        preferences = self.api.getAccountPreferences()
        require(isinstance(preferences, dict) and type(preferences.get("hedgingMode")) is bool,
                "live_trading_mode_unknown")
        require(preferences["hedgingMode"] is True, "live_strategy_requires_existing_hedging_mode_no_settings_changed")
        market = self.api.getSingleMarketDetails(self.symbol)
        risk = risk_estimate(market, account, preferences)
        modes = market["snapshot"]["marketModes"]
        require(not (direction == "BUY" and "SHORT_ONLY" in modes)
                and not (direction == "SELL" and "LONG_ONLY" in modes)
                and "CLOSE_ONLY" not in modes, "live_direction_restricted")
        risk["direction"] = direction
        risk["costs"] = strategy_costs(market, risk, account["accountId"], self.args.fee_profile)
        overnight = market["instrument"]["overnightFee"]
        require(overnight.get("shortRate") is not None, "live_sell_funding_rate_unknown")
        risk["overnight_funding"]["reported_sell_rate"] = str(dec(overnight["shortRate"]))
        distance = dec(self.args.stop_distance)
        rules = market["dealingRules"]
        for name, lower in (("minStopOrProfitDistance", True), ("maxStopOrProfitDistance", False)):
            rule = rules[name]
            value = dec(rule["value"], Decimal("0"))
            require(rule["unit"] in ("POINTS", "PERCENTAGE"), "unsupported_live_stop_distance_unit")
            if rule["unit"] == "PERCENTAGE":
                value *= dec(market["snapshot"]["offer"] if direction == "BUY" else market["snapshot"]["bid"]) / 100
            require(distance >= value if lower else distance <= value, "live_stop_distance_outside_broker_limits")
        risk["stop_distance"] = str(distance)
        require(self.size * distance + dec(risk["costs"]["estimated_roundtrip_cost"]) <= dec(self.args.max_loss),
                "live_planned_stop_loss_exceeds_loss_limit")
        positions = self._positions()
        working = self.api.getAllWorkingOrders()
        require(isinstance(working, dict) and isinstance(working.get("workingOrders"), list),
                "live_working_orders_unavailable")
        require(not positions and not working["workingOrders"], "live_existing_exposure_blocks_new_orders")
        context = {"account": account, "risk": risk, "phase1_passed": True}
        check_limits(context, self.args.max_margin, self.args.max_exposure, self.args.max_charges)
        require(self.charges + dec(risk["costs"]["estimated_roundtrip_cost"]) <= dec(self.args.max_charges),
                "live_cumulative_charge_limit_exceeded")
        equity = dec(account["balance"]["balance"]) + dec(account["balance"]["profitLoss"])
        if self.initial_equity is not None:
            self.peak_equity = max(self.peak_equity, equity)
            remaining = dec(self.args.max_loss) - (self.peak_equity - equity)
            require(self.size * distance + dec(risk["costs"]["estimated_roundtrip_cost"]) <= remaining,
                    "live_session_loss_limit_reached")
        context["equity"] = equity
        return context

    def _save(self):
        state = {"base_url": LIVE_URL, "account_fingerprint": fingerprint(self.account_id),
                 "pending": self.pending, "owned": self.owned, "open_count": self.open_count,
                 "estimated_charges_usd": str(self.charges)}
        with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as stream:
            pending = Path(stream.name)
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, self.path)
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def arm(self):
        self.validate_options(self.args, self.symbol, self.size)
        account = self._account()
        self.account_id = account["accountId"]
        directory = ROOT / ".live-integration/strategies" / fingerprint(self.account_id)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / "state.json"
        self.lock = (directory / "lock").open("a+")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if self.path.exists():
            previous = json.loads(self.path.read_text())
            require(previous.get("base_url") == LIVE_URL and previous.get("pending") is False
                    and previous.get("owned") == {}, "prior_live_run_requires_manual_reconciliation")
        context = self._snapshot("BUY")
        # Verify both possible directions before enabling a strategy.
        self._snapshot("SELL")
        self.initial_equity = context["equity"]
        self.peak_equity = self.initial_equity
        print(json.dumps({"live_risk": context["risk"], "max_opening_orders": self.args.max_orders,
                          "max_margin_usd": str(self.args.max_margin), "max_exposure_usd": str(self.args.max_exposure),
                          "max_charges_usd": str(self.args.max_charges), "max_loss_usd": str(self.args.max_loss)}, indent=2))
        print("Real money: fills, stops, funding and loss estimates are not guaranteed.")
        print("Ctrl+C stops this process. Existing positions remain protected by their broker stops;")
        print("review and manage them on Capital.com. Uncertain orders are NEVER retried.")
        # --REAL is the explicit authorization for an unattended strategy. All
        # account/market/cost/risk checks must pass before the execution gate opens.
        self._save()
        self.armed = True
        atexit.register(self.release)

    def release(self):
        self.armed = False
        if self.lock is not None:
            if self.pending or self.owned:
                print("LIVE execution stopped: review existing/uncertain exposure on Capital.com before restarting.")
            self.lock.close()
            self.lock = None

    def _confirm(self, reference):
        require(isinstance(reference, str) and reference and all(c.isalnum() or c in "_-" for c in reference),
                "invalid_live_order_reference")
        for attempt in range(5):
            if attempt:
                time.sleep(1.1)
            value = self.api.getPositionOrderConfirmation(reference)
            if value is None or value.get("dealStatus") in ("PENDING", "UNKNOWN"):
                continue
            require(value.get("dealReference") == reference and value.get("dealStatus") == "ACCEPTED",
                    "live_order_not_confirmed_accepted")
            return value
        raise IntegrationError("live_order_outcome_uncertain_no_retry")

    def _verify_owned(self, deal_id, *, verify_stop=True):
        expected = self.owned[deal_id]
        value = self.api.getsinglePosition(deal_id)
        require(isinstance(value, dict) and value["market"]["epic"] == self.symbol,
                "live_owned_position_unavailable")
        position = value["position"]
        require(position.get("dealId") == deal_id and position.get("direction") == expected["direction"]
                and dec(position.get("size")) == self.size, "live_owned_position_changed")
        if verify_stop:
            level, stop = dec(position.get("level")), dec(position.get("stopLevel"))
            require((stop < level if expected["direction"] == "BUY" else stop > level)
                    and abs(level - stop) <= dec(self.args.stop_distance), "live_broker_stop_not_verified")

    def execute(self, operation, send, *, payload=None, deal_id=None):
        try:
            require(self.armed and not self.failed and not self.pending, "live_execution_not_armed")
            self._account()
            if operation == "open":
                require(not self.owned and self.open_count < self.args.max_orders, "live_open_order_limit_reached")
                require(payload["epic"] == self.symbol and dec(payload["size"]) == self.size
                        and payload["direction"] in ("BUY", "SELL"), "unexpected_live_strategy_order")
                require(payload == {"epic": self.symbol, "direction": payload["direction"], "size": payload["size"],
                                    "guaranteedStop": False, "stopDistance": float(self.args.stop_distance)},
                        "live_order_stop_or_parameters_changed")
                context = self._snapshot(payload["direction"])
                self.open_count += 1
                self.charges += dec(context["risk"]["costs"]["estimated_roundtrip_cost"])
            else:
                require(operation == "close" and deal_id in self.owned, "live_cannot_close_unrelated_position")
                self._verify_owned(deal_id, verify_stop=False)
            # Persist intent BEFORE the only mutation request. Every failure locks out retries.
            self.pending = True
            self._save()
            result, status = send()
            require(status == 200 and isinstance(result, dict), "live_submission_outcome_uncertain_no_retry")
            confirmation = self._confirm(result.get("dealReference"))
            if operation == "open":
                opened_id = confirmation.get("dealId")
                require(isinstance(opened_id, str) and opened_id and
                        all(c.isalnum() or c in "_-" for c in opened_id), "invalid_live_deal_id")
                require(confirmation.get("epic") == self.symbol
                        and confirmation.get("direction") == payload["direction"]
                        and dec(confirmation.get("size")) == self.size, "live_open_confirmation_mismatch")
                affected = confirmation.get("affectedDeals")
                require(isinstance(affected, list) and len(affected) == 1
                        and affected[0].get("dealId") == opened_id and affected[0].get("status") == "OPENED",
                        "live_order_affected_unexpected_positions")
                self.owned[opened_id] = {"direction": payload["direction"]}
                self._save()
                self._verify_owned(opened_id)
            else:
                require(not any(p["position"]["dealId"] == deal_id for p in self._positions()),
                        "live_closure_not_verified")
                del self.owned[deal_id]
            self.pending = False
            self._save()
            return result, status
        except BaseException as exc:
            self.failed = True
            self.armed = False
            if isinstance(exc, KeyboardInterrupt):
                raise
            code = str(exc) if isinstance(exc, IntegrationError) else "live_safety_check_failed"
            raise LiveSafetyStop(f"{code}; execution stopped, inspect account and journal; no order retry") from None

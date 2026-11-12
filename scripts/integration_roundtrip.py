"""Bounded BUY/close, SELL/close workflow for test_api_key.py. No import-time I/O."""

from contextlib import nullcontext
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.parse import parse_qs, urlsplit

from scripts.live_risk import (IntegrationError, check_limits, dec, fingerprint,
                               number, require, risk_estimate, validate_market)
from scripts.strategy_costs import available_costs, strategy_costs
from scripts.trading_config import endpoint
from scripts.network_errors import REQUEST_TIMEOUT, TradingUnavailable
from scripts.trade_reconciliation import (ReconciliationPending, fx_price_metrics,
    summarize_transactions, transaction_window, valid_transaction_query, validate_close_confirmation)
from test_api_key import DemoTransport, TradeJournal, confirm, identifier, positions


class RoundTripJournal(TradeJournal):
    def __init__(self, directory, environment):
        super().__init__(directory)
        self.base_url = endpoint(environment)

    def __enter__(self):
        super().__enter__()
        try:
            if self.path.exists():
                require(json.loads(self.path.read_text()).get("base_url") == self.base_url,
                        "wrong_environment_journal")
        except BaseException:
            self.lock.close()
            raise
        return self

    def save(self, **changes):
        super().save(**{**changes, "base_url": self.base_url})


class RoundTripTransport(DemoTransport):
    """Final prepared-request guard; each direction and each exact close gets one attempt."""

    def __init__(self, args):
        super().__init__(execute=not args.read_only)
        self.args = args
        self.base_url = endpoint(args.environment)
        self.approved = args.environment == "demo"
        self.previous_closed = True
        self.close_started = False
        self.direction = None
        self.READS = {"/api/v1/ping", "/api/v1/session", "/api/v1/accounts", "/api/v1/accounts/preferences",
                      f"/api/v1/markets/{args.symbol}", "/api/v1/positions", "/api/v1/workingorders",
                      "/api/v1/history/transactions"}

    def send(self, session, request, **kwargs):
        url = urlsplit(request.url)
        require(url.scheme == "https" and url.hostname == urlsplit(self.base_url).hostname
                and url.port in (None, 443) and not url.username and not url.password and not url.fragment,
                "integration_selected_host_required_no_fallback")
        require(len(self.calls) < 110, "integration_request_budget_exceeded")
        method, path = request.method, url.path
        if method == "GET":
            allowed = path in self.READS
            if path.startswith("/api/v1/confirms/"):
                allowed = path.removeprefix("/api/v1/confirms/") in self.references
            if path.startswith("/api/v1/positions/"):
                allowed = self.owned_deal_id is not None and path == f"/api/v1/positions/{self.owned_deal_id}"
            require(allowed, "unlisted_integration_read")
            if path == "/api/v1/history/transactions":
                require(valid_transaction_query(parse_qs(url.query, keep_blank_values=True)), "unexpected_settlement_query")
            else:
                require(not url.query, "unexpected_integration_query")
        elif method == "POST" and path == "/api/v1/session":
            require(self.auth_attempts == 0 and not url.query, "authentication_retry_blocked")
            self.auth_attempts += 1
        elif method == "POST" and path == "/api/v1/positions":
            require(self.execute and self.approved and self.previous_closed and self.open_attempts < 2
                    and self.owned_deal_id is None and not url.query and self.journal is not None
                    and self.journal.data["stage"] == "open_submitting", "integration_open_not_armed")
            direction = ("BUY", "SELL")[self.open_attempts]
            expected = {"epic": self.args.symbol, "direction": direction,
                        "size": self.args.size, "guaranteedStop": False}
            if self.args.environment == "live":
                expected["stopDistance"] = float(self.args.stop_distance)
            require(json.loads(request.body) == expected and self.journal.data["direction"] == direction,
                    "unexpected_integration_order")
            self.open_attempts += 1  # Reserve before sending, including timeouts and redirects.
            self.direction = direction
            self.previous_closed = False
            self.close_started = False
        elif method == "DELETE":
            require(self.execute and self.approved and self.owned_deal_id is not None and not self.close_started
                    and self.close_attempts < self.open_attempts and path == f"/api/v1/positions/{self.owned_deal_id}"
                    and not url.query and request.body in (None, b"", "") and self.journal is not None
                    and self.journal.data["stage"] == "close_submitting", "integration_close_not_owned_or_already_attempted")
            self.close_started = True
            self.close_attempts += 1
        else:
            raise IntegrationError("unlisted_integration_mutation")
        if self.last_sent is not None:
            time.sleep(max(0, 1.1 - (time.monotonic() - self.last_sent)))
        self.last_sent = time.monotonic()
        kwargs.update(timeout=REQUEST_TIMEOUT, verify=True, proxies={}, allow_redirects=False)
        response = self.original_send(session, request, **kwargs)
        self.calls.append({"method": method, "path": path if path in self.READS else path.rsplit("/", 1)[0],
                           "http_status": response.status_code})
        if method == "GET" and path.startswith("/api/v1/confirms/") and response.status_code == 404:
            return response
        if response.status_code in (401, 403) or response.status_code >= 500:
            # Let TradingApi invalidate the session and classify auth/availability failures.
            return response
        require(response.status_code == 200, "integration_http_failure_no_mutation_retry")
        return response


class IntegrationGate:
    """Allow LIVE TradingApi mutations only inside this confirmed, transport-guarded test."""

    def __init__(self, api, args, transport):
        self.api, self.args, self.transport = api, args, transport
        self.tokens = (api.cst, api.x_security_token)
        self.armed = True
        self.owned = {}

    def execute(self, operation, send, *, payload=None, deal_id=None):
        require(self.armed and self.transport.approved and self.tokens == (self.api.cst, self.api.x_security_token),
                "integration_live_gate_not_armed")
        if operation == "close":
            require(deal_id in self.owned, "integration_cannot_close_unrelated_position")
        try:
            return send()
        except BaseException:
            self.armed = False
            raise


def new_report(args):
    return {"environment": args.environment.upper(), "symbol": args.symbol, "size": str(dec(args.size)),
            "read_only": args.read_only,
            "roundtrips": {direction: {"opening_price": None, "closing_price": None, "closure_verified": False,
                                      "execution_status": "NOT_ATTEMPTED", "position_id": None,
                                      "pnl_reconciliation_status": "NOT_REQUESTED", "fee_reconciliation_status": "UNVERIFIED",
                                      "realized_profit_loss": None, "estimated_costs": None, "reported_fee_cashflows": None}
                           for direction in ("BUY", "SELL")},
            "realized_profit_loss": None, "remaining_test_positions": "UNKNOWN (positions not checked)",
            "remaining_positions_verified": False, "remaining_account_positions": None, "passed": False}


class RoundTripTest:
    def __init__(self, api, args, transport, journal, report):
        self.api, self.args, self.transport, self.journal, self.report = api, args, transport, journal, report
        self.account_id = None
        self.initial_tokens = None
        self.hedging_mode = None
        self.before_open_ids = set()
        self.peak_equity = None
        self.spent = Decimal("0")
        self.owned_id = None
        self.close_started = False

    def account(self):
        require(self.api.BASE_URL == endpoint(self.args.environment), "integration_environment_changed")
        if self.initial_tokens is not None:
            require((self.api.cst, self.api.x_security_token) == self.initial_tokens, "integration_session_changed")
        account = self.api.verify_account_environment()
        if self.account_id is not None:
            require(account["accountId"] == self.account_id, "integration_active_account_changed")
        return account

    def verify_trading_mode(self, preferences):
        mode = preferences.get("hedgingMode") if isinstance(preferences, dict) else None
        require(type(mode) is bool, "integration_hedging_mode_unknown")
        if self.hedging_mode is not None:
            require(mode is self.hedging_mode, "integration_trading_mode_changed")
        self.hedging_mode = mode
        self.report["hedging_mode"] = mode

    def check_entry_inventory(self, *, final=False):
        """Require a flat account, especially the requested symbol, before each entry.

        In normal mode an opposite order can offset someone else's position.
        An opposite opening order is never used to close the test's BUY.
        """
        current = positions(self.api)
        working = self.api.getAllWorkingOrders()
        require(isinstance(working, dict) and isinstance(working.get("workingOrders"), list),
                "working_orders_response_unavailable")
        if self.args.read_only:
            return
        require(not any(p.get("market", {}).get("epic") == self.args.symbol for p in current),
                "existing_symbol_position_blocks_integration_test")
        require(not any(o.get("workingOrderData", {}).get("epic") == self.args.symbol
                        for o in working["workingOrders"]), "existing_symbol_working_order_blocks_integration_test")
        # Preserve the existing account-wide exposure policy in both environments.
        require(not current, "existing_positions_block_integration_test")
        require(not working["workingOrders"], "working_orders_block_integration_test")
        if final and self.hedging_mode is False:
            # A working order could have filled while the first two reads ran.
            require(not positions(self.api), "position_appeared_during_non_hedging_entry_check")
        self.before_open_ids = {identifier(p["position"]["dealId"]) for p in current}

    def final_entry_check(self):
        self.account()
        self.verify_trading_mode(self.api.getAccountPreferences())
        self.check_entry_inventory(final=True)

    def preflight(self, direction):
        account = self.account()
        preferences = self.api.getAccountPreferences()
        self.verify_trading_mode(preferences)
        market = self.api.getSingleMarketDetails(self.args.symbol)
        minimum, summary = validate_market(market, epic=self.args.symbol)
        rules = market["dealingRules"]
        size = number(self.args.size)
        require(summary["tradeable"] and minimum <= size <= number(rules["maxDealSize"]["value"])
                and (size - minimum) % number(rules["minSizeIncrement"]["value"]) == 0, "requested_size_not_tradeable")
        modes = market["snapshot"]["marketModes"]
        require("CLOSE_ONLY" not in modes and not (direction == "BUY" and "SHORT_ONLY" in modes)
                and not (direction == "SELL" and "LONG_ONLY" in modes), "integration_direction_restricted")
        self.check_entry_inventory()
        risk = None
        if self.args.environment == "live":
            risk = risk_estimate(market, account, preferences, epic=self.args.symbol, size=size)
            risk["direction"] = direction
            risk["costs"] = strategy_costs(market, risk, account["accountId"], self.args.fee_profile)
            check_limits({"phase1_passed": True, "account": account, "risk": risk},
                         self.args.max_margin, self.args.max_exposure, self.args.max_charges)
            charges = dec(risk["costs"]["estimated_roundtrip_cost"])
            require(self.spent + charges <= self.args.max_charges, "integration_cumulative_charge_limit_exceeded")
            distance = self.args.stop_distance
            for name, lower in (("minStopOrProfitDistance", True), ("maxStopOrProfitDistance", False)):
                rule = rules[name]
                bound = dec(rule["value"], Decimal("0"))
                require(rule["unit"] in ("POINTS", "PERCENTAGE"), "unsupported_stop_distance_unit")
                if rule["unit"] == "PERCENTAGE":
                    bound *= dec(market["snapshot"]["offer"] if direction == "BUY" else market["snapshot"]["bid"]) / 100
                require(distance >= bound if lower else distance <= bound, "integration_stop_outside_market_limits")
            equity = dec(account["balance"]["balance"]) + dec(account["balance"]["profitLoss"])
            self.peak_equity = equity if self.peak_equity is None else max(self.peak_equity, equity)
            require(size * distance + charges <= self.args.max_loss - (self.peak_equity - equity),
                    "integration_loss_limit_exceeded")
        return {"account": account, "market": market, "risk": risk}

    def authenticate(self):
        login = self.api.createNewSession()
        if login is None:
            self.api._require_available()
        require(isinstance(login, dict) and bool(self.api.cst and self.api.x_security_token), "integration_authentication_failed")
        self.initial_tokens = (self.api.cst, self.api.x_security_token)
        account = self.account()
        self.account_id = account["accountId"]
        self.report["account_currency"] = account["currency"]
        self.report["account_mask"] = "****" + self.account_id[-4:]

    def approve_live(self):
        require(sys.stdin.isatty(), "live_test_requires_terminal_confirmation")
        print(f"LIVE REAL MONEY: {self.args.size} {self.args.symbol}; one BUY/close then one SELL/close.")
        print(f"Account {self.report['account_mask']}; margin <= {self.args.max_margin} USD, "
              f"exposure <= {self.args.max_exposure} USD, total estimated costs <= {self.args.max_charges} USD, "
              f"drawdown <= {self.args.max_loss} USD, stop distance {self.args.stop_distance}.")
        print("Fills and stops can slip. A failed close requires immediate manual attention.")
        started = time.monotonic()
        require(input("Type LIVE to confirm both roundtrips: ") == "LIVE", "live_test_confirmation_declined")
        require(time.monotonic() - started <= 60, "live_test_confirmation_expired")
        self.transport.approved = True
        self.api._live_execution = IntegrationGate(self.api, self.args, self.transport)

    def verify_owned(self, direction, *, check_stop=False):
        self.account()
        self.verify_trading_mode(self.api.getAccountPreferences())
        value = self.api.getsinglePosition(self.owned_id)
        require(isinstance(value, dict) and value["market"]["epic"] == self.args.symbol, "test_position_unavailable")
        position = value["position"]
        require(position.get("dealId") == self.owned_id and position.get("direction") == direction
                and dec(position.get("size")) == dec(self.args.size), "test_position_identity_or_size_changed")
        if check_stop and self.args.environment == "live":
            level, stop = number(position.get("level")), number(position.get("stopLevel"))
            require((stop < level if direction == "BUY" else stop > level)
                    and abs(level - stop) <= self.args.stop_distance, "test_broker_stop_not_verified")
        return position

    def close_owned_once(self, direction):
        self.close_started = True
        self.verify_owned(direction)
        current = positions(self.api)
        own = [p for p in current if p["position"]["dealId"] == self.owned_id]
        require(len(own) == 1 and own[0]["market"]["epic"] == self.args.symbol
                and own[0]["position"]["direction"] == direction
                and dec(own[0]["position"]["size"]) == dec(self.args.size),
                "test_position_changed_before_exact_close")
        if self.hedging_mode is False:
            require(sum(p["market"]["epic"] == self.args.symbol for p in current) == 1,
                    "ambiguous_non_hedging_symbol_positions_no_close")
        self.journal.save(stage="close_submitting")
        self.report["roundtrips"][direction]["execution_status"] = "CLOSE_OUTCOME_UNCERTAIN"
        result, status = self.api.closePosition(self.owned_id)
        require(status == 200, "test_close_failed")
        reference = identifier(result["dealReference"])
        self.report["roundtrips"][direction]["close_reference"] = reference
        self.journal.save(stage="close_submitted", close_reference=reference)
        closure = confirm(self.api, reference, self.transport)
        validate_close_confirmation(closure, self.owned_id, self.args.symbol, direction, self.args.size)
        row = self.report["roundtrips"][direction]
        if closure.get("level") is not None:
            row["closing_price"] = str(number(closure["level"]))
        for _ in range(5):
            self.account()
            if not any(p["position"]["dealId"] == self.owned_id for p in positions(self.api)):
                row["closure_verified"] = True
                row["execution_status"] = "CLOSED_CONFIRMED"
                row["execution_closed_at"] = datetime.now(timezone.utc).isoformat()
                self.journal.save(stage="closed", closure_verified=True, close_level=row["closing_price"])
                self.transport.previous_closed = True
                self.transport.owned_deal_id = None
                if self.args.environment == "live":
                    self.api._live_execution.owned.clear()
                self.owned_id = None
                return closure
            time.sleep(1.1)
        raise IntegrationError("test_closure_unverified_no_delete_retry")

    def settlement(self, direction, known):
        row = self.report["roundtrips"][direction]
        headers = {"CST": self.api.cst, "X-SECURITY-TOKEN": self.api.x_security_token}
        row["pnl_reconciliation_status"] = "PENDING"
        for _ in range(5):
            self.account()
            window = transaction_window(row["execution_started_at"])
            response = self.api._request("get", self.api.BASE_URL + "/api/v1/history/transactions",
                                         params=window, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=False)
            require(response.status_code == 200, "settlement_unavailable")
            rows = response.json().get("transactions")
            row["history_window_utc"] = window
            row.update(summarize_transactions(rows, row["position_id"], known,
                                             self.report["account_currency"], self.args.symbol))
            if row["realized_profit_loss"] is not None:
                return
            time.sleep(1.1)
        raise ReconciliationPending("realized_pnl_not_yet_attributable_to_test")

    def roundtrip(self, direction):
        context = self.preflight(direction)  # Always refreshed after confirmation/before SELL.
        row = self.report["roundtrips"][direction]
        row["price_metrics"] = fx_price_metrics(context["market"], context["account"]["currency"],
                                                self.args.symbol, self.args.size)
        if context["risk"] is not None:
            cost = dec(context["risk"]["costs"]["estimated_roundtrip_cost"])
            self.report["roundtrips"][direction]["estimated_costs"] = context["risk"]["costs"]
            self.spent += cost
        else:
            spread = row["price_metrics"]["estimated_roundtrip_spread_cost"]
            observed = available_costs(context["market"], {"estimated_roundtrip_spread_cost": spread,
                                                           "account_currency": context["account"]["currency"]})
            self.report["roundtrips"][direction]["estimated_costs"] = {
                "estimated_roundtrip_cost": None, "available_broker_costs": observed}
        self.final_entry_check()
        self.journal.save(stage="prepared", direction=direction, epic=self.args.symbol, size=str(dec(self.args.size)),
                          deal_id=None, closure_verified=False, open_reference=None, close_reference=None,
                          close_level=None, account_fingerprint=fingerprint(self.account_id), hedging_mode=self.hedging_mode)
        self.transport.journal = self.journal
        self.close_started = False
        self.journal.save(stage="open_submitting")
        row["execution_started_at"] = datetime.now(timezone.utc).isoformat()
        row["execution_status"] = "OPEN_OUTCOME_UNCERTAIN"
        result, status = self.api.createPosition(self.args.symbol, direction, self.args.size)
        require(status == 200, "test_open_submission_failed")
        reference = identifier(result["dealReference"])
        row["open_reference"] = reference
        self.journal.save(stage="open_submitted", open_reference=reference)
        opened = confirm(self.api, reference, self.transport)
        if opened.get("dealStatus") == "REJECTED":
            row["execution_status"] = "REJECTED"
            self.journal.save(stage="rejected")
            raise IntegrationError("test_open_rejected")
        require(opened.get("dealStatus") == "ACCEPTED" and opened.get("epic") == self.args.symbol
                and opened.get("direction") == direction and dec(opened.get("size")) == dec(self.args.size),
                "test_open_confirmation_mismatch")
        affected = opened.get("affectedDeals")
        require(isinstance(affected, list) and len(affected) == 1 and affected[0].get("status") == "OPENED",
                "test_position_ownership_unproven")
        deal_id = identifier(affected[0]["dealId"])
        require(deal_id not in self.before_open_ids, "preexisting_position_cannot_be_owned")
        self.owned_id = deal_id  # Never use the top-level order ID as a position ID.
        row.update(position_id=deal_id, execution_status="OPEN_CONFIRMED")
        self.transport.owned_deal_id = self.owned_id
        if self.args.environment == "live":
            self.api._live_execution.owned[self.owned_id] = {"direction": direction}
        self.journal.save(stage="opened", deal_id=self.owned_id)
        known = {self.owned_id, reference}
        if opened.get("dealId"):
            known.add(identifier(opened["dealId"]))
        row = self.report["roundtrips"][direction]
        try:
            actual = self.verify_owned(direction, check_stop=True)
            price = opened.get("level")
            if price is None:
                price = actual.get("level")
            if price is not None:
                row["opening_price"] = str(number(price))
            current = positions(self.api)
            require(any(p["position"]["dealId"] == self.owned_id for p in current), "test_position_not_in_active_list")
        finally:
            # A proven owned position is closed even if price/stop verification fails.
            # A failed/uncertain close is never repeated and always blocks SELL.
            if self.owned_id is not None and not self.close_started:
                closure = self.close_owned_once(direction)
                if closure.get("dealId"):
                    known.add(identifier(closure["dealId"]))
                known.add(row["close_reference"])
        require(row["opening_price"] is not None and row["closing_price"] is not None, "test_execution_price_unavailable")
        row["price_metrics"] = fx_price_metrics(context["market"], context["account"]["currency"],
            self.args.symbol, self.args.size, direction=direction, opening=row["opening_price"], closing=row["closing_price"])
        self.settlement(direction, known)

    def reconcile(self):
        """Account-wide snapshot, independently of execution/financial success."""
        try:
            self.account()
            current = positions(self.api)
            self.report["remaining_account_positions"] = [
                {"deal_id": identifier(p["position"]["dealId"]), "epic": p["market"]["epic"]}
                for p in current]
            self.report["positions_checked_at"] = datetime.now(timezone.utc).isoformat()
            self.report["remaining_positions_verified"] = True
            known = {row["position_id"] for row in self.report["roundtrips"].values() if row.get("position_id")}
            self.report["remaining_test_positions"] = [p["position"]["dealId"] for p in current
                                                       if p["position"]["dealId"] in known]
            if self.owned_id is not None and not self.report["remaining_test_positions"]:
                self.report["remaining_test_positions"] = "UNKNOWN (closure not confirmed; absent from open positions)"
        except (Exception, TradingUnavailable):
            self.report["remaining_positions_verified"] = False
            if self.owned_id is not None:
                self.report["remaining_test_positions"] = [self.owned_id]
        if self.owned_id is None and self.journal is not None and self.journal.data and self.journal.data["stage"] not in self.journal.TERMINAL:
            self.report["remaining_test_positions"] = "UNKNOWN (unconfirmed order; manual reconciliation required)"


def finalize_report(report):
    """Execution, cashflow attribution, fees and exposure have separate outcomes."""
    rows = list(report["roundtrips"].values())
    if report.get("failure") == "realized_pnl_not_yet_attributable_to_test":
        report.setdefault("financial_reconciliation_pending", True)
    for row in rows:
        # Also render older saved reports without treating a missing SELL as a failed SELL.
        row.setdefault("execution_status", "CLOSED_CONFIRMED" if row["closure_verified"] else "NOT_ATTEMPTED")
        row["pnl_reconciliation_status"] = ("ATTRIBUTED" if row["realized_profit_loss"] is not None else
                                            "PENDING" if row["closure_verified"] else "NOT_REQUESTED")
        row["fee_reconciliation_status"] = "REPORTED_PARTIAL" if row["reported_fee_cashflows"] is not None else "UNVERIFIED"
        if row["execution_status"] == "NOT_ATTEMPTED":
            row["not_attempted_reason"] = ("read-only mode" if report["read_only"] else
                "BUY financial reconciliation incomplete; no SELL order sent" if report.get("financial_reconciliation_pending") else
                report.get("failure", "workflow did not reach this step"))
    attempted = [row for row in rows if row["execution_status"] not in ("NOT_ATTEMPTED", "REJECTED")]
    closed = [row for row in attempted if row["closure_verified"]]
    report["execution_status"] = ("NOT_REQUESTED" if report["read_only"] else "COMPLETE" if len(closed) == len(rows)
                                  else "PARTIAL" if closed else "FAILED" if any(row["execution_status"] == "REJECTED" for row in rows)
                                  else "INCOMPLETE" if attempted else "NOT_STARTED")
    report["pnl_reconciliation_status"] = ("ATTRIBUTED_FOR_EXECUTED_TRADES" if attempted and len(closed) == len(attempted)
        and all(row["realized_profit_loss"] is not None for row in attempted) else "PENDING" if attempted else "NOT_REQUESTED")
    report["attributed_realized_profit_loss"] = (str(sum((dec(row["realized_profit_loss"]) for row in attempted), Decimal("0")))
        if report["pnl_reconciliation_status"] == "ATTRIBUTED_FOR_EXECUTED_TRADES" else None)
    report["fee_reconciliation_status"] = ("REPORTED_PARTIAL" if any(row["reported_fee_cashflows"] is not None for row in rows)
                                            else "UNVERIFIED")
    report["fee_completeness_verified"] = False
    report["overall_status"] = ("PASS" if report["passed"] else "INCOMPLETE" if report.get("financial_reconciliation_pending")
                                else "FAIL")


def print_report(report):
    finalize_report(report)
    missing = "unavailable"
    currency = report.get("account_currency", "currency unknown")
    print(f"Environment: {report['environment']}\nTrading pair: {report['symbol']}\nPosition size: {report['size']}")
    mode = report.get("hedging_mode")
    print("Account trading mode: " + ("HEDGING" if mode is True else "NORMAL (non-hedging)" if mode is False else "unknown"))
    if report["read_only"]:
        print("Mode: read-only preflight; no trades requested")
    for direction, row in report["roundtrips"].items():
        print(f"{direction} execution: {row['execution_status']}")
        if row.get("not_attempted_reason"):
            print(f"{direction} not attempted: {row['not_attempted_reason']}")
        print(f"{direction} opening price: {row['opening_price'] if row['opening_price'] is not None else missing}")
        print(f"{direction} closing price: {row['closing_price'] if row['closing_price'] is not None else missing}")
        pnl = row["realized_profit_loss"]
        print(f"{direction} realized P&L: {pnl if pnl is not None else missing} {currency} ({row['pnl_reconciliation_status']}; broker-reported precision)")
        metrics = row.get("price_metrics", {})
        estimate = metrics.get("price_based_pnl_estimate")
        print(f"{direction} price-based P&L estimate: {estimate if estimate is not None else missing} {currency} (not ledger P&L)")
        print(f"{direction} fees: {json.dumps(row['reported_fee_cashflows']) if row['reported_fee_cashflows'] is not None else 'unavailable; not confirmed zero'} {currency} ({row['fee_reconciliation_status']})")
        if metrics.get("spread_pips") is not None:
            print(f"{direction} quote spread estimate: {metrics['spread_price']} {report['symbol'][3:]} per {report['symbol'][:3]} = {metrics['spread_pips']} pips; "
                  f"{metrics['size_base_units']} × spread = {metrics['estimated_roundtrip_spread_cost']} {currency}")
    print(f"Execution status: {report['execution_status']}")
    total = report["attributed_realized_profit_loss"]
    print(f"Realized P&L reconciliation: {report['pnl_reconciliation_status']}; attributed executed-trade total: {total if total is not None else missing} {currency}")
    print(f"Fee reconciliation: {report['fee_reconciliation_status']}; completeness unverified")
    print("Reported fee cashflows: negative = debit, positive = credit.")
    estimates = {side: row["estimated_costs"]["estimated_roundtrip_cost"] if row["estimated_costs"] else None
                 for side, row in report["roundtrips"].items()}
    fees = {side: row["reported_fee_cashflows"] for side, row in report["roundtrips"].items()}
    spreads = {side: row["estimated_costs"]["available_broker_costs"]["estimated_roundtrip_spread_cost"]
               if row["estimated_costs"] else None for side, row in report["roundtrips"].items()}
    print(f"Trading costs ({currency}): estimated total roundtrip {json.dumps(estimates)}, quote-based spread estimate {json.dumps(spreads)}, reported fee cashflows {json.dumps(fees)}")
    print("Spread is embedded in execution prices; do not subtract it again from trade P&L. Unreported fees are not assumed zero; full net P&L is unverified.")
    label = ("Last known test position IDs (current state unavailable)"
             if report.get("remaining_positions_verified") is False else "Remaining test positions")
    print(f"{label}: {report['remaining_test_positions']}")
    account_positions = report.get("remaining_account_positions")
    print(f"Account-wide remaining positions: {len(account_positions) if account_positions is not None else 'UNKNOWN (not verified)'}")
    if report["remaining_test_positions"]:
        print("!!! POSSIBLE REMAINING TEST EXPOSURE — CHECK CAPITAL.COM AND THE PRIVATE JOURNAL NOW !!!")
    if report.get("failure"):
        print(f"Reason: {report['failure']}")
    if report.get("cleanup_failure"):
        print(f"Close could not be completed: {report['cleanup_failure']}")
    scope = "read-only preflight" if report["read_only"] else "execution and trade cashflows; fee completeness reported separately"
    print(f"Overall: {report['overall_status']} ({scope})")


def run_integration(args, root, api_factory, credential_loader):
    root = Path(root)
    report = new_report(args)
    transport = RoundTripTransport(args)
    directory = root / (".live-integration" if args.environment == "live" else ".demo-integration")
    journal = RoundTripJournal(directory, args.environment) if not args.read_only else None
    tester = None
    original = None
    try:
        if args.environment == "live":
            print("!!! LIVE TEST — REAL MONEY AT RISK !!!")
        original = hashlib.sha256((root / ".env").read_bytes()).digest() if (root / ".env").exists() else None
        with (journal if journal is not None else nullcontext()), transport:
            credentials = credential_loader(root / ".env")
            api = api_factory(*(credentials[key] for key in ("X-CAP-API-KEY", "identifier", "password")),
                              environment=args.environment, account=args.account, auto_authenticate=False)
            tester = RoundTripTest(api, args, transport, journal, report)
            try:
                tester.authenticate()
                buy = tester.preflight("BUY")
                sell = tester.preflight("SELL")
                if not args.read_only:
                    if args.environment == "live":
                        require(sum(dec(ctx["risk"]["costs"]["estimated_roundtrip_cost"]) for ctx in (buy, sell))
                                <= args.max_charges, "integration_two_roundtrip_charge_budget_exceeded")
                        tester.approve_live()
                    try:
                        tester.roundtrip("BUY")
                        require(report["roundtrips"]["BUY"]["closure_verified"], "buy_closure_required_before_sell")
                        tester.roundtrip("SELL")
                        report["realized_profit_loss"] = str(sum(dec(row["realized_profit_loss"]) for row in report["roundtrips"].values()))
                    except BaseException:
                        # Preserve the existing one-close ownership policy. No retry on uncertainty.
                        if tester.owned_id is not None and not tester.close_started:
                            try:
                                tester.close_owned_once(transport.direction)
                            except (Exception, KeyboardInterrupt, TradingUnavailable) as cleanup_error:
                                report["cleanup_failure"] = (str(cleanup_error) if isinstance(cleanup_error, (IntegrationError, TradingUnavailable))
                                                             else type(cleanup_error).__name__)
                        raise
                report["passed"] = True
            finally:
                # Stay inside the selected-host transport; never mutate during reconciliation.
                tester.reconcile()
    except (Exception, KeyboardInterrupt, TradingUnavailable) as exc:
        report["failure"] = str(exc) if isinstance(exc, (IntegrationError, TradingUnavailable)) else type(exc).__name__
        if isinstance(exc, TradingUnavailable):
            report["failure_category"] = exc.reason
        if isinstance(exc, ReconciliationPending):
            report["financial_reconciliation_pending"] = True
        if journal is not None and journal.blocked_prior:
            report["remaining_test_positions"] = "UNKNOWN (unresolved prior test journal)"
    finally:
        try:
            current = hashlib.sha256((root / ".env").read_bytes()).digest() if (root / ".env").exists() else None
            report["env_unchanged"] = original == current
        except Exception:
            report["env_unchanged"] = False
        report["passed"] = bool(report["passed"] and report["env_unchanged"] and report["remaining_positions_verified"]
                                and (args.read_only or report["remaining_test_positions"] == []))
        report.update(open_attempts=transport.open_attempts, close_attempts=transport.close_attempts,
                      authentication_attempts=transport.auth_attempts)
        finalize_report(report)
        # Keep results private and ignored; do not regenerate deleted root-level reports.
        try:
            from test_live_api import private_json
            private_json(directory / "buy-sell-results.json", report)
        except Exception:
            report["passed"] = False
            report["failure"] = "private_test_report_write_failed"
        print_report(report)
    return 0 if report["passed"] else 1

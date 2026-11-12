"""Capital.com BUY/close then SELL/close integration test.

Default: EURUSD, size 100, DEMO orders. --REAL/--real selects LIVE and requires
one terminal confirmation after risk preflight. --read-only disables all orders.
--report-only displays saved results with no authentication or network requests.
Both hedging and normal accounts are supported when entry checks prove the
account is flat; preferences are never changed and each close uses its position ID.
Legacy single-BUY helpers remain available for existing diagnostic callers.
"""

import argparse
from contextlib import nullcontext, redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import fcntl
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests
from TradingApi import TradingApi
from scripts.trading_config import load_credentials, add_environment_arguments, integration_environment
from scripts.live_risk import IntegrationError, require, number, validate_market
from scripts.demo_readonly import DEMO_HOST, authentication_error_details, inspect_credentials
from scripts.network_errors import REQUEST_TIMEOUT

ROOT = Path(__file__).resolve().parent
DEMO_URL = f"https://{DEMO_HOST}"
EPIC = "EURUSD"


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value), "invalid_deal_identifier")
    return value


class DemoTransport:
    """Validate final prepared requests. Mutations are denied unless individually armed."""

    READS = {"/api/v1/ping", "/api/v1/session", "/api/v1/accounts", "/api/v1/accounts/preferences",
             "/api/v1/markets/EURUSD", "/api/v1/prices/EURUSD", "/api/v1/positions", "/api/v1/workingorders"}

    def __init__(self, execute=False):
        self.execute = execute
        self.calls = []
        self.auth_attempts = self.open_attempts = self.close_attempts = 0
        self.last_sent = None
        self.open_size = None
        self.owned_deal_id = None
        self.references = set()
        self.journal = None
        self.original_send = requests.sessions.Session.send

    def send(self, session, request, **kwargs):
        url = urlsplit(request.url)
        require(url.scheme == "https" and url.hostname == DEMO_HOST and url.port in (None, 443)
                and not url.username and not url.password and not url.fragment, "blocked_non_demo_url")
        require(len(self.calls) < 50, "request_budget_exceeded")
        method, path = request.method, url.path
        if method == "POST" and path == "/api/v1/session":
            require(self.auth_attempts == 0 and not url.query, "authentication_retry_blocked")
            self.auth_attempts += 1
        elif method == "GET":
            allowed = path in self.READS
            if path.startswith("/api/v1/confirms/"):
                allowed = path.removeprefix("/api/v1/confirms/") in self.references
            if path.startswith("/api/v1/positions/"):
                allowed = self.owned_deal_id is not None and path == f"/api/v1/positions/{self.owned_deal_id}"
            require(allowed, "unlisted_read_blocked")
            if path == "/api/v1/prices/EURUSD":
                require(parse_qs(url.query) == {"resolution": ["MINUTE_30"], "max": ["210"]}, "unexpected_history_query")
            else:
                require(not url.query, "unexpected_query")
        elif method == "POST" and path == "/api/v1/positions":
            require(self.execute and self.open_size is not None and self.open_attempts == 0 and not url.query
                    and self.journal is not None and self.journal.data["stage"] == "open_submitting",
                    "order_submission_blocked")
            payload = json.loads(request.body)
            require(payload == {"epic": EPIC, "direction": "BUY", "size": float(self.open_size), "guaranteedStop": False},
                    "unexpected_order_payload")
            self.open_attempts += 1
        elif method == "DELETE":
            require(self.execute and self.owned_deal_id is not None and self.close_attempts == 0
                    and path == f"/api/v1/positions/{self.owned_deal_id}" and not url.query
                    and request.body in (None, b"", "") and self.journal is not None
                    and self.journal.data["stage"] == "close_submitting", "position_close_blocked")
            self.close_attempts += 1
        else:
            raise IntegrationError("unlisted_mutation_blocked")
        if self.last_sent is not None:
            time.sleep(max(0, 1.1 - (time.monotonic() - self.last_sent)))
        self.last_sent = time.monotonic()
        kwargs.update(timeout=REQUEST_TIMEOUT, verify=True, proxies={}, allow_redirects=False)
        response = self.original_send(session, request, **kwargs)
        record = {"method": method, "path": path if path in self.READS or path == "/api/v1/session" else path.rsplit("/", 1)[0],
                  "http_status": response.status_code}
        if response.status_code != 200:
            record.update(authentication_error_details(response))
        self.calls.append(record)
        if response.status_code == 404 and method == "GET" and path.startswith("/api/v1/confirms/"):
            return response  # Confirmation may not be available yet; only bounded GET polling is allowed.
        require(response.status_code == 200, "http_request_failed")
        return response

    def __enter__(self):
        original_prepare = requests.sessions.Session.prepare_request
        transport = self
        def prepare(session, request):
            session.trust_env = False
            return original_prepare(session, request)
        def send(session, request, **kwargs):
            return transport.send(session, request, **kwargs)
        self.prepare_patch = patch.object(requests.sessions.Session, "prepare_request", prepare)
        self.send_patch = patch.object(requests.sessions.Session, "send", send)
        self.prepare_patch.start()
        self.send_patch.start()
        return self

    def __exit__(self, *exc):
        self.send_patch.stop()
        self.prepare_patch.stop()


class TradeJournal:
    """Durable intent before mutations; unresolved runs block subsequent trading runs."""

    TERMINAL = {"closed", "rejected"}

    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "trade.json"
        self.data = None
        self.lock = None
        self.blocked_prior = False

    def __enter__(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        fd = os.open(self.directory / "lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.lock = os.fdopen(fd, "a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists():
                previous = json.loads(self.path.read_text())
                terminal = (previous.get("stage") == "rejected" or
                            previous.get("stage") == "closed" and previous.get("closure_verified") is True)
                self.blocked_prior = not terminal
                require(terminal, "unresolved_prior_trade_requires_reconciliation")
        except BaseException:
            self.lock.close()
            raise
        return self

    def save(self, **changes):
        if self.data is None:
            require(changes.get("stage") == "prepared", "journal_not_reserved")
            if self.path.exists():
                os.replace(self.path, self.directory / f"completed-{uuid.uuid4().hex}.json")
            self.data = {"run_id": uuid.uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(),
                         "base_url": DEMO_URL, "epic": EPIC, "direction": "BUY"}
        self.data.update(changes)
        with tempfile.NamedTemporaryFile(mode="w", dir=self.directory, prefix="journal-", delete=False) as stream:
            pending = Path(stream.name)
            try:
                json.dump(self.data, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                pending.unlink(missing_ok=True)
                raise
        os.replace(pending, self.path)
        directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is not None and self.data is not None and self.data.get("stage") not in self.TERMINAL:
                self.save(previous_stage=self.data["stage"], stage="uncertain", error_type=exc_type.__name__)
        finally:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()


def positions(api):
    payload, status = api.getAllPositions()
    require(status == 200 and isinstance(payload.get("positions"), list), "invalid_positions_response")
    return payload["positions"]


def verify_read_only(api, report):
    login = api.createNewSession()
    require(isinstance(login, dict) and bool(api.cst and api.x_security_token), "authentication_failed")
    report["authentication"] = {"passed": True, "tokens_obtained": True}
    ping = api.pingService()
    require(isinstance(ping, tuple) and ping[1] == 200, "ping_failed")
    report["ping"] = {"passed": True}
    session = api.getSessionDetails()
    accounts = api.getAllAccounts()
    account_id = session["accountId"]
    matches = [a for a in accounts["accounts"] if a["accountId"] == account_id]
    require(api.BASE_URL == DEMO_URL and login.get("hasActiveDemoAccounts") is True
            and login.get("currentAccountId") == account_id and len(matches) == 1, "demo_account_not_verified")
    account = matches[0]
    require(account.get("status") == "ENABLED" and account.get("accountType") == "CFD"
            and account.get("demo", True) is True, "unexpected_account_type_or_status")
    report["account"] = {"passed": True, "environment": "DEMO", "product_type": "CFD",
                         "status": "ENABLED", "active_account_matches_session": True}
    preferences = api.getAccountPreferences()
    require(type(preferences.get("hedgingMode")) is bool, "missing_hedging_mode")
    report["account"]["hedging_mode"] = preferences["hedgingMode"]
    minimum, market = validate_market(api.getSingleMarketDetails(EPIC))
    report["market"] = {"passed": True, **market}
    history, status = api.getHistoricalPrices(EPIC, "MINUTE_30", 210)
    candles = history["prices"]
    require(status == 200 and len(candles) >= 210 and all(
        math.isfinite(float(row["closePrice"]["ask"])) and float(row["closePrice"]["ask"]) > 0 for row in candles), "invalid_history_response")
    report["history"] = {"passed": True, "epic": EPIC, "resolution": "MINUTE_30", "candles": len(candles)}
    baseline = positions(api)
    report["positions"] = {"passed": True, "eurusd_positions": sum(p["market"]["epic"] == EPIC for p in baseline)}
    report["trading_api_response_handling"] = {"passed": True}
    return {"account_id": account_id, "minimum": minimum, "market": market, "baseline": baseline}


def verify_same_account(api, context):
    require(api.BASE_URL == DEMO_URL and api.getSessionDetails()["accountId"] == context["account_id"], "active_account_changed")


def confirm(api, reference, transport):
    reference = identifier(reference)
    transport.references.add(reference)
    for _ in range(5):
        value = api.getPositionOrderConfirmation(reference)
        if value is None or value.get("dealStatus") in ("PENDING", "UNKNOWN"):
            time.sleep(1.1)
            continue
        require(value.get("dealReference") == reference, "confirmation_reference_mismatch")
        return value
    raise IntegrationError("confirmation_unresolved_no_mutation_retry")


def verify_owned_position(payload, deal_id, minimum):
    require(payload["position"]["dealId"] == deal_id and payload["market"]["epic"] == EPIC
            and payload["position"]["direction"] == "BUY" and number(payload["position"]["size"]) == minimum,
            "owned_position_changed_or_mismatched")


def execute_trade(api, context, transport, journal, report):
    require(transport.execute and journal is not None, "explicit_execution_permission_required")
    require(not any(p["market"]["epic"] == EPIC for p in context["baseline"]), "existing_eurusd_position_blocks_test")
    verify_same_account(api, context)
    minimum, market = validate_market(api.getSingleMarketDetails(EPIC))
    require(market["tradeable"] and minimum == context["minimum"], "market_closed_or_rules_changed")
    baseline = positions(api)  # Recheck immediately before submitting, particularly in netting mode.
    require(not any(p["market"]["epic"] == EPIC for p in baseline), "existing_eurusd_position_blocks_test")
    baseline_ids = {identifier(p["position"]["dealId"]) for p in baseline}
    working = api.getAllWorkingOrders()
    require(isinstance(working.get("workingOrders"), list), "invalid_working_orders_response")
    require(not any(order["workingOrderData"]["epic"] == EPIC for order in working["workingOrders"]),
            "pending_eurusd_order_blocks_test")
    verify_same_account(api, context)
    journal.save(stage="prepared", size=str(minimum),
                 account_fingerprint=hashlib.sha256(context["account_id"].encode()).hexdigest())
    transport.journal = journal
    transport.open_size = minimum
    journal.save(stage="open_submitting")  # Durable before POST: no retry after an uncertain outcome.
    result, status = api.createPosition(EPIC, "BUY", float(minimum))
    require(status == 200, "order_submission_rejected")
    reference = identifier(result["dealReference"])
    require(reference.startswith("o_"), "unexpected_open_reference")
    journal.save(stage="open_submitted", open_reference=reference)
    opened = confirm(api, reference, transport)
    if opened.get("dealStatus") == "REJECTED":
        journal.save(stage="rejected")
        raise IntegrationError("order_rejected_by_confirmation")
    require(opened.get("dealStatus") == "ACCEPTED" and opened.get("epic") == EPIC
            and opened.get("direction") == "BUY" and number(opened["size"]) == minimum,
            "open_confirmation_mismatched")
    affected = opened.get("affectedDeals", [])
    require(len(affected) == 1 and affected[0].get("status") == "OPENED", "new_position_ownership_unproven")
    deal_id = identifier(affected[0]["dealId"])  # The order's top-level dealId is not necessarily the position ID.
    require(deal_id not in baseline_ids, "preexisting_position_cannot_be_owned")
    journal.save(stage="opened", deal_id=deal_id)
    transport.owned_deal_id = deal_id
    report["trade"] = {"order_accepted": True, "size": str(minimum), "deal_id_recorded_privately": True}
    verify_same_account(api, context)
    verify_owned_position(api.getsinglePosition(deal_id), deal_id, minimum)
    # Verify the current list too: refuse to close merged, partially altered, or ambiguous positions.
    current = positions(api)
    targets = [p for p in current if p["market"]["epic"] == EPIC]
    require(len(targets) == 1, "concurrent_eurusd_position_change")
    verify_owned_position(targets[0], deal_id, minimum)
    journal.save(stage="close_submitting")  # Never repeat DELETE after a timeout or ambiguous result.
    closed, status = api.closePosition(deal_id)
    require(status == 200, "close_request_failed")
    close_reference = identifier(closed["dealReference"])
    journal.save(stage="close_submitted", close_reference=close_reference)
    closure = confirm(api, close_reference, transport)
    require(closure.get("dealStatus") == "ACCEPTED" and closure.get("epic", EPIC) == EPIC,
            "close_confirmation_not_accepted")
    for _ in range(5):
        verify_same_account(api, context)
        remaining = positions(api)
        remaining_ids = {identifier(p["position"]["dealId"]) for p in remaining}
        if deal_id not in remaining_ids:
            require(baseline_ids <= remaining_ids, "baseline_positions_changed_during_test")
            journal.save(stage="closed", closure_verified=True)
            report["trade"].update(close_accepted=True, closure_verified=True, baseline_positions_preserved=True)
            return
        time.sleep(1.1)
    raise IntegrationError("closure_unverified_no_delete_retry")


def main(argv=None):
    """Run BUY/close then SELL/close. Imports and --help never start trading."""
    from scripts.trading_config import add_trading_arguments

    parser = argparse.ArgumentParser(description=__doc__)
    add_trading_arguments(parser)
    parser.set_defaults(max_orders=2)
    parser.add_argument("--environment", choices=("demo", "live"), help="Legacy explicit selector")
    parser.add_argument("--read-only", action="store_true", help="Authenticate and preflight without placing orders")
    parser.add_argument("--report-only", action="store_true", help="Display saved results without credentials, network requests or orders")
    parser.add_argument("--execute-demo-trade", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.environment = integration_environment(argv)
    if args.execute_demo_trade and args.environment != "demo":
        parser.error("--execute-demo-trade conflicts with LIVE selection")
    if args.max_orders != 2:
        parser.error("This test has a fixed limit of two opening orders: one BUY and one SELL")
    if not re.fullmatch(r"[A-Z0-9_.-]{1,64}", args.symbol):
        parser.error("symbol must be an instrument epic without URL/path characters")
    for key in ("max_margin", "max_exposure", "max_charges", "max_loss", "stop_distance"):
        value = getattr(args, key)
        if not value.is_finite() or value <= 0:
            parser.error("risk limits and stop distance must be finite and positive")
    from scripts.integration_roundtrip import print_report, run_integration
    if args.report_only:
        path = ROOT / (".demo-integration" if args.environment == "demo" else ".live-integration") / "buy-sell-results.json"
        try:
            report = json.loads(path.read_text())
            require(report["environment"] == args.environment.upper(), "saved_report_environment_mismatch")
            print("Saved report only; no network requests. Position data is historical.")
            print_report(report)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IntegrationError):
            print("Saved report unavailable or invalid; no network requests made.")
            return 1
        return 0 if report["passed"] else 1
    return run_integration(args, ROOT, TradingApi, load_credentials)


if __name__ == "__main__":
    raise SystemExit(main())

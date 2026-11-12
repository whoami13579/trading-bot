"""LIVE read-only preflight; user-run order test requires limits, flag, and typed TTY confirmation.

Credentials come from the shared .env. LIVE requires --REAL (alias --real)
or the legacy explicit --environment live. The default routes to DEMO checks.
"""

import argparse
from contextlib import nullcontext
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import secrets
import ssl
import sys
import tempfile
import time
from urllib.parse import urlsplit

import requests
import certifi
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from trading_bot.api.auth_diagnostics import authentication_error_details
from trading_bot.utils.files import private_json
from trading_bot.config.trading_config import (ConfigurationError, LIVE_URL, load_credentials,
                                    add_environment_arguments, integration_environment)
from trading_bot.risk.journal import TradeJournal
from trading_bot.risk.confirmations import identifier
from trading_bot.risk.live_risk import (IntegrationError, require, validate_market, dec, fingerprint,
                               risk_estimate, verified_costs, check_limits)

from trading_bot.config.paths import ROOT
EPIC, SIZE = "EURUSD", Decimal("100")


class VerifiedLiveAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cafile=certifi.where())
        kwargs["ssl_context"] = context
        return super().init_poolmanager(connections, maxsize, block=block, **kwargs)


def load_live_configuration(path):
    try:
        return load_credentials(path)
    except ConfigurationError as exc:
        raise IntegrationError(str(exc)) from None


class LiveJournal(TradeJournal):
    def __enter__(self):
        super().__enter__()
        try:
            if self.path.exists():
                require(json.loads(self.path.read_text()).get("base_url") == LIVE_URL, "wrong_environment_journal")
        except BaseException:
            super().__exit__(None, None, None)
            raise
        return self

    def save(self, **changes):
        super().save(**{**changes, "base_url": LIVE_URL})


class LiveClient:
    READS = {"/api/v1/ping", "/api/v1/session", "/api/v1/accounts", "/api/v1/accounts/preferences",
             "/api/v1/markets/EURUSD", "/api/v1/positions", "/api/v1/workingorders"}

    def __init__(self, config, execute=False, *, environment="demo"):
        require(environment == "live", "explicit_live_environment_required")
        self.account_id = None
        self.explicit_account_selection = False
        self.config, self.execute = config, execute
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.mount("https://", VerifiedLiveAdapter(max_retries=Retry(total=0, connect=0, read=0, status=0, redirect=0)))
        self.tokens = {}
        self.calls = []
        self.last_sent = None
        self.auth_attempts = self.open_attempts = self.close_attempts = 0
        self.references, self.owned_id = set(), None
        self.journal = None
        self.manual_approval = False
        self.phase1_passed = False

    def request(self, method, path, payload=None, params=None):
        require(len(self.calls) < 70, "live_request_budget_exceeded")
        require(path.startswith("/api/v1/") and not urlsplit(path).scheme and "?" not in path, "unlisted_live_path")
        if method == "POST" and path == "/api/v1/session":
            require(self.auth_attempts == 0, "live_authentication_retry_blocked")
            self.auth_attempts += 1
        elif method == "GET":
            allowed = path in self.READS
            if path.startswith("/api/v1/confirms/"):
                allowed = path.removeprefix("/api/v1/confirms/") in self.references
            if path.startswith("/api/v1/positions/"):
                allowed = self.owned_id is not None and path == f"/api/v1/positions/{self.owned_id}"
            if path == "/api/v1/history/transactions":
                allowed = self.owned_id is not None and params == {"lastPeriod": 600}
            require(allowed and payload is None, "unlisted_live_read")
        elif method == "POST" and path == "/api/v1/positions":
            require(self.execute and self.manual_approval and self.phase1_passed and self.open_attempts == 0
                    and self.journal is not None and self.journal.data["stage"] == "open_submitting",
                    "live_order_not_armed")
            require(payload == {"epic": EPIC, "direction": "BUY", "size": 100, "guaranteedStop": False}, "unexpected_live_order")
            self.open_attempts += 1
        elif method == "DELETE":
            require(self.execute and self.manual_approval and self.owned_id is not None and self.close_attempts == 0
                    and path == f"/api/v1/positions/{self.owned_id}" and payload is None
                    and self.journal is not None and self.journal.data["stage"] == "close_submitting", "live_close_not_owned_or_armed")
            self.close_attempts += 1
        else:
            raise IntegrationError("live_mutation_blocked")
        require(params is None or path == "/api/v1/history/transactions", "unexpected_live_query")
        if self.last_sent is not None:
            time.sleep(max(0, 1.1 - (time.monotonic() - self.last_sent)))
        self.last_sent = time.monotonic()
        headers = {**self.tokens}
        if path == "/api/v1/session" and method == "POST":
            headers["X-CAP-API-KEY"] = self.config["X-CAP-API-KEY"]
        with self.session.request(method, LIVE_URL + path, json=payload, params=params, headers=headers,
                                  timeout=(5, 15), verify=True, allow_redirects=False, stream=True) as response:
            raw = bytearray()
            for chunk in response.iter_content(16384):
                raw.extend(chunk)
                require(len(raw) <= 256 * 1024, "live_response_too_large")
            # Preserve only status, fixed path labels and safe classifier fields in logs.
            record = {"method": method, "path": path if path in self.READS or path == "/api/v1/session"
                      else path.rsplit("/", 1)[0], "http_status": response.status_code}
            self.calls.append(record)
            if response.status_code != 200:
                safe = requests.Response()
                safe._content = bytes(raw)
                safe.headers = response.headers
                record.update(authentication_error_details(safe))
                if response.status_code == 404 and method == "GET" and path.startswith("/api/v1/confirms/"):
                    return None
                raise IntegrationError("live_http_request_failed")
            value = json.loads(raw)
            require(isinstance(value, dict), "unexpected_live_response_shape")
            if method == "POST" and path == "/api/v1/session":
                cst, security = response.headers.get("CST"), response.headers.get("X-SECURITY-TOKEN")
                require(bool(cst and security), "live_token_headers_missing")
                self.tokens = {"CST": cst, "X-SECURITY-TOKEN": security}
            return value

    def login(self):
        return self.request("POST", "/api/v1/session", {"identifier": self.config["identifier"],
                            "password": self.config["password"], "encryptedPassword": False})

    def close(self):
        self.session.close()


def eligible_live_accounts(accounts):
    require(isinstance(accounts, list), "invalid_live_account_list")
    eligible = [a for a in accounts if a.get("status") == "ENABLED" and a.get("accountType") == "CFD"
                and a.get("demo", False) is False]
    ids = [identifier(a.get("accountId")) for a in eligible]
    require(len(ids) == len(set(ids)), "duplicate_live_account_ids")
    return eligible


def select_live_account(login, session, accounts, selection=None):
    # Environment proof: fixed verified LIVE host, LIVE availability, and matching
    # login/session/account IDs. The API's CFD product type alone is insufficient.
    require(login.get("hasActiveLiveAccounts") is True, "no_active_live_accounts")
    current = identifier(login.get("currentAccountId"))
    require(session.get("accountId") == current, "live_session_account_mismatch")
    eligible = eligible_live_accounts(accounts)
    active = [a for a in eligible if a["accountId"] == current]
    require(len(active) == 1, "active_account_not_enabled_live_cfd")
    if selection is not None:
        selected = [a for a in eligible if selection in (a["accountId"], fingerprint(a["accountId"])[:12])]
        require(len(selected) == 1 and selected[0]["accountId"] == current,
                "selected_live_account_not_active_no_switch_performed")
    return active[0], eligible, len(eligible) > 1 and selection is None


def live_preflight(client, config, report, fee_path, selected_account=None):
    login = client.login()
    report["authentication"] = {"passed": True, "tokens_obtained": True}
    session = client.request("GET", "/api/v1/session")
    client.request("GET", "/api/v1/ping")
    accounts = client.request("GET", "/api/v1/accounts")
    account, eligible, selection_required = select_live_account(login, session, accounts["accounts"], selected_account)
    expected = account["accountId"]
    client.account_id = expected
    client.explicit_account_selection = selected_account is not None
    report["account_selection"] = {"eligible_accounts": len(eligible),
        "explicit_selection": selected_account is not None, "required_before_trading": selection_required,
        "choices": [{"selection_key": fingerprint(a["accountId"])[:12],
                     "account_mask": "****" + a["accountId"][-4:], "active": a["accountId"] == expected}
                    for a in eligible]}
    require(isinstance(account.get("currency"), str) and len(account["currency"]) == 3, "invalid_account_currency")
    balance = account["balance"]
    report["account"] = {"environment": "LIVE", "account_mask": "****" + expected[-4:],
                         "account_fingerprint": fingerprint(expected), "product_type": "CFD", "currency": account["currency"],
                         "balance": str(dec(balance["balance"])), "available": str(dec(balance["available"])),
                         "profit_loss_field": str(dec(balance["profitLoss"])),
                         "balance_minus_available_not_exact_margin": str(dec(balance["balance"]) - dec(balance["available"]))}
    report["account_environment_verified"] = True
    preferences = client.request("GET", "/api/v1/accounts/preferences")
    require(type(preferences.get("hedgingMode")) is bool, "live_hedging_mode_unknown")
    report["account"]["hedging_mode"] = preferences["hedgingMode"]
    report["account"]["currency_leverage"] = str(dec(preferences["leverages"]["CURRENCIES"]["current"], Decimal("1")))
    market = client.request("GET", "/api/v1/markets/EURUSD")
    report["market"] = validate_market(market)[1]
    open_positions = client.request("GET", "/api/v1/positions")["positions"]
    working_orders = client.request("GET", "/api/v1/workingorders")["workingOrders"]
    require(isinstance(open_positions, list) and isinstance(working_orders, list), "invalid_live_position_lists")
    report["existing"] = {"positions": len(open_positions), "working_orders": len(working_orders),
                          "eurusd_positions": sum(p["market"]["epic"] == EPIC for p in open_positions)}
    report["phase1_api_passed"] = True
    risk = risk_estimate(market, account, preferences)
    report["proposal"] = risk
    try:
        risk["costs"] = verified_costs(fee_path, risk, expected, market)
        complete = True
    except (IntegrationError, KeyError, ValueError, TypeError) as exc:
        risk["costs"] = {"complete": False, "reason": str(exc) if isinstance(exc, IntegrationError) else "invalid_live_fee_profile"}
        complete = False
    report["phase1_passed"] = complete and not open_positions and not working_orders and not selection_required
    report["phase2_ready"] = False  # Limits and manual terminal confirmation are additional requirements.
    return {"account_id": expected, "account": account, "risk": risk, "market": market,
            "positions": open_positions, "working_orders": working_orders, "phase1_passed": report["phase1_passed"]}


def manual_confirmation(context, max_margin, max_exposure, max_charges=None):
    check_limits(context, max_margin, max_exposure, max_charges)
    require(sys.stdin.isatty(), "manual_tty_confirmation_required_no_piped_input")
    nonce = secrets.token_hex(4)
    phrase = f"LIVE BUY 100 EURUSD {context['account_id'][-4:]} {nonce}"
    print(json.dumps({"proposed_live_trade": context["risk"], "account_mask": "****" + context["account_id"][-4:],
                      "maximum_margin_usd": str(max_margin), "maximum_exposure_usd": str(max_exposure),
                      "maximum_transaction_charges_usd": str(max_charges)}, indent=2))
    print("Market orders can slip; estimates are not guaranteed fills. Margin and stops do not cap possible losses.")
    print("An uncertain fill/close may leave LIVE exposure requiring immediate manual intervention.")
    require(input(f"Type exactly {phrase!r} to proceed: ") == phrase, "live_manual_confirmation_declined")
    return time.monotonic()


def live_confirm(client, reference):
    reference = identifier(reference)
    client.references.add(reference)
    for _ in range(5):
        payload = client.request("GET", f"/api/v1/confirms/{reference}")
        if payload is None or payload.get("dealStatus") in ("PENDING", "UNKNOWN"):
            continue
        require(payload.get("dealReference") == reference, "live_confirmation_reference_mismatch")
        return payload
    raise IntegrationError("live_confirmation_unresolved")


def verify_live_position(payload, deal_id):
    require(payload["position"]["dealId"] == deal_id and payload["market"]["epic"] == EPIC
            and payload["position"]["direction"] == "BUY" and dec(payload["position"]["size"]) == SIZE,
            "live_owned_position_changed")


def reconcile_remaining(client, journal, report):
    """Only read. Never infer ownership from symbol/time or retry a mutation."""
    try:
        require(client.request("GET", "/api/v1/session")["accountId"] == client.account_id, "live_account_changed")
        rows = client.request("GET", "/api/v1/positions")["positions"]
        owned = journal.data.get("deal_id")
        matching = [row for row in rows if row["position"]["dealId"] == owned] if owned else []
        eurusd = [row for row in rows if row["market"]["epic"] == EPIC]
        report["remaining_exposure"] = {"owned_id_known": bool(owned), "owned_position_visible": bool(matching),
                                        "eurusd_positions": len(eurusd), "manual_intervention_required": True}
        if matching:
            direction = matching[0]["position"].get("direction")
            report["remaining_exposure"].update(direction=direction if direction in ("BUY", "SELL") else "UNKNOWN",
                                                size=str(dec(matching[0]["position"]["size"])),
                                                notional_value="not_repriced_during_failure_reconciliation")
        # No ID after an unconfirmed submission: absence now does not prove that a delayed order cannot execute.
    except Exception:
        report["remaining_exposure"] = {"status": "unknown", "manual_intervention_required": True}


def settle_report(client, journal, context, report):
    known = {journal.data["deal_id"], journal.data["open_reference"], journal.data["close_reference"]}
    known.update(journal.data.get("confirmation_ids", []))
    matched = []
    for _ in range(5):
        rows = client.request("GET", "/api/v1/history/transactions", params={"lastPeriod": 600})["transactions"]
        matched = [row for row in rows if row.get("reference") in known]
        if any(row.get("transactionType") == "TRADE" for row in matched):
            break
    accounts = client.request("GET", "/api/v1/accounts")["accounts"]
    current = [row for row in accounts if row["accountId"] == context["account_id"]]
    require(len(current) == 1, "final_live_account_not_found")
    report["final_account"] = {"currency": current[0]["currency"], "balance": str(dec(current[0]["balance"]["balance"])),
                               "available": str(dec(current[0]["balance"]["available"]))}
    require(matched and all(row.get("currency") == context["account"]["currency"] and row.get("status") == "PROCESSED" for row in matched),
            "realized_live_pnl_not_safely_attributable")
    trades = [dec(row["size"]) for row in matched if row.get("transactionType") == "TRADE"]
    require(bool(trades), "realized_live_pnl_not_safely_attributable")
    fees = {}
    for row in matched:
        kind = row.get("transactionType")
        if kind in ("TRADE_COMMISSION", "FX_COMMISSION", "TRADE_COMMISSION_GSL", "SWAP"):
            fees[kind] = fees.get(kind, Decimal("0")) + dec(row["size"])
    report["settlement"] = {"realized_trade_cashflow_account_currency": str(sum(trades)),
                            "realized_profit_loss_account_currency": str(sum(trades)),
                            "matched_fee_cashflows": {key: str(value) for key, value in fees.items()},
                            "net_matched_cashflow": str(sum(trades) + sum(fees.values())),
                            "source": "exact_reference_matched_processed_transactions",
                            "fee_absence_does_not_prove_all_costs_zero": True}


def execute_live(client, config, context, journal, max_margin, max_exposure, fee_path, report, *, max_charges=None):
    require(client.execute and journal is not None, "live_execution_flag_required")
    check_limits(context, max_margin, max_exposure, max_charges)
    confirmed_at = manual_confirmation(context, max_margin, max_exposure, max_charges)
    # Revalidate every material input. Require the quote to remain within the user's approved limits,
    # without accepting increased spread/cost estimates after confirmation.
    fresh_report = {}
    fresh = live_preflight_snapshot(client, config, fresh_report, fee_path)
    check_limits(fresh, max_margin, max_exposure, max_charges)
    require(time.monotonic() - confirmed_at <= 20, "live_confirmation_expired")
    require(dec(fresh["risk"]["costs"]["estimated_roundtrip_cost"]) <= dec(context["risk"]["costs"]["estimated_roundtrip_cost"]),
            "live_cost_increased_reconfirmation_required")
    context = fresh
    journal.save(stage="prepared", size="100", account_fingerprint=fingerprint(context["account_id"]),
                 approved_max_margin=str(max_margin), approved_max_exposure=str(max_exposure),
                 approved_max_charges=str(max_charges), limit_currency="USD")
    client.journal, client.manual_approval, client.phase1_passed = journal, True, True
    journal.save(stage="open_submitting")
    opened = client.request("POST", "/api/v1/positions", {"epic": EPIC, "direction": "BUY", "size": 100, "guaranteedStop": False})
    reference = identifier(opened["dealReference"])
    require(reference.startswith("o_"), "unexpected_live_open_reference")
    journal.save(stage="open_submitted", open_reference=reference)
    confirmation = live_confirm(client, reference)
    if confirmation.get("dealStatus") == "REJECTED":
        journal.save(stage="rejected")
        raise IntegrationError("live_order_rejected")
    require(confirmation.get("dealStatus") == "ACCEPTED" and confirmation.get("epic") == EPIC
            and confirmation.get("direction") == "BUY" and dec(confirmation["size"]) == SIZE, "live_open_confirmation_mismatched")
    affected = confirmation.get("affectedDeals", [])
    require(len(affected) == 1 and affected[0].get("status") == "OPENED", "live_ownership_unproven")
    deal_id = identifier(affected[0]["dealId"])
    ids = [identifier(confirmation["dealId"])] if confirmation.get("dealId") else []
    journal.save(stage="opened", deal_id=deal_id, confirmation_ids=ids, open_level=str(dec(confirmation["level"])))
    report["trade"] = {"opened": True, "buy_execution_price": journal.data["open_level"], "executed_size": "100",
                       "closure_verified": False, "remaining_owned_position": True,
                       "actual_margin_used": None, "actual_margin_source": "not_reported_by_api",
                       "pretrade_spread": context["risk"]["spread"],
                       "estimated_roundtrip_spread_cost": context["risk"]["estimated_roundtrip_spread_cost"],
                       "actual_spread_at_execution": None}
    client.owned_id = deal_id
    require(client.request("GET", "/api/v1/session")["accountId"] == context["account_id"], "live_account_changed_before_close")
    owned = client.request("GET", f"/api/v1/positions/{deal_id}")
    verify_live_position(owned, deal_id)
    rows = client.request("GET", "/api/v1/positions")["positions"]
    require(len(rows) == 1, "concurrent_live_position_change")
    verify_live_position(rows[0], deal_id)
    journal.save(stage="close_submitting")
    closing = client.request("DELETE", f"/api/v1/positions/{deal_id}")
    close_ref = identifier(closing["dealReference"])
    journal.save(stage="close_submitted", close_reference=close_ref)
    closure = live_confirm(client, close_ref)
    require(closure.get("dealStatus") == "ACCEPTED", "live_close_not_confirmed")
    close_level = str(dec(closure["level"], Decimal("0.00000001"))) if closure.get("level") is not None else None
    journal.save(close_level=close_level)
    report["trade"]["closing_execution_price"] = close_level
    if closure.get("dealId"):
        journal.save(confirmation_ids=journal.data.get("confirmation_ids", []) + [identifier(closure["dealId"])])
    for _ in range(5):
        require(client.request("GET", "/api/v1/session")["accountId"] == context["account_id"], "live_account_changed_during_reconciliation")
        rows = client.request("GET", "/api/v1/positions")["positions"]
        if not any(row["position"]["dealId"] == deal_id for row in rows):
            journal.save(stage="closed", closure_verified=True)
            report["trade"].update(closure_verified=True, remaining_owned_position=False)
            settle_report(client, journal, context, report)
            require(close_level is not None, "live_close_execution_price_unavailable")
            return
    raise IntegrationError("live_closure_unverified_do_not_retry_delete")


def live_preflight_snapshot(client, config, report, fee_path):
    # Recheck without a second login. All operations are read-only.
    expected = client.account_id
    require(client.request("GET", "/api/v1/session")["accountId"] == expected, "live_account_changed")
    accounts = client.request("GET", "/api/v1/accounts")["accounts"]
    eligible = eligible_live_accounts(accounts)
    require(len(eligible) == 1 or client.explicit_account_selection, "multiple_live_accounts_require_explicit_selection")
    selected = [a for a in eligible if a["accountId"] == expected]
    require(len(selected) == 1, "live_account_unavailable")
    preferences = client.request("GET", "/api/v1/accounts/preferences")
    market = client.request("GET", "/api/v1/markets/EURUSD")
    positions = client.request("GET", "/api/v1/positions")["positions"]
    working = client.request("GET", "/api/v1/workingorders")["workingOrders"]
    risk = risk_estimate(market, selected[0], preferences)
    risk["costs"] = verified_costs(fee_path, risk, expected, market)
    return {"account_id": expected, "account": selected[0], "risk": risk, "market": market,
            "positions": positions, "working_orders": working, "phase1_passed": not positions and not working}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    environment = integration_environment(argv)
    if environment == "demo" and not any(flag in argv for flag in ("-h", "--help")):
        from trading_bot.tools.demo_roundtrip import main as demo_main
        return demo_main(["--read-only", *argv])
    parser = argparse.ArgumentParser(description=__doc__)
    add_environment_arguments(parser)
    parser.add_argument("--environment", choices=("demo", "live"), help="Legacy explicit selector")
    parser.add_argument("--execute-live-trade", action="store_true")
    parser.add_argument("--max-margin", type=Decimal, help="User-chosen maximum estimated margin in USD (USD accounts only)")
    parser.add_argument("--max-exposure", type=Decimal, help="User-chosen maximum estimated notional in USD")
    parser.add_argument("--max-charges", type=Decimal, help="User-chosen maximum estimated roundtrip transaction charges in USD, including spread")
    parser.add_argument("--fee-profile", type=Path, default=ROOT / ".live-integration/fees.json")
    args = parser.parse_args(argv)
    args.environment = environment
    if args.execute_live_trade and any(limit is None for limit in (args.max_margin, args.max_exposure, args.max_charges)):
        parser.error("Live execution requires explicit --max-margin, --max-exposure and --max-charges limits")
    print("!!! LIVE ACCOUNT — REAL MONEY; orders require --execute-live-trade and TTY confirmation !!!", file=sys.stderr)
    credential_path = ROOT / ".env"
    original = hashlib.sha256(credential_path.read_bytes()).digest() if credential_path.exists() else None
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "base_url": LIVE_URL,
              "execute_live_trade": args.execute_live_trade, "phase2_ready": False}
    report["requested_limits_usd"] = {"margin": str(args.max_margin) if args.max_margin is not None else None,
                                    "exposure": str(args.max_exposure) if args.max_exposure is not None else None,
                                    "transaction_charges": str(args.max_charges) if args.max_charges is not None else None}
    client = None
    journal = LiveJournal(ROOT / ".live-integration") if args.execute_live_trade else None
    try:
        config = load_live_configuration(credential_path)
        with journal if journal else nullcontext():
            client = LiveClient(config, execute=args.execute_live_trade, environment=args.environment)
            context = live_preflight(client, config, report, args.fee_profile, selected_account=args.account)
            if context["phase1_passed"] and all(limit is not None for limit in (args.max_margin, args.max_exposure, args.max_charges)):
                check_limits(context, args.max_margin, args.max_exposure, args.max_charges)
                report["phase2_ready"] = True
            if args.execute_live_trade:
                execute_live(client, config, context, journal, args.max_margin, args.max_exposure, args.fee_profile, report,
                             max_charges=args.max_charges)
            report["passed"] = bool(context["phase1_passed"])
    except (Exception, KeyboardInterrupt) as exc:
        report.update(passed=False, error_type=type(exc).__name__)
        if isinstance(exc, IntegrationError):
            report["failure"] = str(exc)
        if client is not None and journal is not None and journal.data is not None and journal.data.get("stage") not in journal.TERMINAL:
            reconcile_remaining(client, journal, report)
    finally:
        if client:
            report.update(requests=client.calls, authentication_attempts=client.auth_attempts,
                          open_attempts=client.open_attempts, close_attempts=client.close_attempts)
            client.close()
        else:
            report.update(requests=[], authentication_attempts=0, open_attempts=0, close_attempts=0)
        report["env_unchanged"] = (hashlib.sha256(credential_path.read_bytes()).digest() if credential_path.exists() else None) == original
        report["passed"] = bool(report.get("passed") and report["env_unchanged"])
        private_json(ROOT / ".live-integration" / ("trade-results.json" if args.execute_live_trade else "preflight.json"), report)
        print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Legacy single-BUY verification helpers retained for diagnostic compatibility."""
import hashlib
import math
import time
from trading_bot.config.trading_config import DEMO_URL
from trading_bot.risk.live_risk import IntegrationError, require, number, validate_market
from trading_bot.risk.confirmations import identifier, positions, confirm

EPIC = "EURUSD"

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

"""Opt-in, bounded DEMO verification of the Capital.com API; never selects LIVE.

Run read-only first. --execute-demo explicitly permits one minimum-size EURUSD
position and one short-lived working order, plus an isolated temporary watchlist.
A durable private journal blocks new runs after uncertainty or incomplete cleanup.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit
from unittest.mock import patch
import uuid

import requests
from trading_bot.api.client import TradingApi
from trading_bot.api.network_errors import TradingUnavailable
from trading_bot.api.websocket_client import CapitalWebSocketClient, StreamError
from trading_bot.config.paths import ROOT
from trading_bot.config.trading_config import DEMO_URL
from trading_bot.risk.confirmations import identifier
from trading_bot.risk.live_risk import IntegrationError, require, validate_market


class DemoVerificationTransport:
    """Check final prepared host and exact one-shot mutation intent before sending."""

    def __init__(self, execute=False):
        self.execute = execute
        self.calls = []
        self.auth_count = 0
        self.original_send = requests.sessions.Session.send
        self._intent = None

    @contextmanager
    def intent(self, method, path, payload=None):
        require(self.execute and self._intent is None, "demo_mutation_not_authorized")
        self._intent = (method, "/api/v1" + path, payload)
        try:
            yield
        finally:
            self._intent = None

    def send(self, session, request, **kwargs):
        url = urlsplit(request.url)
        require(
            url.scheme == "https"
            and url.netloc == urlsplit(DEMO_URL).netloc
            and not url.fragment
            and url.path.startswith("/api/v1/"),
            "demo_host_required",
        )
        require(len(self.calls) < 180, "demo_verification_request_budget")
        method, path = request.method, url.path
        if method == "POST" and path == "/api/v1/session":
            require(self.auth_count == 0, "demo_authentication_retry_forbidden")
            self.auth_count += 1
        elif method == "GET":
            pass
        elif method == "DELETE" and path == "/api/v1/session":
            pass
        else:
            body = json.loads(request.body) if request.body else None
            require(
                self.execute and self._intent == (method, path, body) and not url.query,
                "demo_mutation_does_not_match_durable_intent",
            )
            self._intent = None  # A timeout cannot leave this intent reusable.
        kwargs.update(allow_redirects=False, verify=True, timeout=(5, 20))
        record = {"method": method, "path": path}
        self.calls.append(record)
        response = self.original_send(session, request, **kwargs)
        record["status"] = response.status_code
        return response

    def __enter__(self):
        original_prepare = requests.sessions.Session.prepare_request

        def prepare(session, request):
            session.trust_env = (
                False  # Ignore proxy/netrc environment for broker credentials.
            )
            return original_prepare(session, request)

        transport = self

        def send(session, request, **kwargs):
            return transport.send(session, request, **kwargs)

        self._prepare = patch.object(
            requests.sessions.Session, "prepare_request", prepare
        )
        self._send = patch.object(requests.sessions.Session, "send", send)
        self._prepare.start()
        self._send.start()
        return self

    def __exit__(self, *args):
        self._send.stop()
        self._prepare.stop()


class VerificationJournal:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "state.json"
        self.data = {
            "complete": False,
            "operations": [],
            "position": None,
            "order": None,
            "watchlist": None,
        }

    def __enter__(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.lock = (self.directory / "lock").open("a+")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if self.path.exists():
            previous = json.loads(self.path.read_text())
            if previous.get("complete") is not True:
                self.lock.close()
                raise IntegrationError("prior_api_verification_requires_reconciliation")
        self.save()
        return self

    def save(self):
        with tempfile.NamedTemporaryFile(
            mode="w", dir=self.directory, delete=False
        ) as stream:
            name = stream.name
            os.chmod(name, 0o600)
            json.dump(self.data, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, self.path)
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def __exit__(self, *args):
        self.save()
        self.lock.close()


def confirmation(api, reference):
    identifier(reference)
    for attempt in range(5):
        if attempt:
            time.sleep(1.1)
        value = api.getPositionOrderConfirmation(reference)
        if value is None or value.get("dealStatus") in ("PENDING", "UNKNOWN"):
            continue
        require(
            value.get("dealReference") == reference,
            "demo_confirmation_reference_mismatch",
        )
        require(value.get("dealStatus") == "ACCEPTED", "demo_confirmation_not_accepted")
        return value
    raise IntegrationError("demo_confirmation_uncertain_no_retry")


def run_demo_verification(
    *, execute=False, encrypted_login=False, report_path=None, journal_dir=None
):
    report_path = Path(report_path or ROOT / "docs/demo-api-verification.json")
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "environment": "DEMO",
        "live_requests": 0,
        "operations": {},
        "trades": [],
        "mutation_log": [],
        "unresolved_exposure": [],
        "limitations": [],
    }
    api = None
    with DemoVerificationTransport(execute) as transport:
        try:
            api = TradingApi(environment="demo", auto_authenticate=False)
            require(api.BASE_URL == DEMO_URL, "demo_host_required")
            require(
                api.createNewSession(encryptedPassword=encrypted_login) is not None,
                "demo_authentication_failed",
            )
            report["operations"]["createNewSession"] = "DEMO-verified"
            report["login_mode"] = "encrypted" if encrypted_login else "plain"
            account = api.verify_account_environment()
            report["operations"].update(
                getSessionDetails="DEMO-verified", getAllAccounts="DEMO-verified"
            )
            account_id = account["accountId"]

            def probe(name, fn):
                # A failed read latches TradingApi. Don't keep sending after failure.
                value = fn()
                report["operations"][name] = "DEMO-verified"
                return value

            probe("getServerTime", api.getServerTime)
            probe("pingService", api.pingService)
            probe("getEncryptionKey", api.getEncryptionKey)
            probe("getAccountPreferences", api.getAccountPreferences)
            probe(
                "getAccountActivityHistory",
                lambda: api.getAccountActivityHistory(lastPeriod=600, detailed=True),
            )
            probe(
                "getAccountTransactionsHistory",
                lambda: api.getAccountTransactionsHistory(lastPeriod=600),
            )
            nodes = probe(
                "getAllTopLevelMarketCategories", api.getAllTopLevelMarketCategories
            )
            if nodes.get("nodes"):
                probe(
                    "getAllCategorySubNodes",
                    lambda: api.getAllCategorySubNodes(
                        str(nodes["nodes"][0]["id"]), limit=2
                    ),
                )
            probe("getAllMarkets", api.getAllMarkets)
            probe("searchMarkets", lambda: api.searchMarkets("EURUSD"))
            probe(
                "getMarketsDetails", lambda: api.getMarketsDetails(["EURUSD", "GOLD"])
            )
            market = probe(
                "getSingleMarketDetails", lambda: api.getSingleMarketDetails("EURUSD")
            )
            probe(
                "getHistoricalPrices",
                lambda: api.getHistoricalPrices("EURUSD", "MINUTE", 10),
            )
            probe(
                "getClientSentimentForMarkets",
                lambda: api.getClientSentimentForMarkets(["EURUSD"]),
            )
            probe(
                "getClientSentimentForMarket",
                lambda: api.getClientSentimentForMarket("EURUSD"),
            )
            watchlists = probe("getAllWatchlists", api.getAllWatchlists)
            if watchlists.get("watchlists"):
                probe(
                    "getSingleWatchlist",
                    lambda: api.getSingleWatchlist(watchlists["watchlists"][0]["id"]),
                )
            baseline, _ = probe("getAllPositions", api.getAllPositions)
            working = probe("getAllWorkingOrders", api.getAllWorkingOrders)
            try:
                with CapitalWebSocketClient(api, max_reconnects=0) as stream:
                    stream.subscribe_quotes(["EURUSD"])
                    report["operations"]["marketData.subscribe"] = "DEMO-verified"
                    stream.subscribe_ohlc(["EURUSD"], ["MINUTE"], "classic")
                    report["operations"]["OHLCMarketData.subscribe"] = "DEMO-verified"
                    stream.ping()
                    report["operations"]["websocket.ping"] = "DEMO-verified"
                    seen = set()
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        event = stream.receive(min(5, deadline - time.monotonic()))
                        if event:
                            seen.add(event["destination"])
                        if {"quote", "ohlc.event"} <= seen:
                            break
                    report["stream_events"] = sorted(seen)
                    stream.unsubscribe_ohlc(["EURUSD"])
                    report["operations"]["OHLCMarketData.unsubscribe"] = "DEMO-verified"
                    stream.unsubscribe_quotes(["EURUSD"])
                    report["operations"]["marketData.unsubscribe"] = "DEMO-verified"
            except (StreamError, OSError) as exc:
                report["limitations"].append("websocket_" + type(exc).__name__)

            if execute:
                minimum, summary = validate_market(market)
                require(
                    summary["tradeable"] and minimum <= 100,
                    "demo_market_not_suitable_for_minimum_test",
                )
                require(
                    not any(
                        x["market"]["epic"] == "EURUSD" for x in baseline["positions"]
                    )
                    and not any(
                        x["workingOrderData"]["epic"] == "EURUSD"
                        for x in working["workingOrders"]
                    ),
                    "existing_eurusd_exposure_blocks_test",
                )
                require(
                    float(account["balance"]["available"])
                    > float(minimum) * float(market["snapshot"]["offer"]),
                    "insufficient_demo_available_funds",
                )
                with VerificationJournal(
                    journal_dir or ROOT / ".demo-integration/api-verification"
                ) as journal:

                    def mutate(name, method, path, payload, fn):
                        require(
                            api.BASE_URL == DEMO_URL
                            and api.verify_account_environment()["accountId"]
                            == account_id,
                            "demo_account_changed",
                        )
                        record = {"operation": name, "stage": "submitting"}
                        journal.data["operations"].append(record)
                        report["mutation_log"].append(record)
                        journal.save()  # durable before touching the network
                        with transport.intent(method, path, payload):
                            result = fn()
                        value = result[0] if isinstance(result, tuple) else result
                        if "dealReference" in value:
                            record["reference"] = value["dealReference"]
                            journal.save()
                            confirmed = confirmation(api, value["dealReference"])
                            report["operations"]["getPositionOrderConfirmation"] = (
                                "DEMO-verified"
                            )
                        else:
                            confirmed = value
                        record["stage"] = "confirmed"
                        journal.save()
                        report["operations"][name] = "DEMO-verified"
                        return confirmed

                    def owned_position():
                        deal = journal.data["position"]
                        value = api.getsinglePosition(deal)
                        require(
                            value["position"]["dealId"] == deal
                            and value["market"]["epic"] == "EURUSD"
                            and value["position"]["direction"] == "BUY"
                            and Decimal(str(value["position"]["size"])) == minimum,
                            "demo_owned_position_changed",
                        )
                        return value

                    def owned_order():
                        deal = journal.data["order"]
                        rows = api.getAllWorkingOrders()["workingOrders"]
                        matches = [
                            x for x in rows if x["workingOrderData"]["dealId"] == deal
                        ]
                        require(
                            len(matches) == 1
                            and matches[0]["workingOrderData"]["epic"] == "EURUSD"
                            and matches[0]["workingOrderData"]["direction"] == "BUY"
                            and Decimal(
                                str(matches[0]["workingOrderData"]["orderSize"])
                            )
                            == minimum,
                            "demo_owned_order_changed_or_filled",
                        )

                    trade = {
                        "instrument": "EURUSD",
                        "direction": "BUY",
                        "size": str(minimum),
                        "execution_status": "not_submitted",
                        "closure_status": "not_opened",
                    }
                    report["trades"].append(trade)
                    try:
                        open_payload = {
                            "epic": "EURUSD",
                            "direction": "BUY",
                            "size": float(minimum),
                            "guaranteedStop": False,
                        }
                        opened = mutate(
                            "createPosition",
                            "POST",
                            "/positions",
                            open_payload,
                            lambda: api.createPosition("EURUSD", "BUY", float(minimum)),
                        )
                        affected = opened.get("affectedDeals")
                        require(
                            opened.get("epic") == "EURUSD"
                            and opened.get("direction") == "BUY"
                            and Decimal(str(opened.get("size"))) == minimum
                            and isinstance(affected, list)
                            and len(affected) == 1
                            and affected[0].get("status") == "OPENED",
                            "demo_new_position_ownership_unproven",
                        )
                        deal = identifier(affected[0]["dealId"])
                        require(
                            deal
                            not in {
                                x["position"]["dealId"] for x in baseline["positions"]
                            },
                            "demo_position_preexisted",
                        )
                        journal.data["position"] = deal
                        journal.save()
                        trade.update(
                            execution_status="ACCEPTED",
                            deal_id=deal,
                            closure_status="open",
                        )
                        owned_position()
                        report["operations"]["getsinglePosition"] = "DEMO-verified"
                        # Modest protective stop; confirm it before closing this test position.
                        stop = 0.01
                        grant = api.authorize_mutation(
                            "position-update", resource_id=deal
                        )
                        mutate(
                            "updatePosition",
                            "PUT",
                            "/positions/" + deal,
                            {"stopDistance": stop},
                            lambda: api.updatePosition(
                                deal, stopDistance=stop, authorization=grant
                            ),
                        )
                        owned_position()
                    finally:
                        if journal.data["position"] and api._unavailable is None:
                            deal = journal.data["position"]
                            owned_position()
                            mutate(
                                "closePosition",
                                "DELETE",
                                "/positions/" + deal,
                                None,
                                lambda: api.closePosition(deal),
                            )
                            rows, _ = api.getAllPositions()
                            require(
                                deal
                                not in {
                                    x["position"]["dealId"] for x in rows["positions"]
                                },
                                "demo_position_closure_unverified",
                            )
                            journal.data["position"] = None
                            journal.save()
                            trade["closure_status"] = "confirmed_closed"

                    order_report = {
                        "instrument": "EURUSD",
                        "direction": "BUY",
                        "size": str(minimum),
                        "type": "LIMIT",
                        "execution_status": "not_submitted",
                        "cancellation_status": "not_created",
                    }
                    report["trades"].append(order_report)
                    try:
                        latest = api.getSingleMarketDetails("EURUSD")["snapshot"]
                        level = round(float(latest["bid"]) * 0.90, 5)
                        expiry = (
                            datetime.now(timezone.utc) + timedelta(minutes=3)
                        ).strftime("%Y-%m-%dT%H:%M:%S")
                        payload = {
                            "epic": "EURUSD",
                            "direction": "BUY",
                            "size": float(minimum),
                            "level": level,
                            "type": "LIMIT",
                            "goodTillDate": expiry,
                        }
                        grant = api.authorize_mutation("working-order")
                        opened = mutate(
                            "createWorkingOrder",
                            "POST",
                            "/workingorders",
                            payload,
                            lambda: api.createWorkingOrder(
                                "EURUSD",
                                "BUY",
                                float(minimum),
                                level,
                                "LIMIT",
                                goodTillDate=expiry,
                                authorization=grant,
                            ),
                        )
                        deal = identifier(opened["dealId"])
                        journal.data["order"] = deal
                        journal.save()
                        order_report.update(
                            execution_status="ACCEPTED",
                            deal_id=deal,
                            cancellation_status="pending",
                            good_till=expiry,
                        )
                        owned_order()
                        changed = round(level - 0.001, 5)
                        grant = api.authorize_mutation(
                            "working-order", resource_id=deal
                        )
                        mutate(
                            "updateWorkingOrder",
                            "PUT",
                            "/workingorders/" + deal,
                            {"level": changed},
                            lambda: api.updateWorkingOrder(
                                deal, level=changed, authorization=grant
                            ),
                        )
                        owned_order()
                    finally:
                        if journal.data["order"] and api._unavailable is None:
                            deal = journal.data["order"]
                            owned_order()
                            grant = api.authorize_mutation(
                                "working-order", resource_id=deal
                            )
                            mutate(
                                "deleteWorkingOrder",
                                "DELETE",
                                "/workingorders/" + deal,
                                None,
                                lambda: api.deleteWorkingOrder(
                                    deal, authorization=grant
                                ),
                            )
                            require(
                                deal
                                not in {
                                    x["workingOrderData"]["dealId"]
                                    for x in api.getAllWorkingOrders()["workingOrders"]
                                },
                                "demo_order_cancellation_unverified",
                            )
                            journal.data["order"] = None
                            journal.save()
                            order_report["cancellation_status"] = "confirmed_cancelled"
                    try:
                        name = "API test " + uuid.uuid4().hex[:8]
                        grant = api.authorize_mutation("watchlist")
                        result = mutate(
                            "createWatchlist",
                            "POST",
                            "/watchlists",
                            {"name": name, "epics": ["EURUSD"]},
                            lambda: api.createWatchlist(
                                name, ["EURUSD"], authorization=grant
                            ),
                        )
                        watch = identifier(result["watchlistId"])
                        journal.data["watchlist"] = watch
                        journal.save()
                        probe(
                            "getSingleWatchlist", lambda: api.getSingleWatchlist(watch)
                        )
                        mutate(
                            "addMarketToWatchlist",
                            "PUT",
                            "/watchlists/" + watch,
                            {"epic": "GOLD"},
                            lambda: api.addMarketToWatchlist(
                                watch, "GOLD", authorization=grant
                            ),
                        )
                        mutate(
                            "removeMarketFromWatchlist",
                            "DELETE",
                            "/watchlists/" + watch + "/GOLD",
                            None,
                            lambda: api.removeMarketFromWatchlist(
                                watch, "GOLD", authorization=grant
                            ),
                        )
                    finally:
                        if journal.data["watchlist"] and api._unavailable is None:
                            watch = journal.data["watchlist"]
                            grant = api.authorize_mutation("watchlist")
                            mutate(
                                "deleteWatchlist",
                                "DELETE",
                                "/watchlists/" + watch,
                                None,
                                lambda: api.deleteWatchlist(watch, authorization=grant),
                            )
                            journal.data["watchlist"] = None
                            journal.save()
                    require(
                        not any(
                            journal.data[x] for x in ("position", "order", "watchlist")
                        ),
                        "demo_cleanup_incomplete",
                    )
                    journal.data["complete"] = True
                    journal.save()
                final_positions, _ = api.getAllPositions()
                final_orders = api.getAllWorkingOrders()
                report["baseline_positions_preserved"] = {
                    x["position"]["dealId"] for x in baseline["positions"]
                } <= {x["position"]["dealId"] for x in final_positions["positions"]}
                report["baseline_orders_preserved"] = {
                    x["workingOrderData"]["dealId"] for x in working["workingOrders"]
                } <= {
                    x["workingOrderData"]["dealId"]
                    for x in final_orders["workingOrders"]
                }
                require(
                    report["baseline_positions_preserved"]
                    and report["baseline_orders_preserved"],
                    "baseline_exposure_changed_during_verification",
                )
            report["passed"] = True
        except (Exception, TradingUnavailable) as exc:
            report["passed"] = False
            # Only fixed internal codes / categories; never arbitrary exception text.
            report["error_type"] = type(exc).__name__
            if isinstance(exc, TradingUnavailable):
                report["error_category"] = exc.reason
            if isinstance(exc, IntegrationError):
                report["error_category"] = str(exc)
        finally:
            if api is not None and api.cst and api._unavailable is None:
                try:
                    api.logOutOfTheCurrentSession()
                    report["operations"]["logOutOfTheCurrentSession"] = "DEMO-verified"
                except (Exception, TradingUnavailable):
                    report["limitations"].append("logout_unverified")
            for item in report["trades"]:
                if item.get("execution_status") == "ACCEPTED" and (
                    item.get("closure_status") == "open"
                    or item.get("cancellation_status") == "pending"
                ):
                    report["unresolved_exposure"].append(item)
            for record in report["mutation_log"]:
                if record["stage"] == "submitting":
                    record["stage"] = "uncertain"
                    report["unresolved_exposure"].append(
                        {
                            "operation": record["operation"],
                            "outcome": "uncertain_do_not_retry",
                            "reference": record.get("reference"),
                            "private_journal_requires_reconciliation": True,
                        }
                    )
            report["requests"] = transport.calls
            report["limitations"].extend(
                [
                    "account switch, preferences and top-up not exercised: no test requires changing account configuration",
                    "session expiration/reconnect tested offline only",
                ]
            )
            report_path.write_text(json.dumps(report, indent=2) + "\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--encrypted-login",
        action="store_true",
        help="Use the documented encrypted password login",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Output report path; use a separate path to preserve a trading report",
    )
    parser.add_argument(
        "--execute-demo",
        action="store_true",
        help="Explicitly authorize bounded DEMO mutations and cleanup",
    )
    args = parser.parse_args(argv)
    report = run_demo_verification(
        execute=args.execute_demo,
        encrypted_login=args.encrypted_login,
        report_path=args.report,
    )
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] and not report["unresolved_exposure"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

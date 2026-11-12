from contextlib import redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
import importlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from urllib.parse import urlsplit

import requests
import socket
import test_api_key as entry
from scripts import integration_roundtrip as workflow
from scripts import trading_config as config
from scripts.live_risk import IntegrationError, fingerprint


class BuySellIntegrationTests(unittest.TestCase):
    """Exercise the actual CLI, TradingApi and prepared-request guard against fake HTTP."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.credentials = 'X-CAP-API-KEY=fixture-key\nidentifier=fixture-login\npassword=fixture-password\n'
        (self.root / ".env").write_text(self.credentials)
        (self.root / ".env").chmod(0o600)
        self.environment = "demo"
        self.symbol, self.size = "EURUSD", 100
        self.open_positions = {}
        self.working_orders = []
        self.actions = []
        self.events = []
        self.buy_close_confirmed = self.buy_closure_verified = False
        self.closed = []
        self.fail_close = None
        self.fail_open = None
        self.rejected = None
        self.confirm_unresolved = False
        self.login_status = 200
        self.missing_price = None
        self.settlement_missing = False
        self.extra_during_buy = False
        self.closed_market = False
        self.available = 1000
        self.hedging = True
        self.missing_stop = False
        self.balance_after_buy = 1000
        self.http = Mock(side_effect=self.respond)

    def account(self):
        return {"accountId": "test-account-1234", "accountType": "CFD", "status": "ENABLED",
                "demo": self.environment == "demo", "currency": "USD",
                "balance": {"balance": self.balance_after_buy if "buy-owned" in self.closed else 1000,
                            "available": self.available, "profitLoss": 0}}

    def market(self):
        return {"instrument": {"epic": self.symbol, "type": "CURRENCIES", "currency": "USD", "lotSize": 1,
                               "marginFactor": 3.333333, "marginFactorUnit": "PERCENTAGE",
                               "overnightFee": {"longRate": -0.01, "shortRate": 0.001,
                                                "swapChargeTimestamp": int((time.time() + 3600) * 1000)}},
                "snapshot": {"marketStatus": "CLOSED" if self.closed_market else "TRADEABLE",
                             "marketModes": ["REGULAR"], "bid": 1.0998, "offer": 1.1,
                             "scalingFactor": 1, "delayTime": 0},
                "dealingRules": {"minDealSize": {"value": 100, "unit": "POINTS"},
                                 "maxDealSize": {"value": 1000000, "unit": "POINTS"},
                                 "minSizeIncrement": {"value": 100, "unit": "POINTS"},
                                 "minStopOrProfitDistance": {"value": 0.0001, "unit": "POINTS"},
                                 "maxStopOrProfitDistance": {"value": 0.1, "unit": "POINTS"},
                                 "marketOrderPreference": "AVAILABLE_DEFAULT_ON"}}

    def reply(self, request, value, status=200):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(value).encode()
        response.headers.update({"Content-Type": "application/json", "CST": "fixture-cst", "X-SECURITY-TOKEN": "fixture-token"})
        response.request = request
        return response

    def respond(self, session, request, **kwargs):
        url = urlsplit(request.url)
        self.assertEqual(url.hostname, urlsplit(config.endpoint(self.environment)).hostname)
        self.assertIs(kwargs["verify"], True)
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertEqual(kwargs["timeout"], (5, 20))
        self.events.append((request.method, url.path))
        method, path = request.method, url.path
        if path == "/api/v1/session":
            if method == "POST":
                self.assertEqual(json.loads(request.body)["password"], "fixture-password")
                return self.reply(request, {"hasActiveDemoAccounts": True, "hasActiveLiveAccounts": True,
                                            "currentAccountId": "test-account-1234"}, self.login_status)
            return self.reply(request, {"accountId": "test-account-1234"})
        if path == "/api/v1/accounts":
            return self.reply(request, {"accounts": [self.account()]})
        if path == "/api/v1/accounts/preferences":
            return self.reply(request, {"hedgingMode": self.hedging, "leverages": {"CURRENCIES": {"current": 30}}})
        if path == "/api/v1/markets/" + self.symbol:
            return self.reply(request, self.market())
        if path == "/api/v1/workingorders":
            return self.reply(request, {"workingOrders": list(self.working_orders)})
        if method == "POST" and path == "/api/v1/positions":
            payload = json.loads(request.body)
            direction = payload["direction"]
            self.actions.append(("OPEN", direction, payload["size"]))
            state = json.loads((self.root / (".live-integration" if self.environment == "live" else ".demo-integration") / "trade.json").read_text())
            self.assertEqual((state["stage"], state["direction"]), ("open_submitting", direction))
            self.assertIs(state["hedging_mode"], self.hedging)
            if self.hedging is False:
                # Flat normal-mode accounts can open a new position in either
                # direction. Existing target exposure would be unsafe to net.
                self.assertFalse(any(p["market"]["epic"] == self.symbol for p in self.open_positions.values()))
            if self.fail_open == direction:
                raise requests.Timeout("private-response-must-not-appear")
            if direction == "SELL":
                self.assertTrue(self.buy_close_confirmed)
                self.assertTrue(self.buy_closure_verified)
                self.assertNotIn("buy-owned", self.open_positions)
            if self.rejected != direction:
                level = 1.1 if direction == "BUY" else 1.101
                position = {"dealId": direction.lower() + "-owned", "direction": direction, "size": payload["size"],
                            "level": level, "stopLevel": 1.099 if direction == "BUY" else 1.102}
                if self.missing_price == "open":
                    position.pop("level")
                if self.missing_stop:
                    position.pop("stopLevel")
                self.open_positions[position["dealId"]] = {"position": position, "market": {"epic": self.symbol}}
                if self.extra_during_buy and direction == "BUY":
                    self.open_positions["external"] = {"position": {"dealId": "external", "size": 1, "direction": "BUY"},
                                                        "market": {"epic": "GOLD"}}
            return self.reply(request, {"dealReference": "o_" + direction.lower()})
        if path.startswith("/api/v1/confirms/"):
            reference = path.rsplit("/", 1)[-1]
            side = reference.split("_", 1)[1].upper()
            if reference.startswith("o_"):
                if self.confirm_unresolved:
                    return self.reply(request, {"dealStatus": "PENDING"})
                value = {"dealReference": reference, "dealStatus": "REJECTED" if self.rejected == side else "ACCEPTED",
                         "epic": self.symbol, "direction": side, "size": self.size, "level": 1.1 if side == "BUY" else 1.101,
                         "dealId": "order-id-" + side.lower(),
                         "affectedDeals": [{"dealId": side.lower() + "-owned", "status": "OPENED"}]}
                if self.missing_price == "open":
                    value.pop("level")
                return self.reply(request, value)
            if side == "BUY":
                self.buy_close_confirmed = True
            value = {"dealReference": reference, "dealStatus": "ACCEPTED", "epic": self.symbol,
                     "dealId": "close-record-" + side.lower(), "level": 1.1002 if side == "BUY" else 1.1007,
                     "status": "CLOSED", "direction": "SELL" if side == "BUY" else "BUY", "size": self.size,
                     "affectedDeals": [{"dealId": side.lower() + "-owned", "status": "FULLY_CLOSED"}]}
            if self.missing_price == "close":
                value.pop("level")
            return self.reply(request, value)
        if path.startswith("/api/v1/positions/"):
            deal = path.rsplit("/", 1)[-1]
            if method == "DELETE":
                self.actions.append(("CLOSE", deal))
                if self.fail_close == deal.split("-", 1)[0].upper():
                    raise requests.Timeout("private-close-text")
                self.closed.append(deal)
                self.open_positions.pop(deal)
                return self.reply(request, {"dealReference": "p_" + deal.split("-", 1)[0]})
            return self.reply(request, self.open_positions.get(deal), 200 if deal in self.open_positions else 404)
        if path == "/api/v1/positions":
            if self.buy_close_confirmed and "buy-owned" not in self.open_positions:
                self.buy_closure_verified = True
            return self.reply(request, {"positions": list(self.open_positions.values())})
        if path == "/api/v1/history/transactions":
            rows = []
            if not self.settlement_missing:
                for deal in self.closed:
                    side = deal.split("-", 1)[0]
                    rows.extend([{"reference": "close-record-" + side, "transactionType": "TRADE", "size": "0.02" if side == "buy" else "0.03",
                                  "currency": "USD", "status": "PROCESSED"},
                                 {"reference": "close-record-" + side, "transactionType": "TRADE_COMMISSION", "size": "-0.001",
                                  "currency": "USD", "status": "PROCESSED"}])
                rows.append({"reference": "unrelated", "transactionType": "TRADE", "size": "9999", "currency": "USD", "status": "PROCESSED"})
            return self.reply(request, {"transactions": rows})
        raise AssertionError("Unlisted mocked HTTP route")

    def write_fees(self):
        path = self.root / ".live-integration/fees.json"
        path.parent.mkdir(exist_ok=True)
        profile = {"verified_for_this_account": True, "account_fingerprint": fingerprint("test-account-1234"),
                   "currency": "USD", "legal_entity": "Reviewed legal entity", "client_classification": "retail",
                   "instrument": self.symbol, "position_size": self.size, "commission_per_side": "0.001",
                   "conversion_fee_percent": "0", "other_transaction_charges_roundtrip": "0",
                   "reviewed_at": datetime.now(timezone.utc).date().isoformat(),
                   "source_url": "https://capital.com/en-eu/ways-to-trade/pricing",
                   "strategy_holding_costs_verified": True, "strategy_funding_allowance_usd": "0.1"}
        path.write_text(json.dumps(profile))
        path.chmod(0o600)

    def run_main(self, flags=(), *, tty=True, answer="LIVE"):
        self.environment = config.integration_environment(flags)
        output = io.StringIO()
        with patch.object(entry, "ROOT", self.root), patch.object(config, "ROOT", self.root), \
             patch.object(requests.sessions.Session, "send", autospec=True, side_effect=self.http), \
             patch.object(workflow.time, "sleep"), patch("sys.stdin.isatty", return_value=tty), \
             patch("builtins.input", return_value=answer) as prompt, redirect_stdout(output):
            code = entry.main(list(flags))
        path = self.root / (".live-integration" if self.environment == "live" else ".demo-integration") / "buy-sell-results.json"
        report = json.loads(path.read_text())
        self.assertEqual((self.root / ".env").read_text(), self.credentials)
        for secret in ("fixture-key", "fixture-password", "fixture-cst", "fixture-token"):
            self.assertNotIn(secret, output.getvalue())
            self.assertNotIn(secret, path.read_text())
        return code, report, prompt, output.getvalue()

    def test_default_demo_executes_one_buy_and_one_sell_then_verifies_both_closed(self):
        code, report, prompt, output = self.run_main()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.actions, [("OPEN", "BUY", 100), ("CLOSE", "buy-owned"),
                                       ("OPEN", "SELL", 100), ("CLOSE", "sell-owned")])
        self.assertEqual((report["environment"], report["symbol"], report["size"]), ("DEMO", "EURUSD", "100"))
        self.assertEqual(report["roundtrips"]["BUY"]["opening_price"], "1.1")
        self.assertEqual(report["roundtrips"]["BUY"]["closing_price"], "1.1002")
        self.assertEqual(report["roundtrips"]["SELL"]["opening_price"], "1.101")
        self.assertEqual(report["roundtrips"]["SELL"]["closing_price"], "1.1007")
        self.assertEqual(report["realized_profit_loss"], "0.05")
        self.assertEqual(report["roundtrips"]["SELL"]["reported_fee_cashflows"], {"TRADE_COMMISSION": "-0.001"})
        self.assertEqual(report["remaining_test_positions"], [])
        self.assertEqual((report["open_attempts"], report["close_attempts"]), (2, 2))
        prompt.assert_not_called()
        self.assertIn("Overall: PASS", output)
        self.assertEqual(self.open_positions, {})

    def test_readonly_dns_failure_reports_category_without_orders_or_traceback(self):
        self.http.side_effect = requests.ConnectionError(socket.gaierror(socket.EAI_AGAIN, "private-marker"))
        code, report, _, output = self.run_main(["--read-only"])
        self.assertEqual(code, 1)
        self.assertEqual(report["failure_category"], "dns_resolution_failed")
        self.assertEqual((report["authentication_attempts"], report["open_attempts"], report["close_attempts"]), (1, 0, 0))
        self.http.assert_called_once()
        self.assertIn("DNS resolution failed", output)
        self.assertNotIn("private-marker", output)
        self.assertNotIn("Traceback", output)

    def test_readonly_http_401_reports_auth_failure_without_orders(self):
        self.login_status = 401
        code, report, _, output = self.run_main(["--read-only"])
        self.assertEqual(code, 1)
        self.assertEqual(report["failure_category"], "authentication_failed_http_401")
        self.assertIn("HTTP 401", output)
        self.assertEqual(self.actions, [])
        self.http.assert_called_once()

    def test_live_and_legacy_alias_use_live_host_and_one_terminal_confirmation(self):
        for flag in ("--REAL", "--real"):
            with self.subTest(flag=flag):
                self.write_fees()
                code, report, prompt, output = self.run_main([flag])
                self.assertEqual(code, 0, output)
                self.assertEqual(report["environment"], "LIVE")
                self.assertEqual(report["open_attempts"], 2)
                prompt.assert_called_once()
                self.assertIn("REAL MONEY", output)
                # A subsequent explicit user run is allowed only after the prior journal is verified closed.
                self.actions.clear()
                self.closed.clear()
                self.buy_close_confirmed = self.buy_closure_verified = False

    def test_optional_symbol_and_size_overrides_are_not_rounded_or_replaced(self):
        self.symbol, self.size = "AUDUSD", 200
        code, report, _, output = self.run_main(["--symbol", "AUDUSD", "--size", "200"])
        self.assertEqual(code, 0, output)
        self.assertEqual((report["symbol"], Decimal(report["size"])), ("AUDUSD", 200))
        self.assertEqual([a[2] for a in self.actions if a[0] == "OPEN"], [200, 200])

    def test_supported_live_overrides_use_actual_size_for_risk_and_verified_fee_scope(self):
        self.symbol, self.size = "AUDUSD", 200
        self.write_fees()
        code, report, prompt, output = self.run_main(["--REAL", "--symbol", "AUDUSD", "--size", "200",
                                                    "--max-margin", "10", "--max-exposure", "250"])
        self.assertEqual(code, 0, output)
        prompt.assert_called_once()
        self.assertEqual([a[2] for a in self.actions if a[0] == "OPEN"], [200, 200])
        self.assertEqual(report["roundtrips"]["BUY"]["estimated_costs"]["available_broker_costs"]["estimated_roundtrip_spread_cost"], "0.04000")

    def test_readonly_mode_never_submits_orders(self):
        code, report, prompt, output = self.run_main(["--read-only"])
        self.assertEqual(code, 0, output)
        self.assertEqual(self.actions, [])
        prompt.assert_not_called()
        self.assertFalse((self.root / ".demo-integration/trade.json").exists())

    def test_live_missing_fees_margin_or_confirmation_blocks_all_orders(self):
        self.write_fees()
        (self.root / ".live-integration/fees.json").unlink()
        code, report, prompt, _ = self.run_main(["--REAL"])
        self.assertEqual(code, 1)
        self.assertIn("fee_schedule_unavailable", report["failure"])
        prompt.assert_not_called()
        self.write_fees()
        self.available = 0
        code, report, prompt, _ = self.run_main(["--REAL"])
        self.assertEqual(code, 1)
        prompt.assert_not_called()
        self.available = 1000
        code, report, prompt, _ = self.run_main(["--REAL"], tty=False)
        self.assertEqual(code, 1)
        prompt.assert_not_called()
        code, report, prompt, _ = self.run_main(["--REAL"], answer="no")
        self.assertEqual(code, 1)
        prompt.assert_called_once()
        self.assertEqual(self.actions, [])

    def test_live_authentication_failure_never_falls_back_or_retries(self):
        self.login_status = 401
        code, report, prompt, _ = self.run_main(["--REAL"])
        self.assertEqual(code, 1)
        self.assertEqual(report["authentication_attempts"], 1)
        self.assertEqual(self.actions, [])
        prompt.assert_not_called()

    def test_live_two_roundtrip_budget_stop_and_exposure_limits_block_before_confirmation(self):
        self.write_fees()
        for overrides in (["--max-charges", "0.2"], ["--stop-distance", "0.00001"],
                          ["--max-exposure", "100"], ["--max-loss", "0.01"]):
            with self.subTest(overrides=overrides):
                code, report, prompt, _ = self.run_main(["--REAL", *overrides])
                self.assertEqual(code, 1)
                prompt.assert_not_called()
        self.assertEqual(self.actions, [])

    def test_live_drawdown_after_buy_blocks_sell_after_buy_is_safely_closed(self):
        self.write_fees()
        self.balance_after_buy = 997
        code, report, _, _ = self.run_main(["--REAL"])
        self.assertEqual(code, 1)
        self.assertIn("loss_limit", report["failure"])
        self.assertEqual(self.actions, [("OPEN", "BUY", 100), ("CLOSE", "buy-owned")])
        self.assertEqual(report["remaining_test_positions"], [])

    def test_live_confirmation_expires_without_order_or_second_prompt(self):
        self.write_fees()
        clock = SimpleNamespace(value=0)
        def delayed_input(_):
            clock.value = 61
            return "LIVE"
        with patch.object(workflow.time, "monotonic", side_effect=lambda: clock.value):
            # run_main supplies the terminal mock; this callback advances only the confirmation clock.
            original = workflow.RoundTripTest.approve_live
            def delayed_approval(tester):
                with patch("builtins.input", side_effect=delayed_input) as prompt:
                    try:
                        return original(tester)
                    finally:
                        prompt.assert_called_once()
            with patch.object(workflow.RoundTripTest, "approve_live", delayed_approval):
                code, report, _, _ = self.run_main(["--REAL"])
        self.assertEqual(code, 1)
        self.assertIn("confirmation_expired", report["failure"])
        self.assertEqual(self.actions, [])

    def test_failed_buy_close_stops_sell_and_prominently_reports_exact_remaining_position(self):
        self.fail_close = "BUY"
        code, report, _, output = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.actions, [("OPEN", "BUY", 100), ("CLOSE", "buy-owned")])
        self.assertEqual(report["remaining_test_positions"], ["buy-owned"])
        self.assertIn("POSSIBLE REMAINING TEST EXPOSURE", output)
        self.assertIn("buy-owned", output)
        before = self.http.call_count
        code, report, _, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.http.call_count, before)
        self.assertIn("unresolved_prior", report["failure"])

    def test_failed_sell_close_is_not_retried_or_reported_as_success(self):
        self.fail_close = "SELL"
        code, report, _, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertTrue(report["roundtrips"]["BUY"]["closure_verified"])
        self.assertFalse(report["roundtrips"]["SELL"]["closure_verified"])
        self.assertEqual(report["remaining_test_positions"], ["sell-owned"])
        self.assertEqual(len([a for a in self.actions if a == ("CLOSE", "sell-owned")]), 1)

    def test_failed_live_close_stops_all_mutations_and_reports_live_exposure(self):
        self.write_fees()
        self.fail_close = "BUY"
        code, report, prompt, output = self.run_main(["--REAL"])
        self.assertEqual(code, 1)
        prompt.assert_called_once()
        self.assertEqual(self.actions, [("OPEN", "BUY", 100), ("CLOSE", "buy-owned")])
        self.assertEqual(report["remaining_test_positions"], ["buy-owned"])
        self.assertIn("POSSIBLE REMAINING TEST EXPOSURE", output)

    def test_missing_live_broker_stop_closes_proven_test_position_before_failing(self):
        self.write_fees()
        self.missing_stop = True
        code, report, _, _ = self.run_main(["--REAL"])
        self.assertEqual(code, 1)
        self.assertEqual(self.actions, [("OPEN", "BUY", 100), ("CLOSE", "buy-owned")])
        self.assertTrue(report["roundtrips"]["BUY"]["closure_verified"])
        self.assertEqual(self.open_positions, {})

    def test_uncertain_open_is_not_retried_and_never_leads_to_sell(self):
        self.fail_open = "BUY"
        code, report, _, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.actions, [("OPEN", "BUY", 100)])
        self.assertIn("UNKNOWN", report["remaining_test_positions"])

    def test_interrupt_after_ownership_is_recorded_attempts_one_safe_close_then_stops(self):
        original = workflow.RoundTripJournal.save
        def interrupt_after_open(journal, **changes):
            original(journal, **changes)
            if changes.get("stage") == "opened":
                raise KeyboardInterrupt
        with patch.object(workflow.RoundTripJournal, "save", interrupt_after_open):
            code, report, _, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.actions, [("OPEN", "BUY", 100), ("CLOSE", "buy-owned")])
        self.assertEqual(self.open_positions, {})
        self.assertTrue(report["roundtrips"]["BUY"]["closure_verified"])

    def test_unresolved_confirmation_does_not_guess_position_or_retry_order(self):
        self.confirm_unresolved = True
        code, report, _, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.actions, [("OPEN", "BUY", 100)])
        self.assertEqual(sum(path == "/api/v1/confirms/o_buy" for _, path in self.events), 5)
        self.assertIn("UNKNOWN", report["remaining_test_positions"])

    def test_rejected_buy_prevents_sell_without_attempting_a_close(self):
        self.rejected = "BUY"
        code, report, _, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.actions, [("OPEN", "BUY", 100)])
        self.assertEqual(self.open_positions, {})

    def test_missing_price_still_closes_proven_position_then_reports_failure(self):
        self.missing_price = "open"
        code, report, _, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.actions, [("OPEN", "BUY", 100), ("CLOSE", "buy-owned")])
        self.assertTrue(report["roundtrips"]["BUY"]["closure_verified"])
        self.assertEqual(report["remaining_test_positions"], [])

    def test_missing_close_price_never_claims_complete_success(self):
        self.missing_price = "close"
        code, report, _, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.open_positions, {})
        self.assertTrue(report["roundtrips"]["BUY"]["closure_verified"])
        self.assertEqual(report["open_attempts"], 1)

    def test_missing_settlement_is_unavailable_not_invented_profit(self):
        self.settlement_missing = True
        code, report, _, output = self.run_main()
        self.assertEqual(code, 1)
        self.assertIsNone(report["realized_profit_loss"])
        self.assertEqual(self.open_positions, {})
        self.assertEqual(report["open_attempts"], 1)
        self.assertEqual(report["overall_status"], "INCOMPLETE")
        self.assertEqual(report["execution_status"], "PARTIAL")
        self.assertEqual(report["roundtrips"]["BUY"]["execution_status"], "CLOSED_CONFIRMED")
        self.assertEqual(report["roundtrips"]["SELL"]["execution_status"], "NOT_ATTEMPTED")
        self.assertEqual(report["remaining_account_positions"], [])
        self.assertTrue(report["remaining_positions_verified"])
        self.assertIn("BUY financial reconciliation incomplete; no SELL order sent", output)

    def test_report_only_renders_saved_failure_without_credentials_network_or_overwriting_evidence(self):
        self.settlement_missing = True
        self.run_main()
        path = self.root / ".demo-integration/buy-sell-results.json"
        original = path.read_bytes()
        output = io.StringIO()
        with patch.object(entry, "ROOT", self.root), \
             patch.object(entry, "load_credentials", side_effect=AssertionError("credentials forbidden")) as loader, \
             patch.object(requests.sessions.Session, "send", side_effect=AssertionError("network forbidden")) as send, \
             redirect_stdout(output):
            self.assertEqual(entry.main(["--report-only"]), 1)
        loader.assert_not_called()
        send.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        self.assertIn("BUY execution: CLOSED_CONFIRMED", output.getvalue())
        self.assertIn("Overall: INCOMPLETE", output.getvalue())

    def test_real_transaction_schema_with_numeric_ledger_reference_matches_position_id(self):
        original = self.respond
        def real_schema(session, request, **kwargs):
            if urlsplit(request.url).path == "/api/v1/history/transactions":
                return self.reply(request, {"transactions": [
                    {"reference": "ledger-" + deal, "dealId": deal, "transactionType": "TRADE",
                     "size": "0.0", "currency": "USD", "status": "PROCESSED"} for deal in self.closed]})
            return original(session, request, **kwargs)
        self.http.side_effect = real_schema
        code, report, _, output = self.run_main()
        self.assertEqual(code, 0, output)
        self.assertEqual(Decimal(report["realized_profit_loss"]), 0)
        self.assertEqual(report["execution_status"], "COMPLETE")
        self.assertEqual(report["fee_reconciliation_status"], "UNVERIFIED")
        self.assertEqual((report["open_attempts"], report["close_attempts"]), (2, 2))

    def test_delayed_transaction_reporting_polls_reads_only_and_uses_utc_date_window(self):
        original = self.respond
        windows = []
        def delayed(session, request, **kwargs):
            url = urlsplit(request.url)
            if url.path == "/api/v1/history/transactions":
                from urllib.parse import parse_qs
                windows.append(parse_qs(url.query))
                if len(windows) <= 2:
                    return self.reply(request, {"transactions": []})
            return original(session, request, **kwargs)
        self.http.side_effect = delayed
        code, report, _, output = self.run_main()
        self.assertEqual(code, 0, output)
        self.assertEqual(len(windows), 4)
        self.assertTrue(all(set(window) == {"from", "to"} for window in windows))
        self.assertEqual((report["open_attempts"], report["close_attempts"]), (2, 2))

    def test_stale_foreign_close_confirmation_never_supplies_prices_or_proves_closure(self):
        original = self.respond
        def stale(session, request, **kwargs):
            reply = original(session, request, **kwargs)
            if urlsplit(request.url).path == "/api/v1/confirms/p_buy":
                value = reply.json()
                value.update(level=1.11915, dealId="old-foreign-position", date="2026-11-09T11:28:21.786",
                             affectedDeals=[{"dealId": "old-foreign-position", "status": "FULLY_CLOSED"}])
                return self.reply(request, value)
            return reply
        self.http.side_effect = stale
        code, report, _, output = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(report["failure"], "test_close_confirmation_wrong_position")
        self.assertIsNone(report["roundtrips"]["BUY"]["closing_price"])
        self.assertFalse(report["roundtrips"]["BUY"]["closure_verified"])
        self.assertEqual(report["remaining_account_positions"], [])
        self.assertIn("UNKNOWN", report["remaining_test_positions"])
        self.assertNotIn("1.11915", output)
        self.assertEqual(self.actions, [("OPEN", "BUY", 100), ("CLOSE", "buy-owned")])

    def test_empty_opening_affected_deals_does_not_adopt_order_id_or_close_any_position(self):
        original = self.respond
        def empty(session, request, **kwargs):
            reply = original(session, request, **kwargs)
            if urlsplit(request.url).path == "/api/v1/confirms/o_buy":
                return self.reply(request, {**reply.json(), "affectedDeals": []})
            return reply
        self.http.side_effect = empty
        code, report, _, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(report["failure"], "test_position_ownership_unproven")
        self.assertEqual(self.actions, [("OPEN", "BUY", 100)])
        self.assertEqual(report["remaining_account_positions"], [{"deal_id": "buy-owned", "epic": "EURUSD"}])
        self.assertIn("UNKNOWN", report["remaining_test_positions"])

    def test_concurrent_unrelated_position_is_never_closed(self):
        self.extra_during_buy = True
        code, report, _, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.actions, [("OPEN", "BUY", 100), ("CLOSE", "buy-owned")])
        self.assertEqual(set(self.open_positions), {"external"})
        self.assertEqual(report["remaining_test_positions"], [])
        self.assertEqual(report["remaining_account_positions"], [{"deal_id": "external", "epic": "GOLD"}])

    def test_closed_market_prevents_any_order(self):
        self.closed_market = True
        self.assertEqual(self.run_main()[0], 1)
        self.assertEqual(self.actions, [])

    def test_sequential_roundtrips_support_both_modes_without_updating_preferences(self):
        for mode in (True, False):
            with self.subTest(hedging=mode):
                self.hedging = mode
                code, report, _, output = self.run_main()
                self.assertEqual(code, 0, output)
                self.assertIs(report["hedging_mode"], mode)
                self.assertEqual(self.actions, [("OPEN", "BUY", 100), ("CLOSE", "buy-owned"),
                                               ("OPEN", "SELL", 100), ("CLOSE", "sell-owned")])
                self.assertTrue(all(row["closure_verified"] for row in report["roundtrips"].values()))
                self.assertFalse(any(method == "PUT" for method, _ in self.events))
                self.actions.clear()
                self.closed.clear()
                self.events.clear()
                self.buy_close_confirmed = self.buy_closure_verified = False

    def test_non_hedging_live_uses_same_risk_checks_confirmation_and_exact_closes(self):
        self.hedging = False
        self.write_fees()
        code, report, prompt, output = self.run_main(["--REAL"])
        self.assertEqual(code, 0, output)
        self.assertIs(report["hedging_mode"], False)
        self.assertEqual((report["open_attempts"], report["close_attempts"]), (2, 2))
        prompt.assert_called_once()
        self.assertFalse(any(method == "PUT" for method, _ in self.events))

    def test_unknown_hedging_preference_never_enables_orders(self):
        for value in (None, "false", 0):
            with self.subTest(value=value):
                self.hedging = value
                code, report, _, _ = self.run_main()
                self.assertEqual(code, 1)
                self.assertEqual(report["failure"], "integration_hedging_mode_unknown")
        self.assertEqual(self.actions, [])

    def test_target_positions_in_either_direction_block_both_account_modes(self):
        for mode in (True, False):
            for direction in ("BUY", "SELL"):
                with self.subTest(hedging=mode, direction=direction):
                    self.hedging = mode
                    self.open_positions["external"] = {
                        "position": {"dealId": "external", "direction": direction, "size": 100},
                        "market": {"epic": "EURUSD"}}
                    code, report, _, _ = self.run_main()
                    self.assertEqual(code, 1)
                    self.assertEqual(report["failure"], "existing_symbol_position_blocks_integration_test")
                    self.assertEqual(self.actions, [])
                    self.assertEqual(set(self.open_positions), {"external"})

    def test_target_working_orders_block_both_account_modes_without_cancelling_them(self):
        for mode in (True, False):
            with self.subTest(hedging=mode):
                self.hedging = mode
                self.working_orders[:] = [{"workingOrderData": {"dealId": "external-order", "epic": "EURUSD",
                                                                "direction": "SELL", "orderSize": 100}}]
                code, report, _, _ = self.run_main()
                self.assertEqual(code, 1)
                self.assertEqual(report["failure"], "existing_symbol_working_order_blocks_integration_test")
                self.assertEqual(self.actions, [])
                self.assertEqual(len(self.working_orders), 1)

    def test_final_check_blocks_new_target_exposure_after_preflight(self):
        self.hedging = False
        original = workflow.RoundTripTest.final_entry_check
        def concurrent_position(tester):
            self.open_positions["external"] = {"position": {"dealId": "external", "direction": "SELL", "size": 100},
                                                "market": {"epic": "EURUSD"}}
            return original(tester)
        with patch.object(workflow.RoundTripTest, "final_entry_check", concurrent_position):
            code, report, _, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(report["failure"], "existing_symbol_position_blocks_integration_test")
        self.assertEqual(self.actions, [])

    def test_final_check_blocks_new_target_working_order_after_preflight(self):
        self.hedging = False
        original = workflow.RoundTripTest.final_entry_check
        def concurrent_order(tester):
            self.working_orders.append({"workingOrderData": {"dealId": "external-order", "epic": "EURUSD"}})
            return original(tester)
        with patch.object(workflow.RoundTripTest, "final_entry_check", concurrent_order):
            code, report, _, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(report["failure"], "existing_symbol_working_order_blocks_integration_test")
        self.assertEqual(self.actions, [])

    def test_final_non_hedging_position_recheck_catches_a_working_order_fill(self):
        self.hedging = False
        original = self.respond
        original_check = workflow.RoundTripTest.final_entry_check
        checking = False
        def final_check(tester):
            nonlocal checking
            checking = True
            return original_check(tester)
        def fill_while_reading_orders(session, request, **kwargs):
            response = original(session, request, **kwargs)
            if checking and urlsplit(request.url).path == "/api/v1/workingorders":
                self.open_positions["external"] = {"position": {"dealId": "external", "direction": "SELL", "size": 100},
                                                    "market": {"epic": "EURUSD"}}
            return response
        self.http.side_effect = fill_while_reading_orders
        with patch.object(workflow.RoundTripTest, "final_entry_check", final_check):
            code, report, _, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(report["failure"], "position_appeared_during_non_hedging_entry_check")
        self.assertEqual(self.actions, [])

    def test_mode_change_after_preflight_stops_without_changing_preferences(self):
        self.hedging = False
        original = workflow.RoundTripTest.final_entry_check
        def change_mode(tester):
            self.hedging = True
            return original(tester)
        with patch.object(workflow.RoundTripTest, "final_entry_check", change_mode):
            code, report, _, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(report["failure"], "integration_trading_mode_changed")
        self.assertEqual(self.actions, [])
        self.assertFalse(any(method == "PUT" for method, _ in self.events))

    def test_new_target_position_after_buy_closes_blocks_sell_in_normal_mode(self):
        self.hedging = False
        original = workflow.RoundTripTest.preflight
        def new_position_before_sell(tester, direction):
            if direction == "SELL" and tester.transport.open_attempts:
                self.open_positions["external"] = {"position": {"dealId": "external", "direction": "BUY", "size": 100},
                                                    "market": {"epic": "EURUSD"}}
            return original(tester, direction)
        with patch.object(workflow.RoundTripTest, "preflight", new_position_before_sell):
            code, report, _, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertTrue(report["roundtrips"]["BUY"]["closure_verified"])
        self.assertEqual(self.actions, [("OPEN", "BUY", 100), ("CLOSE", "buy-owned")])
        self.assertEqual(set(self.open_positions), {"external"})

    def test_non_hedging_close_timeout_never_starts_sell_or_retries_delete(self):
        self.hedging = False
        self.fail_close = "BUY"
        code, report, _, output = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.actions, [("OPEN", "BUY", 100), ("CLOSE", "buy-owned")])
        self.assertEqual(report["remaining_test_positions"], ["buy-owned"])
        self.assertIn("POSSIBLE REMAINING TEST EXPOSURE", output)

    def test_non_hedging_http_200_with_netted_confirmation_never_adopts_or_closes_other_position(self):
        self.hedging = False
        original = self.respond
        def netted_confirmation(session, request, **kwargs):
            response = original(session, request, **kwargs)
            if urlsplit(request.url).path == "/api/v1/confirms/o_buy":
                value = response.json()
                value["affectedDeals"] = [{"dealId": "external-position", "status": "AMENDED"}]
                return self.reply(request, value)
            return response
        self.http.side_effect = netted_confirmation
        code, report, _, output = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(report["failure"], "test_position_ownership_unproven")
        self.assertEqual(self.actions, [("OPEN", "BUY", 100)])
        self.assertIn("UNKNOWN", report["remaining_test_positions"])
        self.assertIn("POSSIBLE REMAINING TEST EXPOSURE", output)

    def test_existing_position_blocks_test_before_opening_or_closing_anything(self):
        self.open_positions["external"] = {"position": {"dealId": "external", "direction": "SELL", "size": 100},
                                            "market": {"epic": "EURUSD"}}
        code, report, _, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.actions, [])
        self.assertEqual(set(self.open_positions), {"external"})

    def test_import_never_authenticates_or_trades(self):
        with patch.object(entry, "load_credentials", side_effect=AssertionError("no credentials on import")), \
             patch.object(requests.sessions.Session, "send", side_effect=AssertionError("no HTTP on import")):
            importlib.reload(workflow)


class TwoRoundtripTransportTests(unittest.TestCase):
    def transport(self, *, environment="live", read_only=False):
        args = SimpleNamespace(environment=environment, read_only=read_only, symbol="EURUSD", size=100,
                               stop_distance=Decimal("0.001"))
        transport = workflow.RoundTripTransport(args)
        transport.approved = environment == "demo"
        transport.journal = Mock(data={"stage": "open_submitting", "direction": "BUY"})
        response = requests.Response()
        response.status_code = 200
        response._content = b'{}'
        transport.original_send = Mock(return_value=response)
        return transport

    def order(self, transport, direction):
        payload = {"epic": "EURUSD", "direction": direction, "size": 100, "guaranteedStop": False}
        if transport.args.environment == "live": payload["stopDistance"] = 0.001
        return requests.Request("POST", transport.base_url + "/api/v1/positions", json=payload).prepare()

    def test_final_guard_enforces_buy_close_sell_close_and_no_third_open_or_close_retry(self):
        transport = self.transport()
        transport.approved = True
        with requests.Session() as session, patch.object(workflow.time, "sleep"):
            with self.assertRaises(IntegrationError): transport.send(session, self.order(transport, "SELL"))
            for direction in ("BUY", "SELL"):
                transport.send(session, self.order(transport, direction))
                with self.assertRaises(IntegrationError): transport.send(session, self.order(transport, direction))
                transport.owned_deal_id = direction.lower() + "-owned"
                transport.journal.data["stage"] = "close_submitting"
                unrelated = requests.Request("DELETE", transport.base_url + "/api/v1/positions/unrelated").prepare()
                with self.assertRaises(IntegrationError): transport.send(session, unrelated)
                close = requests.Request("DELETE", transport.base_url + "/api/v1/positions/" + transport.owned_deal_id).prepare()
                transport.send(session, close)
                with self.assertRaises(IntegrationError): transport.send(session, close)
                # Only verified closure in the workflow is allowed to advance these fields.
                transport.previous_closed = True
                transport.owned_deal_id = None
                transport.journal.data.update(stage="open_submitting", direction="SELL")
            with self.assertRaises(IntegrationError): transport.send(session, self.order(transport, "BUY"))
        self.assertEqual(transport.original_send.call_count, 4)
        self.assertEqual((transport.open_attempts, transport.close_attempts), (2, 2))

    def test_readonly_unconfirmed_live_and_cross_environment_requests_cannot_reach_http(self):
        with requests.Session() as session:
            for transport in (self.transport(), self.transport(read_only=True)):
                with self.assertRaises(IntegrationError): transport.send(session, self.order(transport, "BUY"))
                request = requests.Request("GET", config.DEMO_URL + "/api/v1/accounts").prepare()
                with self.assertRaises(IntegrationError): transport.send(session, request)
                transport.original_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()

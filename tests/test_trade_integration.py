from contextlib import redirect_stdout
from decimal import Decimal
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import test_api_key as integration


def market():
    return {"instrument": {"epic": "EURUSD", "type": "CURRENCIES", "guaranteedStopAllowed": True},
            "snapshot": {"marketStatus": "TRADEABLE", "marketModes": ["REGULAR"]},
            "dealingRules": {"minDealSize": {"value": 0.1, "unit": "POINTS"},
                             "maxDealSize": {"value": 100, "unit": "POINTS"},
                             "minSizeIncrement": {"value": 0.1, "unit": "POINTS"},
                             "marketOrderPreference": "AVAILABLE_DEFAULT_ON"}}


def fake_api():
    api = Mock(BASE_URL=integration.DEMO_URL, cst="dummy-cst", x_security_token="dummy-token")
    api.createNewSession.return_value = {"hasActiveDemoAccounts": True, "currentAccountId": "demo-account"}
    api.pingService.return_value = ({"status": "OK"}, 200)
    api.getSessionDetails.return_value = {"accountId": "demo-account"}
    api.getAllAccounts.return_value = {"accounts": [{"accountId": "demo-account", "status": "ENABLED", "accountType": "CFD"}]}
    api.getAccountPreferences.return_value = {"hedgingMode": False}
    api.getSingleMarketDetails.return_value = market()
    api.getHistoricalPrices.return_value = ({"prices": [{"closePrice": {"ask": 1.1}}] * 210}, 200)
    api.getAllPositions.return_value = ({"positions": []}, 200)
    api.getAllWorkingOrders.return_value = {"workingOrders": []}
    return api


class IntegrationReadOnlyTests(unittest.TestCase):
    def test_read_only_preflight_checks_demo_and_returns_minimum(self):
        report = {}
        api = fake_api()
        context = integration.verify_read_only(api, report)
        self.assertEqual(context["minimum"], Decimal("0.1"))
        self.assertEqual(report["account"]["environment"], "DEMO")
        self.assertTrue(report["trading_api_response_handling"]["passed"])
        api.createPosition.assert_not_called()
        api.closePosition.assert_not_called()

    def test_account_mismatch_stops_before_market_or_orders(self):
        api = fake_api()
        api.getSessionDetails.return_value = {"accountId": "other-account"}
        with self.assertRaises(integration.IntegrationError):
            integration.verify_read_only(api, {})
        api.getSingleMarketDetails.assert_not_called()
        api.createPosition.assert_not_called()

    def test_market_constraints_are_not_rounded_up_or_replaced(self):
        minimum, summary = integration.validate_market(market())
        self.assertEqual(minimum, Decimal("0.1"))
        self.assertEqual(summary["minimum_size"], "0.1")
        invalid = market()
        invalid["dealingRules"]["minDealSize"]["value"] = -1
        with self.assertRaises(integration.IntegrationError):
            integration.validate_market(invalid)

    def test_default_transport_blocks_live_and_mutating_requests(self):
        transport = integration.DemoTransport()
        transport.original_send = Mock(side_effect=AssertionError("Network must not be reached"))
        for method, url in (("POST", "https://api-capital.backend-capital.com/api/v1/session"),
                            ("POST", integration.DEMO_URL + "/api/v1/positions"),
                            ("DELETE", integration.DEMO_URL + "/api/v1/positions/not-owned"),
                            ("PUT", integration.DEMO_URL + "/api/v1/session")):
            request = requests.Request(method, url).prepare()
            with self.subTest(method=method, url=url), self.assertRaises(integration.IntegrationError):
                transport.send(requests.Session(), request)
        transport.original_send.assert_not_called()

    def test_http_failure_stops_without_retry(self):
        transport = integration.DemoTransport()
        response = requests.Response()
        response.status_code = 401
        response._content = b'{"errorCode":"error.invalid.details"}'
        transport.original_send = Mock(return_value=response)
        request = requests.Request("POST", integration.DEMO_URL + "/api/v1/session").prepare()
        with self.assertRaises(integration.IntegrationError):
            transport.send(requests.Session(), request)
        with self.assertRaises(integration.IntegrationError):
            transport.send(requests.Session(), request)
        transport.original_send.assert_called_once()
        self.assertEqual(transport.calls[0]["error_category"], "invalid_login_details")


def position(deal_id="owned", epic="EURUSD", size=0.1):
    return {"position": {"dealId": deal_id, "direction": "BUY", "size": size}, "market": {"epic": epic}}


def trading_fixture():
    api = fake_api()
    other = position("preexisting-other", "GOLD", 1)
    owned = position()
    api.getAllPositions.side_effect = [({"positions": [other]}, 200),
                                       ({"positions": [other, owned]}, 200),
                                       ({"positions": [other]}, 200)]
    api.createPosition.return_value = ({"dealReference": "o_open"}, 200)
    api.closePosition.return_value = ({"dealReference": "p_close"}, 200)
    api.getsinglePosition.return_value = owned
    api.getPositionOrderConfirmation.side_effect = [
        {"dealReference": "o_open", "dealStatus": "ACCEPTED", "status": "OPEN", "epic": "EURUSD",
         "direction": "BUY", "size": 0.1, "dealId": "order-not-position", "affectedDeals": [{"dealId": "owned", "status": "OPENED"}]},
        {"dealReference": "p_close", "dealStatus": "ACCEPTED", "status": "CLOSED", "epic": "EURUSD"}]
    context = {"account_id": "demo-account", "minimum": Decimal("0.1"), "baseline": [other]}
    return api, context


class TradeLifecycleTests(unittest.TestCase):
    def run_trade(self, api, context, directory, report=None):
        transport = integration.DemoTransport(execute=True)
        with integration.TradeJournal(directory) as journal, patch.object(integration.time, "sleep"):
            integration.execute_trade(api, context, transport, journal, {} if report is None else report)
        return json.loads((Path(directory) / "trade.json").read_text())

    def test_one_minimum_buy_then_close_only_affected_position_id(self):
        api, context = trading_fixture()
        report = {}
        with tempfile.TemporaryDirectory() as directory:
            journal = self.run_trade(api, context, directory, report)
            self.assertEqual((Path(directory) / "trade.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(journal["stage"], "closed")
            self.assertEqual(journal["deal_id"], "owned")
            self.assertNotIn("dummy-cst", json.dumps(journal))
            self.assertNotIn("dummy-token", json.dumps(journal))
        api.createPosition.assert_called_once_with("EURUSD", "BUY", 0.1)
        api.closePosition.assert_called_once_with("owned")
        self.assertTrue(report["trade"]["closure_verified"])
        self.assertTrue(report["trade"]["baseline_positions_preserved"])

    def test_submission_intent_is_durable_before_buy(self):
        api, context = trading_fixture()
        with tempfile.TemporaryDirectory() as directory:
            def submitted(*args):
                state = json.loads((Path(directory) / "trade.json").read_text())
                self.assertEqual(state["stage"], "open_submitting")
                return {"dealReference": "o_open"}, 200
            api.createPosition.side_effect = submitted
            self.run_trade(api, context, directory)

    def test_default_flag_gate_blocks_direct_trade_function(self):
        api, context = trading_fixture()
        with self.assertRaises(integration.IntegrationError):
            integration.execute_trade(api, context, integration.DemoTransport(), None, {})
        api.createPosition.assert_not_called()

    def test_existing_position_pending_order_closed_market_or_new_minimum_blocks_buy(self):
        for scenario in ("existing", "pending", "closed", "size_change", "new_position"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                api, context = trading_fixture()
                if scenario == "existing":
                    context["baseline"] = [position("existing")]
                elif scenario == "pending":
                    api.getAllWorkingOrders.return_value = {"workingOrders": [{"workingOrderData": {"epic": "EURUSD"}}]}
                elif scenario == "closed":
                    api.getSingleMarketDetails.return_value["snapshot"]["marketStatus"] = "CLOSED"
                elif scenario == "size_change":
                    api.getSingleMarketDetails.return_value["dealingRules"]["minDealSize"]["value"] = 0.2
                else:
                    api.getAllPositions.side_effect = [({"positions": [position("concurrent")]}, 200)]
                with self.assertRaises(integration.IntegrationError):
                    self.run_trade(api, context, directory)
                api.createPosition.assert_not_called()
                api.closePosition.assert_not_called()

    def test_open_timeout_is_not_retried_and_next_run_is_blocked(self):
        api, context = trading_fixture()
        api.createPosition.side_effect = requests.Timeout("private-message")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(requests.Timeout):
                self.run_trade(api, context, directory)
            state = json.loads((Path(directory) / "trade.json").read_text())
            self.assertEqual(state["stage"], "uncertain")
            self.assertEqual(state["previous_stage"], "open_submitting")
            self.assertNotIn("private-message", json.dumps(state))
            with self.assertRaises(integration.IntegrationError):
                with integration.TradeJournal(directory):
                    self.fail("Unresolved journal must block restart")
        api.createPosition.assert_called_once()
        api.closePosition.assert_not_called()

    def test_multiple_affected_deals_do_not_grant_ownership(self):
        api, context = trading_fixture()
        api.getPositionOrderConfirmation.side_effect = [{"dealReference": "o_open", "dealStatus": "ACCEPTED",
            "epic": "EURUSD", "direction": "BUY", "size": 0.1,
            "affectedDeals": [{"dealId": "a", "status": "OPENED"}, {"dealId": "b", "status": "OPENED"}]}]
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(integration.IntegrationError):
            self.run_trade(api, context, directory)
        api.closePosition.assert_not_called()

    def test_confirmation_timeout_does_not_repeat_buy_or_guess_position(self):
        api, context = trading_fixture()
        api.getPositionOrderConfirmation.side_effect = [None] * 5
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(integration.IntegrationError):
            self.run_trade(api, context, directory)
        api.createPosition.assert_called_once()
        api.closePosition.assert_not_called()
        self.assertEqual(api.getPositionOrderConfirmation.call_count, 5)

    def test_rejected_open_never_closes_any_position(self):
        api, context = trading_fixture()
        api.getPositionOrderConfirmation.side_effect = [{"dealReference": "o_open", "dealStatus": "REJECTED"}]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(integration.IntegrationError):
                self.run_trade(api, context, directory)
            self.assertEqual(json.loads((Path(directory) / "trade.json").read_text())["stage"], "rejected")
        api.closePosition.assert_not_called()

    def test_changed_owned_size_or_active_account_prevents_close(self):
        for scenario in ("size", "account"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                api, context = trading_fixture()
                if scenario == "size":
                    api.getsinglePosition.return_value = position(size=0.2)
                else:
                    api.getSessionDetails.side_effect = [{"accountId": "demo-account"}, {"accountId": "demo-account"}, {"accountId": "other-account"}]
                with self.assertRaises(integration.IntegrationError):
                    self.run_trade(api, context, directory)
                api.createPosition.assert_called_once()
                api.closePosition.assert_not_called()

    def test_close_timeout_is_never_retried(self):
        api, context = trading_fixture()
        api.closePosition.side_effect = requests.Timeout("do-not-log")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(requests.Timeout):
                self.run_trade(api, context, directory)
            state = json.loads((Path(directory) / "trade.json").read_text())
            self.assertEqual(state["stage"], "uncertain")
            self.assertEqual(state["previous_stage"], "close_submitting")
            self.assertEqual(state["deal_id"], "owned")
        api.closePosition.assert_called_once_with("owned")

    def test_absence_verification_may_poll_but_never_reclose(self):
        api, context = trading_fixture()
        other = position("preexisting-other", "GOLD", 1)
        api.getAllPositions.side_effect = [({"positions": [other]}, 200), ({"positions": [other, position()]}, 200),
                                          ({"positions": [other, position()]}, 200), ({"positions": [other]}, 200)]
        with tempfile.TemporaryDirectory() as directory:
            state = self.run_trade(api, context, directory)
            self.assertEqual(state["stage"], "closed")
        api.closePosition.assert_called_once_with("owned")

    def test_concurrent_script_is_blocked_by_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            with integration.TradeJournal(directory):
                with self.assertRaises(BlockingIOError):
                    with integration.TradeJournal(directory):
                        self.fail("Concurrent integration run must not acquire lock")


class ArmedTransportTests(unittest.TestCase):
    def test_one_armed_buy_and_only_owned_delete_are_permitted(self):
        transport = integration.DemoTransport(execute=True)
        transport.open_size = Decimal("0.1")
        transport.journal = Mock(data={"stage": "open_submitting"})
        reply = requests.Response()
        reply.status_code = 200
        transport.original_send = Mock(return_value=reply)
        request = requests.Request("POST", integration.DEMO_URL + "/api/v1/positions",
                                   json={"epic": "EURUSD", "direction": "BUY", "size": 0.1, "guaranteedStop": False}).prepare()
        transport.send(requests.Session(), request)
        with self.assertRaises(integration.IntegrationError):
            transport.send(requests.Session(), request)
        transport.owned_deal_id = "owned"
        transport.journal.data["stage"] = "close_submitting"
        wrong = requests.Request("DELETE", integration.DEMO_URL + "/api/v1/positions/preexisting-other").prepare()
        with self.assertRaises(integration.IntegrationError):
            transport.send(requests.Session(), wrong)
        close = requests.Request("DELETE", integration.DEMO_URL + "/api/v1/positions/owned").prepare()
        with patch.object(integration.time, "sleep"):
            transport.send(requests.Session(), close)
        with self.assertRaises(integration.IntegrationError):
            transport.send(requests.Session(), close)
        self.assertEqual(transport.original_send.call_count, 2)
        self.assertEqual(transport.open_attempts, 1)
        self.assertEqual(transport.close_attempts, 1)

    def test_wrong_payload_or_missing_durable_intent_blocks_buy(self):
        for wrong in ("SELL", "wrong_size", "no_intent"):
            transport = integration.DemoTransport(execute=True)
            transport.open_size = Decimal("0.1")
            transport.journal = Mock(data={"stage": "prepared" if wrong == "no_intent" else "open_submitting"})
            transport.original_send = Mock(side_effect=AssertionError("Network must not be reached"))
            request = requests.Request("POST", integration.DEMO_URL + "/api/v1/positions",
                                       json={"epic": "EURUSD", "direction": "SELL" if wrong == "SELL" else "BUY",
                                             "size": 1 if wrong == "wrong_size" else 0.1, "guaranteedStop": False}).prepare()
            with self.subTest(wrong=wrong), self.assertRaises(integration.IntegrationError):
                transport.send(requests.Session(), request)
            transport.original_send.assert_not_called()


class IntegrationCliTests(unittest.TestCase):
    def test_default_cli_requests_two_demo_roundtrips_with_size_100(self):
        from scripts import integration_roundtrip as workflow
        with patch.object(workflow, "run_integration", return_value=0) as run:
            self.assertEqual(integration.main([]), 0)
        args = run.call_args.args[0]
        self.assertEqual((args.environment, args.symbol, args.size, args.max_orders), ("demo", "EURUSD", 100, 2))
        self.assertFalse(args.read_only)

    def test_optional_overrides_and_readonly_are_preserved(self):
        from scripts import integration_roundtrip as workflow
        with patch.object(workflow, "run_integration", return_value=0) as run:
            integration.main(["--read-only", "--symbol", "AUDUSD", "--size", "200"])
        args = run.call_args.args[0]
        self.assertEqual((args.symbol, args.size), ("AUDUSD", 200))
        self.assertTrue(args.read_only)

    def test_more_than_two_openings_or_conflicting_environment_is_rejected(self):
        from scripts import integration_roundtrip as workflow
        for args in (["--max-orders", "3"], ["--REAL", "--environment", "demo"]):
            with self.subTest(args=args), patch.object(workflow, "run_integration") as run, \
                 patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                integration.main(args)
            run.assert_not_called()

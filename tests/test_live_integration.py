from contextlib import redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import requests
import test_live_api as live

CONFIG = {"X-CAP-API-KEY": "private-shared-key", "identifier": "private-shared-login",
          "password": "private-shared-password"}
ACCOUNT_ID = "live-account-1234"


def account():
    return {"accountId": ACCOUNT_ID, "status": "ENABLED", "accountType": "CFD", "demo": False,
            "currency": "USD", "balance": {"balance": 1000, "available": 1000, "profitLoss": 0}}


def market():
    return {"instrument": {"epic": "EURUSD", "type": "CURRENCIES", "currency": "USD", "lotSize": 1,
                           "marginFactor": 3.333333, "marginFactorUnit": "PERCENTAGE",
                           "overnightFee": {"longRate": -0.01, "swapChargeTimestamp": int((time.time() + 3600) * 1000)}},
            "snapshot": {"marketStatus": "TRADEABLE", "marketModes": ["REGULAR"], "bid": 1.0998,
                         "offer": 1.1, "scalingFactor": 1, "delayTime": 0},
            "dealingRules": {"minDealSize": {"value": 100, "unit": "POINTS"},
                             "maxDealSize": {"value": 45000000, "unit": "POINTS"},
                             "minSizeIncrement": {"value": 100, "unit": "POINTS"},
                             "marketOrderPreference": "AVAILABLE_DEFAULT_ON"}}


PREFERENCES = {"hedgingMode": False, "leverages": {"CURRENCIES": {"current": 30}}}


def context():
    m, a = market(), account()
    risk = live.risk_estimate(m, a, PREFERENCES)
    risk["costs"] = {"estimated_roundtrip_cost": "0.0200"}
    return {"account_id": ACCOUNT_ID, "account": a, "market": m, "risk": risk,
            "positions": [], "working_orders": [], "phase1_passed": True}


def response(payload=None, status=200, headers=None):
    value = Mock(status_code=status, headers={"Content-Type": "application/json", **(headers or {})})
    value.iter_content.return_value = [json.dumps(payload or {}).encode()]
    value.__enter__ = Mock(return_value=value)
    value.__exit__ = Mock(return_value=False)
    return value


class LiveRiskTests(unittest.TestCase):
    def test_notional_pip_spread_and_margin_use_units_not_standard_lots(self):
        risk = live.risk_estimate(market(), account(), PREFERENCES)
        self.assertEqual(Decimal(risk["notional_usd"]), Decimal("110"))
        self.assertEqual(Decimal(risk["estimated_roundtrip_spread_cost"]), Decimal("0.02"))
        self.assertEqual(Decimal(risk["pip_value_usd"]), Decimal("0.01"))
        self.assertAlmostEqual(float(risk["estimated_margin_account_currency"]), 110 / 30)

    def test_conservative_margin_never_uses_lower_conflicting_estimate(self):
        pref = {"leverages": {"CURRENCIES": {"current": 1}}}
        risk = live.risk_estimate(market(), account(), pref)
        self.assertEqual(Decimal(risk["estimated_margin_account_currency"]), Decimal("110"))

    def test_unknown_contract_currency_or_unsupported_size_blocks_risk_readiness(self):
        for scenario in ("lot", "currency", "size", "closed", "scale"):
            with self.subTest(scenario=scenario):
                m, a = market(), account()
                if scenario == "lot": m["instrument"]["lotSize"] = 100000
                elif scenario == "currency": a["currency"] = "GBP"
                elif scenario == "size": m["dealingRules"]["minDealSize"]["value"] = 200
                elif scenario == "closed": m["snapshot"]["marketStatus"] = "CLOSED"
                else: m["snapshot"]["scalingFactor"] = 100
                with self.assertRaises(live.IntegrationError):
                    live.risk_estimate(m, a, PREFERENCES)

    def test_limits_are_mandatory_and_enforced_without_prompting(self):
        ctx = context()
        for margin, exposure in ((None, None), (0, 200), (1, 200), (10, 100), (Decimal("NaN"), 200)):
            with self.subTest(margin=margin, exposure=exposure), self.assertRaises(live.IntegrationError):
                live.check_limits(ctx, margin, exposure, 1)
        live.check_limits(ctx, Decimal("10"), Decimal("200"), 1)
        ctx["account"]["balance"]["available"] = 1
        with self.assertRaises(live.IntegrationError):
            live.check_limits(ctx, 10, 200, 1)

    def test_transaction_charge_cap_is_required_and_enforced_with_usd_units(self):
        ctx = context()
        for cap in (None, 0, -1, Decimal("NaN"), Decimal("0.01")):
            with self.subTest(cap=cap), self.assertRaises(live.IntegrationError):
                live.check_limits(ctx, 5, 120, cap)
        live.check_limits(ctx, 5, 120, Decimal("0.02"))
        ctx["risk"]["costs"]["estimated_roundtrip_cost"] = "1.01"
        with self.assertRaisesRegex(live.IntegrationError, "live_transaction_charge_limit_exceeded"):
            live.check_limits(ctx, 5, 120, 1)
        ctx["risk"]["costs"]["estimated_roundtrip_cost"] = "0.02"
        ctx["account"]["currency"] = ctx["risk"]["account_currency"] = "EUR"
        with self.assertRaisesRegex(live.IntegrationError, "usd_account_required"):
            live.check_limits(ctx, 5, 120, 1)

    def test_fee_profile_must_be_verified_for_account_and_current(self):
        ctx = context()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fees.json"
            with self.assertRaises(live.IntegrationError):
                live.verified_costs(path, ctx["risk"], ctx["account_id"], ctx["market"])
            profile = {"verified_for_this_account": True, "account_fingerprint": live.fingerprint(ctx["account_id"]),
                       "currency": "USD", "legal_entity": "Capital Com SV Investments Limited", "client_classification": "retail",
                       "instrument": "EURUSD", "commission_per_side": "0", "conversion_fee_percent": "0", "other_transaction_charges_roundtrip": "0",
                       "reviewed_at": datetime.now(timezone.utc).date().isoformat(), "source_url": "https://capital.com/en-eu/ways-to-trade/pricing"}
            path.write_text(json.dumps(profile))
            path.chmod(0o600)
            costs = live.verified_costs(path, ctx["risk"], ctx["account_id"], ctx["market"])
            self.assertEqual(Decimal(costs["estimated_roundtrip_cost"]), Decimal("0.02"))
            for field, invalid in (("legal_entity", ""), ("client_classification", "unknown"),
                                   ("instrument", "OTHER"), ("other_transaction_charges_roundtrip", None),
                                   ("commission_per_side", None)):
                path.write_text(json.dumps({**profile, field: invalid}))
                with self.subTest(field=field), self.assertRaises((live.IntegrationError, TypeError)):
                    live.verified_costs(path, ctx["risk"], ctx["account_id"], ctx["market"])
            path.write_text(json.dumps(profile))
            path.chmod(0o644)
            with self.assertRaisesRegex(live.IntegrationError, "private_regular_file"):
                live.verified_costs(path, ctx["risk"], ctx["account_id"], ctx["market"])
            path.chmod(0o600)
            profile["verified_for_this_account"] = False
            path.write_text(json.dumps(profile))
            with self.assertRaises(live.IntegrationError):
                live.verified_costs(path, ctx["risk"], ctx["account_id"], ctx["market"])


class LiveSafetyTests(unittest.TestCase):
    def test_execute_without_explicit_charge_limit_stops_before_credentials_or_network(self):
        with patch.object(live, "load_live_configuration") as load, patch.object(live, "LiveClient") as factory, \
             patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            live.main(["--environment", "live", "--execute-live-trade", "--max-margin", "5", "--max-exposure", "120"])
        load.assert_not_called()
        factory.assert_not_called()

    def test_readonly_client_blocks_orders_and_demo_or_absolute_urls(self):
        client = live.LiveClient(CONFIG, environment="live")
        with patch.object(client.session, "request", side_effect=AssertionError("Must not reach transport")) as send:
            for method, path in (("POST", "/api/v1/positions"), ("DELETE", "/api/v1/positions/other"),
                                 ("PUT", "/api/v1/session"), ("GET", "https://demo-api-capital.backend-capital.com/api/v1/session")):
                with self.subTest(method=method, path=path), self.assertRaises(live.IntegrationError):
                    client.request(method, path)
            send.assert_not_called()
        client.close()

    def test_one_login_uses_shared_file_values_and_enforces_http_safeguards(self):
        client = live.LiveClient(CONFIG, environment="live")
        reply = response({"hasActiveLiveAccounts": True}, headers={"CST": "dummy-cst", "X-SECURITY-TOKEN": "dummy-token"})
        with patch.object(client.session, "request", return_value=reply) as send:
            client.login()
            with self.assertRaises(live.IntegrationError): client.login()
        send.assert_called_once()
        self.assertEqual(send.call_args.args, ("POST", live.LIVE_URL + "/api/v1/session"))
        kwargs = send.call_args.kwargs
        self.assertIs(kwargs["json"]["encryptedPassword"], False)
        self.assertIs(kwargs["verify"], True)
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertEqual(kwargs["timeout"], (5, 15))
        self.assertFalse(client.session.trust_env)
        self.assertEqual(client.session.get_adapter(live.LIVE_URL).max_retries.total, 0)
        self.assertNotIn("dummy-cst", json.dumps(client.calls))
        client.close()

    def test_even_armed_client_permits_only_one_buy_and_one_owned_delete(self):
        client = live.LiveClient(CONFIG, execute=True, environment="live")
        client.manual_approval = client.phase1_passed = True
        client.journal = Mock(data={"stage": "open_submitting"})
        buy = {"epic": "EURUSD", "direction": "BUY", "size": 100, "guaranteedStop": False}
        with patch.object(client.session, "request", return_value=response({"dealReference": "reference"})) as send, \
             patch.object(live.time, "sleep"):
            with self.assertRaises(live.IntegrationError): client.request("POST", "/api/v1/positions", {**buy, "direction": "SELL"})
            client.request("POST", "/api/v1/positions", buy)
            with self.assertRaises(live.IntegrationError): client.request("POST", "/api/v1/positions", buy)
            client.owned_id = "owned"
            client.journal.data["stage"] = "close_submitting"
            with self.assertRaises(live.IntegrationError): client.request("DELETE", "/api/v1/positions/not-owned")
            client.request("DELETE", "/api/v1/positions/owned")
            with self.assertRaises(live.IntegrationError): client.request("DELETE", "/api/v1/positions/owned")
        self.assertEqual(send.call_count, 2)
        client.close()

    def test_manual_confirmation_requires_real_tty_and_exact_fresh_phrase(self):
        ctx = context()
        with patch.object(live.sys.stdin, "isatty", return_value=False), self.assertRaises(live.IntegrationError):
            live.manual_confirmation(ctx, 10, 200, 1)
        with patch.object(live.sys.stdin, "isatty", return_value=True), patch.object(live.secrets, "token_hex", return_value="nonce"), \
             patch("builtins.input", return_value="yes"), redirect_stdout(io.StringIO()), self.assertRaises(live.IntegrationError):
            live.manual_confirmation(ctx, 10, 200, 1)
        with patch.object(live.sys.stdin, "isatty", return_value=True), patch.object(live.secrets, "token_hex", return_value="nonce"), \
             patch("builtins.input", return_value="LIVE BUY 100 EURUSD 1234 nonce"), redirect_stdout(io.StringIO()):
            self.assertIsInstance(live.manual_confirmation(ctx, 10, 200, 1), float)

    def test_no_explicit_live_selection_blocks_before_credentials_or_network(self):
        import test_api_key as demo
        with patch.object(live, "load_live_configuration") as load, patch.object(live, "LiveClient") as factory, \
             redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()):
            with patch.object(demo, "main", return_value=0) as checker:
                self.assertEqual(live.main([]), 0)
            checker.assert_called_once_with(["--read-only"])
            with self.assertRaises(SystemExit): live.main(["--execute-live-trade", "--max-margin", "10", "--max-exposure", "200"])
        load.assert_not_called()
        factory.assert_not_called()
        with self.assertRaises(live.IntegrationError): live.LiveClient(CONFIG)

    def test_missing_shared_file_never_uses_other_configuration_or_authenticates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.live").write_text("obsolete-file-must-not-be-read")
            output = io.StringIO()
            with patch.object(live, "ROOT", root), patch.object(live, "LiveClient") as factory, redirect_stdout(output):
                self.assertEqual(live.main(["--environment", "live"]), 1)
            factory.assert_not_called()
            result = json.loads(output.getvalue())
            self.assertEqual(result["failure"], "shared_credentials_file_missing")
            self.assertEqual(result["open_attempts"], 0)
            self.assertTrue(result["env_unchanged"])

    def test_account_discovery_checks_environment_and_session_identity(self):
        login = {"hasActiveLiveAccounts": True, "currentAccountId": ACCOUNT_ID}
        session = {"accountId": ACCOUNT_ID}
        a, eligible, needed = live.select_live_account(login, session, [account()])
        self.assertEqual(a["accountId"], ACCOUNT_ID)
        self.assertFalse(needed)
        for bad_login, bad_session, bad_accounts in (
            ({**login, "hasActiveLiveAccounts": False}, session, [account()]),
            (login, {"accountId": "wrong"}, [account()]),
            (login, session, [{**account(), "demo": True}]),
            (login, session, [{**account(), "status": "DISABLED"}]),
            (login, session, [account(), account()])):
            with self.subTest(login=bad_login), self.assertRaises(live.IntegrationError):
                live.select_live_account(bad_login, bad_session, bad_accounts)

    def test_multiple_accounts_require_explicit_active_selection_before_trading(self):
        login, session = {"hasActiveLiveAccounts": True, "currentAccountId": ACCOUNT_ID}, {"accountId": ACCOUNT_ID}
        accounts = [account(), {**account(), "accountId": "second-live-account"}]
        self.assertTrue(live.select_live_account(login, session, accounts)[2])
        for selection in (ACCOUNT_ID, live.fingerprint(ACCOUNT_ID)[:12]):
            self.assertFalse(live.select_live_account(login, session, accounts, selection)[2])
        with self.assertRaises(live.IntegrationError):
            live.select_live_account(login, session, accounts, "second-live-account")
        with self.assertRaises(live.IntegrationError):
            live.select_live_account(login, session, accounts, "unknown")

    def test_multiple_accounts_block_phase1_trade_readiness_but_allow_readonly(self):
        client = Mock()
        client.login.return_value = {"hasActiveLiveAccounts": True, "currentAccountId": ACCOUNT_ID}
        routes = {"/api/v1/session": {"accountId": ACCOUNT_ID}, "/api/v1/ping": {"status": "OK"},
                  "/api/v1/accounts": {"accounts": [account(), {**account(), "accountId": "second"}]},
                  "/api/v1/accounts/preferences": PREFERENCES, "/api/v1/markets/EURUSD": market(),
                  "/api/v1/positions": {"positions": []}, "/api/v1/workingorders": {"workingOrders": []}}
        client.request.side_effect = lambda method, path: routes[path]
        for selection, ready in ((None, False), (live.fingerprint(ACCOUNT_ID)[:12], True)):
            report = {}
            with patch.object(live, "verified_costs", return_value={"estimated_roundtrip_cost": "0.02"}):
                ctx = live.live_preflight(client, CONFIG, report, Path("unused"), selected_account=selection)
            self.assertTrue(report["phase1_api_passed"])
            self.assertEqual(ctx["phase1_passed"], ready)
            self.assertTrue(all(call.args[0] == "GET" for call in client.request.call_args_list))
        client.explicit_account_selection = False
        with self.assertRaisesRegex(live.IntegrationError, "multiple_live_accounts"):
            live.live_preflight_snapshot(client, CONFIG, {}, Path("unused"))

    def test_live_journal_is_separate_and_environment_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            with live.LiveJournal(directory) as journal:
                journal.save(stage="prepared", account_fingerprint="dummy-fingerprint")
                saved = json.loads((Path(directory) / "trade.json").read_text())
                self.assertEqual(saved["base_url"], live.LIVE_URL)
                self.assertEqual((Path(directory) / "trade.json").stat().st_mode & 0o777, 0o600)
            with self.assertRaises(live.IntegrationError):
                with live.LiveJournal(directory): self.fail("Unfinished journal must block rerun")


def fake_trading_client():
    ctx = context()
    client = Mock(execute=True, config=CONFIG, references=set(), owned_id=None, account_id=ACCOUNT_ID)
    owned = {"position": {"dealId": "owned", "size": 100, "direction": "BUY"}, "market": {"epic": "EURUSD"}}
    positions_calls = 0
    def request(method, path, payload=None, params=None):
        nonlocal positions_calls
        if method == "POST": return {"dealReference": "o_open"}
        if method == "DELETE": return {"dealReference": "p_close"}
        if path == "/api/v1/session": return {"accountId": ctx["account_id"]}
        if path == "/api/v1/confirms/o_open":
            return {"dealReference": "o_open", "dealStatus": "ACCEPTED", "epic": "EURUSD", "direction": "BUY", "size": 100,
                    "level": 1.1, "dealId": "order-id", "affectedDeals": [{"dealId": "owned", "status": "OPENED"}]}
        if path == "/api/v1/confirms/p_close": return {"dealReference": "p_close", "dealStatus": "ACCEPTED", "dealId": "close-id", "level": 1.0998}
        if path == "/api/v1/positions/owned": return owned
        if path == "/api/v1/positions":
            positions_calls += 1
            return {"positions": [owned] if positions_calls == 1 else []}
        if path == "/api/v1/history/transactions":
            return {"transactions": [{"reference": "close-id", "transactionType": "TRADE", "size": "-0.02", "currency": "USD", "status": "PROCESSED"}]}
        if path == "/api/v1/accounts":
            a = account()
            a["balance"].update(balance=999.98, available=999.98)
            return {"accounts": [a]}
        raise AssertionError("Unexpected mock route")
    client.request.side_effect = request
    return client, ctx


class LiveLifecycleMockTests(unittest.TestCase):
    def run_trade(self, client, ctx, directory, report):
        with live.LiveJournal(directory) as journal, patch.object(live, "manual_confirmation", return_value=time.monotonic()), \
             patch.object(live, "live_preflight_snapshot", return_value=ctx):
            live.execute_live(client, CONFIG, ctx, journal, Decimal("10"), Decimal("200"), Path("dummy"), report, max_charges=1)

    def test_full_mock_cycle_opens_once_closes_owned_and_records_realized_cashflows(self):
        client, ctx = fake_trading_client()
        report = {}
        with tempfile.TemporaryDirectory() as directory:
            self.run_trade(client, ctx, directory, report)
            state = json.loads((Path(directory) / "trade.json").read_text())
            self.assertEqual(state["stage"], "closed")
            self.assertEqual(state["deal_id"], "owned")
            self.assertEqual(state["approved_max_charges"], "1")
            self.assertEqual(state["close_level"], "1.0998")
        mutations = [call for call in client.request.call_args_list if call.args[0] in ("POST", "DELETE")]
        self.assertEqual(len(mutations), 2)
        self.assertEqual(mutations[0].args[2]["direction"], "BUY")
        self.assertEqual(mutations[1].args, ("DELETE", "/api/v1/positions/owned"))
        self.assertEqual(report["settlement"]["realized_profit_loss_account_currency"], "-0.02")
        self.assertEqual(report["final_account"]["balance"], "999.98")
        self.assertEqual(report["trade"]["buy_execution_price"], "1.1")
        self.assertEqual(report["trade"]["closing_execution_price"], "1.0998")
        self.assertEqual(report["trade"]["executed_size"], "100")
        self.assertIsNone(report["trade"]["actual_margin_used"])

    def test_refreshed_charge_limit_is_enforced_after_confirmation_before_submission(self):
        client, ctx = fake_trading_client()
        fresh = context()
        fresh["risk"]["costs"]["estimated_roundtrip_cost"] = "1.01"
        with tempfile.TemporaryDirectory() as directory, live.LiveJournal(directory) as journal, \
             patch.object(live, "manual_confirmation", return_value=time.monotonic()), \
             patch.object(live, "live_preflight_snapshot", return_value=fresh), \
             self.assertRaisesRegex(live.IntegrationError, "live_transaction_charge_limit_exceeded"):
            live.execute_live(client, CONFIG, ctx, journal, 5, 120, Path("dummy"), {}, max_charges=1)
        client.request.assert_not_called()

    def test_missing_close_price_does_not_claim_complete_success_or_retry_closure(self):
        client, ctx = fake_trading_client()
        original = client.request.side_effect
        def no_price(method, path, payload=None, params=None):
            value = original(method, path, payload, params)
            if path == "/api/v1/confirms/p_close": value.pop("level")
            return value
        client.request.side_effect = no_price
        report = {}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(live.IntegrationError, "live_close_execution_price_unavailable"):
                self.run_trade(client, ctx, directory, report)
            state = json.loads((Path(directory) / "trade.json").read_text())
            self.assertTrue(state["closure_verified"])
        self.assertTrue(report["trade"]["closure_verified"])
        self.assertIsNone(report["trade"]["closing_execution_price"])
        self.assertEqual(sum(call.args[0] == "DELETE" for call in client.request.call_args_list), 1)

    def test_timeout_after_open_intent_never_retries_and_persists_uncertainty(self):
        client, ctx = fake_trading_client()
        client.request.side_effect = requests.Timeout("private-server-text")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(requests.Timeout): self.run_trade(client, ctx, directory, {})
            state = json.loads((Path(directory) / "trade.json").read_text())
            self.assertEqual(state["stage"], "uncertain")
            self.assertEqual(state["previous_stage"], "open_submitting")
            self.assertNotIn("private-server-text", json.dumps(state))
        client.request.assert_called_once()

    def test_confirmation_rejection_blocks_every_order(self):
        client, ctx = fake_trading_client()
        with tempfile.TemporaryDirectory() as directory, live.LiveJournal(directory) as journal, \
             patch.object(live, "manual_confirmation", side_effect=live.IntegrationError("declined")):
            with self.assertRaises(live.IntegrationError):
                live.execute_live(client, CONFIG, ctx, journal, 10, 200, Path("dummy"), {}, max_charges=1)
        client.request.assert_not_called()

    def test_existing_account_exposure_blocks_before_manual_confirmation(self):
        client, ctx = fake_trading_client()
        ctx["phase1_passed"] = False
        with patch.object(live, "manual_confirmation") as prompt, self.assertRaises(live.IntegrationError):
            live.execute_live(client, CONFIG, ctx, Mock(), 10, 200, Path("dummy"), {}, max_charges=1)
        prompt.assert_not_called()
        client.request.assert_not_called()

    def test_close_timeout_is_not_retried_and_remaining_exposure_is_read_only(self):
        client, ctx = fake_trading_client()
        original = client.request.side_effect
        def uncertain(method, path, payload=None, params=None):
            if method == "DELETE": raise requests.Timeout("do-not-log")
            if method == "GET" and path == "/api/v1/positions":
                return {"positions": [{"position": {"dealId": "owned", "size": 100, "direction": "BUY"}, "market": {"epic": "EURUSD"}}]}
            return original(method, path, payload, params)
        client.request.side_effect = uncertain
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(requests.Timeout): self.run_trade(client, ctx, directory, {})
            state = json.loads((Path(directory) / "trade.json").read_text())
            self.assertEqual(state["previous_stage"], "close_submitting")
            journal = Mock(data=state)
            report = {}
            before = client.request.call_count
            live.reconcile_remaining(client, journal, report)
            self.assertTrue(all(call.args[0] == "GET" for call in client.request.call_args_list[before:]))
            self.assertTrue(report["remaining_exposure"]["owned_position_visible"])
            self.assertEqual(report["remaining_exposure"]["size"], "100")
        self.assertEqual(sum(call.args[0] == "DELETE" for call in client.request.call_args_list), 1)

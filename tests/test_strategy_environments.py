import argparse
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
import importlib
import io
import json
from pathlib import Path
import runpy
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import requests
from TradingApi import TradingApi
from scripts import trading_config as config
from scripts import live_execution as execution
from scripts import strategy_defaults as defaults
from scripts.strategy_costs import available_costs
from scripts.live_risk import IntegrationError, fingerprint


STRATEGIES = ("main", "sma", "hma", "multiple_hma", "ema_hma", "test")
READERS = ("ping_service", "test_strategy")
ROOT = Path(__file__).resolve().parents[1]


def arguments(flags=()):
    parser = argparse.ArgumentParser()
    config.add_trading_arguments(parser)
    return parser.parse_args(flags)


class StartupReached(BaseException):
    pass


class StrategyEnvironmentTests(unittest.TestCase):
    def test_shared_defaults_require_only_real_flag_for_live_preflight(self):
        demo, live = arguments(), arguments(["--REAL"])
        self.assertFalse(demo.real)
        self.assertTrue(live.real)
        for args in (demo, live):
            self.assertEqual((args.symbol, args.size), ("EURUSD", 100))
            self.assertEqual((args.max_margin, args.max_exposure, args.max_charges, args.max_loss),
                             (Decimal("5"), Decimal("120"), Decimal("1"), Decimal("2")))
            self.assertEqual((args.max_orders, args.stop_distance), (1, Decimal("0.001")))

    def test_risk_defaults_are_configurable_and_cli_overrides_take_precedence(self):
        with patch.object(defaults, "MAX_EXPOSURE", Decimal("150")):
            self.assertEqual(arguments().max_exposure, Decimal("150"))
            self.assertEqual(arguments(["--max-exposure", "130"]).max_exposure, Decimal("130"))

    def test_optional_symbol_size_reach_every_strategy_order_unchanged(self):
        for name in STRATEGIES:
            module = importlib.import_module(name)
            api = Mock()
            api.createPosition.return_value = ({}, 200)
            api.getAllPositionsList.return_value = []
            def history(symbol, timeframe, count):
                return {"prices": [{"openPrice": {"ask": float(i + 1)}} for i in range(count)]}, 200
            api.getHistoricalPrices.side_effect = history
            if name == "main":
                api.getHistoricalPrices.side_effect = lambda symbol, timeframe, count: (
                    {"prices": [{"openPrice": {"ask": 1 if count > 1 else 2}} for _ in range(count)]}, 200)
            with self.subTest(strategy=name), ExitStack() as stack:
                stack.enter_context(patch("sys.argv", [name + ".py", "--symbol", "AUDUSD", "--size", "200"]))
                stack.enter_context(patch.object(module, "create_trading_api", return_value=api))
                stack.enter_context(redirect_stdout(io.StringIO()))
                if name in ("main", "sma"):
                    stack.enter_context(patch.object(module.time, "sleep", side_effect=StartupReached))
                if name in ("hma", "multiple_hma", "ema_hma"):
                    stack.enter_context(patch.object(module, "wait_until_targets", side_effect=[None, StartupReached]))
                    stack.enter_context(patch.object(module, "calculate_hma_result", return_value="BUY"))
                    if name == "multiple_hma":
                        stack.enter_context(patch.object(module, "calculate_multiple", return_value="BUY"))
                    if name == "ema_hma":
                        stack.enter_context(patch.object(module, "calculate_two_ema_result", return_value="BUY"))
                if name == "test":
                    module.main()
                else:
                    with self.assertRaises(StartupReached): module.main()
                api.createPosition.assert_called_once_with("AUDUSD", "SELL" if name == "test" else "BUY", 200)
                for call in api.getHistoricalPrices.call_args_list:
                    self.assertEqual(call.args[0], "AUDUSD")

    def test_invalid_sizes_are_rejected_instead_of_silently_using_default(self):
        for value in ("0", "-1", "nan", "inf", "garbage"):
            with self.subTest(value=value), patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                arguments(["--size", value])

    def test_all_entry_points_parse_default_uppercase_and_legacy_alias_before_startup(self):
        for module in STRATEGIES + READERS:
            strategy = importlib.import_module(module)
            for flags, expected in (([], False), (["--REAL"], True), (["--real"], True)):
                with self.subTest(module=module, flags=flags), \
                     patch("sys.argv", [module + ".py", *flags]), \
                     patch.object(strategy, "create_trading_api", side_effect=StartupReached) as factory:
                    with self.assertRaises(StartupReached):
                        importlib.import_module(module).main()
                    self.assertIs(factory.call_args.args[0].real, expected)
                    self.assertIsNone(factory.call_args.args[0].account)
                    if module in READERS:
                        self.assertIs(factory.call_args.kwargs["order_execution"], False)
                    else:
                        self.assertEqual(factory.call_args.kwargs, {"symbol": "EURUSD", "size": 100})

    def test_existing_strategy_options_reach_shared_configuration_unchanged(self):
        options = ["--REAL", "--size", "200", "--symbol", "AUDUSD", "--hma_period", "32", "--days", "5"]
        for module in ("hma", "multiple_hma", "ema_hma"):
            strategy = importlib.import_module(module)
            with self.subTest(module=module), patch("sys.argv", [module + ".py", *options]), \
                 patch.object(strategy, "create_trading_api", side_effect=StartupReached) as factory:
                with self.assertRaises(StartupReached): importlib.import_module(module).main()
                args = factory.call_args.args[0]
                self.assertEqual((args.hma_period, args.days, args.size, args.symbol), (32, 5, 200, "AUDUSD"))
                self.assertEqual(factory.call_args.kwargs, {"symbol": "AUDUSD", "size": 200})

    def test_every_help_path_exits_without_credentials_authentication_or_orders(self):
        for module in STRATEGIES + READERS + ("test_api_key", "test_live_api"):
            with self.subTest(module=module), patch("sys.argv", [module + ".py", "--help"]), \
                 patch.object(config, "load_credentials", side_effect=AssertionError("credentials forbidden")), \
                 patch.object(TradingApi, "__init__", side_effect=AssertionError("authentication forbidden")), \
                 redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as result:
                    runpy.run_path(str(ROOT / (module + ".py")), run_name="__main__")
                self.assertEqual(result.exception.code, 0)
                self.assertIn("--REAL", output.getvalue())
                self.assertIn("--real", output.getvalue())

    def test_helper_authenticates_selected_endpoint_and_verifies_before_enabling_orders(self):
        for flags, environment in (([], "demo"), (["--REAL"], "live"), (["--real"], "live")):
            api = Mock()
            api.createNewSession.return_value = {}
            api.verify_account_environment.return_value = {"accountId": "verified-1234"}
            with self.subTest(flags=flags), patch("TradingApi.TradingApi", return_value=api) as factory, \
                 patch.object(execution, "LiveExecution") as gate_factory, redirect_stdout(io.StringIO()):
                result = config.create_trading_api(arguments(flags), symbol="EURUSD", size=100)
                self.assertIs(result, api)
                factory.assert_called_once_with(environment=environment, account=None, auto_authenticate=False)
                api.createNewSession.assert_called_once()
                api.verify_account_environment.assert_called_once()
                self.assertEqual(gate_factory.call_count, int(environment == "live"))

    def test_invalid_live_limit_prevents_authentication(self):
        with patch("TradingApi.TradingApi") as factory, redirect_stdout(io.StringIO()), self.assertRaises(IntegrationError):
            config.create_trading_api(arguments(["--REAL", "--max-margin", "0"]), symbol="EURUSD", size=100)
        factory.assert_not_called()

    def test_live_auth_failure_never_constructs_demo_or_arms_execution(self):
        api = Mock()
        api.createNewSession.return_value = None
        with patch("TradingApi.TradingApi", return_value=api) as factory, \
             patch.object(execution.LiveExecution, "validate_options"), \
             patch.object(execution.LiveExecution, "arm") as arm, redirect_stdout(io.StringIO()), \
             self.assertRaisesRegex(config.ConfigurationError, "live_authentication_failed_no_fallback"):
            config.create_trading_api(arguments(["--REAL"]), symbol="EURUSD", size=100)
        factory.assert_called_once_with(environment="live", account=None, auto_authenticate=False)
        api.verify_account_environment.assert_not_called()
        arm.assert_not_called()

    def test_account_verification_failure_never_arms_execution(self):
        api = Mock()
        api.verify_account_environment.side_effect = RuntimeError("environment_mismatch")
        with patch("TradingApi.TradingApi", return_value=api), \
             patch.object(execution.LiveExecution, "validate_options"), \
             patch.object(execution.LiveExecution, "arm") as arm, redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            config.create_trading_api(arguments(["--REAL"]), symbol="EURUSD", size=100)
        arm.assert_not_called()

    def test_read_only_live_selection_never_arms_execution(self):
        api = Mock()
        api.verify_account_environment.return_value = {"accountId": "verified-1234"}
        with patch("TradingApi.TradingApi", return_value=api), patch.object(execution.LiveExecution, "arm") as arm, \
             redirect_stdout(io.StringIO()):
            config.create_trading_api(arguments(["--REAL"]), order_execution=False)
        arm.assert_not_called()

    def test_shared_helper_loads_same_unchanged_env_with_independent_tokens(self):
        content = 'X-CAP-API-KEY=fixture-key\nidentifier=fixture-login\npassword="fixture#password"\nCST=stale\nX_SECURITY_TOKEN=stale\n'
        clients = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".env"
            path.write_text(content)
            path.chmod(0o600)
            for flags, environment in (([], "demo"), (["--REAL"], "live")):
                reply = Mock(status_code=200, headers={"CST": environment + "-cst", "X-SECURITY-TOKEN": environment + "-token"})
                reply.json.return_value = {"hasActiveLiveAccounts": True, "hasActiveDemoAccounts": True,
                                           "currentAccountId": "active"}
                with patch.object(config, "ROOT", root), patch("TradingApi.requests.post", return_value=reply) as post, \
                     patch.object(TradingApi, "verify_account_environment", return_value={"accountId": "active"}) as verify, \
                     redirect_stdout(io.StringIO()):
                    client = config.create_trading_api(arguments(flags), order_execution=False)
                self.assertEqual(client.PASSWORD, "fixture#password")
                self.assertEqual(client.cst, environment + "-cst")
                self.assertEqual(post.call_args.args[0], config.endpoint(environment) + "/api/v1/session")
                verify.assert_called_once()
                clients.append(client)
            self.assertEqual(path.read_text(), content)
        self.assertNotEqual(clients[0].x_security_token, clients[1].x_security_token)

    def test_live_login_session_account_mismatch_blocks_execution(self):
        api = TradingApi("key", "login", "password", environment="live", auto_authenticate=False)
        api._login_has_active_environment = True
        api._login_account_id = "login-account"
        with patch.object(api, "getSessionDetails", return_value={"accountId": "different-account"}), \
             patch.object(api, "getAllAccounts", return_value={"accounts": []}), \
             self.assertRaisesRegex(RuntimeError, "login_session_account_mismatch"):
            api.verify_account_environment()

    def test_environment_availability_flag_without_login_account_is_insufficient(self):
        api = TradingApi("key", "login", "password", environment="live", auto_authenticate=False)
        api._login_has_active_environment = True
        with patch.object(api, "getSessionDetails") as session, self.assertRaisesRegex(RuntimeError, "login_account_not_verified"):
            api.verify_account_environment()
        session.assert_not_called()

    def test_integration_aliases_select_new_guarded_live_workflow(self):
        import test_api_key as demo
        from scripts import integration_roundtrip as workflow
        for flag in ("--REAL", "--real"):
            with self.subTest(flag=flag), patch.object(workflow, "run_integration", return_value=0) as run, \
                 patch.object(demo, "load_credentials") as load:
                self.assertEqual(demo.main([flag]), 0)
                self.assertEqual(run.call_args.args[0].environment, "live")
                load.assert_not_called()

    def test_conflicting_integration_selectors_reject_without_authentication(self):
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            config.integration_environment(["--REAL", "--environment", "demo"])

    def test_abbreviated_live_flags_are_rejected(self):
        for flag in ("--R", "--REA", "--rea"):
            with self.subTest(flag=flag), patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                arguments([flag])

    def test_live_api_without_strategy_gate_cannot_mutate(self):
        api = TradingApi("key", "login", "password", environment="live", auto_authenticate=False)
        with patch.object(api, "verify_account_environment"), patch.object(api, "_request") as send:
            for callback in (lambda: api.createPosition("EURUSD", "BUY", 100), lambda: api.closePosition("unrelated")):
                with self.assertRaises(execution.LiveSafetyStop): callback()
            send.assert_not_called()

    def test_cross_host_redirects_disabled_for_all_authenticated_calls(self):
        api = TradingApi("key", "login", "password", auto_authenticate=False)
        with patch.object(api, "_request", return_value=Mock(status_code=200, json=Mock(return_value={"positions": []}))) as send:
            api.getSessionDetails()
            api.getAllAccounts()
            api.getSingleMarketDetails("EURUSD")
            api.getHistoricalPrices("EURUSD", "MINUTE", 20)
            api.createPosition("EURUSD", "BUY", 100)
            api.closePosition("owned")
        for call in send.call_args_list:
            self.assertIs(call.kwargs["allow_redirects"], False)
            self.assertGreater(call.kwargs["timeout"], 0)

    def test_existing_broker_stop_and_profit_fields_are_sent(self):
        api = TradingApi("key", "login", "password", auto_authenticate=False)
        with patch.object(api, "_request", return_value=Mock(status_code=200, json=Mock(return_value={}))) as send:
            api.createPosition("EURUSD", "BUY", 100, stopDistance=0.001, profitLevel=1.2)
        self.assertEqual(send.call_args.kwargs["json"]["stopDistance"], 0.001)
        self.assertEqual(send.call_args.kwargs["json"]["profitLevel"], 1.2)


class LiveExecutionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(execution, "ROOT", self.directory))
        self.input = self.stack.enter_context(patch("builtins.input", side_effect=AssertionError("strategy must not prompt")))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.api = TradingApi("key", "login", "password", environment="live", auto_authenticate=False)
        self.api.cst, self.api.x_security_token = "live-cst", "live-token"
        self.api._login_has_active_environment = True
        self.api._login_account_id = "account-1234"
        self.account = {"accountId": "account-1234", "status": "ENABLED", "accountType": "CFD", "demo": False,
                        "currency": "USD", "balance": {"balance": 1000, "available": 1000, "profitLoss": 0}}
        self.market = {"instrument": {"epic": "EURUSD", "type": "CURRENCIES", "currency": "USD", "lotSize": 1,
                                      "marginFactor": 3.333333, "marginFactorUnit": "PERCENTAGE",
                                      "overnightFee": {"longRate": -0.01, "shortRate": 0.001,
                                                       "swapChargeTimestamp": int((time.time() + 3600) * 1000)}},
                       "snapshot": {"marketStatus": "TRADEABLE", "marketModes": ["REGULAR"], "bid": 1.0998,
                                    "offer": 1.1, "scalingFactor": 1, "delayTime": 0},
                       "dealingRules": {"minDealSize": {"value": 100, "unit": "POINTS"},
                                        "maxDealSize": {"value": 100000, "unit": "POINTS"},
                                        "minSizeIncrement": {"value": 100, "unit": "POINTS"},
                                        "minStopOrProfitDistance": {"value": 0.0001, "unit": "POINTS"},
                                        "maxStopOrProfitDistance": {"value": 0.1, "unit": "POINTS"},
                                        "marketOrderPreference": "AVAILABLE_DEFAULT_ON"}}
        self.positions = []
        self.working = []
        self.api.getSessionDetails = Mock(return_value={"accountId": "account-1234"})
        self.api.getAllAccounts = Mock(return_value={"accounts": [self.account]})
        self.api.getAccountPreferences = Mock(return_value={"hedgingMode": True, "leverages": {"CURRENCIES": {"current": 30}}})
        self.api.getSingleMarketDetails = Mock(return_value=self.market)
        self.api.getAllPositions = Mock(side_effect=lambda: ({"positions": self.positions}, 200))
        self.api.getAllWorkingOrders = Mock(side_effect=lambda: {"workingOrders": self.working})
        self.api.getsinglePosition = Mock(side_effect=lambda deal: next((p for p in self.positions if p["position"]["dealId"] == deal), None))
        self.profile = {"verified_for_this_account": True, "account_fingerprint": fingerprint("account-1234"),
                        "currency": "USD", "legal_entity": "Reviewed legal entity", "client_classification": "retail",
                        "instrument": "EURUSD", "commission_per_side": "0", "conversion_fee_percent": "0",
                        "other_transaction_charges_roundtrip": "0", "reviewed_at": datetime.now(timezone.utc).date().isoformat(),
                        "source_url": "https://capital.com/en-eu/ways-to-trade/pricing",
                        "strategy_holding_costs_verified": True, "strategy_funding_allowance_usd": "0.10"}
        self.fee_path = self.directory / "fees.json"
        self.write_profile()
        self.args = arguments(["--REAL", "--max-margin", "5", "--max-exposure", "120", "--max-charges", "1",
                               "--max-loss", "2", "--max-orders", "2", "--stop-distance", "0.001",
                               "--fee-profile", str(self.fee_path)])
        self.gate = execution.LiveExecution(self.api, self.args, "EURUSD", 100)
        self.stack.callback(self.gate.release)
        self.api._live_execution = self.gate
        self.send = Mock(side_effect=self.fake_mutation)
        self.api._request = self.send
        self.api.getPositionOrderConfirmation = Mock(side_effect=self.fake_confirmation)

    def write_profile(self):
        self.fee_path.write_text(json.dumps(self.profile))
        self.fee_path.chmod(0o600)

    def fake_mutation(self, method, url, **kwargs):
        self.assertTrue(json.loads(self.gate.path.read_text())["pending"])
        if method == "post":
            payload = kwargs["json"]
            direction = payload["direction"]
            self.positions.append({"market": {"epic": "EURUSD"},
                                   "position": {"dealId": "owned", "direction": direction, "size": 100,
                                                "level": 1.1, "stopLevel": 1.099 if direction == "BUY" else 1.101}})
            self.last_direction = direction
            return Mock(status_code=200, json=Mock(return_value={"dealReference": "open-ref"}))
        self.assertEqual(method, "delete")
        self.assertTrue(url.endswith("/positions/owned"))
        self.positions[:] = [p for p in self.positions if p["position"]["dealId"] != "owned"]
        return Mock(status_code=200, json=Mock(return_value={"dealReference": "close-ref"}))

    def fake_confirmation(self, reference):
        if reference == "open-ref":
            return {"dealReference": reference, "dealStatus": "ACCEPTED", "dealId": "owned", "epic": "EURUSD",
                    "direction": self.last_direction, "size": 100, "affectedDeals": [{"dealId": "owned", "status": "OPENED"}]}
        return {"dealReference": reference, "dealStatus": "ACCEPTED"}

    def test_confirmed_buy_and_sell_send_stops_and_close_only_owned_positions(self):
        self.gate.arm()
        for direction in ("BUY", "SELL"):
            self.api.createPosition("EURUSD", direction, 100)
            self.assertEqual(self.send.call_args.kwargs["json"]["stopDistance"], 0.001)
            unrelated = {"market": {"epic": "EURUSD"}, "position": {"dealId": "unrelated"}}
            self.positions.append(unrelated)
            self.assertEqual([p.dealId for p in self.api.getAllPositionsList()], ["owned"])
            self.api.closePosition("owned")
            self.assertEqual(self.positions, [unrelated])
            self.positions.clear()
        self.assertEqual(self.send.call_count, 4)
        state = json.loads(self.gate.path.read_text())
        self.assertFalse(state["pending"])
        self.assertEqual(state["owned"], {})

    def test_unattended_live_preflight_arms_without_input_or_terminal(self):
        with patch("sys.stdin.isatty", return_value=False) as tty:
            self.gate.arm()
        self.assertTrue(self.gate.armed)
        self.input.assert_not_called()
        tty.assert_not_called()
        self.send.assert_not_called()

    def test_bare_real_flag_runs_actual_shared_preflight_with_defaults_and_existing_evidence(self):
        evidence = self.directory / ".live-integration/fees.json"
        evidence.parent.mkdir()
        evidence.write_text(json.dumps(self.profile))
        evidence.chmod(0o600)
        with patch.object(config, "ROOT", self.directory):
            args = arguments(["--REAL"])
        with patch("TradingApi.TradingApi", return_value=self.api), \
             patch.object(self.api, "createNewSession", return_value={}):
            api = config.create_trading_api(args, symbol=args.symbol, size=args.size)
        self.stack.callback(api._live_execution.release)
        self.assertTrue(api._live_execution.armed)
        self.assertEqual((args.max_orders, args.symbol, args.size), (1, "EURUSD", 100))
        self.input.assert_not_called()
        self.send.assert_not_called()

    def test_available_broker_costs_are_collected_without_fabricating_fees(self):
        risk = {"estimated_roundtrip_spread_cost": "0.02", "account_currency": "USD"}
        information = available_costs(self.market, risk)
        self.assertEqual(information["estimated_roundtrip_spread_cost"], "0.02")
        self.assertEqual(information["reported_buy_funding_rate"], "-0.01")
        self.assertEqual(information["reported_sell_funding_rate"], "0.001")
        self.assertFalse(information["complete"])
        self.assertIn("commission", information["missing_account_fee_information"])
        self.assertNotIn("commission_roundtrip", information)

    def test_missing_fee_evidence_blocks_automated_execution_without_any_order(self):
        self.args.fee_profile = self.directory / "missing-fee-evidence.json"
        with self.assertRaisesRegex(IntegrationError, "fee_schedule_unavailable_from_api"):
            self.gate.arm()
        self.assertFalse(self.gate.armed)
        self.input.assert_not_called()
        self.send.assert_not_called()

    def test_failed_cost_preflight_never_falls_back_to_demo(self):
        self.args.fee_profile = self.directory / "missing-fees.json"
        with patch("TradingApi.TradingApi", return_value=self.api) as factory, \
             patch.object(self.api, "createNewSession", return_value={}), \
             self.assertRaisesRegex(IntegrationError, "fee_schedule_unavailable_from_api"):
            config.create_trading_api(self.args, symbol="EURUSD", size=100)
        factory.assert_called_once_with(environment="live", account=None, auto_authenticate=False)
        self.send.assert_not_called()

    def test_unknown_fees_or_stale_unverified_evidence_cannot_enable_automated_trading(self):
        for field, invalid in (("commission_per_side", None), ("verified_for_this_account", False),
                               ("reviewed_at", "2000-01-01"), ("strategy_funding_allowance_usd", None)):
            with self.subTest(field=field), patch.dict(self.profile, {field: invalid}):
                self.write_profile()
                try:
                    with self.assertRaises(IntegrationError): self.gate.arm()
                    self.assertFalse(self.gate.armed)
                finally:
                    self.gate.release()
        self.send.assert_not_called()

    def test_equity_drawdown_from_peak_blocks_orders_even_above_starting_equity(self):
        self.gate.arm()
        self.account["balance"]["profitLoss"] = 10
        self.gate._snapshot("BUY")
        self.account["balance"]["profitLoss"] = 7
        with self.assertRaisesRegex(execution.LiveSafetyStop, "session_loss_limit"):
            self.api.createPosition("EURUSD", "BUY", 100)
        self.send.assert_not_called()

    def test_no_real_flag_cannot_arm_a_live_client(self):
        self.args.real = False
        with self.assertRaisesRegex(IntegrationError, "explicit_live_strategy_opt_in"):
            self.gate.arm()
        self.assertFalse(self.gate.armed)
        self.send.assert_not_called()

    def test_authentication_and_account_selection_alone_do_not_arm_gate(self):
        with self.assertRaises(execution.LiveSafetyStop): self.api.createPosition("EURUSD", "BUY", 100)
        self.send.assert_not_called()

    def test_market_specs_size_margin_cost_funding_stop_and_existing_exposure_block_arming(self):
        cases = ("closed", "delayed", "contract", "size", "margin", "funds", "fees", "funding", "stop", "loss", "positions", "orders", "direction", "netting")
        for case in cases:
            with self.subTest(case=case), ExitStack() as stack:
                def change(obj, key, value):
                    stack.enter_context(patch.dict(obj, {key: value}))
                if case == "closed": change(self.market["snapshot"], "marketStatus", "CLOSED")
                elif case == "delayed": change(self.market["snapshot"], "delayTime", 1)
                elif case == "contract": change(self.market["instrument"], "lotSize", 100000)
                elif case == "size": change(self.market["dealingRules"]["minDealSize"], "value", 200)
                elif case == "margin": stack.enter_context(patch.object(self.args, "max_margin", Decimal("1")))
                elif case == "funds": change(self.account["balance"], "available", 1)
                elif case == "fees": stack.enter_context(patch.object(self.args, "max_charges", Decimal("0.01")))
                elif case == "funding":
                    stack.enter_context(patch.dict(self.profile, {"strategy_holding_costs_verified": False}))
                    self.write_profile()
                elif case == "stop": stack.enter_context(patch.object(self.args, "stop_distance", Decimal("0.00001")))
                elif case == "loss": stack.enter_context(patch.object(self.args, "max_loss", Decimal("0.01")))
                elif case == "positions": self.positions.append({"position": {"dealId": "unrelated"}})
                elif case == "orders": self.working.append({"order": "unrelated"})
                elif case == "netting": change(self.api.getAccountPreferences.return_value, "hedgingMode", False)
                else: change(self.market["snapshot"], "marketModes", ["REGULAR", "LONG_ONLY"])
                try:
                    with self.assertRaises(IntegrationError): self.gate.arm()
                finally:
                    self.gate.release()
                    self.positions.clear()
                    self.working.clear()
            self.write_profile()
        self.send.assert_not_called()

    def test_fresh_market_checks_block_unsafe_order_after_startup(self):
        self.gate.arm()
        self.market["snapshot"]["offer"] = 2
        with self.assertRaises(execution.LiveSafetyStop):
            self.api.createPosition("EURUSD", "BUY", 100)
        self.assertFalse(self.gate.armed)
        self.send.assert_not_called()

    def test_open_order_count_limit_stops_new_orders(self):
        self.args.max_orders = 1
        self.gate.arm()
        self.api.createPosition("EURUSD", "BUY", 100)
        self.api.closePosition("owned")
        with self.assertRaises(execution.LiveSafetyStop): self.api.createPosition("EURUSD", "BUY", 100)
        self.assertEqual(self.send.call_count, 2)

    def test_cumulative_cost_limit_stops_second_entry(self):
        self.args.max_charges = Decimal("0.2")
        self.gate.arm()
        self.api.createPosition("EURUSD", "BUY", 100)
        self.api.closePosition("owned")
        with self.assertRaisesRegex(execution.LiveSafetyStop, "cumulative_charge_limit"):
            self.api.createPosition("EURUSD", "BUY", 100)
        self.assertEqual(self.send.call_count, 2)

    def test_loss_limit_blocks_entries_and_still_allows_owned_closure(self):
        self.gate.arm()
        self.api.createPosition("EURUSD", "BUY", 100)
        self.account["balance"]["profitLoss"] = -3
        self.api.closePosition("owned")
        with self.assertRaises(execution.LiveSafetyStop): self.api.createPosition("EURUSD", "BUY", 100)
        self.assertEqual(self.send.call_count, 2)

    def test_closing_unrelated_deal_is_blocked_even_for_same_symbol(self):
        self.gate.arm()
        with self.assertRaises(execution.LiveSafetyStop): self.api.closePosition("unrelated")
        self.send.assert_not_called()

    def test_token_replacement_requires_new_automated_preflight(self):
        self.gate.arm()
        self.api.cst = "different-session"
        with self.assertRaises(execution.LiveSafetyStop): self.api.createPosition("EURUSD", "BUY", 100)
        self.send.assert_not_called()

    def test_timeout_is_durable_and_never_retried(self):
        self.gate.arm()
        self.send.side_effect = requests.Timeout("private response must not be logged")
        with self.assertRaises(execution.LiveSafetyStop): self.api.createPosition("EURUSD", "BUY", 100)
        with self.assertRaises(execution.LiveSafetyStop): self.api.createPosition("EURUSD", "BUY", 100)
        self.send.assert_called_once()
        self.assertTrue(json.loads(self.gate.path.read_text())["pending"])
        self.gate.release()
        second = execution.LiveExecution(self.api, self.args, "EURUSD", 100)
        self.stack.callback(second.release)
        with self.assertRaisesRegex(IntegrationError, "manual_reconciliation"): second.arm()

    def test_keyboard_interrupt_during_submission_persists_intent(self):
        self.gate.arm()
        self.send.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt): self.api.createPosition("EURUSD", "BUY", 100)
        self.assertFalse(self.gate.armed)
        self.assertTrue(json.loads(self.gate.path.read_text())["pending"])
        self.send.assert_called_once()

    def test_confirmation_timeout_permits_only_bounded_reads_and_no_mutation_retry(self):
        self.gate.arm()
        self.api.getPositionOrderConfirmation.side_effect = None
        self.api.getPositionOrderConfirmation.return_value = None
        with patch.object(execution.time, "sleep"), self.assertRaises(execution.LiveSafetyStop):
            self.api.createPosition("EURUSD", "BUY", 100)
        self.assertEqual(self.api.getPositionOrderConfirmation.call_count, 5)
        self.send.assert_called_once()
        self.assertTrue(json.loads(self.gate.path.read_text())["pending"])

    def test_missing_broker_stop_stops_process_after_one_submission(self):
        self.gate.arm()
        send = self.fake_mutation
        def without_stop(*args, **kwargs):
            response = send(*args, **kwargs)
            self.positions[0]["position"].pop("stopLevel")
            return response
        self.send.side_effect = without_stop
        with self.assertRaises(execution.LiveSafetyStop): self.api.createPosition("EURUSD", "BUY", 100)
        self.send.assert_called_once()
        self.assertTrue(json.loads(self.gate.path.read_text())["pending"])

    def test_concurrent_live_strategy_is_blocked_before_any_mutation(self):
        self.gate.arm()
        second = execution.LiveExecution(self.api, self.args, "EURUSD", 100)
        self.stack.callback(second.release)
        with self.assertRaises(BlockingIOError): second.arm()
        self.send.assert_not_called()

    def test_close_timeout_never_retries_mutations(self):
        self.gate.arm()
        self.api.createPosition("EURUSD", "BUY", 100)
        self.send.side_effect = requests.Timeout("close uncertain")
        with self.assertRaises(execution.LiveSafetyStop): self.api.closePosition("owned")
        with self.assertRaises(execution.LiveSafetyStop): self.api.closePosition("owned")
        self.assertEqual(self.send.call_count, 2)
        self.assertTrue(json.loads(self.gate.path.read_text())["pending"])

    def test_incorrect_confirmation_never_grants_reusable_authorization(self):
        self.gate.arm()
        self.api.getPositionOrderConfirmation.side_effect = lambda ref: {**self.fake_confirmation(ref), "epic": "OTHER"}
        with self.assertRaises(execution.LiveSafetyStop): self.api.createPosition("EURUSD", "BUY", 100)
        self.send.assert_called_once()
        self.assertTrue(json.loads(self.gate.path.read_text())["pending"])

    def test_executed_position_blocks_restart_until_manually_reconciled(self):
        self.gate.arm()
        self.api.createPosition("EURUSD", "BUY", 100)
        self.gate.release()
        second = execution.LiveExecution(self.api, self.args, "EURUSD", 100)
        self.stack.callback(second.release)
        with self.assertRaisesRegex(IntegrationError, "manual_reconciliation"): second.arm()

    def test_live_stop_exits_legacy_catch_exception_loops(self):
        self.assertFalse(issubclass(execution.LiveSafetyStop, Exception))


if __name__ == "__main__":
    unittest.main()

import io
import os
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from TradingApi import TradingApi
from scripts import trading_config as config
import test_live_api as live
import test_api_key as demo


TEXT = 'X-CAP-API-KEY=fixture-key\nidentifier=fixture-login\npassword="fixture#custom$pass"\nCST=stale-cache\nX_SECURITY_TOKEN=stale-cache\n'


class SharedConfigurationTests(unittest.TestCase):
    def fixture(self, root, text=TEXT, mode=0o600):
        path = Path(root) / ".env"
        path.write_text(text)
        path.chmod(mode)
        return path

    def test_both_clients_use_literal_shared_credentials_ignoring_shell_and_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(directory)
            with patch.object(config, "ROOT", Path(directory)), patch.dict(os.environ, {
                "X-CAP-API-KEY": "wrong-shell-key", "identifier": "wrong-shell-login",
                "password": "wrong-shell-password", "CST": "wrong-shell-cache"}):
                api = TradingApi(auto_authenticate=False)
                client = live.LiveClient(live.load_live_configuration(path), environment="live")
                self.assertEqual(api.X_CAP_API_KEY, client.config["X-CAP-API-KEY"])
                self.assertEqual(api.IDENTIFIER, client.config["identifier"])
                self.assertEqual(api.PASSWORD, "fixture#custom$pass")
                self.assertEqual(api.PASSWORD, client.config["password"])
                self.assertEqual((api.cst, api.x_security_token), ("", ""))
                self.assertEqual(client.tokens, {})
                self.assertEqual(set(client.config), set(config.CREDENTIAL_KEYS))
                client.close()
            self.assertEqual(path.read_text(), TEXT)

    def test_default_is_demo_live_is_explicit_and_endpoint_cannot_be_retargeted(self):
        args = ("fixture-key", "fixture-login", "fixture-password")
        api = TradingApi(*args, auto_authenticate=False)
        self.assertEqual(api.BASE_URL, config.DEMO_URL)
        for kwargs in ({"environment": "live"}, {"real_account": True}):
            self.assertEqual(TradingApi(*args, auto_authenticate=False, **kwargs).BASE_URL, config.LIVE_URL)
        for kwargs in ({"environment": "invalid"}, {"environment": "live", "real_account": False},
                       {"environment": "demo", "real_account": True}):
            with self.assertRaises(ValueError): TradingApi(*args, auto_authenticate=False, **kwargs)
        for name, value in (("BASE_URL", config.LIVE_URL), ("environment", "live")):
            with self.assertRaises(AttributeError): setattr(api, name, value)
        with self.assertRaises(ValueError): TradingApi("partial", auto_authenticate=False)

    def test_tokens_are_per_client_and_legacy_cache_methods_never_touch_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(directory)
            with patch.object(config, "ROOT", Path(directory)):
                api = TradingApi(auto_authenticate=False)
                other = TradingApi(environment="live", auto_authenticate=False)
                for client, token in ((api, "demo-only"), (other, "live-only")):
                    reply = Mock(status_code=200, headers={"CST": token, "X-SECURITY-TOKEN": token})
                    reply.json.return_value = {}
                    with patch("TradingApi.requests.post", return_value=reply) as send:
                        client.createNewSession()
                    self.assertFalse(send.call_args.kwargs["allow_redirects"])
                    client.load_keys()
                    client.write_keys()
                self.assertEqual(api.cst, "demo-only")
                self.assertEqual(other.cst, "live-only")
                fresh = TradingApi(auto_authenticate=False)
                fresh.load_keys()
                self.assertEqual(fresh.cst, "")
                failure = Mock(status_code=401)
                with patch("TradingApi.requests.post", return_value=failure), redirect_stdout(io.StringIO()):
                    other.createNewSession()
                self.assertEqual((other.cst, other.x_security_token), ("", ""))
                self.assertEqual(api.cst, "demo-only")
            self.assertEqual(path.read_text(), TEXT)

    def test_invalid_file_is_rejected_without_values_in_error(self):
        cases = [TEXT + "EXTRA_CONFIGURATION=forbidden\n", TEXT + "password=duplicate-secret\n",
                 TEXT.replace('"fixture#custom$pass"', '" trailing-secret "'),
                 TEXT.replace('"fixture#custom$pass"', '"${SECRET}"'),
                 TEXT.replace('"fixture#custom$pass"', 'your_custom_api_password'),
                 TEXT.replace('"fixture#custom$pass"', 'secret # ambiguous'),
                 TEXT.replace('identifier=fixture-login\n', ''),
                 'password="unterminated\n']
        with tempfile.TemporaryDirectory() as directory:
            for text in cases:
                with self.subTest(case=cases.index(text)):
                    path = self.fixture(directory, text)
                    with self.assertRaises(config.ConfigurationError) as result:
                        config.load_credentials(path)
                    self.assertNotIn("secret", str(result.exception))
            path = self.fixture(directory, mode=0o644)
            with self.assertRaises(config.ConfigurationError): config.load_credentials(path)
            target = Path(directory) / "linked"
            target.symlink_to(path)
            with self.assertRaises(config.ConfigurationError): config.load_credentials(target)

    def test_readonly_live_cli_loads_shared_file_and_preserves_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.fixture(root)
            output = io.StringIO()
            client = Mock(calls=[], auth_attempts=1, open_attempts=0, close_attempts=0)
            with patch.object(live, "ROOT", root), patch.object(live, "LiveClient", return_value=client) as factory, \
                 patch.object(live, "live_preflight", return_value={"phase1_passed": False}) as preflight, \
                 redirect_stdout(output):
                self.assertEqual(live.main(["--environment", "live"]), 1)
            factory.assert_called_once_with(config.load_credentials(path), execute=False, environment="live")
            self.assertIsNone(preflight.call_args.kwargs["selected_account"])
            self.assertEqual(path.read_text(), TEXT)
            self.assertNotIn("fixture-key", output.getvalue())
            client.request.assert_not_called()

    def test_integration_cli_selects_live_without_routing_to_old_single_buy_checker(self):
        from scripts import integration_roundtrip as workflow
        with patch.object(demo, "load_credentials") as load, patch.object(demo, "TradingApi") as factory, \
             patch.object(workflow, "run_integration", return_value=7) as run:
            self.assertEqual(demo.main(["--environment", "live"]), 7)
        self.assertEqual(run.call_args.args[0].environment, "live")
        load.assert_not_called()
        factory.assert_not_called()

    def test_generic_live_api_checks_accounts_before_any_position_mutation(self):
        row = {"accountId": "active", "status": "ENABLED", "accountType": "CFD", "demo": False}
        api = TradingApi("fixture-key", "fixture-login", "fixture-password", environment="live", auto_authenticate=False)
        api._login_has_active_environment = True
        api._login_account_id = "active"
        with patch.object(api, "getSessionDetails", return_value={"accountId": "active"}), \
             patch.object(api, "getAllAccounts", return_value={"accounts": [row, {**row, "accountId": "other"}]}), \
             patch("TradingApi.requests.post") as post, patch("TradingApi.requests.delete") as delete:
            for callback in (lambda: api.createPosition("EURUSD", "BUY", 100), lambda: api.closePosition("anything")):
                with self.assertRaisesRegex(RuntimeError, "multiple_live_accounts_require_explicit_selection"):
                    callback()
            post.assert_not_called()
            delete.assert_not_called()
            api._account_selection = "active"
            self.assertEqual(api.verify_account_environment(), row)
            api._account_selection = "other"
            with self.assertRaisesRegex(RuntimeError, "selected_account_not_active"):
                api.verify_account_environment()
            api._account_selection = "active"
            api._login_has_active_environment = False
            with self.assertRaisesRegex(RuntimeError, "authenticated_environment_not_verified"):
                api.verify_account_environment()

    def test_generic_environment_verification_rejects_opposite_environment_account(self):
        for environment, wrong_demo in (("demo", False), ("live", True)):
            api = TradingApi("fixture-key", "fixture-login", "fixture-password", environment=environment, auto_authenticate=False)
            api._login_has_active_environment = True
            api._login_account_id = "active"
            row = {"accountId": "active", "status": "ENABLED", "accountType": "CFD", "demo": wrong_demo}
            with patch.object(api, "getSessionDetails", return_value={"accountId": "active"}), \
                 patch.object(api, "getAllAccounts", return_value={"accounts": [row]}), self.assertRaisesRegex(RuntimeError, "environment_mismatch"):
                api.verify_account_environment()

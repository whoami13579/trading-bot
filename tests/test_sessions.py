import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import requests
from TradingApi import TradingApi


def response(status=200, payload=None, headers=None):
    return Mock(status_code=status, headers=headers or {}, text="sensitive-response-marker",
                json=Mock(return_value=payload or {"status": "OK"}))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(requests.sessions.Session, "send",
                                    side_effect=AssertionError("Network forbidden in unit tests"))
        self.network.start()
        self.addCleanup(self.network.stop)
        with patch.object(TradingApi, "pingService"):
            self.api = TradingApi("dummy-key", "dummy-login", "dummy-password", real_account=False)

    def test_plaintext_login_explicitly_disables_encryption(self):
        with patch("TradingApi.requests.post", return_value=response(headers={"CST": "dummy-cst", "X-SECURITY-TOKEN": "dummy-token"})) as post:
            self.assertEqual(self.api.getCstAndXSecurityToken(), ("dummy-cst", "dummy-token"))
        self.assertIs(post.call_args.kwargs["json"].get("encryptedPassword"), False)
        self.assertEqual(post.call_args.args[0], self.api.BASE_URL + "/api/v1/session")

    def test_prepared_login_matches_documented_header_and_json_contract(self):
        import json
        reply = requests.Response()
        reply.status_code = 200
        reply.headers.update({"CST": "dummy-cst", "X-SECURITY-TOKEN": "dummy-token"})
        reply._content = b'{}'
        self.api.PASSWORD = "dummy#custom$password"
        with patch.object(requests.sessions.Session, "send", return_value=reply) as send:
            self.api.createNewSession()
        prepared = send.call_args.args[0]
        self.assertEqual(prepared.method, "POST")
        self.assertEqual(prepared.url, "https://demo-api-capital.backend-capital.com/api/v1/session")
        self.assertEqual(prepared.headers["X-CAP-API-KEY"], "dummy-key")
        self.assertEqual(prepared.headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(prepared.body), {"identifier": "dummy-login",
                                                   "password": "dummy#custom$password", "encryptedPassword": False})

    def test_encryption_key_get_has_no_json_body_and_has_a_timeout(self):
        reply = requests.Response()
        reply.status_code = 200
        reply._content = b'{"encryptionKey":"dummy-public-key","timeStamp":123}'
        with patch.object(requests.sessions.Session, "send", return_value=reply) as send:
            self.api.getEncryptionKey()
        prepared = send.call_args.args[0]
        self.assertEqual(prepared.method, "GET")
        self.assertEqual(prepared.url, "https://demo-api-capital.backend-capital.com/api/v1/session/encryptionKey")
        self.assertIsNone(prepared.body)
        self.assertNotIn("Content-Type", prepared.headers)
        self.assertEqual(prepared.headers["X-CAP-API-KEY"], "dummy-key")
        self.assertEqual(send.call_args.kwargs["timeout"], (5, 20))

    def test_close_position_is_a_single_body_free_delete_with_timeout(self):
        self.api.cst, self.api.x_security_token = "dummy-cst", "dummy-token"
        reply = requests.Response()
        reply.status_code = 200
        reply._content = b'{"dealReference":"p_close"}'
        with patch.object(requests.sessions.Session, "send", return_value=reply) as send:
            self.assertEqual(self.api.closePosition("owned"), ({"dealReference": "p_close"}, 200))
        send.assert_called_once()
        prepared = send.call_args.args[0]
        self.assertEqual(prepared.method, "DELETE")
        self.assertEqual(prepared.url, "https://demo-api-capital.backend-capital.com/api/v1/positions/owned")
        self.assertIsNone(prepared.body)
        self.assertEqual(send.call_args.kwargs["timeout"], (5, 20))

    def test_login_rejects_incomplete_token_headers(self):
        with patch("TradingApi.requests.post", return_value=response(headers={"CST": "dummy-cst"})), redirect_stdout(io.StringIO()):
            self.assertIsNone(self.api.getCstAndXSecurityToken())

    def test_login_failure_does_not_print_response_body(self):
        output = io.StringIO()
        with patch("TradingApi.requests.post", return_value=response(status=401)), redirect_stdout(output):
            self.assertIsNone(self.api.getCstAndXSecurityToken())
        self.assertNotIn("sensitive-response-marker", output.getvalue())

    def test_ping_returns_consistent_tuple_after_refresh(self):
        with patch("TradingApi.requests.get", side_effect=[response(401), response()]), \
             patch.object(self.api, "getCstAndXSecurityToken", return_value=("new-cst", "new-token")):
            self.assertEqual(self.api.pingService(), ({"status": "OK"}, 200))
        self.assertEqual((self.api.cst, self.api.x_security_token), ("new-cst", "new-token"))

    def test_ping_authenticated_session_does_not_login(self):
        with patch("TradingApi.requests.get", return_value=response()), \
             patch.object(self.api, "getCstAndXSecurityToken") as login:
            self.assertEqual(self.api.pingService(), ({"status": "OK"}, 200))
        login.assert_not_called()

    def test_load_keys_preserves_fresh_in_memory_tokens(self):
        self.api.cst, self.api.x_security_token = "fresh-cst", "fresh-token"
        with patch.dict(os.environ, {"CST": "", "X_SECURITY_TOKEN": ""}):
            self.api.load_keys()
        self.assertEqual((self.api.cst, self.api.x_security_token), ("fresh-cst", "fresh-token"))

    def test_create_session_updates_in_memory_tokens(self):
        with patch("TradingApi.requests.post", return_value=response(headers={"CST": "new-cst", "X-SECURITY-TOKEN": "new-token"})):
            self.assertIsInstance(self.api.createNewSession(), dict)
        self.assertEqual((self.api.cst, self.api.x_security_token), ("new-cst", "new-token"))

    def test_two_hour_prices_return_requested_two_hour_samples(self):
        payload = {"prices": [{"closePrice": {"ask": p}} for p in range(1, 9)]}
        with patch.object(self.api, "getHistoricalPrices", return_value=(payload, 200)) as history:
            self.assertEqual(self.api.getHistoricalPricesList("EURUSD", "HOUR_2", 4), [2, 4, 6, 8])
        history.assert_called_once_with("EURUSD", "HOUR", 8)

    def test_read_only_endpoint_wrappers_with_mocked_responses(self):
        for name, arguments, path in (("getServerTime", (), "/time"),
                                      ("getSessionDetails", (), "/session"),
                                      ("getAllAccounts", (), "/accounts"),
                                      ("getSingleMarketDetails", ("EURUSD",), "/markets/EURUSD"),
                                      ("getHistoricalPrices", ("EURUSD", "MINUTE_30", 210), "/prices/EURUSD")):
            with self.subTest(endpoint=name), patch("TradingApi.requests.get", return_value=response()) as get:
                actual = getattr(self.api, name)(*arguments)
                self.assertIsNotNone(actual)
                self.assertTrue(get.call_args.args[0].startswith(self.api.BASE_URL + "/api/v1" + path))

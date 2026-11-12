"""Mocked transports only: no .env access, API traffic, or actual order submissions."""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import runpy
import socket
import unittest
from unittest.mock import Mock, patch

import requests
from urllib3.exceptions import MaxRetryError, NameResolutionError, ReadTimeoutError

from TradingApi import TradingApi
from scripts.network_errors import TradingUnavailable, network_failure_reason
from scripts import trading_config


def dns_error():
    cause = socket.gaierror(socket.EAI_AGAIN, "private-password-marker")
    reason = NameResolutionError("private-proxy-marker", None, cause)
    return requests.ConnectionError(MaxRetryError(None, "/private-token-marker", reason))


def response(status=200, payload=None):
    reply = requests.Response()
    reply.status_code = status
    reply._content = b'{}' if payload is None else payload
    reply.headers.update({"CST": "fixture-cst", "X-SECURITY-TOKEN": "fixture-token"})
    return reply


class NetworkFailureTests(unittest.TestCase):
    def client(self):
        api = TradingApi("fixture-key", "fixture-login", "fixture-password", auto_authenticate=False)
        api.cst, api.x_security_token = "fixture-cst", "fixture-token"
        return api

    def test_typed_nested_dns_and_connection_errors_have_distinct_categories(self):
        cases = [(dns_error(), "dns_resolution_failed"),
                 (requests.ConnectionError("secret"), "connection_failed"),
                 (requests.ConnectTimeout("secret"), "connection_timeout"),
                 (requests.ReadTimeout("secret"), "read_timeout"),
                 (requests.Timeout("secret"), "network_timeout"),
                 (requests.exceptions.ProxyError(dns_error()), "proxy_connection_failed"),
                 (requests.exceptions.SSLError("secret"), "tls_connection_failed"),
                 (requests.RequestException("secret"), "network_request_failed"),
                 (socket.gaierror(socket.EAI_NONAME, "secret"), "dns_resolution_failed"),
                 (requests.ConnectionError(ReadTimeoutError(None, "/secret", "secret")), "read_timeout")]
        for error, category in cases:
            with self.subTest(category=category):
                self.assertEqual(network_failure_reason(error), category)

    def test_exception_chain_cycles_are_safe_and_text_is_not_used_for_classification(self):
        error = requests.ConnectionError("socket.gaierror: secret")
        error.__cause__ = error
        self.assertEqual(network_failure_reason(error), "connection_failed")

    def test_login_dns_failure_is_one_attempt_and_never_discloses_details(self):
        api = self.client()
        with patch("TradingApi.requests.post", side_effect=dns_error()) as post, \
             self.assertRaises(TradingUnavailable) as stopped:
            api.createNewSession()
        post.assert_called_once()
        self.assertEqual(stopped.exception.reason, "dns_resolution_failed")
        self.assertNotIn("401", str(stopped.exception))
        self.assertNotIn("private-", str(stopped.exception))
        self.assertEqual((api.cst, api.x_security_token), ("", ""))
        self.assertEqual(post.call_args.kwargs["timeout"], (5, 20))

    def test_failed_history_never_becomes_prices_or_a_signal_and_latches_all_requests(self):
        for error in (dns_error(), requests.ReadTimeout("secret"), requests.ConnectionError("secret")):
            with self.subTest(error=type(error).__name__):
                api = self.client()
                with patch("TradingApi.requests.get", side_effect=error) as get, \
                     self.assertRaises(TradingUnavailable):
                    api.getHistoricalPricesList("EURUSD", "MINUTE_30", 210)
                get.assert_called_once()
                with patch.object(requests.sessions.Session, "send") as send:
                    for operation in (api.getServerTime, api.createNewSession,
                                      lambda: api.createPosition("EURUSD", "BUY", 100),
                                      lambda: api.closePosition("fixture-id")):
                        with self.assertRaises(TradingUnavailable):
                            operation()
                    send.assert_not_called()

    def test_legacy_exception_handler_cannot_swallow_network_stop(self):
        api = self.client()
        recovered = Mock()
        with patch("TradingApi.requests.get", side_effect=dns_error()), self.assertRaises(TradingUnavailable):
            try:
                api.getHistoricalPricesList("EURUSD", "MINUTE", 210)
            except Exception:
                recovered()
        recovered.assert_not_called()

    def test_position_changing_timeouts_send_once_and_block_every_followup(self):
        for method, operation in (("post", lambda api: api.createPosition("EURUSD", "BUY", 100)),
                                  ("delete", lambda api: api.closePosition("fixture-id"))):
            with self.subTest(method=method):
                api = self.client()
                with patch(f"TradingApi.requests.{method}", side_effect=requests.ReadTimeout("private-token")) as send:
                    with self.assertRaises(TradingUnavailable) as stopped:
                        operation(api)
                    self.assertIn("outcome may be uncertain", str(stopped.exception))
                    with self.assertRaises(TradingUnavailable):
                        operation(api)
                    send.assert_called_once()
                self.assertNotIn("private-token", str(stopped.exception))

    def test_unauthenticated_client_cannot_send_a_mutation(self):
        api = self.client()
        api.cst = api.x_security_token = ""
        with patch.object(requests.sessions.Session, "send") as send, self.assertRaises(TradingUnavailable):
            api.createPosition("EURUSD", "SELL", 100)
        send.assert_not_called()

    def test_http_auth_failure_differs_from_dns_and_blocks_orders(self):
        api = self.client()
        with patch("TradingApi.requests.post", return_value=response(401)) as post, redirect_stdout(io.StringIO()):
            self.assertIsNone(api.createNewSession())
        post.assert_called_once()
        with patch.object(requests.sessions.Session, "send") as send, self.assertRaises(TradingUnavailable) as stopped:
            api.createPosition("EURUSD", "BUY", 100)
        self.assertEqual(stopped.exception.reason, "authentication_failed_http_401")
        self.assertIn("HTTP 401", str(stopped.exception))
        send.assert_not_called()

    def test_http_errors_and_invalid_json_stop_before_market_data_is_used(self):
        for status, body, category in ((401, b'{}', "authentication_failed_http_401"),
                                       (403, b'{}', "permission_denied_http_403"),
                                       (503, b'<html>private-password</html>', "http_503"),
                                       (429, b'{}', "http_429"),
                                       (200, b'private-password', "invalid_api_response")):
            with self.subTest(status=status):
                api = self.client()
                with patch("TradingApi.requests.get", return_value=response(status, body)), self.assertRaises(TradingUnavailable) as stopped:
                    api.getHistoricalPricesList("EURUSD", "MINUTE", 210)
                self.assertEqual(stopped.exception.reason, category)
                self.assertNotIn("private-password", str(stopped.exception))

    def test_shared_factory_stops_with_a_sanitized_auth_failure(self):
        args = Mock(real=False, account=None)
        api = self.client()
        with patch("TradingApi.TradingApi", return_value=api), \
             patch("TradingApi.requests.post", return_value=response(401)), \
             patch.object(api, "verify_account_environment") as verify, \
             redirect_stdout(io.StringIO()), self.assertRaises(TradingUnavailable) as stopped:
            trading_config.create_trading_api(args)
        self.assertEqual(stopped.exception.reason, "authentication_failed_http_401")
        verify.assert_not_called()

    def test_order_script_exits_one_without_traceback_or_order_on_dns_failure(self):
        output = io.StringIO()
        with patch("sys.argv", ["test.py"]), \
             patch("TradingApi.load_credentials", return_value={"X-CAP-API-KEY": "fixture-key",
                                                                 "identifier": "fixture-login", "password": "fixture-password"}), \
             patch("TradingApi.requests.post", side_effect=dns_error()) as post, \
             patch.object(TradingApi, "createPosition") as order, redirect_stderr(output), \
             self.assertRaises(SystemExit) as stopped:
            runpy.run_path(str(Path(__file__).resolve().parents[1] / "test.py"), run_name="__main__")
        self.assertEqual(stopped.exception.code, 1)
        self.assertIn("DNS resolution failed", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())
        self.assertNotIn("private-", output.getvalue())
        post.assert_called_once()
        order.assert_not_called()

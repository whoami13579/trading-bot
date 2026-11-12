from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from scripts.demo_readonly import DEMO_HOST, DemoReadOnlyTransport, SafetyError, sanitized_auth_error, validate_request
from scripts import demo_readonly


class DemoSafetyTests(unittest.TestCase):
    def test_only_explicit_demo_read_requests_and_login_are_allowed(self):
        for method, path in (("POST", "/session"), ("GET", "/ping"), ("GET", "/time"),
                             ("GET", "/session"), ("GET", "/accounts"),
                             ("GET", "/markets/EURUSD"), ("GET", "/prices/EURUSD")):
            validate_request(method, f"https://{DEMO_HOST}/api/v1{path}")

    def test_live_endpoint_and_lookalikes_are_blocked(self):
        for url in ("https://api-capital.backend-capital.com/api/v1/session",
                    f"http://{DEMO_HOST}/api/v1/session", f"https://{DEMO_HOST}.evil.test/api/v1/session",
                    f"https://{DEMO_HOST}:8443/api/v1/session", f"https://user@{DEMO_HOST}/api/v1/session"):
            with self.subTest(url=url), self.assertRaises(SafetyError):
                validate_request("POST", url)

    def test_mutating_requests_and_unlisted_reads_are_blocked(self):
        for method, path in (("POST", "/positions"), ("PUT", "/positions/example"),
                             ("DELETE", "/positions/example"), ("PUT", "/session"),
                             ("POST", "/accounts/topUp"), ("POST", "/workingorders"),
                             ("GET", "/positions")):
            with self.subTest(method=method, path=path), self.assertRaises(SafetyError):
                validate_request(method, f"https://{DEMO_HOST}/api/v1{path}")

    def test_prepared_live_request_never_reaches_transport(self):
        request = requests.Request("POST", "https://api-capital.backend-capital.com/api/v1/session").prepare()
        transport = DemoReadOnlyTransport()
        transport.original_send = Mock(side_effect=AssertionError("Network must not be reached"))
        with self.assertRaises(SafetyError):
            transport.send(requests.Session(), request)
        transport.original_send.assert_not_called()

    def test_redirects_tls_timeouts_and_rate_limit(self):
        request = requests.Request("GET", f"https://{DEMO_HOST}/api/v1/ping").prepare()
        transport = DemoReadOnlyTransport()
        transport.original_send = Mock(return_value=Mock(status_code=200))
        transport.last_sent = 100.0
        with patch("scripts.demo_readonly.time.monotonic", return_value=100.1), \
             patch("scripts.demo_readonly.time.sleep") as sleep:
            transport.send(requests.Session(), request, verify=False, allow_redirects=True)
        self.assertAlmostEqual(sleep.call_args.args[0], 1.0)
        kwargs = transport.original_send.call_args.kwargs
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertIs(kwargs["verify"], True)
        self.assertEqual(kwargs["timeout"], (5, 20))
        self.assertEqual(kwargs["proxies"], {})

    def test_redirect_response_is_rejected(self):
        transport = DemoReadOnlyTransport()
        transport.original_send = Mock(return_value=Mock(status_code=302))
        request = requests.Request("GET", f"https://{DEMO_HOST}/api/v1/ping").prepare()
        with self.assertRaises(SafetyError):
            transport.send(requests.Session(), request)

    def test_second_authentication_attempt_is_blocked_before_sending(self):
        transport = DemoReadOnlyTransport()
        transport.original_send = Mock(return_value=Mock(status_code=200))
        request = requests.Request("POST", f"https://{DEMO_HOST}/api/v1/session").prepare()
        transport.send(requests.Session(), request)
        with self.assertRaises(SafetyError):
            transport.send(requests.Session(), request)
        self.assertEqual(transport.auth_attempts, 1)
        transport.original_send.assert_called_once()

    def test_sanitized_auth_errors_never_return_arbitrary_server_text(self):
        self.assertEqual(sanitized_auth_error({"errorCode": "error.public-api.invalid.api.key"}),
                         "error.public-api.invalid.api.key")
        for payload in ({"errorCode": "private-value-marker"}, {"errorCode": ["unexpected"]}, "raw-response", {}):
            with self.subTest(payload=payload):
                self.assertEqual(sanitized_auth_error(payload), "unclassified_authentication_error")

    def test_failed_authentication_stops_before_any_followup_endpoint(self):
        def unauthorized(*args, **kwargs):
            result = requests.Response()
            result.status_code = 401
            result._content = b'{"errorCode":"error.public-api.invalid.api.key"}'
            return result

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / ".env"
            env.write_text("X-CAP-API-KEY=unit-key-marker\nidentifier=unit-login-marker\npassword=unit-password-marker\n")
            env.chmod(0o600)
            values = {"X-CAP-API-KEY": "unit-key-marker", "identifier": "unit-login-marker", "password": "unit-password-marker"}
            output = io.StringIO()
            with patch.object(demo_readonly, "ROOT", root), \
                 patch.object(demo_readonly, "dotenv_values", side_effect=[values, {}]), \
                 patch.object(demo_readonly.subprocess, "run", side_effect=[Mock(returncode=0), Mock(returncode=1)]), \
                 patch.object(requests.sessions.Session, "send", side_effect=unauthorized), \
                 patch.object(demo_readonly.time, "sleep"), redirect_stdout(output):
                self.assertEqual(demo_readonly.main(), 1)
            report = json.loads((root / "DEMO_API_RESULTS.json").read_text())
            self.assertEqual(report["authentication_attempts"], 1)
            self.assertEqual([(r["method"], r["path"]) for r in report["requests"]],
                             [("GET", "/api/v1/ping"), ("POST", "/api/v1/session")])
            self.assertEqual(report["endpoints"]["authentication"]["error_code"], "error.public-api.invalid.api.key")
            self.assertIn("skipped", report["endpoints"]["getServerTime"])
            self.assertEqual(env.read_text(), "X-CAP-API-KEY=unit-key-marker\nidentifier=unit-login-marker\npassword=unit-password-marker\n")
            for secret in values.values():
                self.assertNotIn(secret, output.getvalue())
                self.assertNotIn(secret, (root / "DEMO_API_RESULTS.json").read_text())

    def test_successful_login_followed_by_unauthorized_ping_stops(self):
        def reply(session, request, **kwargs):
            result = requests.Response()
            result._content = b'{}'
            if request.method == "POST":
                result.status_code = 200
                result.headers.update({"CST": "dummy-cst", "X-SECURITY-TOKEN": "dummy-token"})
            elif request.url.endswith("/ping"):
                result.status_code = 401
            else:
                raise SafetyError("Stop after mocked authentication sequence")
            return result

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("X-CAP-API-KEY=unit-key\nidentifier=unit-login\npassword=unit-password\n")
            (root / ".env").chmod(0o600)
            values = {"X-CAP-API-KEY": "unit-key", "identifier": "unit-login", "password": "unit-password"}
            with patch.object(demo_readonly, "ROOT", root), \
                 patch.object(demo_readonly, "dotenv_values", side_effect=[values, {}]), \
                 patch.object(demo_readonly.subprocess, "run", side_effect=[Mock(returncode=0), Mock(returncode=1)]), \
                 patch.object(requests.sessions.Session, "send", reply), \
                 patch.object(demo_readonly.time, "sleep"), redirect_stdout(io.StringIO()):
                self.assertEqual(demo_readonly.main(), 1)
            report = json.loads((root / "DEMO_API_RESULTS.json").read_text())
            self.assertFalse(report["endpoints"]["authentication"]["passed"])
            self.assertEqual(report["endpoints"]["authentication"]["error_category"], "authentication_failed_http_401")
            self.assertEqual(report["endpoints"]["authentication"]["http_status"], 200)
            self.assertEqual([r["status"] for r in report["requests"]], [401, 200, 401])

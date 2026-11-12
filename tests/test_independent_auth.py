from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from scripts import diagnose_demo_auth as independent

VALUES = {"X-CAP-API-KEY": "dummy-key-marker", "identifier": "dummy-login-marker",
          "password": "dummy#custom$password-\u00e9"}


def connection_reply(status=200, payload=None, headers=None):
    response = Mock(status=status)
    response.read.return_value = json.dumps({} if payload is None else payload).encode()
    mapping = {"Content-Type": "application/json", **(headers or {})}
    response.getheader.side_effect = lambda key, default=None: mapping.get(key, default)
    connection = Mock()
    connection.getresponse.return_value = response
    return connection


class IndependentAuthenticationTests(unittest.TestCase):
    def test_prepare_matches_stdlib_json_and_preserves_special_characters(self):
        summary = independent.prepare_summary(VALUES)
        self.assertTrue(all(v for v in summary.values() if isinstance(v, bool)))
        self.assertNotIn(VALUES["password"], json.dumps(summary))
        self.assertEqual(summary["base_url"], "https://demo-api-capital.backend-capital.com")

    def test_permission_required_at_cli_and_callable_boundaries(self):
        with patch.object(independent.http.client, "HTTPSConnection") as factory:
            for probe in ("session", "encryption-key"):
                with self.subTest(probe=probe), self.assertRaises(PermissionError):
                    independent.probe_once(VALUES, probe)
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                    independent.main(["--probe", probe])
                self.assertEqual(exc.exception.code, 2)
            factory.assert_not_called()

    def test_default_mode_never_connects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / ".env"
            env.write_text("dummy-only\n")
            output = io.StringIO()
            with patch.object(independent, "ROOT", root), \
                 patch.object(independent, "inspect_credentials", return_value=(VALUES, {"ready_for_request": True})), \
                 patch.object(independent.http.client, "HTTPSConnection") as factory, redirect_stdout(output):
                self.assertEqual(independent.main([]), 0)
            factory.assert_not_called()
            result = json.loads(output.getvalue())
            self.assertEqual(result["network_requests"], 0)
            self.assertEqual(result["authentication_attempts"], 0)
            self.assertEqual(env.read_text(), "dummy-only\n")
            for value in VALUES.values():
                self.assertNotIn(value, output.getvalue())

    def test_single_stdlib_session_request_has_correct_contract(self):
        connection = connection_reply(headers={"CST": "dummy-cst-marker", "X-SECURITY-TOKEN": "dummy-token-marker"})
        with patch.object(independent.http.client, "HTTPSConnection", return_value=connection) as factory:
            result = independent.probe_once(VALUES, "session", approved=True)
        self.assertTrue(result["passed"])
        self.assertTrue(result["tokens_obtained"])
        self.assertEqual(result["authentication_attempts"], 1)
        factory.assert_called_once()
        self.assertEqual(factory.call_args.args[0], independent.DEMO_HOST)
        self.assertEqual(factory.call_args.kwargs["timeout"], 15)
        context = factory.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertIsNone(context.keylog_filename)
        connection.request.assert_called_once()
        self.assertEqual(connection.request.call_args.args, ("POST", "/api/v1/session"))
        payload = json.loads(connection.request.call_args.kwargs["body"])
        self.assertEqual(payload, {"identifier": VALUES["identifier"], "password": VALUES["password"], "encryptedPassword": False})
        self.assertEqual(connection.request.call_args.kwargs["headers"]["X-CAP-API-KEY"], VALUES["X-CAP-API-KEY"])
        connection.close.assert_called_once()
        for secret in (*VALUES.values(), "dummy-cst-marker", "dummy-token-marker"):
            self.assertNotIn(secret, json.dumps(result))

    def test_network_session_does_not_use_requests_transport_response_or_ca_lookup(self):
        connection = connection_reply(401, {"errorCode": "error.invalid.details"})
        with patch.object(independent.http.client, "HTTPSConnection", return_value=connection), \
             patch.object(requests.sessions.Session, "send", side_effect=AssertionError("requests transport forbidden")), \
             patch.object(requests, "Response", side_effect=AssertionError("requests response wrapper forbidden")), \
             patch.object(requests.certs, "where", side_effect=AssertionError("requests CA lookup forbidden")):
            result = independent.probe_once(VALUES, "session", approved=True)
        self.assertEqual(result["http_status"], 401)
        self.assertEqual(result["error_code"], "error.invalid.details")
        self.assertFalse(result["cst_header_present"])
        self.assertFalse(result["security_token_header_present"])
        self.assertFalse(result["tokens_obtained"])
        connection.request.assert_called_once()

    def test_key_probe_sends_no_password_identifier_or_session_request(self):
        connection = connection_reply(payload={"encryptionKey": "public-key-marker", "timeStamp": 123})
        with patch.object(independent.http.client, "HTTPSConnection", return_value=connection):
            result = independent.probe_once(VALUES, "encryption-key", approved=True)
        self.assertTrue(result["passed"])
        self.assertEqual(result["authentication_attempts"], 0)
        self.assertTrue(result["encryption_key_present"])
        self.assertTrue(result["timestamp_present"])
        self.assertEqual(connection.request.call_args.args, ("GET", "/api/v1/session/encryptionKey"))
        self.assertIsNone(connection.request.call_args.kwargs["body"])
        self.assertEqual(set(connection.request.call_args.kwargs["headers"]), {"X-CAP-API-KEY", "Accept-Encoding"})
        self.assertNotIn("public-key-marker", json.dumps(result))

    def test_rejected_key_probe_reports_field_absence_without_values_or_retries(self):
        connection = connection_reply(401, {"errorCode": "error.invalid.api.key"})
        with patch.object(independent.http.client, "HTTPSConnection", return_value=connection):
            result = independent.probe_once(VALUES, "encryption-key", approved=True)
        self.assertEqual(result["http_status"], 401)
        self.assertFalse(result["encryption_key_present"])
        self.assertFalse(result["timestamp_present"])
        self.assertEqual(result["authentication_attempts"], 0)
        connection.request.assert_called_once()

    def test_failed_session_and_key_probes_are_classified_without_retries(self):
        for probe, status, code, category in (("session", 401, "error.invalid.details", "invalid_login_details"),
                                               ("session", 401, "error.invalid.api.key", "invalid_api_key"),
                                               ("session", 429, "error.too-many.requests", "rate_limited"),
                                               ("encryption-key", 401, "private-server-marker", "unknown_error_code")):
            with self.subTest(probe=probe, status=status):
                connection = connection_reply(status, {"errorCode": code})
                with patch.object(independent.http.client, "HTTPSConnection", return_value=connection):
                    result = independent.probe_once(VALUES, probe, approved=True)
                self.assertFalse(result["passed"])
                self.assertEqual(result["http_status"], status)
                self.assertEqual(result["error_category"], category)
                connection.request.assert_called_once()
                self.assertNotIn("private-server-marker", json.dumps(result))

    def test_redirect_and_non_json_response_do_not_trigger_followups(self):
        for status in (302, 401):
            connection = connection_reply(status)
            connection.getresponse.return_value.read.return_value = b'<html>private-server-marker</html>'
            with patch.object(independent.http.client, "HTTPSConnection", return_value=connection):
                result = independent.probe_once(VALUES, "session", approved=True)
            self.assertEqual(result["error_category"], "redirect_refused" if status == 302 else "non_json_response")
            connection.request.assert_called_once()
            self.assertNotIn("private-server-marker", json.dumps(result))

    def test_network_exception_messages_never_escape_main(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("dummy-only\n")
            output = io.StringIO()
            with patch.object(independent, "ROOT", root), \
                 patch.object(independent, "inspect_credentials", return_value=(VALUES, {"ready_for_request": True})), \
                 patch.object(independent.http.client, "HTTPSConnection", side_effect=TimeoutError("private-server-marker")) as factory, \
                 redirect_stdout(output):
                self.assertEqual(independent.main(["--probe", "session", "--approve-demo-request"]), 1)
            factory.assert_called_once()
            self.assertNotIn("private-server-marker", output.getvalue())
            result = json.loads(output.getvalue())
            self.assertEqual(result["error_type"], "TimeoutError")
            self.assertEqual(result["authentication_attempts"], 1)

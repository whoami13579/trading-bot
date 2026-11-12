from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from scripts.demo_readonly import AUTH_ERROR_CATEGORIES, DemoReadOnlyTransport, authentication_error_details, inspect_credentials


def error_response(payload=None, content_type="application/json", invalid_json=False):
    response = Mock(headers={"Content-Type": content_type})
    response.json = Mock(side_effect=ValueError("private-body-marker")) if invalid_json else Mock(return_value=payload)
    return response


class AuthenticationDiagnosticsTests(unittest.TestCase):
    def test_documented_session_codes_have_specific_categories(self):
        for code, category in (("error.null.api.key", "missing_api_key"),
                               ("error.invalid.details", "invalid_login_details"),
                               ("error.too-many.requests", "rate_limited")):
            with self.subTest(code=code):
                details = authentication_error_details(error_response({"errorCode": code}))
                self.assertEqual(details["error_code"], code)
                self.assertEqual(details["error_category"], category)
                self.assertEqual(details["error_code_state"], "recognized")

    def test_every_allowlisted_code_has_a_category(self):
        for code, category in AUTH_ERROR_CATEGORIES.items():
            details = authentication_error_details(error_response({"errorCode": code}))
            self.assertEqual(details["error_category"], category)
            self.assertNotEqual(category, "unclassified_authentication_error")

    def test_response_shapes_are_distinguished_without_exposing_server_data(self):
        cases = [(error_response(invalid_json=True, content_type="text/html; charset=utf-8"), "non_json_response"),
                 (error_response(["private-body-marker"]), "unexpected_json_shape"),
                 (error_response({"message": "private-body-marker"}), "missing_error_code"),
                 (error_response({"errorCode": "private-body-marker"}), "unknown_error_code"),
                 (error_response({"errorCode": ["private-body-marker"]}), "invalid_error_code_type")]
        for response, category in cases:
            with self.subTest(category=category):
                details = authentication_error_details(response)
                self.assertEqual(details["error_category"], category)
                self.assertEqual(details["error_code"], "unclassified_authentication_error")
                self.assertNotIn("private-body-marker", json.dumps(details))

    def test_json_error_can_be_classified_even_with_text_content_type(self):
        details = authentication_error_details(error_response({"errorCode": "error.invalid.details"}, "text/plain"))
        self.assertEqual(details["error_category"], "invalid_login_details")
        self.assertEqual(details["error_content_type"], "text")

    def test_http_challenge_and_encoding_diagnostics_never_expose_header_values(self):
        response = error_response({"errorCode": "private-server-marker"})
        response.headers.update({"WWW-Authenticate": 'Basic realm="private-realm-marker"', "Content-Encoding": "gzip"})
        details = authentication_error_details(response)
        self.assertEqual(details["www_authenticate_scheme"], "basic")
        self.assertEqual(details["content_encoding"], "gzip")
        self.assertNotIn("private-realm-marker", json.dumps(details))
        self.assertNotIn("private-server-marker", json.dumps(details))

    def test_netrc_cannot_inject_basic_authentication(self):
        with patch("requests.sessions.get_netrc_auth", side_effect=AssertionError("netrc must not be read")):
            with DemoReadOnlyTransport():
                session = requests.Session()
                request = session.prepare_request(requests.Request("POST", "https://demo-api-capital.backend-capital.com/api/v1/session"))
                self.assertFalse(session.trust_env)
                self.assertNotIn("Authorization", request.headers)


class CredentialParsingTests(unittest.TestCase):
    def inspect(self, content, environ=None):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_bytes(content.encode("utf-8"))
            example = Path(directory) / ".env.example"
            example.write_text("X-CAP-API-KEY=your_demo_api_key\nidentifier=your_demo_login_identifier\npassword=your_demo_api_password\n")
            # Real parser on temporary dummy values only. The offline runner forbids credential loading.
            from dotenv.main import DotEnv
            def parse(path, **kwargs):
                return DotEnv(path, interpolate=False).dict()
            with patch("scripts.demo_readonly.dotenv_values", side_effect=parse), redirect_stderr(io.StringIO()):
                values, diagnostics = inspect_credentials(env_path, example, environ={} if environ is None else environ)
            self.assertEqual(env_path.read_bytes(), content.encode("utf-8"))
            return values, diagnostics

    def test_file_values_win_over_shell_and_special_characters_survive(self):
        content = 'X-CAP-API-KEY=file-key-marker\nidentifier=file-login-marker\npassword=\'hash#dollar${UNDEFINED} literal\'\n'
        values, diagnostics = self.inspect(content, {"identifier": "shell-login-marker", "UNDEFINED": "substitution-marker"})
        self.assertEqual(values["identifier"], "file-login-marker")
        self.assertEqual(values["password"], "hash#dollar${UNDEFINED} literal")
        self.assertTrue(diagnostics["fields"]["identifier"]["shell_value_differs"])
        self.assertNotIn("file-login-marker", json.dumps(diagnostics))
        self.assertTrue(diagnostics["fields"]["password"]["literal_interpolation_marker"])

    def test_crlf_and_quoted_hash_password_parse_without_mutation(self):
        values, diagnostics = self.inspect('X-CAP-API-KEY=key-marker\r\nidentifier=login-marker\r\npassword="has#and$dollars"\r\n')
        self.assertEqual(values["password"], "has#and$dollars")
        self.assertTrue(diagnostics["ready_for_request"])

    def test_problematic_inputs_fail_preflight(self):
        base = "X-CAP-API-KEY=key-marker\nidentifier=login-marker\npassword=password-marker\n"
        for content in ("\ufeff" + base, base + "password=another-marker\n",
                        base.replace("password=password-marker", "password=' password-marker '"),
                        base.replace("password=password-marker", "password='line\nbreak'"),
                        base.replace("key-marker", "your_demo_api_key"),
                        base.replace("key-marker", "ENTER_GENERATED_API_KEY"),
                        base.replace("key-marker", "masked****value"),
                        base.replace("password=password-marker", "password=password-marker #part-of-password"),
                        base.replace("password=password-marker", "password=\"unclosed")):
            with self.subTest(case_number=len(content)):
                _, diagnostics = self.inspect(content)
                self.assertFalse(diagnostics["ready_for_request"])

"""Explicit demo-only connection checks. No strategy entry points or order calls."""

from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import unicodedata
from unittest.mock import patch
from urllib.parse import urlsplit

import requests
from dotenv import dotenv_values
from dotenv.parser import parse_stream

from trading_bot.config.paths import ROOT
from trading_bot.api.network_errors import REQUEST_TIMEOUT, TradingUnavailable
from trading_bot.api.auth_diagnostics import (
    AUTH_ERROR_CATEGORIES, sanitized_auth_error, authentication_error_details,
)
from trading_bot.config.trading_config import DEMO_URL
from urllib.parse import urlsplit as _urlsplit
DEMO_HOST = _urlsplit(DEMO_URL).hostname
ALLOWED = {
    ("POST", "/api/v1/session"),
    ("GET", "/api/v1/ping"),
    ("GET", "/api/v1/time"),
    ("GET", "/api/v1/session"),
    ("GET", "/api/v1/accounts"),
    ("GET", "/api/v1/markets/EURUSD"),
    ("GET", "/api/v1/prices/EURUSD"),
}


def inspect_credentials(env_path, example_path, environ=None):
    """Return private values and separate value-free diagnostics; never normalize secrets."""
    from trading_bot.config.trading_config import ALLOWED_KEYS
    environ = os.environ if environ is None else environ
    raw = env_path.read_bytes()
    bindings = list(parse_stream(io.StringIO(raw.decode("utf-8"))))
    values = dotenv_values(env_path, interpolate=False)
    placeholders = dotenv_values(example_path, interpolate=False)
    fields = {}
    for key in ("X-CAP-API-KEY", "identifier", "password"):
        value = values.get(key)
        assignments = [b for b in bindings if b.key == key]
        unquoted_comment = any(
            not (tail := b.original.string.split("=", 1)[-1].lstrip()).startswith(("'", '"'))
            and any(part in tail for part in (" #", "\t#")) for b in assignments)
        fields[key] = {
            "present": isinstance(value, str) and bool(value),
            "example_placeholder": bool(value and value == placeholders.get(key)),
            "placeholder_like": bool(value and (value.casefold().startswith(("your_", "enter_", "replace_me"))
                                                 or (value.startswith("<") and value.endswith(">")))),
            "duplicate_assignment": len(assignments) > 1,
            "unquoted_inline_comment": unquoted_comment,
            "boundary_whitespace": bool(value and value != value.strip()),
            "control_or_invisible_characters": bool(value and any(unicodedata.category(ch).startswith("C") for ch in value)),
            "literal_interpolation_marker": bool(value and "${" in value),
            "masked_value": bool(value and ("****" in value or "••••" in value)),
            "shell_variable_present": key in environ,
            "shell_value_differs": key in environ and environ[key] != value,
        }
    clean = not raw.startswith(b"\xef\xbb\xbf") and not any(b.error for b in bindings)
    unexpected = any(b.key is not None and b.key not in ALLOWED_KEYS for b in bindings)
    clean = clean and not unexpected
    clean = clean and all(f["present"] and not any(f[k] for k in (
        "example_placeholder", "placeholder_like", "duplicate_assignment", "boundary_whitespace",
        "control_or_invisible_characters", "literal_interpolation_marker", "masked_value",
        "unquoted_inline_comment")) for f in fields.values())
    diagnostics = {"utf8_bom": raw.startswith(b"\xef\xbb\xbf"), "parse_error_count": sum(b.error for b in bindings),
                   "unexpected_configuration_variables": unexpected,
                   "fields": fields, "credentials_from": "project_env_only", "ready_for_request": bool(clean)}
    return values, diagnostics


class SafetyError(RuntimeError):
    """A request violated the demo-only, read-only policy."""


def validate_request(method, url):
    target = urlsplit(url)
    if (target.scheme != "https" or target.hostname != DEMO_HOST
            or target.port not in (None, 443) or target.username or target.password
            or target.fragment or (method.upper(), target.path) not in ALLOWED):
        raise SafetyError("Blocked request outside the demo read-only allowlist")


class DemoReadOnlyTransport:
    """Guard the final prepared request, including requests made by TradingApi."""

    def __init__(self, interval=1.1):
        self.interval = max(1.1, interval)
        self.calls = []
        self.last_sent = None
        self.auth_attempts = 0
        self.original_send = requests.sessions.Session.send

    def send(self, session, request, **kwargs):
        validate_request(request.method, request.url)
        if len(self.calls) >= 20:
            raise SafetyError("Demo check request budget exceeded")
        if request.method == "POST":
            if self.auth_attempts >= 1:
                raise SafetyError("Only one authentication attempt is permitted")
            # Count before sending: failed or timed-out logins must never be retried.
            self.auth_attempts += 1
        if self.last_sent is not None:
            time.sleep(max(0, self.interval - (time.monotonic() - self.last_sent)))
        self.last_sent = time.monotonic()
        # Never follow redirects, use environment proxies, disable TLS, or wait forever.
        kwargs.update(allow_redirects=False, timeout=REQUEST_TIMEOUT, verify=True, proxies={})
        response = self.original_send(session, request, **kwargs)
        record = {"method": request.method, "path": urlsplit(request.url).path,
                  "status": response.status_code}
        if request.method == "POST" and response.status_code != 200:
            record.update(authentication_error_details(response))
        self.calls.append(record)
        if 300 <= response.status_code < 400:
            raise SafetyError("Redirect rejected by demo-only transport")
        return response

    def __enter__(self):
        transport = self
        original_prepare = requests.sessions.Session.prepare_request

        def guarded_prepare(session, request):
            # Disable netrc authentication before request preparation, not just proxies at send time.
            session.trust_env = False
            return original_prepare(session, request)

        def guarded_send(session, request, **kwargs):
            return transport.send(session, request, **kwargs)

        self.guard = patch.object(requests.sessions.Session, "send", guarded_send)
        self.prepare_guard = patch.object(requests.sessions.Session, "prepare_request", guarded_prepare)
        self.prepare_guard.start()
        self.guard.start()
        return self

    def __exit__(self, *exc):
        self.guard.stop()
        self.prepare_guard.stop()


def forbidden(*args, **kwargs):
    raise SafetyError("Credential-file writes and order operations are forbidden")


def price_snapshot(payload):
    candles = payload["prices"]
    result = {key: [float(candle[key + "Price"]["ask"]) for candle in candles]
              for key in ("open", "close")}
    if len(candles) < 210 or not all(math.isfinite(p) and p > 0
                                   for values in result.values() for p in values):
        raise ValueError("Insufficient or invalid historical prices")
    return result


def main(argv=None):
    import argparse
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    from trading_bot.api.client import TradingApi

    env_path = ROOT / ".env"
    original_digest = hashlib.sha256(env_path.read_bytes()).digest()
    report = {"checked_at": datetime.now(timezone.utc).isoformat(),
              "environment": {}, "endpoints": {}, "requests": []}
    snapshots = {}
    transport = DemoReadOnlyTransport()

    def check(name, callback, validator):
        before = len(transport.calls)
        safety_error = None
        try:
            # Legacy API error logging must never reach the terminal or report.
            with redirect_stdout(io.StringIO()):
                value = callback()
            ok = bool(validator(value))
            result = {"passed": ok}
        except (Exception, TradingUnavailable) as exc:
            value = None
            result = {"passed": False, "error_type": type(exc).__name__}
            if isinstance(exc, TradingUnavailable):
                result.update(error_category=exc.reason, message=str(exc))
            if isinstance(exc, SafetyError):
                safety_error = exc
        relevant = transport.calls[before:]
        if name == "authentication":
            # The constructor pings before/after login. Its final GET is not the login status.
            relevant = [call for call in relevant if call["method"] == "POST" and call["path"] == "/api/v1/session"]
        if relevant:
            result["http_status"] = relevant[-1]["status"]
        report["endpoints"][name] = result
        print(f"{'PASS' if result['passed'] else 'FAIL'}: {name}")
        if safety_error is not None:
            raise safety_error
        return value

    try:
        ignored = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT).returncode == 0
        untracked = subprocess.run(["git", "ls-files", "--error-unmatch", ".env"],
                                   cwd=ROOT, capture_output=True).returncode != 0
        values, diagnostics = inspect_credentials(env_path, ROOT / ".env.example")
        report["credential_diagnostics"] = diagnostics
        keys = ("X-CAP-API-KEY", "identifier", "password")
        configured = diagnostics["ready_for_request"]
        report["environment"] = {"python_312": sys.version_info[:2] == (3, 12),
                                 "env_ignored": ignored, "env_untracked": untracked,
                                 "env_permissions_600": env_path.stat().st_mode & 0o777 == 0o600,
                                 "credentials_loaded_from_project_env": bool(configured)}
        if not all(report["environment"].values()):
            raise SafetyError("Environment configuration checks failed")
        # No .env writes, token caching, or order methods are permitted in this run.
        with transport, patch.object(TradingApi, "write_keys", forbidden), \
             patch.object(TradingApi, "createPosition", forbidden), \
             patch.object(TradingApi, "closePosition", forbidden), \
             patch.object(TradingApi, "updatePosition", forbidden):
            api = check("authentication", lambda: TradingApi(*(values[k] for k in keys), real_account=False),
                        lambda value: value.BASE_URL == f"https://{DEMO_HOST}" and bool(value.cst and value.x_security_token))
            report["tokens_obtained"] = bool(api and api.cst and api.x_security_token)
            if not report["endpoints"]["authentication"]["passed"]:
                login = next((call for call in transport.calls if call["method"] == "POST"), {})
                result = report["endpoints"]["authentication"]
                result["http_status"] = login.get("status")
                result["error_code"] = login.get("error_code", "missing_session_tokens" if login.get("status") == 200 else "request_failed")
                for field in ("error_category", "error_content_type", "error_response_format", "error_code_state",
                              "www_authenticate_scheme", "content_encoding"):
                    if field in login:
                        result[field] = login[field]
                print(f"Authentication stopped: HTTP {result['http_status']}; code {result['error_code']}")
                for name in ("getServerTime", "pingService", "getSessionDetails", "getAllAccounts", "EURUSD market",
                             "EURUSD prices MINUTE", "EURUSD prices MINUTE_5", "EURUSD prices MINUTE_30", "EURUSD prices HOUR_4"):
                    report["endpoints"][name] = {"passed": False, "skipped": "demo_authentication_failed"}
                raise RuntimeError("Demo authentication unsuccessful; protected requests skipped")
            check("getServerTime", api.getServerTime, lambda value: isinstance(value, dict) and bool(value))
            check("pingService", api.pingService,
                  lambda value: isinstance(value, tuple) and value[1] == 200)
            check("getSessionDetails", api.getSessionDetails,
                  lambda value: isinstance(value, dict) and bool(value.get("accountId")))
            check("getAllAccounts", api.getAllAccounts,
                  lambda value: isinstance(value, dict) and bool(value.get("accounts")))
            check("EURUSD market", lambda: api.getSingleMarketDetails("EURUSD"),
                  lambda value: isinstance(value, dict) and value.get("instrument", {}).get("epic") == "EURUSD")
            for resolution in ("MINUTE", "MINUTE_5", "MINUTE_30", "HOUR_4"):
                value = check(f"EURUSD prices {resolution}",
                              lambda: api.getHistoricalPrices("EURUSD", resolution, 210),
                              lambda result: isinstance(result, tuple) and result[1] == 200
                              and bool(price_snapshot(result[0])))
                if report["endpoints"][f"EURUSD prices {resolution}"]["passed"]:
                    snapshots[resolution] = price_snapshot(value[0])
    except (Exception, TradingUnavailable) as exc:
        # Exception messages and API response bodies can include secrets. Record type only.
        report["error_type"] = type(exc).__name__
    finally:
        report["requests"] = transport.calls
        report["authentication_attempts"] = transport.auth_attempts
        report["demo_base_url"] = f"https://{DEMO_HOST}"
        report["env_unchanged"] = hashlib.sha256(env_path.read_bytes()).digest() == original_digest
        report["passed"] = (bool(report["endpoints"]) and not report.get("error_type")
                            and all(r["passed"] for r in report["endpoints"].values())
                            and report["env_unchanged"])
        (ROOT / "DEMO_API_RESULTS.json").write_text(json.dumps(report, indent=2) + "\n")
        if snapshots:
            fixture = ROOT / "tests" / "fixtures" / "eurusd_demo_prices.json"
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text(json.dumps({"source": "Capital.com DEMO", "retrieved_at": report["checked_at"],
                                           "prices": snapshots}, indent=2) + "\n")
        print("PASS: .env contents unchanged" if report["env_unchanged"] else "FAIL: .env changed")
        print("Sanitized results saved to DEMO_API_RESULTS.json; no credentials or tokens saved.")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

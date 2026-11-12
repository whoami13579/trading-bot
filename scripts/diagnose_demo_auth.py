"""Independent demo authentication probe. Default: offline preparation only.

Network modes require an explicit approval flag and perform exactly one request.
No TradingApi construction, initial ping, cached tokens, retries, or order methods.
"""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import http.client
import json
from pathlib import Path
import ssl
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import certifi
from scripts.demo_readonly import DEMO_HOST, authentication_error_details, inspect_credentials

BASE_URL = f"https://{DEMO_HOST}"
TIMEOUT = 15
MAX_RESPONSE_BYTES = 128 * 1024


def build_request(values, probe):
    headers = {"X-CAP-API-KEY": values["X-CAP-API-KEY"]}
    if probe == "encryption-key":
        return "GET", "/api/v1/session/encryptionKey", headers, None
    if probe not in ("prepare", "session"):
        raise ValueError("Unsupported diagnostic operation")
    headers["Content-Type"] = "application/json"
    payload = {"identifier": values["identifier"], "password": values["password"], "encryptedPassword": False}
    body = json.dumps(payload, allow_nan=False).encode("utf-8")
    return "POST", "/api/v1/session", headers, body


def prepare_summary(values):
    import requests  # Offline comparison only; network probes never call requests.

    method, path, headers, body = build_request(values, "prepare")
    # Use the actual json= preparation path, without constructing a session or sending.
    prepared = requests.Request(method, BASE_URL + path, headers=headers,
                                json={"identifier": values["identifier"], "password": values["password"],
                                      "encryptedPassword": False}).prepare()
    actual = json.loads(prepared.body)
    return {"method": method, "base_url": BASE_URL, "path": path,
            "json_fields_match_loaded_values": actual == json.loads(body),
            "api_key_header_matches_loaded_value": prepared.headers["X-CAP-API-KEY"] == values["X-CAP-API-KEY"],
            "content_type_json": prepared.headers["Content-Type"] == "application/json",
            "encrypted_password_is_false": actual["encryptedPassword"] is False,
            "content_length_correct": int(prepared.headers["Content-Length"]) == len(prepared.body),
            "body_bytes_match_stdlib_json": prepared.body == body,
            "authorization_header_absent": "Authorization" not in prepared.headers,
            "cached_tokens_absent": not any(k in prepared.headers for k in ("CST", "X-SECURITY-TOKEN"))}


def probe_once(values, probe, *, approved=False):
    # Enforce permission at the callable boundary too, not just in argument parsing.
    if approved is not True:
        raise PermissionError("Explicit approval is required before any demo network request")
    if probe not in ("session", "encryption-key"):
        raise ValueError("Unsupported network operation")
    method, path, headers, body = build_request(values, probe)
    # Use the same CA bundle as requests while excluding SSLKEYLOGFILE/CA environment overrides.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=certifi.where())
    # Request uncompressed bytes: http.client does not automatically decompress responses.
    headers["Accept-Encoding"] = "identity"
    result = {"method": method, "path": path, "network_requests": 1,
              "authentication_attempts": 1 if probe == "session" else 0}
    # http.client has no automatic redirects, retry policy, netrc lookup, or environment proxy support.
    with closing(http.client.HTTPSConnection(DEMO_HOST, timeout=TIMEOUT, context=context)) as connection:
        connection.set_debuglevel(0)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read(MAX_RESPONSE_BYTES + 1)
        result["http_status"] = response.status
        if probe == "session":
            result["cst_header_present"] = bool(response.getheader("CST"))
            result["security_token_header_present"] = bool(response.getheader("X-SECURITY-TOKEN"))
            result["tokens_obtained"] = result["cst_header_present"] and result["security_token_header_present"]
        if 300 <= response.status < 400:
            return {**result, "passed": False, "error_category": "redirect_refused"}
        if len(data) > MAX_RESPONSE_BYTES:
            return {**result, "passed": False, "error_category": "response_too_large"}
        # Reuse only the value-free classifier, not requests' transport or TradingApi.
        safe_response = SimpleNamespace(
            headers={name: response.getheader(name, "") for name in
                     ("Content-Type", "Content-Encoding", "WWW-Authenticate")},
            json=lambda: json.loads(data),
        )
        if probe == "encryption-key":
            try:
                payload = safe_response.json()
            except ValueError:
                payload = None
            key_present = isinstance(payload, dict) and isinstance(payload.get("encryptionKey"), str) and bool(payload["encryptionKey"])
            timestamp_present = (isinstance(payload, dict) and isinstance(payload.get("timeStamp"), int)
                                 and not isinstance(payload["timeStamp"], bool))
            result.update(encryption_key_present=key_present, timestamp_present=timestamp_present,
                          encryption_key_and_timestamp_present=key_present and timestamp_present)
        if response.status != 200:
            return {**result, "passed": False, **authentication_error_details(safe_response)}
        if probe == "session":
            # Header presence is the authentication outcome; JSON shape is reported separately.
            try:
                valid_body = isinstance(safe_response.json(), dict)
            except ValueError:
                valid_body = False
            return {**result, "passed": result["tokens_obtained"], "session_body_json_object": valid_body}
        return {**result, "passed": result["encryption_key_and_timestamp_present"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", choices=("prepare", "encryption-key", "session"), default="prepare")
    parser.add_argument("--approve-demo-request", action="store_true",
                        help="Explicitly approve exactly one request for the selected demo network probe")
    args = parser.parse_args(argv)
    if args.probe != "prepare" and not args.approve_demo_request:
        parser.error("Network probes require explicit --approve-demo-request permission; no request was sent")
    env_path = ROOT / ".env"
    original_digest = hashlib.sha256(env_path.read_bytes()).digest()
    result = {"checked_at": datetime.now(timezone.utc).isoformat(), "probe": args.probe,
              "base_url": BASE_URL, "network_requests": 0, "authentication_attempts": 0}
    try:
        values, diagnostics = inspect_credentials(env_path, ROOT / ".env.example")
        result["credential_preflight_clean"] = diagnostics["ready_for_request"]
        if not diagnostics["ready_for_request"]:
            result.update(passed=False, error_category="credential_preflight_requires_review")
        elif args.probe == "prepare":
            summary = prepare_summary(values)
            result.update(prepared_request=summary,
                          passed=all(value for value in summary.values() if isinstance(value, bool)))
        else:
            # Count before entering transport so a timeout is not reported as zero attempts.
            result.update(network_requests=1, authentication_attempts=int(args.probe == "session"))
            result.update(probe_once(values, args.probe, approved=args.approve_demo_request))
    except Exception as exc:
        # Never print exception messages, bodies, headers, identifiers, passwords, or tokens.
        result.update(passed=False, error_type=type(exc).__name__)
    finally:
        result["env_unchanged"] = hashlib.sha256(env_path.read_bytes()).digest() == original_digest
        result["passed"] = bool(result.get("passed") and result["env_unchanged"])
        print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

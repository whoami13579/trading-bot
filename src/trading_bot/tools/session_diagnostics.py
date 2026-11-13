"""DEMO-only session lifecycle verification. Never computes signals or writes trading state."""

import argparse
from contextlib import contextmanager
import json
import time
from unittest.mock import patch
from urllib.parse import urlsplit

import requests
from trading_bot.api.client import TradingApi
from trading_bot.api.network_errors import TradingUnavailable, network_failure_reason
from trading_bot.config.trading_config import DEMO_URL


@contextmanager
def demo_readonly_transport(records, login_budget):
    """Validate final prepared requests; never log headers, bodies or account identifiers."""
    original_send = requests.sessions.Session.send
    original_prepare = requests.sessions.Session.prepare_request
    logins = 0
    allowed_reads = {
        "/api/v1/session",
        "/api/v1/accounts",
        "/api/v1/ping",
        "/api/v1/prices/EURUSD",
    }

    def prepare(session, request):
        session.trust_env = False
        return original_prepare(session, request)

    def send(session, request, **kwargs):
        nonlocal logins
        url = urlsplit(request.url)
        if (
            url.scheme != "https"
            or url.netloc != urlsplit(DEMO_URL).netloc
            or url.fragment
        ):
            raise PermissionError("diagnostic_requires_exact_demo_host")
        if request.method == "POST" and url.path == "/api/v1/session" and not url.query:
            logins += 1
            if logins > login_budget:
                raise PermissionError("diagnostic_login_budget_exhausted")
        elif request.method != "GET" or url.path not in allowed_reads:
            raise PermissionError("diagnostic_allows_only_login_and_allowlisted_gets")
        if len(records) >= 100:
            raise PermissionError("diagnostic_request_budget_exhausted")
        record = {
            "method": request.method,
            "path": url.path,
            "authentication_headers_present": bool(
                request.headers.get("CST") and request.headers.get("X-SECURITY-TOKEN")
            ),
        }
        records.append(record)
        kwargs.update(verify=True, allow_redirects=False, timeout=(5, 20))
        try:
            response = original_send(session, request, **kwargs)
        except requests.RequestException as exc:
            record["failure_reason"] = network_failure_reason(exc)
            raise
        record["status"] = response.status_code
        return response

    with (
        patch.object(requests.sessions.Session, "prepare_request", prepare),
        patch.object(requests.sessions.Session, "send", send),
    ):
        yield


def run_diagnostic(
    *, idle_seconds=0, cycles=3, interval_seconds=1, probe_expired=False
):
    report = {
        "environment": "DEMO",
        "idle_seconds": idle_seconds,
        "requests": [],
        "cycles": [],
        "live_requests": 0,
        "trading_mutations": 0,
        "signals_computed": False,
    }
    try:
        with demo_readonly_transport(report["requests"], login_budget=cycles + 1):
            api = TradingApi(environment="demo", auto_authenticate=False)
            if api.createNewSession() is None:
                api._require_available()
                raise RuntimeError("diagnostic_login_failed")
            account = api.verify_account_environment()
            api.pin_session_identity(account)
            _, status = api.getHistoricalPrices("EURUSD", "MINUTE_30", 48)
            report["initial_history_status"] = status
            started = time.monotonic()
            print(
                "DEMO authenticated and history GET passed; starting read-only session diagnostic.",
                flush=True,
            )
            time.sleep(idle_seconds)
            report["measured_idle_seconds"] = round(time.monotonic() - started, 2)
            if probe_expired:
                # Reproduce the old history GET without latching the production client.
                # This diagnostic-only request is read-only and never replays a mutation.
                before = (api.cst, api.x_security_token)
                report["probe_network_failures"] = []
                for attempt in range(2):
                    try:
                        expired = requests.get(
                            DEMO_URL + "/api/v1/prices/EURUSD",
                            headers={
                                "CST": api.cst,
                                "X-SECURITY-TOKEN": api.x_security_token,
                            },
                            params={"resolution": "MINUTE_30", "max": 48},
                            timeout=(5, 20),
                            allow_redirects=False,
                        )
                    except requests.RequestException as exc:
                        report["probe_network_failures"].append(
                            network_failure_reason(exc)
                        )
                        if attempt == 0:
                            time.sleep(1)
                        continue
                    report["idle_history_status"] = expired.status_code
                    report["expiration_reproduced"] = expired.status_code == 401
                    print(
                        f"Original history request after idle: HTTP {expired.status_code}",
                        flush=True,
                    )
                    break
                report["same_authentication_headers"] = before == (
                    api.cst,
                    api.x_security_token,
                )
            for cycle in range(cycles):
                if cycle:
                    time.sleep(interval_seconds)
                renewed = api.prepare_trading_cycle()
                payload, status = api.getHistoricalPrices("EURUSD", "MINUTE_30", 48)
                report["cycles"].append(
                    {
                        "cycle": cycle + 1,
                        "renewed": renewed,
                        "history_status": status,
                        "prices": len(payload["prices"]),
                        "account_unchanged": api._login_account_id
                        == account["accountId"],
                    }
                )
                print(
                    f"Read-only cycle {cycle + 1}: history HTTP {status}; session "
                    + ("renewed" if renewed else "validated"),
                    flush=True,
                )
            report["passed"] = all(
                c["history_status"] == 200 and c["account_unchanged"]
                for c in report["cycles"]
            )
    except (Exception, TradingUnavailable) as exc:
        report["passed"] = False
        report["failure_type"] = type(exc).__name__
        if isinstance(exc, TradingUnavailable):
            report["failure_reason"] = exc.reason
        elif isinstance(exc, requests.RequestException):
            report["failure_reason"] = network_failure_reason(exc)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--probe-expired",
        action="store_true",
        help="After at least 610 idle seconds, probe the original GET (at most two network attempts) before validating the fixed lifecycle",
    )
    parser.add_argument(
        "--idle-seconds",
        type=int,
        default=0,
        help="Initial idle interval; 610 reproduces the documented inactivity window",
    )
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--interval-seconds", type=int, default=1)
    args = parser.parse_args(argv)
    if (
        not 0 <= args.idle_seconds <= 3600
        or not 1 <= args.cycles <= 10
        or not 0 <= args.interval_seconds <= 60
    ):
        parser.error("idle must be 0–3600 seconds, cycles 1–10, interval 0–60 seconds")
    if args.probe_expired and args.idle_seconds < 610:
        parser.error("--probe-expired requires --idle-seconds at least 610")
    report = run_diagnostic(**vars(args))
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

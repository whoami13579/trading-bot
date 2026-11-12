"""DEMO prepared-request allowlist. No request is made at import time."""
import json
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import requests
from trading_bot.api.auth_diagnostics import authentication_error_details
from trading_bot.api.network_errors import REQUEST_TIMEOUT
from trading_bot.config.trading_config import DEMO_URL
from trading_bot.risk.live_risk import IntegrationError, require

DEMO_HOST = urlsplit(DEMO_URL).hostname
EPIC = "EURUSD"

class DemoTransport:
    """Validate final prepared requests. Mutations are denied unless individually armed."""

    READS = {"/api/v1/ping", "/api/v1/session", "/api/v1/accounts", "/api/v1/accounts/preferences",
             "/api/v1/markets/EURUSD", "/api/v1/prices/EURUSD", "/api/v1/positions", "/api/v1/workingorders"}

    def __init__(self, execute=False):
        self.execute = execute
        self.calls = []
        self.auth_attempts = self.open_attempts = self.close_attempts = 0
        self.last_sent = None
        self.open_size = None
        self.owned_deal_id = None
        self.references = set()
        self.journal = None
        self.original_send = requests.sessions.Session.send

    def send(self, session, request, **kwargs):
        url = urlsplit(request.url)
        require(url.scheme == "https" and url.hostname == DEMO_HOST and url.port in (None, 443)
                and not url.username and not url.password and not url.fragment, "blocked_non_demo_url")
        require(len(self.calls) < 50, "request_budget_exceeded")
        method, path = request.method, url.path
        if method == "POST" and path == "/api/v1/session":
            require(self.auth_attempts == 0 and not url.query, "authentication_retry_blocked")
            self.auth_attempts += 1
        elif method == "GET":
            allowed = path in self.READS
            if path.startswith("/api/v1/confirms/"):
                allowed = path.removeprefix("/api/v1/confirms/") in self.references
            if path.startswith("/api/v1/positions/"):
                allowed = self.owned_deal_id is not None and path == f"/api/v1/positions/{self.owned_deal_id}"
            require(allowed, "unlisted_read_blocked")
            if path == "/api/v1/prices/EURUSD":
                require(parse_qs(url.query) == {"resolution": ["MINUTE_30"], "max": ["210"]}, "unexpected_history_query")
            else:
                require(not url.query, "unexpected_query")
        elif method == "POST" and path == "/api/v1/positions":
            require(self.execute and self.open_size is not None and self.open_attempts == 0 and not url.query
                    and self.journal is not None and self.journal.data["stage"] == "open_submitting",
                    "order_submission_blocked")
            payload = json.loads(request.body)
            require(payload == {"epic": EPIC, "direction": "BUY", "size": float(self.open_size), "guaranteedStop": False},
                    "unexpected_order_payload")
            self.open_attempts += 1
        elif method == "DELETE":
            require(self.execute and self.owned_deal_id is not None and self.close_attempts == 0
                    and path == f"/api/v1/positions/{self.owned_deal_id}" and not url.query
                    and request.body in (None, b"", "") and self.journal is not None
                    and self.journal.data["stage"] == "close_submitting", "position_close_blocked")
            self.close_attempts += 1
        else:
            raise IntegrationError("unlisted_mutation_blocked")
        if self.last_sent is not None:
            time.sleep(max(0, 1.1 - (time.monotonic() - self.last_sent)))
        self.last_sent = time.monotonic()
        kwargs.update(timeout=REQUEST_TIMEOUT, verify=True, proxies={}, allow_redirects=False)
        response = self.original_send(session, request, **kwargs)
        record = {"method": method, "path": path if path in self.READS or path == "/api/v1/session" else path.rsplit("/", 1)[0],
                  "http_status": response.status_code}
        if response.status_code != 200:
            record.update(authentication_error_details(response))
        self.calls.append(record)
        if response.status_code == 404 and method == "GET" and path.startswith("/api/v1/confirms/"):
            return response  # Confirmation may not be available yet; only bounded GET polling is allowed.
        require(response.status_code == 200, "http_request_failed")
        return response

    def __enter__(self):
        original_prepare = requests.sessions.Session.prepare_request
        transport = self
        def prepare(session, request):
            session.trust_env = False
            return original_prepare(session, request)
        def send(session, request, **kwargs):
            return transport.send(session, request, **kwargs)
        self.prepare_patch = patch.object(requests.sessions.Session, "prepare_request", prepare)
        self.send_patch = patch.object(requests.sessions.Session, "send", send)
        self.prepare_patch.start()
        self.send_patch.start()
        return self

    def __exit__(self, *exc):
        self.send_patch.stop()
        self.prepare_patch.stop()

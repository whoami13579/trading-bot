"""Fixed diagnostics only: requests exceptions can contain credentials and URLs."""

import socket

import requests
from urllib3.exceptions import NameResolutionError, ReadTimeoutError

REQUEST_TIMEOUT = (5, 20)  # Connection, then socket read inactivity (not a total deadline).

MESSAGES = {
    "request_interrupted": "An API request was interrupted; inspect execution state before any manual retry.",
    "session_identity_not_pinned": "A verified startup account is required before scheduled session renewal.",
    "session_identity_changed": "The authenticated account or environment changed; no account switch was performed.",
    "session_renewal_failed": "The scheduled session could not be renewed.",
    "live_session_renewed_requires_preflight": "The LIVE session renewed and its execution gate remains disarmed; stop and reconcile before a new full preflight.",
    "dns_resolution_failed": "DNS resolution failed; check the network and WSL/Windows resolver. No HTTP authentication result was received.",
    "proxy_connection_failed": "The configured proxy could not be reached.",
    "connection_timeout": "The API connection timed out.",
    "read_timeout": "The API response timed out.",
    "network_timeout": "The network request timed out.",
    "tls_connection_failed": "The HTTPS connection or certificate verification failed.",
    "connection_failed": "The API connection failed; check network connectivity.",
    "network_request_failed": "The network request failed.",
    "authentication_failed_http_401": "HTTP 401: authentication was rejected or the session expired.",
    "permission_denied_http_403": "HTTP 403: API access was denied.",
    "missing_session_tokens": "Authentication did not provide the required session headers.",
    "authentication_unavailable": "An authenticated API session is required.",
    "account_verification_failed": "The active account could not be verified after an explicit switch.",
    "invalid_api_response": "The API returned an invalid response.",
}


class TradingUnavailable(SystemExit):
    """Stop even legacy strategy loops that catch Exception and continue trading.

    CLI/report boundaries may catch this explicitly, report it, and return failure.
    Never include the original exception, request, response, or session data.
    """

    def __init__(self, reason, *, mutation=False, broker_code=None):
        if reason not in MESSAGES and reason not in {f"http_{n}" for n in range(300, 600)}:
            reason = "network_request_failed"
        self.reason = reason
        self.outcome_uncertain = mutation
        self.status_code = int(reason[5:]) if reason.startswith('http_') else (401 if reason.endswith('401') else 403 if reason.endswith('403') else None)
        from trading_bot.api.errors import KNOWN_BROKER_ERRORS
        self.broker_code = broker_code if broker_code in KNOWN_BROKER_ERRORS else None
        message = MESSAGES.get(reason, f"API request failed with HTTP {reason.removeprefix('http_')}.")
        message += " Trading stopped; no automatic retry."
        if mutation:
            message += " Order outcome may be uncertain; inspect the account before any manual retry."
        super().__init__(message)


def network_failure_reason(exc):
    """Traverse typed wrappers (including urllib3.reason), never exception text."""
    pending, seen, errors = [exc], set(), []
    while pending:
        error = pending.pop()
        if not isinstance(error, BaseException) or id(error) in seen:
            continue
        seen.add(id(error))
        errors.append(error)
        pending.extend((error.__cause__, error.__context__, getattr(error, "reason", None)))
        pending.extend(arg for arg in error.args if isinstance(arg, BaseException))

    def contains(types):
        return any(isinstance(error, types) for error in errors)

    if contains(requests.exceptions.ProxyError):
        return "proxy_connection_failed"
    if contains((socket.gaierror, NameResolutionError)):
        return "dns_resolution_failed"
    if contains(requests.exceptions.SSLError):
        return "tls_connection_failed"
    if contains(requests.exceptions.ConnectTimeout):
        return "connection_timeout"
    if contains((requests.exceptions.ReadTimeout, ReadTimeoutError)):
        return "read_timeout"
    if contains((requests.exceptions.Timeout, TimeoutError)):
        return "network_timeout"
    if contains(requests.exceptions.ConnectionError):
        return "connection_failed"
    return "network_request_failed"

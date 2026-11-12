"""Synchronous Capital.com streaming client. One caller owns receive/subscribe operations.

Application pings run while receive() is being polled; the WebSocket protocol's own
ping frames don't replace Capital.com's authenticated `ping` destination.
"""

from collections import deque
import itertools
import json
import logging
import time
from typing import Any, Callable

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect
from trading_bot.api.validation import RESOLUTIONS, integer, items

STREAM_URL = "wss://api-streaming-capital.backend-capital.com/connect"
TYPES = frozenset(("classic", "heikin-ashi"))


class StreamError(RuntimeError):
    """Only fixed messages; never includes server bodies or underlying exceptions."""


def _private_logger():
    logger = logging.Logger("capital-stream-private")
    logger.disabled = True
    logger.propagate = False
    return logger


class CapitalWebSocketClient:
    """Read-only stream bound to an authenticated TradingApi session.

    Subscription calls wait for ACKs and buffer intervening events (bounded at 1000).
    Only acknowledged subscriptions are restored, with at most max_reconnects attempts
    over this client's lifetime. No session creation, account switching, or orders.
    """

    def __init__(
        self,
        api,
        *,
        max_reconnects: int = 3,
        ack_timeout: float = 10,
        ping_interval: float = 300,
        connector: Callable | None = None,
    ):
        integer(max_reconnects, 0, 10)
        if not 0 < ack_timeout <= 60 or not 0 < ping_interval <= 540:
            raise ValueError("invalid_stream_timeout")
        self.api = api
        self.max_reconnects = max_reconnects
        self.ack_timeout = ack_timeout
        self.ping_interval = ping_interval
        self._connector = connector or connect
        self._socket = None
        self._identity = None
        self._ids = itertools.count(1)
        self._quotes: set[str] = set()
        self._candles: set[tuple[str, str, str]] = set()
        self._events: deque[dict[str, Any]] = deque()
        self._last_ping = 0.0
        self._reconnects = 0
        self._closed = False
        self._fatal = False

    def _session_identity(self):
        self.api._require_available()
        if not self.api.cst or not self.api.x_security_token:
            raise StreamError("stream_requires_authenticated_session")
        return (
            self.api.environment,
            self.api.BASE_URL,
            self.api._login_account_id,
            self.api._session_generation,
            self.api.cst,
            self.api.x_security_token,
        )

    def _check_session(self):
        if self._fatal or self._closed:
            raise StreamError("stream_stopped")
        if self._identity is not None and self._session_identity() != self._identity:
            self._fatal = True
            self._disconnect()
            raise StreamError("stream_session_changed_create_new_client")

    def connect(self):
        self._check_session()
        if self._socket is not None:
            return self
        self._identity = self._session_identity()
        try:
            self._socket = self._connector(
                STREAM_URL,
                open_timeout=10,
                close_timeout=5,
                max_size=1048576,
                max_queue=32,
                logger=_private_logger(),
            )
        except (OSError, TimeoutError, WebSocketException):
            raise StreamError("stream_connection_failed") from None
        self._last_ping = time.monotonic()
        return self

    def _disconnect(self):
        socket, self._socket = self._socket, None
        if socket is not None:
            try:
                socket.close()
            except (OSError, WebSocketException):
                pass

    def close(self):
        self._closed = True
        self._disconnect()
        self._quotes.clear()
        self._candles.clear()
        self._events.clear()

    def __enter__(self):
        return self.connect()

    def __exit__(self, *args):
        self.close()

    def _redact(self, value):
        secrets = (
            self.api.cst,
            self.api.x_security_token,
            self.api.X_CAP_API_KEY,
            self.api.PASSWORD,
            self.api.IDENTIFIER,
        )
        if isinstance(value, dict):
            return {
                self._redact(k): self._redact(v)
                for k, v in value.items()
                if k.lower().replace("-", "").replace("_", "")
                not in {
                    "cst",
                    "securitytoken",
                    "xsecuritytoken",
                    "xcapapikey",
                    "password",
                    "identifier",
                }
            }
        if isinstance(value, list):
            return [self._redact(x) for x in value]
        if isinstance(value, str):
            for secret in secrets:
                if secret:
                    value = value.replace(secret, "[REDACTED]")
        return value

    def _read(self, timeout):
        try:
            raw = self._socket.recv(timeout=timeout)
        except TimeoutError:
            raise
        except (OSError, WebSocketException):
            self._disconnect()
            raise StreamError("stream_disconnected") from None
        try:
            event = json.loads(raw)
            if (
                not isinstance(event, dict)
                or not isinstance(event.get("destination"), str)
                or not isinstance(event.get("payload", {}), dict)
            ):
                raise ValueError()
        except (ValueError, TypeError, UnicodeError):
            self._fatal = True
            self._disconnect()
            raise StreamError("malformed_stream_event") from None
        if event.get("status") != "OK":
            self._fatal = True  # May indicate expired/invalid tokens. Never reauthenticate automatically.
            self._disconnect()
            raise StreamError("stream_request_rejected_or_session_expired")
        return self._redact(event)

    def _buffer(self, event):
        if len(self._events) >= 1000:
            self._fatal = True
            self._disconnect()
            raise StreamError("stream_event_buffer_full")
        self._events.append(event)

    def _exchange(self, destination, payload=None, keys=()):
        self._check_session()
        if self._socket is None:
            raise StreamError("stream_not_connected")
        correlation = str(next(self._ids))
        message = {
            "destination": destination,
            "correlationId": correlation,
            "cst": self.api.cst,
            "securityToken": self.api.x_security_token,
        }
        if payload is not None:
            message["payload"] = payload
        try:
            self._socket.send(json.dumps(message))
        except (OSError, WebSocketException):
            self._disconnect()
            raise StreamError("stream_disconnected") from None
        deadline = time.monotonic() + self.ack_timeout
        while time.monotonic() < deadline:
            try:
                event = self._read(max(0.001, deadline - time.monotonic()))
            except TimeoutError:
                break
            if event.get("correlationId") == correlation:
                if event["destination"] != destination:
                    break
                processed = event.get("payload", {}).get("subscriptions", {})
                if keys and (
                    not isinstance(processed, dict)
                    or any(processed.get(k) != "PROCESSED" for k in keys)
                ):
                    # Ambiguous partial ACK: shut down, keep the previous authorized set.
                    self._disconnect()
                    raise StreamError("stream_subscription_not_confirmed")
                return event
            if event["destination"] in ("quote", "ohlc.event"):
                self._buffer(event)
            # Unknown/late ACKs don't create or change subscriptions.
        self._disconnect()
        raise StreamError("stream_acknowledgment_timeout")

    def _capacity(self, epics):
        if len(self._quotes | {x[0] for x in self._candles} | set(epics)) > 40:
            raise ValueError("stream_maximum_40_instruments")

    def subscribe_quotes(self, epics):
        values = items(epics, maximum=40)
        self._capacity(values)
        values = [x for x in values if x not in self._quotes]
        if not values:
            return None
        result = self._exchange("marketData.subscribe", {"epics": values}, values)
        self._quotes.update(values)
        return result

    def unsubscribe_quotes(self, epics):
        values = [x for x in items(epics, maximum=40) if x in self._quotes]
        if not values:
            return None
        result = self._exchange("marketData.unsubscribe", {"epics": values}, values)
        self._quotes.difference_update(values)
        return result

    def subscribe_ohlc(self, epics, resolutions=None, type="classic"):
        values = items(epics, maximum=40)
        resolutions = items(resolutions if resolutions is not None else ["MINUTE"])
        if set(resolutions) - RESOLUTIONS or type not in TYPES:
            raise ValueError("unsupported_candle_resolution_or_type")
        self._capacity(values)
        results = []
        # Group by resolution so overlapping subscriptions never duplicate existing triples.
        for resolution in resolutions:
            new = [
                (x, resolution, type)
                for x in values
                if (x, resolution, type) not in self._candles
            ]
            if new:
                results.append(
                    self._exchange(
                        "OHLCMarketData.subscribe",
                        {
                            "epics": [x[0] for x in new],
                            "resolutions": [resolution],
                            "type": type,
                        },
                        [":".join(x) for x in new],
                    )
                )
                self._candles.update(new)
        return results

    def unsubscribe_ohlc(self, epics, resolutions=None, types=None):
        values = items(epics, maximum=40)
        resolutions = items(resolutions) if resolutions is not None else None
        types = items(types) if types is not None else None
        if (resolutions is not None and set(resolutions) - RESOLUTIONS) or (
            types is not None and set(types) - TYPES
        ):
            raise ValueError("unsupported_candle_resolution_or_type")
        removed = {
            x
            for x in self._candles
            if x[0] in values
            and (resolutions is None or x[1] in resolutions)
            and (types is None or x[2] in types)
        }
        if not removed:
            return None
        payload = {"epics": values}
        if resolutions is not None:
            payload["resolutions"] = resolutions
        if types is not None:
            payload["types"] = types
        result = self._exchange(
            "OHLCMarketData.unsubscribe", payload, [":".join(x) for x in removed]
        )
        self._candles.difference_update(removed)
        return result

    def ping(self):
        result = self._exchange("ping")
        self._last_ping = time.monotonic()
        return result

    def reconnect(self):
        self._check_session()
        quotes, candles = set(self._quotes), set(self._candles)
        self._disconnect()
        self._events.clear()
        while self._reconnects < self.max_reconnects:
            self._reconnects += 1
            time.sleep(min(2 ** (self._reconnects - 1), 8))
            try:
                self.connect()
                self._quotes.clear()
                self._candles.clear()
                if quotes:
                    self.subscribe_quotes(sorted(quotes))
                for epic, resolution, type_ in sorted(candles):
                    self.subscribe_ohlc([epic], [resolution], type_)
                return self
            except StreamError:
                self._disconnect()
                if self._fatal:
                    break
        self._quotes, self._candles = quotes, candles
        self._fatal = True
        raise StreamError("stream_reconnect_budget_exhausted") from None

    def receive(self, timeout: float = 30) -> dict | None:
        """Return one parsed event, or None on idle timeout. Bounded recovery on disconnect."""
        if not 0 < timeout <= 60:
            raise ValueError("invalid_stream_receive_timeout")
        self._check_session()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self._socket is None:
                    self.reconnect()
                if time.monotonic() - self._last_ping >= self.ping_interval:
                    self.ping()
                if self._events:
                    return self._events.popleft()
                event = self._read(
                    min(
                        deadline - time.monotonic(),
                        max(
                            0.001,
                            self.ping_interval - (time.monotonic() - self._last_ping),
                        ),
                    )
                )
                return event
            except TimeoutError:
                continue
            except StreamError:
                if self._fatal:
                    raise
                self.reconnect()
        return None

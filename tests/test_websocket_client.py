"""Streaming protocol tests using deterministic local in-memory transports."""

from collections import deque
import json
import unittest
from unittest.mock import Mock, patch

from trading_bot.api.websocket_client import (
    CapitalWebSocketClient,
    STREAM_URL,
    StreamError,
)
from trading_bot.api.validation import RESOLUTIONS
from test_api_coverage import client


class Socket:
    def __init__(self):
        self.sent = []
        self.incoming = deque()
        self.quotes = set()
        self.candles = set()
        self.closed = False
        self.reject = False
        self.partial = False
        self.auto_ack = True
        self.before_ack = None

    def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        destination, payload = message["destination"], message.get("payload", {})
        subscriptions = {}
        if destination.startswith("marketData."):
            for epic in payload["epics"]:
                subscriptions[epic] = "PROCESSED"
                self.quotes.add(epic) if destination.endswith(
                    "subscribe"
                ) and not destination.endswith("unsubscribe") else self.quotes.discard(
                    epic
                )
        if destination == "OHLCMarketData.subscribe":
            values = {
                (e, r, payload.get("type", "classic"))
                for e in payload["epics"]
                for r in payload.get("resolutions", ["MINUTE"])
            }
            self.candles.update(values)
            subscriptions = {":".join(x): "PROCESSED" for x in values}
        if destination == "OHLCMarketData.unsubscribe":
            values = {
                x
                for x in self.candles
                if x[0] in payload["epics"]
                and ("resolutions" not in payload or x[1] in payload["resolutions"])
                and ("types" not in payload or x[2] in payload["types"])
            }
            self.candles.difference_update(values)
            subscriptions = {":".join(x): "PROCESSED" for x in values}
        if self.partial:
            subscriptions = {k: "REJECTED" for k in subscriptions}
        event = {
            "status": "ERROR" if self.reject else "OK",
            "destination": destination,
            "correlationId": message["correlationId"],
            "payload": {"subscriptions": subscriptions} if subscriptions else {},
        }
        if self.before_ack:
            self.incoming.append(json.dumps(self.before_ack))
        if self.auto_ack:
            self.incoming.append(json.dumps(event))

    def recv(self, timeout):
        if not self.incoming:
            raise TimeoutError()
        value = self.incoming.popleft()
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed = True


class WebSocketTests(unittest.TestCase):
    def setUp(self):
        self.api = client()
        self.socket = Socket()
        self.connector = Mock(return_value=self.socket)
        self.stream = CapitalWebSocketClient(
            self.api, connector=self.connector, ack_timeout=0.01
        )
        self.stream.connect()
        self.addCleanup(self.stream.close)

    def test_official_url_and_tls_bounded_transport_and_private_logging(self):
        self.assertEqual(self.connector.call_args.args, (STREAM_URL,))
        logger = self.connector.call_args.kwargs["logger"]
        self.assertTrue(logger.disabled)
        self.assertFalse(logger.propagate)
        self.assertEqual(self.connector.call_args.kwargs["max_size"], 1048576)
        self.stream.connect()
        self.connector.assert_called_once()

    def test_quotes_subscribe_ack_auth_ids_and_unsubscribe(self):
        ack = self.stream.subscribe_quotes(["EURUSD", "GOLD", "EURUSD"])
        self.assertEqual(ack["status"], "OK")
        message = self.socket.sent[0]
        self.assertEqual(
            message,
            {
                "destination": "marketData.subscribe",
                "correlationId": "1",
                "cst": self.api.cst,
                "securityToken": self.api.x_security_token,
                "payload": {"epics": ["EURUSD", "GOLD"]},
            },
        )
        self.assertIsNone(self.stream.subscribe_quotes(["EURUSD"]))
        self.assertEqual(len(self.socket.sent), 1)
        self.stream.unsubscribe_quotes(["EURUSD"])
        self.assertEqual(self.socket.sent[-1]["destination"], "marketData.unsubscribe")
        self.assertEqual(self.socket.sent[-1]["correlationId"], "2")
        self.assertEqual(self.stream._quotes, {"GOLD"})

    def test_every_resolution_and_candle_type_and_filtered_unsubscribe(self):
        for type_ in ("classic", "heikin-ashi"):
            self.stream.subscribe_ohlc(["EURUSD"], sorted(RESOLUTIONS), type_)
        self.assertEqual(len(self.stream._candles), 16)
        count = len(self.socket.sent)
        self.stream.subscribe_ohlc(["EURUSD"], ["MINUTE"], "classic")
        self.assertEqual(len(self.socket.sent), count)
        self.stream.unsubscribe_ohlc(["EURUSD"], ["MINUTE"], ["classic"])
        self.assertNotIn(("EURUSD", "MINUTE", "classic"), self.stream._candles)
        self.assertIn(("EURUSD", "MINUTE", "heikin-ashi"), self.stream._candles)
        self.stream.unsubscribe_ohlc(["EURUSD"])
        self.assertFalse(self.stream._candles)
        self.assertEqual(self.socket.sent[-1]["payload"], {"epics": ["EURUSD"]})

    def test_ohlc_defaults_and_overlapping_requests_never_duplicate_triples(self):
        self.stream.subscribe_ohlc(["EURUSD"])
        self.assertEqual(
            self.socket.sent[-1]["payload"],
            {"epics": ["EURUSD"], "resolutions": ["MINUTE"], "type": "classic"},
        )
        self.stream.subscribe_ohlc(["EURUSD", "GOLD"], ["MINUTE", "HOUR"])
        triples = []
        for message in self.socket.sent:
            p = message["payload"]
            triples.extend(
                (e, r, p["type"]) for e in p["epics"] for r in p["resolutions"]
            )
        self.assertEqual(len(triples), len(set(triples)))

    def test_40_instrument_cap_is_shared_between_quotes_and_candles(self):
        self.stream.subscribe_quotes([str(x) for x in range(40)])
        self.stream.subscribe_ohlc(["0"])
        with self.assertRaises(ValueError):
            self.stream.subscribe_ohlc(["41"])
        self.stream.unsubscribe_quotes(["1"])
        self.stream.subscribe_ohlc(["41"])
        with self.assertRaises(ValueError):
            self.stream.subscribe_quotes([str(x) for x in range(41)])

    def test_events_interleaved_with_ack_are_buffered_and_parsed(self):
        event = {
            "status": "OK",
            "destination": "quote",
            "payload": {"epic": "EURUSD", "bid": 1.1, "ofr": 1.2, "timestamp": 123},
        }
        self.socket.before_ack = event
        self.stream.subscribe_quotes(["EURUSD"])
        self.assertEqual(self.stream.receive(0.01), event)
        candle = {
            "status": "OK",
            "destination": "ohlc.event",
            "payload": {
                "epic": "EURUSD",
                "resolution": "MINUTE",
                "type": "classic",
                "priceType": "bid",
                "t": 123,
                "o": 1,
                "h": 2,
                "l": 0.5,
                "c": 1.5,
            },
        }
        self.socket.incoming.append(json.dumps(candle))
        self.assertEqual(self.stream.receive(0.01), candle)

    def test_ping_uses_authenticated_application_destination_and_no_payload(self):
        self.stream.ping()
        self.assertEqual(
            self.socket.sent[-1],
            {
                "destination": "ping",
                "correlationId": "1",
                "cst": self.api.cst,
                "securityToken": self.api.x_security_token,
            },
        )
        self.stream._last_ping = 0
        self.stream.receive(0.001)
        self.assertEqual(len(self.socket.sent), 2)

    def test_partial_or_rejected_ack_never_grants_subscription(self):
        for flag in ("reject", "partial"):
            socket = Socket()
            setattr(socket, flag, True)
            stream = CapitalWebSocketClient(
                self.api, connector=Mock(return_value=socket), ack_timeout=0.001
            ).connect()
            with self.subTest(flag=flag), self.assertRaises(StreamError):
                stream.subscribe_quotes(["EURUSD"])
            self.assertFalse(stream._quotes)
            self.assertTrue(socket.closed)
            stream.close()

    def test_ack_timeout_and_late_ack_do_not_grant_subscription(self):
        self.socket.auto_ack = False
        self.socket.incoming.append(
            json.dumps(
                {
                    "status": "OK",
                    "destination": "marketData.subscribe",
                    "correlationId": "unmatched",
                    "payload": {"subscriptions": {"EURUSD": "PROCESSED"}},
                }
            )
        )
        with self.assertRaisesRegex(StreamError, "acknowledgment_timeout"):
            self.stream.subscribe_quotes(["EURUSD"])
        self.assertFalse(self.stream._quotes)
        self.assertTrue(self.socket.closed)

    def test_malformed_responses_stop_stream_without_exposing_payload(self):
        for raw in (
            "private-password",
            "[]",
            '{"destination": 1}',
            '{"destination":"quote","payload":[]}',
        ):
            socket = Socket()
            socket.incoming.append(raw)
            stream = CapitalWebSocketClient(
                self.api, connector=Mock(return_value=socket)
            ).connect()
            with self.assertRaises(StreamError) as caught:
                stream.receive(0.001)
            self.assertNotIn("private-password", str(caught.exception))
            self.assertTrue(socket.closed)
            stream.close()

    def test_credentials_are_removed_even_when_server_echoes_them(self):
        secrets = (
            self.api.cst,
            self.api.x_security_token,
            self.api.X_CAP_API_KEY,
            self.api.PASSWORD,
            self.api.IDENTIFIER,
        )
        self.socket.incoming.append(
            json.dumps(
                {
                    "status": "OK",
                    "destination": "quote",
                    "cst": self.api.cst,
                    "payload": {
                        "securityToken": self.api.x_security_token,
                        "echo": "|".join(secrets),
                    },
                }
            )
        )
        event = self.stream.receive(0.01)
        for secret in secrets:
            self.assertNotIn(secret, json.dumps(event))
        self.assertNotIn("cst", event)
        self.assertNotIn("securityToken", event["payload"])

    def test_reconnect_restores_only_acknowledged_subscriptions_without_trading(self):
        self.stream.subscribe_quotes(["EURUSD"])
        self.stream.subscribe_ohlc(["GOLD"], ["HOUR"], "heikin-ashi")
        next_socket = Socket()
        self.connector.side_effect = [next_socket]
        self.socket.incoming.append(OSError("private-token"))
        with (
            patch("trading_bot.api.websocket_client.time.sleep"),
            patch.object(self.api, "createNewSession") as login,
            patch.object(self.api, "_request") as rest,
        ):
            self.stream.receive(0.001)
        login.assert_not_called()
        rest.assert_not_called()
        self.assertEqual(
            [x["destination"] for x in next_socket.sent],
            ["marketData.subscribe", "OHLCMarketData.subscribe"],
        )
        ids = [x["correlationId"] for x in self.socket.sent + next_socket.sent]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(self.socket.closed)

    def test_reconnect_budget_is_finite_and_no_loop_resets_it(self):
        self.connector.side_effect = OSError("private-token")
        with (
            patch("trading_bot.api.websocket_client.time.sleep"),
            self.assertRaisesRegex(StreamError, "budget_exhausted"),
        ):
            self.stream.reconnect()
        self.assertEqual(self.stream._reconnects, 3)
        self.assertEqual(self.connector.call_count, 4)
        with self.assertRaises(StreamError):
            self.stream.reconnect()
        self.assertEqual(self.connector.call_count, 4)

    def test_changed_session_account_or_tokens_never_restore_subscriptions(self):
        for attr, value in (
            ("cst", "changed"),
            ("_login_account_id", "other-account"),
            ("_session_generation", 99),
        ):
            api = client()
            stream = CapitalWebSocketClient(
                api, connector=Mock(return_value=Socket())
            ).connect()
            setattr(api, attr, value)
            with (
                self.subTest(attr=attr),
                self.assertRaisesRegex(StreamError, "session_changed"),
            ):
                stream.reconnect()
            self.assertTrue(stream._fatal)
            stream.close()

    def test_session_expiration_is_fatal_and_does_not_retry_authentication(self):
        self.socket.incoming.append(
            json.dumps(
                {
                    "status": "ERROR",
                    "destination": "ping",
                    "payload": {"error": self.api.cst},
                }
            )
        )
        with (
            patch.object(self.api, "createNewSession") as login,
            self.assertRaises(StreamError),
        ):
            self.stream.receive(0.01)
        login.assert_not_called()
        self.assertEqual(self.connector.call_count, 1)

    def test_invalid_subscription_options_and_closed_client(self):
        for callback in (
            lambda: self.stream.subscribe_quotes([]),
            lambda: self.stream.subscribe_ohlc(["EURUSD"], ["HOUR_2"]),
            lambda: self.stream.subscribe_ohlc(["EURUSD"], type="other"),
            lambda: self.stream.unsubscribe_ohlc(["EURUSD"], types=["other"]),
        ):
            with self.assertRaises(ValueError):
                callback()
        self.assertFalse(self.socket.sent)
        self.stream.close()
        with self.assertRaises(StreamError):
            self.stream.receive(0.01)

"""Official inventory-driven REST contract tests. No credentials or broker traffic."""

from contextlib import ExitStack
import base64
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import requests
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5
from trading_bot.api import rate_limits
from trading_bot.api.client import TradingApi
from trading_bot.api.network_errors import TradingUnavailable
from trading_bot.api.session import encrypt_password

INVENTORY = json.loads(
    (Path(__file__).parent / "fixtures/capital_api_inventory.json").read_text()
)
ACCOUNT = {
    "accountId": "account-one",
    "status": "ENABLED",
    "accountType": "CFD",
    "demo": True,
    "balance": {"balance": 1000},
}


def reply(value=None, status=200, empty=False):
    response = requests.Response()
    response.status_code = status
    response._content = (
        b"" if empty else json.dumps(value if value is not None else {}).encode()
    )
    response.headers.update(
        {"CST": "fixture-session-long", "X-SECURITY-TOKEN": "fixture-security-long"}
    )
    return response


def client(environment="demo"):
    api = TradingApi(
        "fixture-api-key-long",
        "fixture-login-long",
        "fixture-password-long",
        environment=environment,
        auto_authenticate=False,
    )
    api.cst, api.x_security_token = "fixture-session-long", "fixture-security-long"
    api._login_has_active_environment = True
    api._login_account_id = ACCOUNT["accountId"]
    return api


CALLS = {
    "switchesActiveAccount": (("account-one",), {"impact": "account-switch"}),
    "updateAccountPreferences": ((), {"hedgingMode": False, "impact": "preferences"}),
    "adjustBalanceOfDemoAccount": ((100,), {"impact": "demo-balance"}),
    "getPositionOrderConfirmation": (("reference",), {}),
    "createPosition": (("EURUSD", "BUY", 1), {}),
    "getsinglePosition": (("deal-one",), {}),
    "updatePosition": (
        ("deal-one",),
        {"stopDistance": 0.01, "impact": "position-update"},
    ),
    "closePosition": (("deal-one",), {}),
    "createWorkingOrder": (
        ("EURUSD", "BUY", 1, 1.0, "LIMIT"),
        {"impact": "working-order"},
    ),
    "updateWorkingOrder": (("deal-one",), {"level": 1.1, "impact": "working-order"}),
    "deleteWorkingOrder": (("deal-one",), {"impact": "working-order"}),
    "getAllCategorySubNodes": (("category-one",), {}),
    "getMarketsDetails": (("EURUSD,GOLD",), {}),
    "getSingleMarketDetails": (("EURUSD",), {}),
    "getHistoricalPrices": (("EURUSD", "MINUTE", 10), {}),
    "getClientSentimentForMarket": (("EURUSD",), {}),
    "createWatchlist": (("Test", ["EURUSD"]), {"impact": "watchlist"}),
    "getSingleWatchlist": (("watch-one",), {}),
    "addMarketToWatchlist": (("watch-one", "EURUSD"), {"impact": "watchlist"}),
    "deleteWatchlist": (("watch-one",), {"impact": "watchlist"}),
    "removeMarketFromWatchlist": (("watch-one", "EURUSD"), {"impact": "watchlist"}),
}


class EndpointContracts(unittest.TestCase):
    pass


def endpoint_test(row):
    def check(self):
        api = client()
        args, kwargs = CALLS.get(row["python_method"], ((), {}))
        kwargs = dict(kwargs)
        with ExitStack() as stack:
            stack.enter_context(patch("trading_bot.api.rate_limits.throttle"))
            stack.enter_context(
                patch.object(api, "verify_account_environment", return_value=ACCOUNT)
            )
            if "impact" in kwargs:
                impact = kwargs.pop("impact")
                target = (
                    args[0]
                    if row["python_method"]
                    in ("updatePosition", "updateWorkingOrder", "deleteWorkingOrder")
                    else None
                )
                kwargs["authorization"] = api.authorize_mutation(
                    impact, resource_id=target
                )
            payload = row["response_examples"][0] if row["response_examples"] else {}
            if isinstance(payload, str):
                payload = json.loads(payload)
            if row["python_method"] == "switchesActiveAccount":
                stack.enter_context(
                    patch.object(
                        api, "getAllAccounts", return_value={"accounts": [ACCOUNT]}
                    )
                )
            send = stack.enter_context(
                patch(
                    "trading_bot.api.client.requests." + row["method"],
                    return_value=reply(payload),
                )
            )
            result = getattr(api, row["python_method"])(*args, **kwargs)
        self.assertEqual(result[0] if isinstance(result, tuple) else result, payload)
        send.assert_called_once()
        call = send.call_args
        prepared = requests.Request(
            row["method"].upper(),
            call.args[0],
            headers=call.kwargs.get("headers"),
            params=call.kwargs.get("params"),
            json=call.kwargs.get("json"),
        ).prepare()
        path = row["path"]
        for key, value in {
            "dealReference": "reference",
            "dealId": "deal-one",
            "nodeId": "category-one",
            "epic": "EURUSD",
            "marketId": "EURUSD",
            "watchlistId": "watch-one",
        }.items():
            path = path.replace("{" + key + "}", value)
        self.assertEqual(prepared.path_url.split("?")[0], path)
        self.assertTrue(
            prepared.url.startswith(
                "https://demo-api-capital.backend-capital.com/api/v1/"
            )
        )
        headers = call.kwargs.get("headers", {})
        if row["python_method"] in ("getEncryptionKey", "createNewSession"):
            self.assertEqual(headers["X-CAP-API-KEY"], api.X_CAP_API_KEY)
        elif row["python_method"] != "getServerTime":
            self.assertEqual(headers["CST"], "fixture-session-long")
            self.assertEqual(headers["X-SECURITY-TOKEN"], "fixture-security-long")
        self.assertIs(call.kwargs["allow_redirects"], False)
        self.assertIs(call.kwargs["verify"], True)
        self.assertEqual(call.kwargs["timeout"], (5, 20))
        if row["method"] == "get":
            self.assertIsNone(prepared.body)

    return check


for row in INVENTORY:
    setattr(
        EndpointContracts,
        "test_" + row["method"] + "_" + row["python_method"],
        endpoint_test(row),
    )


class QueryAndPayloadTests(unittest.TestCase):
    def setUp(self):
        self.api = client()
        self.throttle = patch("trading_bot.api.rate_limits.throttle").start()
        self.addCleanup(patch.stopall)

    def test_inventory_has_every_official_operation(self):
        self.assertEqual(len(INVENTORY), 36)
        self.assertEqual(len({(r["method"], r["path"]) for r in INVENTORY}), 36)

    def test_optional_queries_and_encoding(self):
        cases = [
            (
                lambda: self.api.getAccountActivityHistory(
                    from_="2026-01-01T00:00:00",
                    to="2026-01-02T00:00:00",
                    lastPeriod=60,
                    detailed=True,
                    dealId="a/b & c",
                    filter="epic==EURUSD;status==REJECTED",
                ),
                {
                    "from": "2026-01-01T00:00:00",
                    "to": "2026-01-02T00:00:00",
                    "lastPeriod": 60,
                    "detailed": "true",
                    "dealId": "a/b & c",
                    "filter": "epic==EURUSD;status==REJECTED",
                },
            ),
            (
                lambda: self.api.getAccountTransactionsHistory(
                    from_="2026-01-01T00:00:00",
                    to="2026-02-01T00:00:00",
                    lastPeriod=100,
                    type="DEPOSIT",
                ),
                {
                    "from": "2026-01-01T00:00:00",
                    "to": "2026-02-01T00:00:00",
                    "lastPeriod": 100,
                    "type": "DEPOSIT",
                },
            ),
            (
                lambda: self.api.getHistoricalPrices(
                    "EURUSD",
                    "DAY",
                    1000,
                    from_="2026-01-01T00:00:00",
                    to="2026-02-01T00:00:00",
                ),
                {
                    "resolution": "DAY",
                    "max": 1000,
                    "from": "2026-01-01T00:00:00",
                    "to": "2026-02-01T00:00:00",
                },
            ),
            (lambda: self.api.getAllCategorySubNodes("A/B", 500), {"limit": 500}),
            (lambda: self.api.searchMarkets("A & B/€"), {"searchTerm": "A & B/€"}),
            (
                lambda: self.api.getClientSentimentForMarkets(["EURUSD", "GOLD"]),
                {"marketIds": "EURUSD,GOLD"},
            ),
        ]
        for operation, expected in cases:
            with (
                self.subTest(expected=expected),
                patch(
                    "trading_bot.api.client.requests.get", return_value=reply()
                ) as send,
            ):
                operation()
                actual = send.call_args.kwargs["params"]
                self.assertEqual(actual, expected)
                from urllib.parse import parse_qs, urlsplit

                prepared = requests.Request(
                    "GET", send.call_args.args[0], params=actual
                ).prepare()
                self.assertEqual(
                    parse_qs(urlsplit(prepared.url).query),
                    {k: [str(v)] for k, v in expected.items()},
                )
                if "limit" in expected:
                    self.assertTrue(urlsplit(prepared.url).path.endswith("A%2FB"))

    def test_all_markets_has_no_filters(self):
        with patch("trading_bot.api.client.requests.get", return_value=reply()) as send:
            self.api.getAllMarkets()
        self.assertNotIn("params", send.call_args.kwargs)

    def test_all_stop_fields_and_false_values_are_preserved(self):
        for field in (
            "stopLevel",
            "stopDistance",
            "stopAmount",
            "profitLevel",
            "profitDistance",
            "profitAmount",
        ):
            with (
                self.subTest(field=field),
                patch(
                    "trading_bot.api.client.requests.post",
                    return_value=reply({"dealReference": "ref"}),
                ) as send,
            ):
                self.api.createPosition(
                    "EURUSD", "BUY", 0.1, trailingStop=False, **{field: 0.01}
                )
                payload = send.call_args.kwargs["json"]
                self.assertEqual(payload[field], 0.01)
                self.assertIs(payload["trailingStop"], False)
        with (
            patch.object(self.api, "_authorize"),
            patch(
                "trading_bot.api.client.requests.put",
                return_value=reply({"dealReference": "fixture-reference"}),
            ) as send,
        ):
            self.api.updatePosition(
                "deal", trailingStop=True, stopDistance=0.01, profitAmount=5
            )
            self.assertEqual(
                send.call_args.kwargs["json"],
                {"trailingStop": True, "stopDistance": 0.01, "profitAmount": 5},
            )

    def test_working_order_schema_and_update_does_not_invent_size_or_type(self):
        with (
            patch.object(self.api, "_authorize"),
            patch(
                "trading_bot.api.client.requests.post",
                return_value=reply({"dealReference": "fixture-reference"}),
            ) as post,
            patch(
                "trading_bot.api.client.requests.put",
                return_value=reply({"dealReference": "fixture-reference"}),
            ) as put,
        ):
            self.api.createWorkingOrder(
                "EURUSD",
                "SELL",
                0.1,
                2,
                "STOP",
                goodTillDate="2026-12-01T00:00:00",
                guaranteedStop=True,
                stopAmount=1,
                profitDistance=0.02,
            )
            payload = post.call_args.kwargs["json"]
            self.assertEqual(
                payload,
                {
                    "epic": "EURUSD",
                    "direction": "SELL",
                    "size": 0.1,
                    "level": 2,
                    "type": "STOP",
                    "goodTillDate": "2026-12-01T00:00:00",
                    "guaranteedStop": True,
                    "stopAmount": 1,
                    "profitDistance": 0.02,
                },
            )
            self.api.updateWorkingOrder(
                "order", level=1.9, guaranteedStop=False, trailingStop=False
            )
            self.assertEqual(
                put.call_args.kwargs["json"],
                {"level": 1.9, "guaranteedStop": False, "trailingStop": False},
            )

    def test_watchlists_preserve_partial_success(self):
        with (
            patch.object(self.api, "_authorize"),
            patch(
                "trading_bot.api.client.requests.post",
                return_value=reply(
                    {
                        "status": "SUCCESS_NOT_ALL_INSTRUMENTS_ADDED",
                        "watchlistId": "one",
                    }
                ),
            ) as post,
        ):
            result = self.api.createWatchlist("My list", ["EURUSD", "GOLD"])
        self.assertEqual(
            post.call_args.kwargs["json"],
            {"name": "My list", "epics": ["EURUSD", "GOLD"]},
        )
        self.assertEqual(result["status"], "SUCCESS_NOT_ALL_INSTRUMENTS_ADDED")

    def test_invalid_parameters_never_reach_network(self):
        cases = [
            lambda: self.api.createPosition("EURUSD", "BAD", 1),
            lambda: self.api.createPosition("EURUSD", "BUY", float("nan")),
            lambda: self.api.createPosition("EURUSD", "BUY", True),
            lambda: self.api.createPosition("EURUSD", "BUY", 1, trailingStop=True),
            lambda: self.api.createPosition("EURUSD", "BUY", 1, guaranteedStop=True),
            lambda: self.api.createPosition(
                "EURUSD", "BUY", 1, stopDistance=1, stopLevel=2
            ),
            lambda: self.api.getHistoricalPrices("EURUSD", "HOUR_2", 10),
            lambda: self.api.getHistoricalPrices("EURUSD", "DAY", 1001),
            lambda: self.api.getHistoricalPrices("EURUSD", from_="not-a-date"),
            lambda: self.api.getHistoricalPrices(
                "EURUSD", from_="2026-01-02T00:00:00", to="2026-01-01T00:00:00"
            ),
            lambda: self.api.getMarketsDetails(["epic"] * 51),
            lambda: self.api.getAllCategorySubNodes("node", 501),
            lambda: self.api.getAccountActivityHistory(lastPeriod=86401),
            lambda: self.api.getAccountActivityHistory(
                from_="2026-01-01T00:00:00", to="2026-01-03T00:00:00"
            ),
            lambda: self.api.getAccountTransactionsHistory(type="BOGUS"),
            lambda: self.api.createWatchlist("x" * 21),
            lambda: self.api.createWorkingOrder("EURUSD", "BUY", 1, 1, "MARKET"),
            lambda: self.api.updateWorkingOrder("one", size=4),
            lambda: self.api.getSingleWatchlist(".."),
            lambda: self.api.adjustBalanceOfDemoAccount(400001),
        ]
        with patch.object(requests.sessions.Session, "send") as send:
            for operation in cases:
                with (
                    self.subTest(operation=operation),
                    self.assertRaises((ValueError, PermissionError)),
                ):
                    operation()
            send.assert_not_called()

    def test_preference_updates_are_explicit_and_use_available_leverage_only(self):
        with (
            patch.object(self.api, "_authorize"),
            patch.object(
                self.api,
                "getAccountPreferences",
                return_value={"leverages": {"CURRENCIES": {"available": [1, 10]}}},
            ),
            patch("trading_bot.api.client.requests.put", return_value=reply()) as send,
        ):
            self.api.updateAccountPreferences(leverages={"CURRENCIES": 10})
            self.assertEqual(
                send.call_args.kwargs["json"], {"leverages": {"CURRENCIES": 10}}
            )
            self.api.updateAccountPreferences(hedgingMode=False)
            self.assertEqual(send.call_args.kwargs["json"], {"hedgingMode": False})
            for kwargs in ({}, {"leverages": {"CURRENCIES": 100}}, {"hedgingMode": 1}):
                with self.assertRaises(ValueError):
                    self.api.updateAccountPreferences(**kwargs)

    def test_rsa_encryption_matches_documented_algorithm_and_encrypted_login(self):
        key = RSA.generate(1024)
        public = base64.b64encode(key.public_key().export_key(format="DER")).decode()
        encoded = encrypt_password("secret", public, 123)
        decrypted = PKCS1_v1_5.new(key).decrypt(base64.b64decode(encoded), None)
        self.assertEqual(base64.b64decode(decrypted), b"secret|123")
        with (
            patch.object(
                self.api,
                "getEncryptionKey",
                return_value={"encryptionKey": public, "timeStamp": 123},
            ),
            patch("trading_bot.api.client.requests.post", return_value=reply()) as post,
        ):
            self.api.createNewSession(encryptedPassword=True)
        payload = post.call_args.kwargs["json"]
        self.assertIs(payload["encryptedPassword"], True)
        self.assertNotEqual(payload["password"], self.api.PASSWORD)

    def test_empty_logout_clears_all_session_state(self):
        with patch(
            "trading_bot.api.client.requests.delete", return_value=reply(empty=True)
        ):
            self.assertEqual(self.api.logOutOfTheCurrentSession(), {})
        self.assertFalse(self.api.cst)
        self.assertFalse(self.api.x_security_token)


class AuthorizationAndFailureTests(unittest.TestCase):
    def test_new_mutations_require_their_own_session_bound_authorization(self):
        api = client()
        with patch.object(api, "verify_account_environment", return_value=ACCOUNT):
            grant = api.authorize_mutation("watchlist")
            api._authorize(grant, "watchlist")
            for other, impact in (
                (grant, "preferences"),
                (None, "watchlist"),
                (object(), "watchlist"),
            ):
                with self.assertRaises(PermissionError):
                    api._authorize(other, impact)
            api.x_security_token = "different-token"
            with self.assertRaises(PermissionError):
                api._authorize(grant, "watchlist")
        self.assertNotIn("fixture-", repr(grant))

    def test_new_live_mutations_and_raw_helpers_cannot_bypass_gate(self):
        api = client("live")
        with (
            patch.object(api, "verify_account_environment", return_value=ACCOUNT),
            patch.object(requests.sessions.Session, "send") as send,
        ):
            for row in INVENTORY:
                if row["method"] == "get" or row["python_method"] == "createNewSession":
                    continue
                args, kwargs = CALLS.get(row["python_method"], ((), {}))
                kwargs = {k: v for k, v in kwargs.items() if k != "impact"}
                with (
                    self.subTest(method=row["python_method"]),
                    self.assertRaises((PermissionError, SystemExit)),
                ):
                    getattr(api, row["python_method"])(*args, **kwargs)
            for operation in (
                lambda: api._post_position({"epic": "EURUSD"}),
                lambda: api._delete_position("unrelated"),
                lambda: api.authorize_mutation("demo-balance"),
            ):
                with self.assertRaises((PermissionError, SystemExit)):
                    operation()
            send.assert_not_called()

    def test_endpoint_isolation_at_raw_transport_boundary(self):
        for env, wrong in (
            ("demo", "https://api-capital.backend-capital.com"),
            ("live", "https://demo-api-capital.backend-capital.com"),
        ):
            with (
                patch.object(requests.sessions.Session, "send") as send,
                self.assertRaises(PermissionError),
            ):
                client(env)._request("get", wrong + "/api/v1/accounts")
            send.assert_not_called()

    def test_switch_requires_explicit_grant_and_refreshes_rotated_tokens(self):
        api = client()
        with (
            patch.object(api, "verify_account_environment", return_value=ACCOUNT),
            patch.object(api, "getAllAccounts", return_value={"accounts": [ACCOUNT]}),
            patch("trading_bot.api.rate_limits.throttle"),
            patch(
                "trading_bot.api.client.requests.put",
                return_value=reply({"dealingEnabled": True}),
            ) as put,
        ):
            with self.assertRaises(PermissionError):
                api.switchesActiveAccount("account-one")
            put.assert_not_called()
            grant = api.authorize_mutation("account-switch")
            api.switchesActiveAccount("account-one", authorization=grant)
            self.assertEqual(put.call_args.kwargs["json"], {"accountId": "account-one"})
            self.assertEqual(api._session_generation, 1)

    def test_topup_cap_and_live_rejection(self):
        api = client()
        with patch.object(api, "verify_account_environment", return_value=ACCOUNT):
            grant = api.authorize_mutation("demo-balance")
            with self.assertRaises(ValueError):
                api.adjustBalanceOfDemoAccount(100000, authorization=grant)
        with self.assertRaises(PermissionError):
            client("live").adjustBalanceOfDemoAccount(1, authorization=grant)

    def test_mutation_errors_latch_without_retry_or_secret_leak(self):
        for status in (400, 401, 403, 429, 500, 302):
            api = client()
            with (
                patch("trading_bot.api.rate_limits.throttle"),
                patch(
                    "trading_bot.api.client.requests.post",
                    return_value=reply({"errorCode": "private-secret"}, status),
                ) as send,
            ):
                with self.assertRaises(TradingUnavailable) as caught:
                    api.createPosition("EURUSD", "BUY", 1)
                self.assertNotIn("private-secret", str(caught.exception))
                with self.assertRaises(TradingUnavailable):
                    api.createPosition("EURUSD", "BUY", 1)
                send.assert_called_once()

    def test_new_mutation_timeout_is_uncertain_and_not_retried(self):
        api = client()
        with patch.object(api, "verify_account_environment", return_value=ACCOUNT):
            grant = api.authorize_mutation("working-order")
            with (
                patch("trading_bot.api.rate_limits.throttle"),
                patch(
                    "trading_bot.api.client.requests.post",
                    side_effect=requests.ReadTimeout("secret"),
                ) as send,
            ):
                with self.assertRaises(TradingUnavailable) as caught:
                    api.createWorkingOrder(
                        "EURUSD", "BUY", 1, 1, "LIMIT", authorization=grant
                    )
                self.assertIn("uncertain", str(caught.exception))
                with self.assertRaises(TradingUnavailable):
                    api.createWorkingOrder(
                        "EURUSD", "BUY", 1, 1, "LIMIT", authorization=grant
                    )
                send.assert_called_once()


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        rate_limits._STATE.clear()
        self.addCleanup(rate_limits._STATE.clear)

    def test_all_environments_and_clients_share_request_and_login_limits(self):
        with (
            patch.object(rate_limits.time, "monotonic", return_value=100),
            patch.object(rate_limits.time, "sleep") as sleep,
        ):
            rate_limits.throttle("key", "demo", "post", "/api/v1/session", None)
            rate_limits.throttle("key", "live", "post", "/api/v1/session", None)
            self.assertAlmostEqual(sleep.call_args.args[0], 1.05)
            rate_limits.throttle("key", "demo", "get", "/api/v1/accounts", None)
            self.assertAlmostEqual(sleep.call_args.args[0], 0.11)

    def test_hourly_demo_order_limit_is_shared_across_positions_and_workingorders(self):
        with (
            patch.object(rate_limits.time, "monotonic", return_value=100),
            patch.object(rate_limits.time, "sleep"),
        ):
            for _ in range(1000):
                rate_limits.throttle(
                    "key", "demo", "post", "/api/v1/positions", "account"
                )
            with self.assertRaises(PermissionError):
                rate_limits.throttle(
                    "key", "demo", "post", "/api/v1/workingorders", "account"
                )
        with patch.object(rate_limits.time, "monotonic", return_value=3701):
            rate_limits.throttle(
                "key", "demo", "post", "/api/v1/workingorders", "account"
            )

    def test_topup_quota_is_per_account_and_never_waits_a_day(self):
        with (
            patch.object(rate_limits.time, "monotonic", return_value=100),
            patch.object(rate_limits.time, "sleep"),
        ):
            for _ in range(100):
                rate_limits.throttle(
                    "key", "demo", "post", "/api/v1/accounts/topUp", "account"
                )
            with self.assertRaises(PermissionError):
                rate_limits.throttle(
                    "key", "demo", "post", "/api/v1/accounts/topUp", "account"
                )
            rate_limits.throttle(
                "key", "demo", "post", "/api/v1/accounts/topUp", "other-account"
            )

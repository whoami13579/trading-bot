"""Safety boundary tests for the opt-in DEMO verifier. No broker access."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from trading_bot.config.trading_config import DEMO_URL, LIVE_URL
from trading_bot.risk.live_risk import IntegrationError
from trading_bot.tools.api_integration import (
    DemoVerificationTransport,
    VerificationJournal,
    confirmation,
)
from test_api_coverage import ACCOUNT, client, reply


class VerificationTransportTests(unittest.TestCase):
    def prepared(self, method="POST", path="/positions", payload=None, base=DEMO_URL):
        return requests.Request(method, base + "/api/v1" + path, json=payload).prepare()

    def test_live_and_lookalike_hosts_never_reach_send(self):
        guard = DemoVerificationTransport(True)
        guard.original_send = Mock()
        for base in (
            LIVE_URL,
            DEMO_URL + ".evil.example",
            DEMO_URL + ":8443",
            "http://demo-api-capital.backend-capital.com",
        ):
            with self.subTest(base=base), self.assertRaises(IntegrationError):
                guard.send(requests.Session(), self.prepared(base=base))
        guard.original_send.assert_not_called()

    def test_default_mode_rejects_mutation_even_with_an_intent(self):
        guard = DemoVerificationTransport()
        guard.original_send = Mock()
        with self.assertRaises(IntegrationError):
            with guard.intent("POST", "/positions", {"size": 100}):
                pass
        with self.assertRaises(IntegrationError):
            guard.send(requests.Session(), self.prepared())
        guard.original_send.assert_not_called()

    def test_exact_durable_intent_is_single_use_even_after_timeout(self):
        guard = DemoVerificationTransport(True)
        guard.original_send = Mock(side_effect=requests.ReadTimeout("private-token"))
        payload = {"epic": "EURUSD", "size": 100}
        request = self.prepared(payload=payload)
        with guard.intent("POST", "/positions", payload):
            with self.assertRaises(requests.ReadTimeout):
                guard.send(requests.Session(), request)
            with self.assertRaises(IntegrationError):
                guard.send(requests.Session(), request)
        guard.original_send.assert_called_once()

    def test_wrong_resource_payload_and_method_are_blocked(self):
        guard = DemoVerificationTransport(True)
        guard.original_send = Mock(return_value=reply())
        for request in (
            self.prepared("DELETE", "/positions/unrelated"),
            self.prepared("PUT", "/positions/owned", {"stopDistance": 2}),
            self.prepared("POST", "/positions", {"size": 200}),
        ):
            with (
                guard.intent("DELETE", "/positions/owned"),
                self.assertRaises(IntegrationError),
            ):
                guard.send(requests.Session(), request)
        guard.original_send.assert_not_called()

    def test_mutation_forces_tls_redirect_and_timeout_controls(self):
        guard = DemoVerificationTransport(True)
        guard.original_send = Mock(return_value=reply())
        with guard.intent("DELETE", "/workingorders/owned"):
            guard.send(
                requests.Session(),
                self.prepared("DELETE", "/workingorders/owned"),
                verify=False,
                allow_redirects=True,
            )
        kwargs = guard.original_send.call_args.kwargs
        self.assertIs(kwargs["verify"], True)
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertEqual(kwargs["timeout"], (5, 20))

    def test_only_one_auth_attempt_and_bounded_request_budget(self):
        guard = DemoVerificationTransport()
        guard.original_send = Mock(return_value=reply())
        guard.send(
            requests.Session(),
            self.prepared("POST", "/session", {"password": "fixture"}),
        )
        with self.assertRaises(IntegrationError):
            guard.send(
                requests.Session(),
                self.prepared("POST", "/session", {"password": "fixture"}),
            )
        guard.calls = [{}] * 180
        with self.assertRaises(IntegrationError):
            guard.send(requests.Session(), self.prepared("GET", "/time"))

    def test_journal_is_private_and_unresolved_run_blocks_next_run(self):
        with tempfile.TemporaryDirectory() as root:
            with VerificationJournal(root) as journal:
                journal.data["operations"].append(
                    {"operation": "createPosition", "stage": "submitting"}
                )
                journal.save()
                self.assertEqual(journal.path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(IntegrationError):
                with VerificationJournal(root):
                    pass
            # Reconciliation, not an order retry, is required for the unresolved journal.
            self.assertFalse(
                json.loads((Path(root) / "state.json").read_text())["complete"]
            )

    def test_confirmation_is_bounded_and_only_polls_reads(self):
        api = Mock()
        api.getPositionOrderConfirmation.return_value = None
        with (
            patch("trading_bot.tools.api_integration.time.sleep"),
            self.assertRaises(IntegrationError),
        ):
            confirmation(api, "reference-one")
        self.assertEqual(api.getPositionOrderConfirmation.call_count, 5)
        self.assertEqual(
            {x[0] for x in api.mock_calls}, {"getPositionOrderConfirmation"}
        )

    def test_confirmation_rejects_mismatched_references(self):
        api = Mock()
        api.getPositionOrderConfirmation.return_value = {
            "dealReference": "unrelated",
            "dealStatus": "ACCEPTED",
        }
        with self.assertRaises(IntegrationError):
            confirmation(api, "reference-one")


class AuthorizationTargetTests(unittest.TestCase):
    def test_updates_and_cancellation_are_bound_to_explicit_target(self):
        api = client()
        with (
            patch.object(api, "verify_account_environment", return_value=ACCOUNT),
            patch.object(requests.sessions.Session, "send") as send,
        ):
            new_order = api.authorize_mutation("working-order")
            with self.assertRaises(PermissionError):
                api.deleteWorkingOrder("unrelated", authorization=new_order)
            selected = api.authorize_mutation("working-order", resource_id="owned")
            with self.assertRaises(PermissionError):
                api.deleteWorkingOrder("unrelated", authorization=selected)
            selected_position = api.authorize_mutation(
                "position-update", resource_id="owned"
            )
            with self.assertRaises(PermissionError):
                api.updatePosition(
                    "unrelated", stopDistance=0.01, authorization=selected_position
                )
            send.assert_not_called()

    def test_session_generation_change_invalidates_authorization_even_if_tokens_are_same(
        self,
    ):
        api = client()
        with patch.object(api, "verify_account_environment", return_value=ACCOUNT):
            grant = api.authorize_mutation("watchlist")
            api._session_generation += 1
            with self.assertRaises(PermissionError):
                api._authorize(grant, "watchlist")

    def test_broker_error_identifiers_are_allowlisted_and_never_echo_values(self):
        from trading_bot.api.errors import broker_error_code

        self.assertEqual(
            broker_error_code(
                reply({"errorCode": "error.invalid.stoploss.minvalue: private-secret"})
            ),
            "error.invalid.stoploss.minvalue",
        )
        self.assertIsNone(broker_error_code(reply({"errorCode": "private-secret"})))
        api = client()
        with (
            patch("trading_bot.api.rate_limits.throttle"),
            patch(
                "trading_bot.api.client.requests.get",
                return_value=reply({"errorCode": "error.invalid.from"}, 400),
            ),
        ):
            from trading_bot.api.network_errors import TradingUnavailable

            with self.assertRaises(TradingUnavailable) as caught:
                api.getAccountActivityHistory()
        self.assertEqual(caught.exception.broker_code, "error.invalid.from")
        self.assertEqual(caught.exception.status_code, 400)
        self.assertFalse(caught.exception.outcome_uncertain)


class AdditionalFailureTests(unittest.TestCase):
    def test_switch_missing_rotated_headers_fails_closed(self):
        from trading_bot.api.network_errors import TradingUnavailable

        api = client()
        res = reply({"dealingEnabled": True})
        res.headers.clear()
        with (
            patch.object(api, "verify_account_environment", return_value=ACCOUNT),
            patch.object(api, "getAllAccounts", return_value={"accounts": [ACCOUNT]}),
            patch("trading_bot.api.rate_limits.throttle"),
            patch("trading_bot.api.client.requests.put", return_value=res),
        ):
            grant = api.authorize_mutation("account-switch")
            with self.assertRaises(TradingUnavailable):
                api.switchesActiveAccount("account-one", authorization=grant)
        self.assertFalse(api.cst)
        self.assertIsNotNone(api._unavailable)

    def test_switch_postcondition_failure_invalidates_session(self):
        from trading_bot.api.network_errors import TradingUnavailable

        api = client()
        with (
            patch.object(
                api,
                "verify_account_environment",
                side_effect=[ACCOUNT, ACCOUNT, RuntimeError("fixed")],
            ),
            patch.object(api, "getAllAccounts", return_value={"accounts": [ACCOUNT]}),
            patch("trading_bot.api.rate_limits.throttle"),
            patch("trading_bot.api.client.requests.put", return_value=reply()),
        ):
            grant = api.authorize_mutation("account-switch")
            with self.assertRaises(TradingUnavailable) as caught:
                api.switchesActiveAccount("account-one", authorization=grant)
        self.assertEqual(caught.exception.reason, "account_verification_failed")

    def test_new_trading_mutation_missing_reference_is_uncertain_not_success(self):
        from trading_bot.api.network_errors import TradingUnavailable

        api = client()
        with patch.object(api, "verify_account_environment", return_value=ACCOUNT):
            grant = api.authorize_mutation("working-order")
            with (
                patch("trading_bot.api.rate_limits.throttle"),
                patch(
                    "trading_bot.api.client.requests.post", return_value=reply()
                ) as post,
            ):
                with self.assertRaises(TradingUnavailable) as caught:
                    api.createWorkingOrder(
                        "EURUSD", "BUY", 100, 1.0, "LIMIT", authorization=grant
                    )
                self.assertTrue(caught.exception.outcome_uncertain)
                self.assertEqual(caught.exception.reason, "invalid_api_response")
                post.assert_called_once()

    def test_requests_are_paced_per_user_across_different_api_keys(self):
        from trading_bot.api import rate_limits

        rate_limits._STATE.clear()
        self.addCleanup(rate_limits._STATE.clear)
        with (
            patch.object(rate_limits.time, "monotonic", return_value=100),
            patch.object(rate_limits.time, "sleep") as sleep,
        ):
            rate_limits.throttle(
                "key-one",
                "demo",
                "get",
                "/api/v1/accounts",
                "account",
                user_identifier="same-user",
            )
            rate_limits.throttle(
                "key-two",
                "demo",
                "get",
                "/api/v1/accounts",
                "account",
                user_identifier="same-user",
            )
            self.assertAlmostEqual(sleep.call_args.args[0], 0.11)

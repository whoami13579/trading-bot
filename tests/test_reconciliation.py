"""Synthetic broker-shaped records only. No credentials, network or real orders."""

from contextlib import redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
import io
import unittest
from unittest.mock import Mock, patch

from scripts.trade_reconciliation import (fx_price_metrics, summarize_transactions,
    transaction_window, valid_transaction_query, validate_close_confirmation)
from scripts.live_risk import IntegrationError
from scripts.integration_roundtrip import finalize_report, new_report, print_report


def transaction(**changes):
    return {"dealId": "owned-position", "reference": "separate-ledger-id", "currency": "USD",
            "transactionType": "TRADE", "status": "PROCESSED", "size": "0.0", "instrumentName": "EURUSD", **changes}


def summarize(rows):
    return summarize_transactions(rows, "owned-position", {"o_open", "p_owned-position"}, "USD", "EURUSD")


def market():
    return {"instrument": {"epic": "EURUSD", "type": "CURRENCIES", "currency": "USD", "lotSize": 1},
            "snapshot": {"bid": "1.11407", "offer": "1.11413", "scalingFactor": 1}}


class TransactionReconciliationTests(unittest.TestCase):
    def test_real_schema_deal_id_matches_opaque_ledger_reference_and_zero_is_valid(self):
        value = summarize([transaction()])
        self.assertEqual(value["realized_profit_loss"], "0.0")
        self.assertEqual(value["pnl_reconciliation_status"], "ATTRIBUTED")
        self.assertIsNone(value["reported_fee_cashflows"])
        self.assertFalse(value["fee_completeness_verified"])

    def test_other_position_cannot_be_attributed_even_with_a_known_reference(self):
        value = summarize([transaction(dealId="other-position", reference="o_open", size="9999")])
        self.assertIsNone(value["realized_profit_loss"])

    def test_unrelated_pending_other_currency_and_other_market_transactions_are_ignored(self):
        rows = [transaction(dealId="other-position"), transaction(currency="EUR"),
                transaction(status="PENDING"), transaction(instrumentName="GOLD")]
        self.assertEqual(summarize(rows)["pnl_reconciliation_status"], "PENDING")

    def test_legacy_reference_fallback_requires_absent_deal_id(self):
        row = transaction(reference="p_owned-position", size="-0.01")
        row.pop("dealId")
        self.assertEqual(summarize([row])["realized_profit_loss"], "-0.01")

    def test_fees_are_preserved_even_without_a_trade_pnl_record(self):
        value = summarize([transaction(transactionType="TRADE_COMMISSION", size="-0.01")])
        self.assertIsNone(value["realized_profit_loss"])
        self.assertEqual(value["reported_fee_cashflows"], {"TRADE_COMMISSION": "-0.01"})
        self.assertEqual(value["fee_reconciliation_status"], "REPORTED_PARTIAL")

    def test_duplicate_ledger_record_is_not_counted_twice(self):
        self.assertEqual(summarize([transaction(size="0.01"), transaction(size="0.01")])["realized_profit_loss"], "0.01")
        with self.assertRaisesRegex(IntegrationError, "conflicting_transaction_cashflows"):
            summarize([transaction(size="0.01"), transaction(size="0.02")])

    def test_nonfinite_or_missing_cashflows_never_become_zero(self):
        for value in (None, "NaN", "Infinity", "secret-invalid-number"):
            with self.subTest(value=value), self.assertRaises(IntegrationError):
                summarize([transaction(size=value)])

    def test_explicit_utc_window_retains_trades_older_than_ten_minutes(self):
        window = transaction_window("2026-11-11T08:29:38+00:00", datetime(2026, 11, 11, 9, 30, tzinfo=timezone.utc))
        self.assertEqual(window, {"from": "2026-11-11T08:28:38", "to": "2026-11-11T09:30:00"})
        self.assertTrue(valid_transaction_query({k: [v] for k, v in window.items()}))
        self.assertFalse(valid_transaction_query({"lastPeriod": ["600"]}))
        self.assertFalse(valid_transaction_query({"from": [window["from"]], "to": [window["to"]], "extra": [""]}))

    def test_taipei_timestamp_is_converted_to_utc_for_history_filters(self):
        result = transaction_window("2026-11-11T16:29:38+08:00", datetime(2026, 11, 11, 9, tzinfo=timezone.utc))
        self.assertEqual(result["from"], "2026-11-11T08:28:38")


class CostAndConfirmationTests(unittest.TestCase):
    def test_spread_uses_base_currency_units_not_standard_lots_or_percentage(self):
        result = fx_price_metrics(market(), "USD", "EURUSD", 100)
        self.assertEqual(Decimal(result["spread_price"]), Decimal("0.00006"))
        self.assertEqual(Decimal(result["spread_pips"]), Decimal("0.6"))
        self.assertEqual(Decimal(result["estimated_roundtrip_spread_cost"]), Decimal("0.006"))
        self.assertEqual(Decimal(result["pip_value_account_currency"]), Decimal("0.01"))
        self.assertEqual(result["size_base_units"], "100 EUR")

    def test_fill_price_estimate_includes_spread_and_is_not_rounded_ledger_pnl(self):
        for side, expected in (("BUY", "-0.002"), ("SELL", "0.002")):
            result = fx_price_metrics(market(), "USD", "EURUSD", 100,
                                     direction=side, opening="1.11409", closing="1.11407")
            self.assertEqual(Decimal(result["price_based_pnl_estimate"]), Decimal(expected))
        self.assertEqual(summarize([transaction()])["realized_profit_loss"], "0.0")

    def test_unverified_contract_or_conversion_does_not_produce_a_cost_guess(self):
        for changed in ("lotSize", "scalingFactor", "currency", "type"):
            value = market()
            if changed == "scalingFactor": value["snapshot"][changed] = 100
            elif changed == "lotSize": value["instrument"][changed] = 100000
            elif changed == "currency": value["instrument"][changed] = "EUR"
            else: value["instrument"][changed] = "INDICES"
            with self.subTest(changed=changed):
                self.assertIsNone(fx_price_metrics(value, "USD", "EURUSD", 100)["estimated_roundtrip_spread_cost"])

    def test_close_reference_echo_cannot_override_a_different_affected_position(self):
        value = {"dealReference": "p_owned-position", "dealStatus": "ACCEPTED", "status": "CLOSED", "epic": "EURUSD",
                 "dealId": "old-foreign-position", "direction": "SELL", "size": 100,
                 "affectedDeals": [{"dealId": "old-foreign-position", "status": "FULLY_CLOSED"}]}
        with self.assertRaisesRegex(IntegrationError, "wrong_position"):
            validate_close_confirmation(value, "owned-position", "EURUSD", "BUY", 100)
        value["affectedDeals"][0]["dealId"] = "owned-position"
        validate_close_confirmation(value, "owned-position", "EURUSD", "BUY", 100)
        for changes in ({"size": 1}, {"direction": "BUY"}, {"affectedDeals": []}):
            with self.subTest(changes=changes), self.assertRaises(IntegrationError):
                validate_close_confirmation({**value, **changes}, "owned-position", "EURUSD", "BUY", 100)


class IndependentReportingTests(unittest.TestCase):
    def report(self):
        args = Mock(environment="demo", symbol="EURUSD", size=100, read_only=False)
        return new_report(args)

    def test_closed_buy_and_unattempted_sell_are_not_both_execution_failures(self):
        report = self.report()
        report.update(account_currency="USD", financial_reconciliation_pending=True)
        report["roundtrips"]["BUY"].update(closure_verified=True, execution_status="CLOSED_CONFIRMED", opening_price="1.11409", closing_price="1.11407")
        output = io.StringIO()
        with redirect_stdout(output): print_report(report)
        self.assertEqual(report["execution_status"], "PARTIAL")
        self.assertEqual(report["overall_status"], "INCOMPLETE")
        self.assertIn("BUY execution: CLOSED_CONFIRMED", output.getvalue())
        self.assertIn("SELL execution: NOT_ATTEMPTED", output.getvalue())
        self.assertIn("no SELL order sent", output.getvalue())
        self.assertIn("Account-wide remaining positions: UNKNOWN", output.getvalue())

    def test_zero_pnl_and_unknown_fees_are_reported_independently(self):
        report = self.report()
        report["account_currency"] = "USD"
        report["roundtrips"]["BUY"].update(closure_verified=True, execution_status="CLOSED_CONFIRMED", realized_profit_loss=0)
        output = io.StringIO()
        with redirect_stdout(output): print_report(report)
        self.assertIn("BUY realized P&L: 0 USD", output.getvalue())
        self.assertIn("fees: unavailable; not confirmed zero", output.getvalue())
        self.assertIn("do not subtract it again", output.getvalue())

    def test_submission_script_does_not_claim_a_fill_or_closure_or_print_arbitrary_body(self):
        import test as entry
        api = Mock()
        api.createPosition.return_value = ({"dealReference": "o_fixture", "private": "secret-marker"}, 200)
        output = io.StringIO()
        with patch("sys.argv", ["test.py"]), patch.object(entry, "create_trading_api", return_value=api), redirect_stdout(output):
            self.assertEqual(entry.main(), 0)
        api.createPosition.assert_called_once_with("EURUSD", "SELL", 100)
        api.closePosition.assert_not_called()
        self.assertIn("Execution confirmation: NOT CHECKED", output.getvalue())
        self.assertIn("Position closure: NOT REQUESTED", output.getvalue())
        self.assertNotIn("secret-marker", output.getvalue())

"""Capital.com BUY/close then SELL/close integration test.

Default: EURUSD, size 100, DEMO orders. --REAL/--real selects LIVE and requires
one terminal confirmation after risk preflight. --read-only disables all orders.
--report-only displays saved results with no authentication or network requests.
Both hedging and normal accounts are supported when entry checks prove the
account is flat; preferences are never changed and each close uses its position ID.
Legacy single-BUY helpers remain available for existing diagnostic callers.
"""

import argparse
import json
import re
import time  # Legacy diagnostic callers patch the shared clock here.

from trading_bot.api.client import TradingApi
from trading_bot.config.trading_config import load_credentials, integration_environment
from trading_bot.risk.live_risk import IntegrationError, require, number, validate_market
from trading_bot.api.demo_transport import DemoTransport, DEMO_HOST
from trading_bot.risk.journal import TradeJournal
from trading_bot.risk.confirmations import identifier, positions, confirm
from trading_bot.risk.legacy_demo import (verify_read_only, verify_same_account,
    verify_owned_position, execute_trade)

from trading_bot.config.paths import ROOT
DEMO_URL = f"https://{DEMO_HOST}"
EPIC = "EURUSD"


def main(argv=None):
    """Run BUY/close then SELL/close. Imports and --help never start trading."""
    from trading_bot.config.trading_config import add_trading_arguments

    parser = argparse.ArgumentParser(description=__doc__)
    add_trading_arguments(parser)
    parser.set_defaults(max_orders=2)
    parser.add_argument("--environment", choices=("demo", "live"), help="Legacy explicit selector")
    parser.add_argument("--read-only", action="store_true", help="Authenticate and preflight without placing orders")
    parser.add_argument("--report-only", action="store_true", help="Display saved results without credentials, network requests or orders")
    parser.add_argument("--execute-demo-trade", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.environment = integration_environment(argv)
    if args.execute_demo_trade and args.environment != "demo":
        parser.error("--execute-demo-trade conflicts with LIVE selection")
    if args.max_orders != 2:
        parser.error("This test has a fixed limit of two opening orders: one BUY and one SELL")
    if not re.fullmatch(r"[A-Z0-9_.-]{1,64}", args.symbol):
        parser.error("symbol must be an instrument epic without URL/path characters")
    for key in ("max_margin", "max_exposure", "max_charges", "max_loss", "stop_distance"):
        value = getattr(args, key)
        if not value.is_finite() or value <= 0:
            parser.error("risk limits and stop distance must be finite and positive")
    from trading_bot.tools.roundtrip import print_report, run_integration
    if args.report_only:
        path = ROOT / (".demo-integration" if args.environment == "demo" else ".live-integration") / "buy-sell-results.json"
        try:
            report = json.loads(path.read_text())
            require(report["environment"] == args.environment.upper(), "saved_report_environment_mismatch")
            print("Saved report only; no network requests. Position data is historical.")
            print_report(report)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IntegrationError):
            print("Saved report unavailable or invalid; no network requests made.")
            return 1
        return 0 if report["passed"] else 1
    return run_integration(args, ROOT, TradingApi, load_credentials)


if __name__ == "__main__":
    raise SystemExit(main())

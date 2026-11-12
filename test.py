"""Order-capable SELL script, unsuitable as a connectivity check. Defaults to DEMO."""

import argparse
from scripts.trading_config import ConfigurationError, add_trading_arguments, create_trading_api
from scripts.network_errors import TradingUnavailable
import re
import sys

# --- Configuration ---
SYMBOL: str = "EURUSD"
HMA_PERIOD: int = 9  # Length of the Hull window
# TIMEFRAME = "MINUTE_30"
TIMEFRAME = "MINUTE_30"


def main() -> int:
    parser = argparse.ArgumentParser()
    add_trading_arguments(parser)
    args = parser.parse_args()
    tradingApi = create_trading_api(args, symbol=args.symbol, size=args.size)
    results, code = tradingApi.createPosition(args.symbol, "SELL", args.size)
    print(f"Environment: {'LIVE' if args.real else 'DEMO'}")
    print(f"SELL order submission: HTTP {code}")
    reference = results.get("dealReference") if isinstance(results, dict) else None
    submitted = code == 200 and isinstance(reference, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", reference)
    if submitted:
        print(f"Order reference: {reference}")
    print("Execution confirmation: NOT CHECKED; submission alone does not confirm a fill")
    print("Position closure: NOT REQUESTED; this script leaves any opened position for manual review")
    print("Realized P&L: NOT RECONCILED; fees: NOT RECONCILED")
    print("Account-wide remaining positions: UNKNOWN (not checked)")
    return 0 if submitted else 1

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (TradingUnavailable, ConfigurationError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None

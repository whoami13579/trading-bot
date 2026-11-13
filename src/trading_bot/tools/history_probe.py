"""Legacy broker-history diagnostic followed by the compatibility quote simulation.

This command authenticates to fetch quotes; it never submits or closes orders.
Use run_hma_backtest with supplied prices for a fully offline simulation.
"""

import argparse

from trading_bot.config.trading_config import (
    add_environment_arguments,
    create_trading_api,
)
from trading_bot.strategies.backtest import (
    HMA_PERIOD,
    SYMBOL as SYMBOL,
    TIME as TIME,
    TestStrategy,
    calculate_hma as calculate_hma,
    calculate_wma as calculate_wma,
    run_hma_backtest,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_environment_arguments(parser)
    args = parser.parse_args(argv)
    api = create_trading_api(args, order_execution=False)
    history = TestStrategy(api)
    result = run_hma_backtest(history.all_prices, HMA_PERIOD)
    print(f"Closed trades: {len(result.trades)}")
    print(
        f"Realized gross P&L (one unit, quote-price units): {result.realized_pnl:.8f}"
    )
    print(f"Unrealized gross P&L (quote-price units): {result.unrealized_pnl:.8f}")
    print(f"Remaining simulated position: {result.open_direction or 'none'}")
    print(
        "Fees, spread between bid/ask, slippage and currency conversion are not modeled."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

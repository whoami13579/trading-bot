"""Compatibility entry point for provider-backed historical-only backtesting.

The former Capital.com-authenticated history-fetch command has been replaced;
--REAL and account arguments are intentionally unsupported for simulation.
"""

from trading_bot.backtesting.cli import main
from trading_bot.strategies.backtest import (
    SYMBOL as SYMBOL,
    TIME as TIME,
    HMA_PERIOD as HMA_PERIOD,
    TestStrategy as TestStrategy,
    run_hma_backtest as run_hma_backtest,
    calculate_hma as calculate_hma,
    calculate_wma as calculate_wma,
)

if __name__ == "__main__":
    raise SystemExit(main())

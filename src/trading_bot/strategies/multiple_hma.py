"""multiple-hma: 4H 13-indicator vote with a 30-minute HMA20 slope trigger.

Online execution is the shared single-position engine
(``trading_bot.execution.single_position``). This module supplies the signal
inputs and the pure decision shared with the backtest (``signals.agreement_target``):
when the long-term vote and the trigger agree, hold that side (opening only when
flat); when they disagree (including a Neutral vote), be flat.

Signal inputs are unchanged by the shared execution: the newest broker candles of
each timeframe, ending with the candle still forming, and a long-term vote that
includes the long-term candle in progress. Completed-candle inputs are issue #11.
"""

from trading_bot.config.trading_config import (
    add_execution_arguments,
    add_trading_arguments,
    check_execution_arguments,
    create_trading_api,
    network_policies,
)
from trading_bot.strategies.indicators import MULTIPLE_HISTORY, calculate_multiple, calculate_hma_result
from trading_bot.strategies import signals
import argparse
from trading_bot.utils.scheduler import SCHEDULE_TIMEFRAMES, wait_until_targets, timeframe_to_minutes

# --- Configuration ---
DEFAULT_SYMBOL: str = "EURUSD"
DEFAULT_HMA_PERIOD: int = signals.MULTIPLE_HMA_HMA_PERIOD  # Length of the Hull window
DEFAULT_QUANTITY: int = 100
DEFAULT_SHORT_TERM = signals.MULTIPLE_HMA_SHORT_TERM
DEFAULT_LONG_TERM = signals.MULTIPLE_HMA_LONG_TERM
DEFAULT_DAYS = 5


def target(value, position):
    """Agreement holds or opens that side; disagreement (or a Neutral vote) is flat."""
    from trading_bot.execution.single_position import FLAT

    long_term, short_term = value
    return signals.agreement_target(long_term, short_term, position) or FLAT


def main() -> None:
    parser = argparse.ArgumentParser()
    add_trading_arguments(parser)
    parser.add_argument("--long_term", type=str)
    parser.add_argument("--hma_period", type=int)
    parser.add_argument("--short_term", type=str)
    parser.add_argument("--days", type=int)
    add_execution_arguments(parser)

    args = parser.parse_args()
    check_execution_arguments(parser, args)
    if args.short_term and args.short_term not in SCHEDULE_TIMEFRAMES:
        parser.error(f"--short_term must be one of {', '.join(SCHEDULE_TIMEFRAMES)}")
    network_policies(args)  # Validated before authentication.
    QUANTITY = args.size
    SYMBOL = args.symbol

    HMA_PERIOD: int = args.hma_period or DEFAULT_HMA_PERIOD
    SHORT_TERM: str = args.short_term or DEFAULT_SHORT_TERM
    DAYS: int = args.days or DEFAULT_DAYS
    LONG_TERM: str = args.long_term or DEFAULT_LONG_TERM

    from trading_bot.execution.single_position import SinglePositionStrategy, run

    def signal(api, boundary):
        # The newest broker candles, including those still forming (issue #11).
        long_term_result = calculate_multiple(api, SYMBOL, LONG_TERM)
        short_term_result = calculate_hma_result(api, SYMBOL, SHORT_TERM, HMA_PERIOD)
        return long_term_result, short_term_result

    strategy = SinglePositionStrategy(
        key="multiple-hma", symbol=SYMBOL, size=QUANTITY,
        schedule=timeframe_to_minutes(SHORT_TERM),
        signal=signal, target=target,
        probes=((LONG_TERM, MULTIPLE_HISTORY), (SHORT_TERM, HMA_PERIOD * 3)),
        days=DAYS,
    )
    # Module-level names are resolved at call time, so they remain patch points.
    run(strategy, args,
        connect=lambda **options: create_trading_api(args, symbol=SYMBOL, size=QUANTITY, **options),
        wait=lambda *targets, **options: wait_until_targets(*targets, **options))


if __name__ == "__main__":
    main()

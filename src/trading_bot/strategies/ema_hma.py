"""ema-hma: EMA 200/20 trend filter with an HMA16 slope trigger on 5-minute ask closes.

Online execution is the shared single-position engine
(``trading_bot.execution.single_position``). This module supplies the signal
inputs and the pure decision shared with the backtest (``signals.agreement_target``):
when trend and trigger agree, hold that side (opening only when flat); when they
disagree, be flat.

Signal input is unchanged by the shared execution: the newest broker candles,
which end with the candle still forming at the boundary. The completed-candle
selection HMA uses is issue #11.
"""

from trading_bot.config.trading_config import (
    add_execution_arguments,
    add_trading_arguments,
    check_execution_arguments,
    create_trading_api,
    network_policies,
)
from trading_bot.strategies.indicators import calculate_hma_result, calculate_two_ema_result
from trading_bot.strategies import signals
from trading_bot.utils.colors import colors
import argparse
from trading_bot.utils.scheduler import SCHEDULE_TIMEFRAMES, wait_until_targets, timeframe_to_minutes

# --- Configuration ---
DEFAULT_SYMBOL: str = "EURUSD"
DEFAULT_HMA_PERIOD: int = signals.EMA_HMA_HMA_PERIOD  # Length of the Hull window
DEFAULT_QUANTITY: int = 100
DEFAULT_DAYS = 7
DEFAULT_LONG_PERIOD = signals.EMA_HMA_LONG_PERIOD
DEFAULT_SHORT_PERIOD = signals.EMA_HMA_SHORT_PERIOD
DEFAULT_TIME_FRAME = signals.EMA_HMA_TIME_FRAME


def target(value, position):
    """Agreement holds or opens that side; disagreement is flat."""
    from trading_bot.execution.single_position import FLAT

    trend, trigger = value
    return signals.agreement_target(trend, trigger, position) or FLAT


def main() -> None:
    parser = argparse.ArgumentParser()
    add_trading_arguments(parser)
    parser.add_argument("--long_period", type=int)
    parser.add_argument("--short_period", type=int)
    parser.add_argument("--hma_period", type=int)
    parser.add_argument("--days", type=int)
    parser.add_argument("--time_frame", type=str)
    add_execution_arguments(parser)

    args = parser.parse_args()
    check_execution_arguments(parser, args)
    if args.time_frame and args.time_frame not in SCHEDULE_TIMEFRAMES:
        parser.error(f"--time_frame must be one of {', '.join(SCHEDULE_TIMEFRAMES)}")
    network_policies(args)  # Validated before authentication.
    QUANTITY = args.size
    SYMBOL = args.symbol

    HMA_PERIOD: int = args.hma_period or DEFAULT_HMA_PERIOD
    DAYS: int = args.days or DEFAULT_DAYS
    LONG_PERIOD: int = args.long_period or DEFAULT_LONG_PERIOD
    SHORT_PERIOD: int = args.short_period or DEFAULT_SHORT_PERIOD
    TIME_FRAME: str = args.time_frame or DEFAULT_TIME_FRAME

    from trading_bot.execution.single_position import SinglePositionStrategy, run

    def signal(api, boundary):
        # The newest broker candles, including the one still forming (issue #11).
        ema_result = calculate_two_ema_result(api, SYMBOL, TIME_FRAME, LONG_PERIOD, SHORT_PERIOD)
        print(f"{colors.YELLOW}ema_result: {ema_result}{colors.ENDC}")
        hma_result = calculate_hma_result(api, SYMBOL, TIME_FRAME, HMA_PERIOD)
        print(f"{colors.YELLOW}hma_result: {hma_result}{colors.ENDC}")
        return ema_result, hma_result

    strategy = SinglePositionStrategy(
        key="ema-hma", symbol=SYMBOL, size=QUANTITY,
        schedule=timeframe_to_minutes(TIME_FRAME),
        signal=signal, target=target,
        probes=((TIME_FRAME, LONG_PERIOD), (TIME_FRAME, HMA_PERIOD * 3)),
        days=DAYS,
    )
    # Module-level names are resolved at call time, so they remain patch points.
    run(strategy, args,
        connect=lambda **options: create_trading_api(args, symbol=SYMBOL, size=QUANTITY, **options),
        wait=lambda *targets, **options: wait_until_targets(*targets, **options))


if __name__ == "__main__":
    main()

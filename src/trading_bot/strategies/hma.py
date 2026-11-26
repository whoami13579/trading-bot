from trading_bot.config.trading_config import (
    add_execution_arguments,
    add_trading_arguments,
    check_execution_arguments,
    create_trading_api,
    network_policies,
)
from trading_bot.strategies import signals
from trading_bot.strategies.indicators import calculate_hma_result
from trading_bot.utils.colors import colors
import argparse
from trading_bot.utils.scheduler import (
    SCHEDULE_TIMEFRAMES,
    timeframe_to_minutes,
    wait_until_targets,
)

# --- Configuration ---
DEFAULT_SYMBOL: str = "EURUSD"
DEFAULT_HMA_PERIOD: int = 16  # Length of the Hull window
DEFAULT_QUANTITY: int = 100
DEFAULT_TIMEFRAME = "MINUTE_30"
DEFAULT_DAYS = 7
DEFAULT_DIRECTION = "BUY"


def main(*, simulation_help: bool = True) -> None:
    """``hma.py``/``trading-bot-hma``: DEMO/LIVE trading, or ``--backtest``.

    ``simulation_help=False`` only hides the ``--backtest`` options from --help;
    parsing and behavior are unchanged.
    """
    parser = argparse.ArgumentParser(allow_abbrev=False)
    add_trading_arguments(parser)
    from trading_bot.backtesting.cli import SIMULATION_OPTIONS, add_arguments, execute
    from trading_bot.backtesting.timeframes import MINUTES
    add_arguments(parser, hidden=not simulation_help)
    parser.add_argument("--hma_period", "--hma-period", type=int)
    parser.add_argument("--time_frame", "--time-frame", choices=MINUTES)
    parser.add_argument("--schedule-time-frame", choices=SCHEDULE_TIMEFRAMES,
                        help="Override scheduling only; candle timeframe stays unchanged")
    parser.add_argument("--days", type=int)
    parser.add_argument("--direction", type=str)
    add_execution_arguments(parser)

    args = parser.parse_args()
    if args.backtest:
        status = execute(args, parser)
        if status:
            raise SystemExit(status)
        return
    import sys
    if any(option.split("=", 1)[0] in SIMULATION_OPTIONS for option in sys.argv[1:]):
        parser.error("Historical data/account assumptions require --backtest")
    if args.time_frame and args.time_frame not in SCHEDULE_TIMEFRAMES and not args.schedule_time_frame:
        parser.error("This candle timeframe requires --schedule-time-frame for online execution")
    check_execution_arguments(parser, args)
    network_policies(args)  # Validated before authentication.
    QUANTITY = args.size
    SYMBOL = args.symbol

    if args.hma_period:
        HMA_PERIOD: int = args.hma_period
    else:
        HMA_PERIOD: int = DEFAULT_HMA_PERIOD

    if args.time_frame:
        TIMEFRAME: str = args.time_frame
    else:
        TIMEFRAME: str = DEFAULT_TIMEFRAME

    if args.days:
        DAYS: str = args.days
    else:
        DAYS: str = DEFAULT_DAYS

    if args.direction:
        DIRECTION: str = args.direction
    else:
        DIRECTION: str = DEFAULT_DIRECTION

    # Online-only import: --backtest must never load broker modules.
    from trading_bot.execution.single_position import FLAT, SinglePositionStrategy, run

    strategy = SinglePositionStrategy(
        key="hma",
        symbol=SYMBOL,
        size=QUANTITY,
        schedule=timeframe_to_minutes(args.schedule_time_frame or TIMEFRAME),
        # Only candles completed by this boundary; None when the expected one is missing.
        signal=lambda api, boundary: calculate_hma_result(api, SYMBOL, TIMEFRAME, HMA_PERIOD, boundary),
        # The configured side while the HMA slope agrees, otherwise flat.
        target=lambda signal, position: signals.configured_side_target(signal, DIRECTION) or FLAT,
        probes=((TIMEFRAME, HMA_PERIOD * 3),),
        days=DAYS,
    )
    # Module-level names are resolved at call time, so they remain patch points.
    run(strategy, args,
        connect=lambda **options: create_trading_api(args, symbol=SYMBOL, size=QUANTITY, **options),
        wait=lambda *targets, **options: wait_until_targets(*targets, **options))


def trade() -> None:
    """``tb-trade --strategy hma``: the same command, without advertising the
    ``--backtest`` options that tb-trade refuses (use tb-backtest instead)."""
    return main(simulation_help=False)


if __name__ == "__main__":
    main()

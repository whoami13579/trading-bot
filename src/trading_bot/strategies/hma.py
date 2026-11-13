from trading_bot.config.trading_config import add_trading_arguments, create_trading_api
from trading_bot.strategies.indicators import calculate_hma_result
from trading_bot.utils.colors import colors
import argparse
from trading_bot.utils.scheduler import SCHEDULE_TIMEFRAMES, wait_until_targets, timeframe_to_minutes

# --- Configuration ---
DEFAULT_SYMBOL: str = "EURUSD"
DEFAULT_HMA_PERIOD: int = 16  # Length of the Hull window
DEFAULT_QUANTITY: int = 100
DEFAULT_TIMEFRAME = "MINUTE_30"
DEFAULT_DAYS = 7
DEFAULT_DIRECTION = "BUY"


def main() -> None:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    add_trading_arguments(parser)
    from trading_bot.backtesting.cli import SIMULATION_OPTIONS, add_arguments, execute
    from trading_bot.backtesting.timeframes import MINUTES
    add_arguments(parser)
    parser.add_argument("--hma_period", "--hma-period", type=int)
    parser.add_argument("--time_frame", "--time-frame", choices=MINUTES)
    parser.add_argument("--schedule-time-frame", choices=SCHEDULE_TIMEFRAMES,
                        help="Override scheduling only; candle timeframe stays unchanged")
    parser.add_argument("--read-only", action="store_true",
                        help="DEMO session/history diagnostics; never calculates signals or places orders")
    parser.add_argument("--cycles", type=int, help="Stop after this many scheduled cycles")
    parser.add_argument("--days", type=int)
    parser.add_argument("--direction", type=str)

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
    if args.cycles is not None and args.cycles <= 0:
        parser.error("--cycles must be positive")
    if args.read_only and args.real:
        parser.error("HMA read-only diagnostics require DEMO; omit --REAL/--real")
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

    # 1. Environment & Auth
    if args.read_only:
        tradingApi = create_trading_api(args, symbol=SYMBOL, size=QUANTITY, order_execution=False)
    else:
        tradingApi = create_trading_api(args, symbol=SYMBOL, size=QUANTITY)

    TIMES = timeframe_to_minutes(args.schedule_time_frame or TIMEFRAME)

    # 3. Execution Loop
    completed_cycles = 0
    while args.cycles is None or completed_cycles < args.cycles:
        try:
            wait_until_targets(TIMES, keepalive=tradingApi.prepare_trading_cycle)
            completed_cycles += 1

            if DAYS == 7:
                pass
            elif DAYS == 5:
                if not tradingApi.is_market_open():
                    tradingApi.wait_until_open()

            renewed = tradingApi.prepare_trading_cycle()
            if args.read_only:
                history, code = tradingApi.getHistoricalPrices(SYMBOL, TIMEFRAME, HMA_PERIOD * 3)
                print(f"DEMO read-only cycle {completed_cycles}: history HTTP {code}; "
                      f"{len(history['prices'])} prices; session {'renewed' if renewed else 'validated'}")
                continue

            hma_result = calculate_hma_result(tradingApi, SYMBOL, TIMEFRAME, HMA_PERIOD)

            if hma_result != DIRECTION:
                all_positions = tradingApi.getAllPositionsList()
                targets = [pos for pos in all_positions if pos.epic == SYMBOL]
                if 0 < len(targets):
                    for target in targets:
                        result, code = tradingApi.closePosition(target.dealId)
                        if code != 200:
                            print(f"failed to close position ({result})")

                    if code == 200:
                        print(f">>> {colors.YELLOW}close position{colors.ENDC}")
                    else:
                        print(f"failed to close position ({result})")
            elif hma_result == DIRECTION:
                all_positions = tradingApi.getAllPositionsList()
                targets = [pos for pos in all_positions if pos.epic == SYMBOL]
                if len(targets) == 0:
                    result, code = tradingApi.createPosition(SYMBOL, hma_result, QUANTITY)

                    if code == 200:
                        color = colors.BLUE if hma_result == "BUY" else colors.RED
                        print(f">>> {color}{hma_result}{colors.ENDC} order submitted")
                    else:
                        print(f"failed to create {hma_result} position ({result})")

        except Exception as e:
            if args.read_only:
                raise SystemExit("DEMO read-only cycle failed; no order was submitted.") from None
            print(f"{colors.WARNING}Error in main loop: {e}{colors.ENDC}")



if __name__ == "__main__":
    main()

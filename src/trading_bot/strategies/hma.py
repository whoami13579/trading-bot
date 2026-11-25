from trading_bot.config.trading_config import (
    add_network_arguments,
    add_trading_arguments,
    create_trading_api,
    network_policies,
)
from trading_bot.strategies.indicators import calculate_hma_result
from trading_bot.utils.colors import colors
import argparse
from trading_bot.utils.scheduler import (
    SCHEDULE_TIMEFRAMES,
    schedule_interval_seconds,
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
    parser.add_argument("--read-only", action="store_true",
                        help="DEMO session/history diagnostics; never calculates signals or places orders")
    parser.add_argument("--cycles", type=int, help="Stop after this many scheduled cycles")
    parser.add_argument("--days", type=int)
    parser.add_argument("--direction", type=str)
    add_network_arguments(parser)

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
    if args.network_retry_max_delay < args.network_retry_delay:
        parser.error("--network-retry-max-delay must be at least --network-retry-delay")
    retry_policy, recovery_policy = network_policies(args)
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

    # Online-only imports: --backtest must never load broker modules.
    from trading_bot.utils.network_recovery import (
        CycleGuard,
        GracefulShutdown,
        NetworkSupervisor,
        PendingOrders,
        ShutdownRequested,
        configure_runtime_logging,
        warn_unconfirmed,
    )
    from trading_bot.config import strategy_defaults

    configure_runtime_logging()
    shutdown = GracefulShutdown()
    # Held here, not by a client, so unconfirmed DEMO orders survive client replacement.
    pending = PendingOrders(SYMBOL, enabled=not args.real,
                            timeout=strategy_defaults.ORDER_CONFIRMATION_TIMEOUT,
                            poll_interval=strategy_defaults.ORDER_CONFIRMATION_POLL_INTERVAL)
    supervisor = NetworkSupervisor(
        recovery_policy,
        environment="live" if args.real else "demo",
        symbol=SYMBOL,
        shutdown=shutdown,
        pending=pending,
    )
    TIMES = timeframe_to_minutes(args.schedule_time_frame or TIMEFRAME)
    guard = CycleGuard(schedule_interval_seconds(TIMES), args.max_signal_delay)

    current = {"api": None}  # The client in use, including after a recovery.
    try:
        with shutdown:
            # 1. Environment & Auth: a transient DEMO startup outage waits before any trade.
            if args.read_only:
                tradingApi = supervisor.start(lambda: create_trading_api(
                    args, symbol=SYMBOL, size=QUANTITY, order_execution=False))
            else:
                tradingApi = supervisor.start(lambda: create_trading_api(
                    args, symbol=SYMBOL, size=QUANTITY))
            current["api"] = tradingApi
            tradingApi.configure_read_retries(retry_policy)
            gate = getattr(tradingApi, "_live_execution", None) if args.real else None
            if gate is not None:
                # The LIVE gate reads account and market state before sending; it rechecks
                # this cycle's deadline immediately before the order itself.
                gate.bind_cycle(lambda: guard.fresh("live_send"))
            _run_cycles(args, current, supervisor, guard, shutdown, pending, TIMES,
                        SYMBOL, TIMEFRAME, HMA_PERIOD, QUANTITY, DAYS, DIRECTION)
    except ShutdownRequested as stop:
        _report_shutdown(current["api"])
        raise SystemExit(stop.exit_code) from None
    except KeyboardInterrupt:
        _report_shutdown(current["api"])
        raise SystemExit(130) from None
    finally:
        # Every exit, including normal --cycles completion and any stop: acknowledged
        # orders still awaiting confirmation must be reconciled before a restart.
        warn_unconfirmed(pending)


def trade() -> None:
    """``tb-trade --strategy hma``: the same command, without advertising the
    ``--backtest`` options that tb-trade refuses (use tb-backtest instead)."""
    return main(simulation_help=False)


def _report_shutdown(api):
    import logging

    uncertain = getattr(api, "_mutation_outcome_uncertain", False) is True
    logging.getLogger("trading_bot.strategies.hma").warning(
        "Shutdown complete: no further signals or orders. Open positions are not closed%s",
        "; an order request was interrupted and its outcome is uncertain: reconcile on "
        "Capital.com before restarting" if uncertain else "",
    )


def _run_cycles(args, current, supervisor, guard, shutdown, pending, TIMES,
                SYMBOL, TIMEFRAME, HMA_PERIOD, QUANTITY, DAYS, DIRECTION):
    from trading_bot.api.network_errors import TradingUnavailable

    tradingApi = current["api"]
    # 3. Execution Loop
    completed_cycles = 0
    while args.cycles is None or completed_cycles < args.cycles:
        try:
            # Pending confirmations are polled (reads only) while waiting, so their deadline
            # does not depend on the candle schedule.
            polling = {"poll": lambda: pending.poll(tradingApi)} if pending.pollable() else {}
            boundary = wait_until_targets(TIMES, keepalive=tradingApi.prepare_trading_cycle, **polling)
            completed_cycles += 1

            if DAYS == 7:
                pass
            elif DAYS == 5:
                if not tradingApi.is_market_open():
                    # Confirmation checks and their deadline stay active over the weekend.
                    # They only read; the boundary that found the market closed is stale
                    # by reopening, so no strategy cycle runs until the next boundary.
                    closed = {"poll": lambda: pending.poll(tradingApi)} if pending.pollable() else {}
                    tradingApi.wait_until_open(**closed)

            # Each boundary runs at most once, and never after it has become stale.
            if not guard.start(boundary):
                continue
            renewed = tradingApi.prepare_trading_cycle()
            if args.read_only:
                history, code = tradingApi.getHistoricalPrices(SYMBOL, TIMEFRAME, HMA_PERIOD * 3)
                print(f"DEMO read-only cycle {completed_cycles}: history HTTP {code}; "
                      f"{len(history['prices'])} prices; session {'renewed' if renewed else 'validated'}")
                continue

            # Positions are only trustworthy once every acknowledged order is reflected.
            if not pending.resolve(tradingApi):
                continue
            if not guard.fresh("signal"):
                continue
            # Only candles completed by this boundary; None when the expected one is missing.
            hma_result = calculate_hma_result(tradingApi, SYMBOL, TIMEFRAME, HMA_PERIOD, guard.current)
            if hma_result is None:
                continue

            if hma_result != DIRECTION:
                all_positions = tradingApi.getAllPositionsList()
                targets = [pos for pos in all_positions if pos.epic == SYMBOL]
                code = None
                for target in targets:
                    # Each close needs the previous one confirmed and a still-current
                    # boundary; otherwise the rest wait for the next cycle untouched.
                    if not pending.resolve(tradingApi, attempts=3) or not guard.fresh("order"):
                        break
                    with shutdown.order_request():
                        result, sent = tradingApi.closePosition(target.dealId)
                        # Recorded before a deferred shutdown can take effect.
                        if sent == 200:
                            pending.record_close(result, deal_id=target.dealId)
                    if sent is None:
                        break  # LIVE gate: the cycle expired during its checks; nothing sent.
                    code = sent
                    if code != 200:
                        print(f"failed to close position ({result})")

                if code == 200:
                    print(f">>> {colors.YELLOW}close position{colors.ENDC}")
                elif code is not None:
                    print(f"failed to close position ({result})")
            elif hma_result == DIRECTION:
                all_positions = tradingApi.getAllPositionsList()
                targets = [pos for pos in all_positions if pos.epic == SYMBOL]
                if len(targets) == 0:
                    if not pending.resolve(tradingApi) or not guard.fresh("order"):
                        continue
                    with shutdown.order_request():
                        result, code = tradingApi.createPosition(SYMBOL, hma_result, QUANTITY)
                        # Recorded before a deferred shutdown can take effect.
                        if code == 200:
                            pending.record_open(result, direction=hma_result, size=QUANTITY)

                    if code is None:
                        continue  # LIVE gate: the cycle expired during its checks; nothing sent.
                    if code == 200:
                        color = colors.BLUE if hma_result == "BUY" else colors.RED
                        print(f">>> {color}{hma_result}{colors.ENDC} order submitted")
                    else:
                        print(f"failed to create {hma_result} position ({result})")

        except TradingUnavailable as unavailable:
            # Pauses and returns a verified replacement client, or re-raises to stop:
            # uncertain orders, authentication/identity failures and LIVE never resume.
            tradingApi = current["api"] = supervisor.recover(unavailable, tradingApi)
        except Exception as e:
            if args.read_only:
                raise SystemExit("DEMO read-only cycle failed; no order was submitted.") from None
            print(f"{colors.WARNING}Error in main loop: {e}{colors.ENDC}")



if __name__ == "__main__":
    main()

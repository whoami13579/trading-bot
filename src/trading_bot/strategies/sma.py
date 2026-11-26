"""sma: SMA 9 / SMA 21 crossover of one-minute ask opens.

Online execution is the shared single-position engine
(``trading_bot.execution.single_position``). This module supplies the signal input
(the newest 21 broker candles' ask opens, unchanged) and the pure decision shared
with the backtest (``signals.reversal_target`` over ``signals.sma_side``).

The decision is stop-and-reverse: BUY while SMA(fast) is above SMA(slow), SELL
while below. The shared engine never sends an opposite order over an open
position, so the first reversal stops trading with the owned position left open
until issue #10 adds a confirmed close-then-open.
"""

import argparse

from trading_bot.config.trading_config import (
    add_execution_arguments,
    add_trading_arguments,
    check_execution_arguments,
    create_trading_api,
    network_policies,
)
from trading_bot.strategies.indicators import calculate_sma
from trading_bot.strategies.signals import (
    POLLING_TIME_FRAME,
    SMA_FAST_PERIOD,
    SMA_SLOW_PERIOD,
    reversal_target,
    sma_side,
)
from trading_bot.utils.scheduler import timeframe_to_minutes, wait_until_targets

# Configuration Constants
SYMBOL = "EURUSD"
FAST_PERIOD = SMA_FAST_PERIOD
SLOW_PERIOD = SMA_SLOW_PERIOD
QUANTITY = 100


def target(prices, position):
    """The backtest rule: the side of SMA(fast) against SMA(slow); equal values hold."""
    from trading_bot.execution.single_position import HOLD

    return reversal_target(sma_side(prices, FAST_PERIOD, SLOW_PERIOD), position) or HOLD


def main():
    parser = argparse.ArgumentParser()
    add_trading_arguments(parser)
    add_execution_arguments(parser)
    args = parser.parse_args()
    check_execution_arguments(parser, args)
    network_policies(args)  # Validated before authentication.
    symbol, size = args.symbol, args.size

    from trading_bot.execution.single_position import SinglePositionStrategy, run
    from trading_bot.utils.candles import newest_opens

    def signal(api, boundary):
        history, _ = api.getHistoricalPrices(symbol, POLLING_TIME_FRAME, SLOW_PERIOD)
        prices = newest_opens(history, SLOW_PERIOD)
        if prices is not None:
            print(f"Price: {prices[-1]:.5f} | Fast: {calculate_sma(prices, FAST_PERIOD):.5f} | "
                  f"Slow: {calculate_sma(prices, SLOW_PERIOD):.5f}")
        return prices

    strategy = SinglePositionStrategy(
        key="sma", symbol=symbol, size=size,
        schedule=timeframe_to_minutes(POLLING_TIME_FRAME),
        signal=signal, target=target,
        probes=((POLLING_TIME_FRAME, SLOW_PERIOD),),
    )
    # Module-level names are resolved at call time, so they remain patch points.
    run(strategy, args,
        connect=lambda **options: create_trading_api(args, symbol=symbol, size=size, **options),
        wait=lambda *targets, **options: wait_until_targets(*targets, **options))


if __name__ == "__main__":
    main()

"""moving-average: the newest one-minute ask open against the mean of the last 20.

Online execution is the shared single-position engine
(``trading_bot.execution.single_position``). This module supplies the signal input
(the newest broker candles' ask opens, unchanged), the startup rule and the pure
decision shared with the backtest (``signals.reversal_target``).

The decision is stop-and-reverse: BUY above the mean, SELL below it. The shared
engine never sends an opposite order over an open position, so the first reversal
stops trading with the owned position left open until issue #10 adds a confirmed
close-then-open.
"""

import argparse

from trading_bot.config.trading_config import (
    add_execution_arguments,
    add_trading_arguments,
    check_execution_arguments,
    create_trading_api,
    network_policies,
)
from trading_bot.strategies.signals import (
    MOVING_AVERAGE_PERIOD,
    POLLING_TIME_FRAME,
    moving_average_side,
    price_average,
    reversal_target,
)
from trading_bot.utils.scheduler import timeframe_to_minutes, wait_until_targets

SYMBOL = "EURUSD"
QUANTITY = 100


class StartupCross:
    """The startup wait of the original loop, unchanged.

    No decision is made until the newest open has crossed the window mean once (from
    below to at or above it, or from above to at or below it); trading starts with
    the window after the crossing one. The backtest does not model this wait.
    """

    def __init__(self):
        self.below = None  # Side of the mean at the first window.
        self.armed = False

    def ready(self, prices) -> bool:
        if self.armed:
            return True
        newest, mean = prices[-1], price_average(prices)
        if self.below is None:
            self.below = newest < mean
        if not (newest < mean if self.below else newest > mean):
            self.armed = True  # Crossed: this window is consumed, the next one decides.
        return False


def target(prices, position):
    """The backtest rule: the side of the newest open; equal values hold."""
    from trading_bot.execution.single_position import HOLD

    return reversal_target(moving_average_side(prices), position) or HOLD


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

    startup = StartupCross()

    def signal(api, boundary):
        history, _ = api.getHistoricalPrices(symbol, POLLING_TIME_FRAME, MOVING_AVERAGE_PERIOD)
        prices = newest_opens(history, MOVING_AVERAGE_PERIOD)
        if prices is None or not startup.ready(prices):
            return None
        return prices

    strategy = SinglePositionStrategy(
        key="moving-average", symbol=symbol, size=size,
        schedule=timeframe_to_minutes(POLLING_TIME_FRAME),
        signal=signal, target=target,
        probes=((POLLING_TIME_FRAME, MOVING_AVERAGE_PERIOD),),
    )
    # Module-level names are resolved at call time, so they remain patch points.
    run(strategy, args,
        connect=lambda **options: create_trading_api(args, symbol=symbol, size=size, **options),
        wait=lambda *targets, **options: wait_until_targets(*targets, **options))


if __name__ == "__main__":
    main()

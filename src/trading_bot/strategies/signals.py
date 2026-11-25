"""Pure signal and position rules shared by the online strategies and their backtests.

No broker, account, network or file access. The online strategy modules import
their default periods and timeframes from here, so a backtest's defaults are
the online defaults by construction. Every function takes plain price lists
(oldest first) and returns "BUY", "SELL" or None (or "Neutral" where the
online rule has one).
"""

from trading_bot.strategies.indicators import calculate_sma

# moving_average.py: the ask open of the last 20 one-minute candles.
MOVING_AVERAGE_PERIOD = 20
# sma.py: SMA 9 / SMA 21 over the ask opens of one-minute candles.
SMA_FAST_PERIOD = 9
SMA_SLOW_PERIOD = 21
# moving_average.py and sma.py poll one-minute candles every 60 seconds.
POLLING_TIME_FRAME = "MINUTE"
# ema_hma.py: EMA 200 / EMA 20 trend and HMA16 slope on 5-minute ask closes.
EMA_HMA_LONG_PERIOD = 200
EMA_HMA_SHORT_PERIOD = 20
EMA_HMA_HMA_PERIOD = 16
EMA_HMA_TIME_FRAME = "MINUTE_5"
# multiple_hma.py: HMA20 slope on 30-minute closes and the 4H multiple vote.
MULTIPLE_HMA_HMA_PERIOD = 20
MULTIPLE_HMA_SHORT_TERM = "MINUTE_30"
MULTIPLE_HMA_LONG_TERM = "HOUR_4"


def price_average(prices: list) -> float:
    """Arithmetic mean of the whole list (moving_average.py's rolling window)."""
    return sum(prices) / len(prices)


def price_side(value, reference):
    """BUY when value is above reference, SELL when below, None when equal."""
    if value > reference:
        return "BUY"
    if value < reference:
        return "SELL"
    return None


def moving_average_side(prices: list) -> str | None:
    """moving_average.py: the newest price against the mean of the window."""
    return price_side(prices[-1], price_average(prices))


def sma_side(prices: list, fast_period: int, slow_period: int) -> str | None:
    """sma.py: SMA(fast) against SMA(slow) over the newest prices."""
    return price_side(calculate_sma(prices, fast_period), calculate_sma(prices, slow_period))


def reversal_target(side: str | None, position: str | None) -> str | None:
    """moving-average and sma: a side signal moves to that side; no signal holds.

    The online loops send the opposite order when the tracked side changes and
    never send one while the side is unchanged or the values are equal.
    """
    return side or position


def agreement_target(trend: str, trigger: str, position: str | None) -> str | None:
    """ema-hma and multiple-hma: disagreement closes the position; agreement opens
    the agreed side only when flat. An existing position is held while they agree,
    even if it is on the other side (the online loops only open when no position
    exists and only close when the signals disagree)."""
    if trend != trigger:
        return None
    return trigger if position is None else position

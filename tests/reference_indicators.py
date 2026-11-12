"""Independent trailing-window reference calculations; no project imports or I/O."""

from math import isqrt
from statistics import fmean


def wma(values, period):
    window = values[-period:]
    return sum((index + 1) * value for index, value in enumerate(window)) / (period * (period + 1) / 2)


def ema(values, period):
    value = fmean(values[:period])
    alpha = 2 / (period + 1)
    for price in values[period:]:
        value = alpha * price + (1 - alpha) * value
    return value


def hma_tail(values, period):
    raw = [2 * wma(values[:end], period // 2) - wma(values[:end], period)
           for end in range(period, len(values) + 1)]
    return [wma(raw[:end], isqrt(period)) for end in (len(raw) - 1, len(raw))]


def multiple_signal(values):
    votes = []
    for period in (10, 20, 30, 50, 100, 200):
        votes.append(1 if values[-1] >= fmean(values[-period:]) else -1)
        votes.append(1 if values[-1] >= ema(values, period) else -1)
    previous, current = hma_tail(values, 27)
    score = sum(votes) + (1 if current > previous else -1)
    return "BUY" if score > 1 else "SELL" if score < -1 else "Neutral"

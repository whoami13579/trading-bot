"""Compatibility import for trading_bot.risk.live_risk."""

from importlib import import_module
import sys

_module = import_module("trading_bot.risk.live_risk")
sys.modules[__name__] = _module

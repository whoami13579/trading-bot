"""Compatibility import for trading_bot.risk.strategy_costs."""

from importlib import import_module
import sys

_module = import_module("trading_bot.risk.strategy_costs")
sys.modules[__name__] = _module

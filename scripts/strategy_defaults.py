"""Compatibility import for trading_bot.config.strategy_defaults."""

from importlib import import_module
import sys

_module = import_module("trading_bot.config.strategy_defaults")
sys.modules[__name__] = _module

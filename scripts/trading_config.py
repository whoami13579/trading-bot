"""Compatibility import for trading_bot.config.trading_config."""

from importlib import import_module
import sys

_module = import_module("trading_bot.config.trading_config")
sys.modules[__name__] = _module

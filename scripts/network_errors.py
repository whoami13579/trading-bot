"""Compatibility import for trading_bot.api.network_errors."""

from importlib import import_module
import sys

_module = import_module("trading_bot.api.network_errors")
sys.modules[__name__] = _module

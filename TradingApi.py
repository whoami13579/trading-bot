"""Compatibility import for trading_bot.api.client."""

from importlib import import_module
import sys

_module = import_module("trading_bot.api.client")
sys.modules[__name__] = _module

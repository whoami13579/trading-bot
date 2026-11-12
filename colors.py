"""Compatibility import for trading_bot.utils.colors."""

from importlib import import_module
import sys

_module = import_module("trading_bot.utils.colors")
sys.modules[__name__] = _module

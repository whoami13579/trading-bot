"""Compatibility import for trading_bot.utils.scheduler."""

from importlib import import_module
import sys

_module = import_module("trading_bot.utils.scheduler")
sys.modules[__name__] = _module

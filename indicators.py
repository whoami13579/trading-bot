"""Compatibility import for trading_bot.strategies.indicators."""

from importlib import import_module
import sys

_module = import_module("trading_bot.strategies.indicators")
sys.modules[__name__] = _module

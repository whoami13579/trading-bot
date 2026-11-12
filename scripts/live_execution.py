"""Compatibility import for trading_bot.risk.live_execution."""

from importlib import import_module
import sys

_module = import_module("trading_bot.risk.live_execution")
sys.modules[__name__] = _module

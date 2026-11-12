"""Compatibility import for trading_bot.tools.roundtrip."""

from importlib import import_module
import sys

_module = import_module("trading_bot.tools.roundtrip")
sys.modules[__name__] = _module

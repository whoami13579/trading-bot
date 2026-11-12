"""Compatibility import for trading_bot.risk.trade_reconciliation."""

from importlib import import_module
import sys

_module = import_module("trading_bot.risk.trade_reconciliation")
sys.modules[__name__] = _module

"""Compatibility import for trading_bot.strategies.ema_hma."""

from importlib import import_module
import sys

_module = import_module("trading_bot.strategies.ema_hma")
if __name__ == "__main__":
    raise SystemExit(_module.main())
else:
    sys.modules[__name__] = _module

"""Compatibility import for trading_bot.tools.live_preflight."""

from importlib import import_module
import sys

_module = import_module("trading_bot.tools.live_preflight")
if __name__ == "__main__":
    raise SystemExit(_module.main())
else:
    sys.modules[__name__] = _module

"""Compatibility import for trading_bot.tools.demo_roundtrip."""

from importlib import import_module
import sys

_module = import_module("trading_bot.tools.demo_roundtrip")
if __name__ == "__main__":
    raise SystemExit(_module.main())
else:
    sys.modules[__name__] = _module

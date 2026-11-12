"""Executable wrapper; install the package with uv sync first."""

from trading_bot.tools.demo_roundtrip import main

if __name__ == "__main__":
    raise SystemExit(main())

"""Executable wrapper; install the package with uv sync first."""

from trading_bot.tools.live_preflight import main

if __name__ == "__main__":
    raise SystemExit(main())

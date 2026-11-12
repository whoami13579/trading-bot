"""Executable wrapper; install the package with uv sync first."""

from trading_bot.tools.backtest import main

if __name__ == "__main__":
    raise SystemExit(main())

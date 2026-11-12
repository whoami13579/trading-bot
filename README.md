# Trading bot

Capital.com trading strategies, guarded DEMO/LIVE integration tools, and an
offline HMA backtester. Python **3.12** and the existing **uv** lock workflow are
used throughout. DEMO is the default; LIVE always requires explicit selection.

**`test.py` and the roundtrip tools can submit orders. They are not offline tests.**
No account connection is needed for installation, automated tests, or CLI help.

## Quick start: offline

```bash
uv sync --locked
uv pip check --python .venv/bin/python
uv run --locked scripts/check_environment.py
uv run --locked scripts/check_offline.py
uv run --locked scripts/demo_roundtrip.py --help
```

The offline runner blocks socket connections, HTTP sends, and credential loading.
`uv --offline` alone controls package downloads; it does not block application
network access. Keep `.env` and integration state private and ignored.

## Structure

```text
src/trading_bot/
  api/          Capital.com client, models, network/auth diagnostics and DEMO transport
  strategies/   SMA, HMA, EMA/HMA, multiple HMA, moving average, indicators, backtest
  risk/         LIVE execution controls, costs, journals, confirmations, reconciliation
  config/       Credential parsing, endpoint selection, strategy defaults, fixed paths
  utils/        Scheduler, terminal colors, private report writes
  tools/        Packaged CLI implementations
scripts/        Executable wrappers and legacy module aliases
tests/          Offline tests and public price fixtures
docs/           Setup, configuration, safety, integration procedures and migration notes
test.py         Original SELL integration entry point, preserved byte-for-byte
```

Legacy root modules remain as small compatibility aliases to the package; they
contain no duplicate strategy/API implementation. `main.py` remains the moving
average strategy entry point. `test_strategy.py` now delegates to the read-only
historical-data backtest command. The fee template remains in the repository root.

## Common commands and imports

| Purpose | Preferred command | Legacy command |
| --- | --- | --- |
| Offline suite | `uv run scripts/check_offline.py` | `uv run scripts/test_offline.py` |
| CLI help | `uv run scripts/demo_roundtrip.py --help` | `uv run test_api_key.py --help` |
| DEMO read-only preflight | `uv run scripts/demo_roundtrip.py --read-only` | `uv run test_api_key.py --read-only` |
| LIVE read-only preflight | `uv run scripts/live_preflight.py --REAL` | `uv run test_live_api.py --REAL` |
| Historical simulation with read-only DEMO fetch | `uv run scripts/backtest.py` | `uv run test_strategy.py` |

The account-access commands above are documentation, not part of offline validation.
Installed console commands such as `uv run trading-bot-hma --help`,
`uv run trading-bot-demo-roundtrip --help`, and `uv run trading-bot-backtest --help`
work from an installed environment without checkout-relative import tricks.
Arguments such as `--REAL`/`--real`, `--account`, and existing risk limits retain
their prior meanings. A roundtrip without `--read-only` is order-capable by default.

```python
from trading_bot.api.client import TradingApi
from trading_bot.strategies.indicators import calculate_hma
from trading_bot.strategies.backtest import run_hma_backtest

# Supplied prices: pure simulation, no authentication or network calls.
result = run_hma_backtest([1 + i / 1000 for i in range(100)], period=20)
```

## Documentation

- [Setup and dependencies](docs/setup.md)
- [Configuration and environment selection](docs/configuration.md)
- [Trading safeguards and execution commands](docs/trading-safety.md)
- [Offline tests, read-only checks and order-capable integration tools](docs/integration-testing.md)
- [Migration, compatibility and backtest corrections](docs/refactoring.md)

Review the safety and integration documents before any authenticated command.


## Complete API client

See [API usage and safe DEMO examples](docs/api-usage.md) and the
[official-operation coverage matrix](docs/api-coverage.md).
The client covers 36 REST operations and five streaming destinations. New LIVE
mutation categories remain blocked pending dedicated risk policies; existing
LIVE open/close controls are preserved. `test.py` remains unchanged.

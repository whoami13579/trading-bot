# Trading bot

Capital.com trading strategies, guarded DEMO/LIVE integration tools, and a
unified multi-strategy historical backtester covering every strategy. Python
**3.12** and the existing **uv** lock workflow are used throughout.

Strategies run in three separate ways:

| Mode | Command | Broker access | Orders |
| --- | --- | --- | --- |
| Historical backtesting | `uv run tb-backtest --strategy NAME` | None; public market data or a local snapshot | Simulated only |
| DEMO trading (default) | `uv run tb-trade --strategy NAME` | Capital.com DEMO | **Real DEMO orders** |
| LIVE trading | `uv run tb-trade --strategy NAME --REAL` | Capital.com LIVE | **Real-money orders**, behind the LIVE gate |

DEMO is the default; LIVE always requires explicit `--REAL` or `--real`.
**`tb-trade`, `test.py` and the roundtrip tools can submit orders. They are not
offline tests.** No account connection is needed for installation, automated
tests, `--help` or `--list-strategies`.

## Quick start: offline

```bash
uv sync --locked
uv pip check --python .venv/bin/python
uv run --locked tb-check-environment
uv run --locked tb-check-offline
uv run tb-trade --list-strategies
uv run tb-backtest --list-strategies
uv run tb-trade --strategy hma --help
```

The offline runner blocks socket connections, HTTP sends, and credential loading.
`uv --offline` alone controls package downloads; it does not block application
network access. Keep `.env` and integration state private and ignored.

## Strategies

| Strategy | DEMO | LIVE | Backtesting |
| --- | --- | --- | --- |
| `moving-average` | Yes | Yes | Yes |
| `sma` | Yes | Yes | Yes |
| `hma` | Yes | Yes | Yes |
| `ema-hma` | Yes | Yes | Yes |
| `multiple-hma` | Yes | Yes | Yes |
| `donchian` | No | No | Yes |
| `grid` | No | No | Yes |

Every strategy can be backtested. Donchian and Grid are historical simulations
only; `tb-trade` rejects them before any broker access. DEMO/LIVE support for them
is planned in #12 and #13 and is not implemented.

The backtests reuse each online strategy's **signal rule** and defaults, but not
necessarily its online candle timing, order execution or LIVE limits:

- moving-average and sma simulate stop-and-reverse, while online the first
  reversal stops trading with the owned position open (no opposite order is
  sent; confirmed reversals are #10);
- ema-hma and multiple-hma use completed candles, while online they read the
  forming candle (#11).

Every affected report states these differences. All five order-capable
strategies share one online lifecycle, the
[shared single-position execution](docs/shared-execution.md): candle-boundary
scheduling, stale-signal protection, DEMO order confirmation, broker-confirmed
ownership (only positions the process opened are closed), DEMO network
pause/resume, graceful shutdown and the LIVE gate. Offline tests are not a DEMO
validation: see the [DEMO validation plan](docs/demo-validation-plan.md). See
[strategy backtesting](docs/strategy-backtesting.md) and the
[safety comparison](docs/trading-safety.md#safety-capabilities-by-strategy). Each strategy is implemented once under
`src/trading_bot/strategies/` and declared once in
`src/trading_bot/strategies/registry.py`. The [CLI guide](docs/cli.md) covers
every option and default, which commands can place orders, the registry design,
migration from the older commands, and how to add a strategy without a new
wrapper script.

```bash
uv run tb-trade --strategy hma                       # DEMO
uv run tb-trade --strategy hma --REAL                # LIVE, real money
uv run tb-trade --strategy hma --symbol EURUSD --size 100 --time-frame MINUTE_5
uv run tb-backtest --strategy grid --grid-lookback 100 --grid-intervals 20
```

`tb-trade` passes every other argument, unchanged, to the selected strategy's
existing parser. It then runs that strategy's existing authentication, LIVE
preflight, risk limits and network recovery, and refuses backtesting options.
`tb-backtest` refuses `--REAL`, account, risk-limit and scheduling options.

## Structure

```text
src/trading_bot/
  api/          Capital.com client, models, network/auth diagnostics and DEMO transport
  backtesting/  Shared historical data, engine, cost models, strategy adapters,
                evaluation safeguards, reports and the unified backtest CLI
  strategies/   Moving average, SMA, HMA, EMA/HMA, multiple HMA, Donchian, Grid,
                indicators, legacy backtest helpers and the strategy registry
  risk/         LIVE execution controls, costs, journals, confirmations, reconciliation
  config/       Credential parsing, endpoint selection, strategy defaults, fixed paths
  utils/        Scheduler, terminal colors, network recovery, private report writes
  tools/        Packaged CLI implementations (tb-trade, tb-backtest, diagnostics)
scripts/        Executable wrappers and legacy module aliases
tests/          Offline tests and public price fixtures
docs/           CLI, setup, configuration, safety, integration and migration notes
test.py         Original SELL integration entry point, preserved byte-for-byte
```

Legacy root modules remain as small compatibility aliases to the package; they
contain no duplicate strategy/API implementation. `main.py` remains the moving
average strategy entry point. `test_strategy.py` retains the legacy authenticated,
read-only broker-history diagnostic. Historical backtesting uses independent market
data providers and never loads Capital.com credentials. The fee template remains
in the repository root.

## Common commands and imports

| Purpose | Preferred command | Backward-compatible commands |
| --- | --- | --- |
| Offline suite | `uv run tb-check-offline` | `uv run scripts/check_offline.py`, `trading-bot-check-offline`, `uv run scripts/test_offline.py` |
| Environment check | `uv run tb-check-environment` | `uv run scripts/check_environment.py`, `trading-bot-check-environment` |
| Strategy list / help | `uv run tb-trade --list-strategies`, `uv run tb-trade --strategy hma --help` | `uv run trading-bot-hma --help` |
| DEMO strategy | `uv run tb-trade --strategy hma` | `uv run trading-bot-hma`, `uv run hma.py` |
| LIVE strategy | `uv run tb-trade --strategy hma --REAL` | `uv run trading-bot-hma --REAL`, `uv run hma.py --REAL` |
| DEMO read-only strategy diagnostics (any order-capable strategy) | `uv run tb-trade --strategy hma --read-only --cycles 1` | `uv run hma.py --read-only --cycles 1` |
| DEMO read-only preflight | `uv run tb-demo-roundtrip --read-only` | `uv run scripts/demo_roundtrip.py --read-only`, `uv run test_api_key.py --read-only` |
| LIVE read-only preflight | `uv run tb-live-preflight --REAL` | `uv run scripts/live_preflight.py --REAL`, `uv run test_live_api.py --REAL` |
| Historical simulation, no broker access | `uv run tb-backtest --strategy NAME` (any strategy) | `trading-bot-backtest`, `uv run hma.py --backtest`, `uv run src/trading_bot/strategies/donchian.py --backtest`, `uv run scripts/backtest.py` |
| Legacy read-only broker-history diagnostic | `uv run test_strategy.py` | — |

The account-access commands above are documentation, not part of offline validation.
Every `tb-*` and `trading-bot-*` console command works from an installed
environment without checkout-relative import tricks; all legacy commands keep
their arguments and behavior (see the [migration table](docs/cli.md#backward-compatible-commands-and-migration)).
Arguments such as `--REAL`/`--real`, `--account`, and existing risk limits retain
their prior meanings. A roundtrip without `--read-only` is order-capable by default.

```python
from trading_bot.api.client import TradingApi
from trading_bot.strategies.indicators import calculate_hma
from trading_bot.strategies.backtest import run_hma_backtest

# Supplied prices: pure simulation, no authentication or network calls.
result = run_hma_backtest([1 + i / 1000 for i in range(100)], period=20)
```

## Historical backtesting

One entry point, `tb-backtest` (also installed under its original name
`trading-bot-backtest`), runs every supported strategy on the same data, engine,
cost models and report:

```bash
uv run tb-backtest --list-strategies
uv run tb-backtest --strategy hma --symbol EURUSD --time-frame HOUR --size 100
uv run tb-backtest --strategy sma --start 2025-09-01 --end 2025-10-01 --slippage-pips 0.1
uv run tb-backtest --strategy ema-hma --long-period 200 --short-period 20 --hma-period 16
uv run tb-backtest --strategy multiple-hma --time-frame MINUTE_30 --long-term HOUR_4
uv run tb-backtest --strategy donchian --symbol EURUSD --time-frame HOUR --size 100
uv run tb-backtest --strategy donchian --cost-case high
uv run tb-backtest --strategy hma --end 2026-11-12 --slippage-pips 0.2
uv run tb-backtest --strategy grid --symbol EURUSD --time-frame HOUR \
  --start 2025-09-01 --end 2025-10-01 --ema-period 50 \
  --grid-lower 1.1500 --grid-upper 1.1900 --grid-intervals 40 \
  --level-size 100 --max-position 500 --slippage-pips 0.1
# Automatic bounds: lowest low / highest high of the 100 1H candles before --start.
uv run tb-backtest --strategy grid --symbol EURUSD --time-frame HOUR \
  --start 2025-09-01 --end 2025-10-01 --ema-period 50 --grid-lookback 100 \
  --grid-intervals 40 --level-size 100 --max-position 500 --slippage-pips 0.1
```

The grid strategy picks a long or short fixed-price grid from an EMA of completed
closes and simulates many working limit orders with per-lot accounting. Its
direction rule, fill and cost assumptions are in
[grid backtesting](docs/grid-backtesting.md).

Omitted options use each strategy's preserved defaults: HMA uses Dukascopy,
30-minute candles, the last 30 complete UTC days and continuous execution;
Donchian uses the verified study snapshot, 1H candles, 2023-11-01 to 2026-11-01
and the research window with base costs. The existing commands keep working with
their previous defaults and numbers:

```bash
uv run src/trading_bot/strategies/donchian.py --backtest
uv run hma.py --backtest
uv run hma.py --backtest --time-frame MINUTE_5
uv run hma.py --backtest --data-source yfinance
uv run hma.py --backtest --initial-balance 5000 --size 200
uv run hma.py --backtest --data-source dukascopy --lookback-days 30 \
  --initial-balance 1000 --size 100 --time-frame MINUTE_30 --symbol EURUSD
```

Defaults are Dukascopy, the last 30 complete UTC calendar days, $1,000, 100 EUR
base units, EURUSD, 30-minute candles and HMA period 16 in BUY/flat mode. Both
`--time-frame` and `--time_frame` work. Use `--direction SELL` for the existing
SELL/flat mode and `--end YYYY-MM-DD` for a fixed historical period.

Signals share the live HMA implementation, use completed candles and fill at the
next observed open. Dukascopy provides historical bid/ask candles; Yahoo provides
indicative OHLC with unknown spread. Balance is initial cash plus realized net
cash P&L; equity adds unrealized P&L. Size is exposure, not dollars spent. The
default simulated collateral is 1:1; margin reserves funds without spending cash.
Commission, financing and slippage remain explicitly **unmodeled** unless configured.

Each run saves JSON, trade/equity CSVs and an interactive `equity.html` in ignored
`backtests/`; public price caches live in ignored `.cache/backtests/`. Backtest mode
rejects `--REAL` and account/risk/scheduling options. Without `--backtest`, `hma.py`
retains DEMO trading and `hma.py --REAL` retains guarded LIVE trading;
`tb-trade --strategy hma` is the preferred spelling of both.

See [backtesting architecture, parameters, provider limits and verified results](docs/backtesting.md)
and [the unified framework, compatibility notes and strategy integration guide](docs/backtesting-framework.md).

## Documentation

- [Command-line interface, strategy registry and migration](docs/cli.md)
- [Backtesting the order-capable strategies](docs/strategy-backtesting.md)
- [Setup and dependencies](docs/setup.md)
- [Historical backtesting](docs/backtesting.md)
- [Unified backtesting framework](docs/backtesting-framework.md)
- [Fixed-grid backtesting](docs/grid-backtesting.md)
- [Configuration and environment selection](docs/configuration.md)
- [Trading safeguards and execution commands](docs/trading-safety.md)
- [Network retries, pause and recovery](docs/network-recovery.md)
- [Offline tests, read-only checks and order-capable integration tools](docs/integration-testing.md)
- [Migration, compatibility and backtest corrections](docs/refactoring.md)

Review the safety and integration documents before any authenticated command.


## Complete API client

See [API usage and safe DEMO examples](docs/api-usage.md) and the
[official-operation coverage matrix](docs/api-coverage.md).
The client covers 36 REST operations and five streaming destinations. New LIVE
mutation categories remain blocked pending dedicated risk policies; existing
LIVE open/close controls are preserved. `test.py` remains unchanged.

For scheduler-related HTTP 401 failures, see [session renewal and HMA read-only diagnostics](docs/session-lifecycle.md).
For DNS/connection outages during scheduled trading, see [network recovery](docs/network-recovery.md).

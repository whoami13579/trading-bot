# Command-line interface and strategy registry

Two commands cover strategy use:

- `tb-trade` runs order-capable strategies against Capital.com: **DEMO by
  default**, LIVE only with `--REAL` or `--real`.
- `tb-backtest` runs historical simulations: no broker access, no credentials,
  no orders.

Both select a strategy with `--strategy`. Strategy code lives only in
`src/trading_bot/strategies/`. Both commands read one registry
(`src/trading_bot/strategies/registry.py`) for which strategy supports which
mode.

## Strategy capability matrix

| Strategy | DEMO | LIVE | Backtesting | Description |
| --- | --- | --- | --- | --- |
| `moving-average` | Yes | Yes | Yes | Price vs. 20-candle moving average (1-minute) |
| `sma` | Yes | Yes | Yes | SMA 9/21 crossover (1-minute) |
| `hma` | Yes | Yes | Yes | Hull moving average, configured side / flat |
| `ema-hma` | Yes | Yes | Yes | EMA 200/20 trend filter with HMA confirmation |
| `multiple-hma` | Yes | Yes | Yes | 4H 13-indicator vote with 30-minute HMA confirmation |
| `donchian` | No | No | Yes | Donchian channel breakout (historical simulation only) |
| `grid` | No | No | Yes | Candle-directed fixed grid (historical simulation only) |

`uv run tb-trade --list-strategies` prints this matrix from the registry, and
`uv run tb-backtest --list-strategies` prints each backtest's defaults. Neither
command needs credentials or network access.

Donchian and Grid are historical simulations only. `tb-trade` rejects them in
every mode, before importing any strategy or broker code. DEMO and guarded LIVE
trading for them is planned, not implemented: Donchian in #12, Grid in #13. A
mode becomes available only when it is implemented, tested and validated on DEMO,
never through a registry flag alone.

A backtest reproduces each strategy's **signal rule**, not necessarily its online
candle timing, order execution or LIVE limits. Every backtest report of an
order-capable strategy lists the differences (`online_differences`); details are in
[strategy backtesting](strategy-backtesting.md#what-equivalent-means-here). Online,
every order-capable strategy runs the
[shared single-position execution](shared-execution.md); their signal inputs and
decisions still differ
([comparison](trading-safety.md#safety-capabilities-by-strategy)).

## Command reference

### Preferred `tb-*` commands

| Command | Purpose | Account access / orders |
| --- | --- | --- |
| `tb-trade --strategy NAME` | Unified DEMO/LIVE strategy runner | **Order-capable** (DEMO by default) |
| `tb-trade --strategy NAME --REAL` | Same strategy on LIVE | **Real-money orders**, behind the LIVE gate |
| `tb-trade --strategy NAME --read-only` | DEMO session/history diagnostics of any order-capable strategy | DEMO reads, no orders (the client refuses mutations) |
| `tb-backtest --strategy NAME` | Historical simulation of any registered strategy | None |
| `tb-check-offline` | Full offline test suite under network/credential guards | None |
| `tb-check-environment` | Offline environment, dependency and help-path checks | None |
| `tb-demo-readonly` | Explicit DEMO connection checks | DEMO reads, no orders |
| `tb-diagnose-demo-auth` | DEMO authentication diagnostics (offline unless explicitly approved) | Optional single DEMO request |
| `tb-demo-roundtrip` | DEMO BUY/close then SELL/close integration test | **Order-capable** unless `--read-only` |
| `tb-live-preflight` | LIVE read-only preflight (`--REAL`) | LIVE reads; order test needs extra flags and typed confirmation |
| `tb-ping` | Repeating session keep-alive/ping service | Authenticated reads, no orders |

Run each with `uv run` from the checkout, for example `uv run tb-trade --help`.

### Safe commands (no credentials, no network)

```bash
uv run tb-trade --help
uv run tb-trade --list-strategies
uv run tb-trade --strategy hma --help        # the strategy's own options
uv run tb-backtest --help
uv run tb-backtest --list-strategies
uv run tb-check-offline
uv run tb-check-environment
```

Any rejected `tb-trade` command exits with status 2 before it imports a strategy
or broker module. That covers a missing, unknown, repeated or backtest-only
strategy, an unsupported mode, a backtesting option, or a foreign environment
selector. `tb-backtest` refuses `--REAL`, `--real`, `--account`, LIVE risk
limits, scheduling and read-only options with status 2, before any data access.

### `tb-trade`

```bash
uv run tb-trade --strategy hma                                  # DEMO
uv run tb-trade --strategy hma --REAL                           # LIVE (real money)
uv run tb-trade --strategy hma --symbol EURUSD --size 100 --time-frame MINUTE_5
uv run tb-trade --strategy ema-hma --long_period 100 --time_frame MINUTE_15
uv run tb-trade --strategy hma --read-only --cycles 1           # DEMO diagnostics, no orders
uv run tb-trade --strategy sma --read-only --cycles 2           # the same for any strategy
```

Rules:

- `--strategy` is required and may appear only once (`--strategy=hma` also works).
- **DEMO is the default.** LIVE requires exactly `--REAL` or `--real`; abbreviations,
  `--REAL=…`, `--live`, `--demo` and `--environment` are refused.
- Every other argument goes, unchanged, to the strategy's own parser. That parser
  applies its defaults and validation, and rejects unknown options with status 2.
  `tb-trade` never drops an argument or adds a default.
- The strategy's existing `main()` runs exactly once. `tb-trade` runs the same
  authentication, account verification, LIVE preflight, risk limits, journals,
  confirmation, reconciliation, network recovery and shutdown code as the
  legacy command. It has no broker code of its own.
- tb-backtest options that the selected strategy's trading command does not accept
  are refused with a pointer to `tb-backtest`, before any strategy is loaded. That
  covers simulation and research options (`--backtest`, `--start`, `--data-source`,
  `--snapshot`, `--execution-model`, …) and parameters of other strategies (for
  example `--hma-period` for sma). When only the spelling differs, the message names
  the trading spelling: ema-hma trades with `--long_period`, not `--long-period`.
- `tb-trade --strategy hma --help` omits `hma.py`'s `--backtest` options, which
  tb-trade refuses; `trading-bot-hma --help` and `hma.py --help` still list them.
- Exit status is the strategy's own: argparse errors exit with 2, and strategies
  keep their documented stop codes.

#### Options shared by every order-capable strategy

| Option | Default | Notes |
| --- | --- | --- |
| `--REAL`, `--real` | off (DEMO) | Selects LIVE; never guessed or inherited |
| `--account` | none | Identifies the already-active account; never switches accounts |
| `--symbol` | `EURUSD` | Instrument epic |
| `--size` | `100` | Finite positive size |
| `--max-margin`, `--max-exposure`, `--max-charges`, `--max-loss` | USD 5, 120, 1, 2 | LIVE limits |
| `--max-orders` | `1` | LIVE opening orders per process |
| `--stop-distance` | `0.001` | LIVE broker stop distance, price units |
| `--fee-profile` | `.live-integration/fees.json` | Existing verified fee evidence |

The LIVE contract mapping remains **EURUSD, size 100, USD accounts**. See
[configuration](configuration.md) and [trading safety](trading-safety.md).

#### Option categories

| Category | Examples | `tb-trade` | `tb-backtest` |
| --- | --- | --- | --- |
| Command options | `--strategy`, `--list-strategies`, `--help` | own | own |
| Shared by both commands, every strategy | `--symbol`, `--size` (grid: `--level-size`) | forwarded | yes |
| Trading only | `--REAL`/`--real`, `--account`, LIVE limits, `--fee-profile`, `--read-only`, `--cycles`, network options (all order-capable strategies); `--days`, hma `--schedule-time-frame`, multiple-hma `--short_term` | forwarded to the strategy's parser | refused (exit 2) |
| Backtesting only | `--start`, `--end`, `--data-source`, `--initial-balance`, cost options, `--execution-model`, `--snapshot`, `--average-period`, `--fast-period`, … | refused (exit 2) | yes |
| Strategy-specific, in both commands | hma `--hma-period`/`--hma_period`, `--direction`, `--time-frame`/`--time_frame`; ema-hma `--long_period`, `--short_period`, `--hma_period`, `--time_frame`; multiple-hma `--hma_period`, `--long_term` | forwarded for that strategy only | for that strategy only |

The overlap for each strategy is declared once, in the registry
(`Strategy.trading_options`). An offline test checks every declaration against
the strategy's real parser: each declared option must be accepted, and every other
tb-backtest option must be rejected by that parser or refused by tb-trade.

#### Strategy-specific options

| Strategy | Options (defaults) |
| --- | --- |
| `moving-average` | none; newest 20 one-minute ask opens, decided at each one-minute boundary |
| `sma` | none; newest 21 one-minute ask opens, SMA 9/21, decided at each one-minute boundary |
| `hma` | `--hma-period`/`--hma_period` (16), `--time-frame`/`--time_frame` (`MINUTE_30`), `--schedule-time-frame`, `--direction` (`BUY`), `--days` (7; 5 waits for market open) |
| `ema-hma` | `--long_period` (200), `--short_period` (20), `--hma_period` (16), `--time_frame` (`MINUTE_5`; one of `MINUTE`, `MINUTE_5`, `MINUTE_15`, `MINUTE_30`), `--days` (7) |
| `multiple-hma` | `--long_term` (`HOUR_4`), `--short_term` (`MINUTE_30`; a schedule timeframe), `--hma_period` (20), `--days` (5) |

Every order-capable strategy also accepts the shared execution options:
`--read-only` (DEMO only), `--cycles N`, and the [network recovery](network-recovery.md)
options (`--network-retries`, `--network-retry-delay`, `--network-retry-max-delay`,
`--network-retry-jitter`, `--recovery-interval`, `--max-outage`,
`--max-signal-delay`, `--no-network-recovery`). They are validated before any
credential is read. See [shared execution](shared-execution.md#command-line-options).

The spellings are unchanged from the original commands. `ema-hma` and
`multiple-hma` accept only the underscore forms, as before. An unsupported
ema-hma `--time_frame` or multiple-hma `--short_term` is now rejected before
authentication (it previously failed after login).

### `tb-backtest`

`tb-backtest` is the existing unified backtester under its preferred name:
`trading-bot-backtest` runs the same function. Its default strategy is still
`hma`. Data providers, snapshots, caching, cost and execution models, holdout
protection, reports and reproducibility metadata are unchanged. The command
name is not part of the configuration hash, so both names produce identical
reports and hashes.

```bash
uv run tb-backtest --strategy moving-average
uv run tb-backtest --strategy sma
uv run tb-backtest --strategy hma
uv run tb-backtest --strategy ema-hma
uv run tb-backtest --strategy multiple-hma
uv run tb-backtest --strategy donchian
uv run tb-backtest --strategy grid                                   # automatic bounds
uv run tb-backtest --strategy grid --grid-lower 1.15 --grid-upper 1.19
uv run tb-backtest --strategy grid --grid-lookback 100 --grid-intervals 20
```

Strategy options belong to the strategies that declare them. `--hma-period`
is shared by hma (default 16), ema-hma (16) and multiple-hma (20). Any other
strategy rejects a foreign option before loading data. The four strategies added
with issue #7 reuse the online signal functions and defaults; their rules,
options, warm-ups and simulation assumptions are in
[strategy backtesting](strategy-backtesting.md).

| Strategy | Backtest options (defaults) | Default timeframe, data, execution |
| --- | --- | --- |
| `moving-average` | `--average-period` (20) | `MINUTE`, dukascopy, continuous |
| `sma` | `--fast-period` (9), `--slow-period` (21) | `MINUTE`, dukascopy, continuous |
| `hma` | `--hma-period` (16), `--direction` (`BUY`) | `MINUTE_30`, dukascopy, continuous |
| `ema-hma` | `--long-period` (200), `--short-period` (20), `--hma-period` (16) | `MINUTE_5`, dukascopy, continuous |
| `multiple-hma` | `--hma-period` (20), `--long-term` (`HOUR_4`); `--time-frame` is the signal timeframe | `MINUTE_30`, dukascopy, continuous |
| `donchian` | `--entry-lookback` (20), `--exit-lookback` (10) | `HOUR`, snapshot, research |
| `grid` | `--ema-period`, `--grid-lower`/`--grid-upper` or `--grid-lookback`, `--grid-intervals`, `--level-size`, `--max-position` | `HOUR`, dukascopy, grid |

See [historical backtesting](backtesting.md), the
[framework guide](backtesting-framework.md) and
[grid backtesting](grid-backtesting.md) for the common options.

## Backward-compatible commands and migration

Every earlier command still works with its original arguments, defaults and
behavior. No legacy command was removed.

| Earlier command | Preferred command |
| --- | --- |
| `trading-bot-hma`, `uv run hma.py` | `tb-trade --strategy hma` |
| `trading-bot-sma`, `uv run sma.py` | `tb-trade --strategy sma` |
| `trading-bot-ema-hma`, `uv run ema_hma.py` | `tb-trade --strategy ema-hma` |
| `trading-bot-multiple-hma`, `uv run multiple_hma.py` | `tb-trade --strategy multiple-hma` |
| `trading-bot-moving-average`, `uv run main.py` | `tb-trade --strategy moving-average` |
| `… --REAL` / `… --real` on any of the above | `tb-trade --strategy NAME --REAL` |
| `trading-bot-backtest`, `uv run scripts/backtest.py` | `tb-backtest` |
| `uv run hma.py --backtest …` | `tb-backtest --strategy hma …` |
| `uv run src/trading_bot/strategies/donchian.py --backtest` | `tb-backtest --strategy donchian` (same numbers; also saves report files) |
| `trading-bot-check-offline`, `uv run scripts/check_offline.py` | `tb-check-offline` |
| `trading-bot-check-environment`, `uv run scripts/check_environment.py` | `tb-check-environment` |
| `trading-bot-demo-readonly`, `uv run scripts/demo_readonly.py` | `tb-demo-readonly` |
| `trading-bot-diagnose-demo-auth`, `uv run scripts/diagnose_demo_auth.py` | `tb-diagnose-demo-auth` |
| `trading-bot-demo-roundtrip`, `uv run scripts/demo_roundtrip.py`, `uv run test_api_key.py` | `tb-demo-roundtrip` |
| `trading-bot-live-preflight`, `uv run scripts/live_preflight.py`, `uv run test_live_api.py` | `tb-live-preflight` |
| `trading-bot-ping`, `uv run scripts/ping_service.py`, `uv run ping_service.py` | `tb-ping` |

`tb-trade` adds no new strategy behavior. A strategy's legacy command and
`tb-trade --strategy NAME` pass identical parsed arguments to the identical
startup function. Offline tests check this for every strategy through the
installed console scripts. `test.py` is unchanged and remains an order-capable
SELL script, not a test.

## Architecture

```text
tb-trade (tools/trade.py)            tb-backtest = trading-bot-backtest (backtesting/cli.py)
   │  validates selection/mode            │  existing parser, options, defaults
   ▼                                      ▼
strategies/registry.py  ◄── capability table ──►  backtesting/adapters.py (ADAPTERS)
   │  "trading_bot.strategies.hma:main"            simulation parameters/defaults
   ▼  (imported only on dispatch)                 ▼
strategies/<name>.py main()  ──►  config/trading_config.create_trading_api ──► risk/, api/
```

- **Registry.** `registry.STRATEGIES` explicitly declares each strategy: key,
  name, supported modes (`demo`, `live`, `backtest`), the `module:function` of
  its existing trading `main()`, its backtest adapter key, and its legacy commands.
  Construction rejects inconsistent records. For example, LIVE without DEMO, a
  trading mode without an entry point, a backtest without an adapter, and an entry
  point outside `trading_bot.strategies` are all refused. Lookup is by registered
  key only; user input never becomes an import path.
- **Lazy loading.** The registry module imports nothing from the project, so help,
  listing and rejected commands load no strategy or broker module. A strategy
  module loads only when `tb-trade` dispatches to it. `tb-backtest` never imports
  `trading_bot.api` or `trading_bot.risk`.
- **Single sources of truth.** Trading options and defaults stay in each
  strategy's parser and in `config/strategy_defaults.py`. Simulation parameters
  and defaults stay in the backtesting adapters. The registry names those
  implementations and does not copy them. Offline tests fail if the registry's
  backtest set differs from `ADAPTERS`, or if a trading entry differs from the
  matching `trading-bot-<name>` console script.
- **Option overlap.** `tb-backtest` options come from its parser
  (`backtesting.cli.option_names()`, from the public `Action.option_strings`).
  Each strategy's overlap with its trading parser is `Strategy.trading_options`.
  `tb-trade` refuses `option_names() - {--symbol, --size, ...} - trading_options`.
- **Report warnings.** Each backtest adapter declares `online_differences`, which
  every report shows.
- **Mode gate.** `tb-trade` checks the requested mode (`demo`, or `live` with
  `--REAL`/`--real`) against the registry before dispatch. Registering a strategy
  does not grant it a mode.

## Adding a strategy

1. Implement it under `src/trading_bot/strategies/`. For trading, provide a
   `main()` that builds its own `argparse` parser with `add_trading_arguments`
   (plus `add_network_arguments` where it applies). It must authenticate only
   through `create_trading_api` and use the existing risk controls. For
   backtesting, provide a pure strategy class.
2. Add one `Strategy(...)` entry to `STRATEGIES` in `registry.py`. List only the
   modes that already work. Declare in `trading_options` the trading options that
   tb-backtest also defines with the same meaning. Use `{DEMO}` alone until LIVE is reviewed, because
   `tb-trade` then refuses `--REAL` for it.
3. For backtesting, put the signal rule in a broker-free module
   (`strategies/indicators.py` or `strategies/signals.py`) and call it from both
   the online loop and a candle strategy class in `backtesting/strategy.py`. Then
   add a `StrategyAdapter` to `ADAPTERS` in `backtesting/adapters.py`, using the
   same key. `tb-backtest` picks it up automatically, including options it shares
   with other strategies (see [the framework guide](backtesting-framework.md)).
4. Declare the adapter's `online_differences`: every way its online candles,
   orders or limits differ from the simulation. Add offline tests, update the
   capability matrix in this document and the README, and add the strategy to the
   [safety comparison](trading-safety.md#safety-capabilities-by-strategy). The registry tests show which expectations to extend.

No executable wrapper, root-level script or new console-script entry is needed.
The strategy is available as `tb-trade --strategy NAME` and/or
`tb-backtest --strategy NAME` as soon as it is registered. Add a
`trading-bot-NAME` console script only if a separate legacy-style command is
explicitly wanted.

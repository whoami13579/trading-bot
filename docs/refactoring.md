# Structural refactor review

## Scope and baseline

Implemented locally on branch `master`, starting from a clean working tree at
`ce720a8995a1819a2d63dc7423f576dd2fc846a4`. No existing uncommitted work was
present. The private prior audit was reviewed as context; its private reports and
integration state were not migrated or published. No files were deleted.

`test.py` remains at its original path with its bytes unchanged. `.env` and all
previous private integration-state files are protected by before/after hashes.
The existing DEMO-default, explicit LIVE selection, endpoint/account checks,
order limits, stops, cost checks, journals, uncertain-outcome stops, position
ownership checks, reconciliation and network-error behavior were retained.
No trading accounts were contacted and no actual requests/orders were submitted.

## Complete public source tree

This includes every tracked/nonignored project file, including compatibility
modules. Ignored `.env`, `.demo-integration/`, `.live-integration/`, local reports,
`.venv/`, Python caches, `src/trading_bot.egg-info/`, `build/` and `dist/` remain
local and are intentionally not expanded here.

```text
trading-bot/
├── docs/
│   ├── configuration.md
│   ├── integration-testing.md
│   ├── refactoring.md
│   ├── setup.md
│   └── trading-safety.md
├── scripts/
│   ├── backtest.py
│   ├── check_environment.py
│   ├── check_offline.py
│   ├── demo_readonly.py
│   ├── demo_roundtrip.py
│   ├── diagnose_demo_auth.py
│   ├── integration_roundtrip.py
│   ├── live_execution.py
│   ├── live_preflight.py
│   ├── live_risk.py
│   ├── network_errors.py
│   ├── ping_service.py
│   ├── strategy_costs.py
│   ├── strategy_defaults.py
│   ├── test_offline.py
│   ├── trade_reconciliation.py
│   └── trading_config.py
├── src/
│   └── trading_bot/
│       ├── api/
│       │   ├── __init__.py
│       │   ├── auth_diagnostics.py
│       │   ├── client.py
│       │   ├── demo_transport.py
│       │   ├── models.py
│       │   └── network_errors.py
│       ├── config/
│       │   ├── __init__.py
│       │   ├── paths.py
│       │   ├── strategy_defaults.py
│       │   └── trading_config.py
│       ├── risk/
│       │   ├── __init__.py
│       │   ├── confirmations.py
│       │   ├── journal.py
│       │   ├── legacy_demo.py
│       │   ├── live_execution.py
│       │   ├── live_risk.py
│       │   ├── strategy_costs.py
│       │   └── trade_reconciliation.py
│       ├── strategies/
│       │   ├── __init__.py
│       │   ├── backtest.py
│       │   ├── ema_hma.py
│       │   ├── hma.py
│       │   ├── indicators.py
│       │   ├── moving_average.py
│       │   ├── multiple_hma.py
│       │   └── sma.py
│       ├── tools/
│       │   ├── __init__.py
│       │   ├── backtest.py
│       │   ├── check_environment.py
│       │   ├── check_offline.py
│       │   ├── demo_readonly.py
│       │   ├── demo_roundtrip.py
│       │   ├── diagnose_demo_auth.py
│       │   ├── live_preflight.py
│       │   ├── ping_service.py
│       │   └── roundtrip.py
│       ├── utils/
│       │   ├── __init__.py
│       │   ├── colors.py
│       │   ├── files.py
│       │   └── scheduler.py
│       └── __init__.py
├── tests/
│   ├── fixtures/
│   │   └── eurusd_ecb_daily.json
│   ├── reference_indicators.py
│   ├── test_auth_diagnostics.py
│   ├── test_backtest.py
│   ├── test_buy_sell_integration.py
│   ├── test_demo_safety.py
│   ├── test_historical_signals.py
│   ├── test_independent_auth.py
│   ├── test_indicators.py
│   ├── test_live_integration.py
│   ├── test_network_failures.py
│   ├── test_package_layout.py
│   ├── test_reconciliation.py
│   ├── test_sessions.py
│   ├── test_shared_config.py
│   ├── test_strategy_environments.py
│   └── test_trade_integration.py
├── .env.example
├── .gitignore
├── .python-version
├── MANIFEST.in
├── README.md
├── TradingApi.py
├── colors.py
├── ema_hma.py
├── hma.py
├── indicators.py
├── live_fee_profile.example.json
├── main.py
├── multiple_hma.py
├── ping_service.py
├── pyproject.toml
├── scheduler.py
├── sma.py
├── test.py
├── test_api_key.py
├── test_live_api.py
├── test_strategy.py
├── tradingPosition.py
└── uv.lock
```

## Implementation relocations and retained legacy paths

`git mv` was used for the implementations. Each old path was recreated as a
lightweight alias/wrapper; hence the final unstaged Git view shows modifications
to old files and new package files rather than deleted legacy paths. The index
was returned to its initial clean state without changing working-tree files.

| Original path | Canonical implementation | Retained original path |
| --- | --- | --- |
| `TradingApi.py` | `src/trading_bot/api/client.py` | Import alias |
| `colors.py` | `src/trading_bot/utils/colors.py` | Import alias |
| `ema_hma.py` | `src/trading_bot/strategies/ema_hma.py` | Import alias and CLI wrapper |
| `hma.py` | `src/trading_bot/strategies/hma.py` | Import alias and CLI wrapper |
| `indicators.py` | `src/trading_bot/strategies/indicators.py` | Import alias |
| `main.py` | `src/trading_bot/strategies/moving_average.py` | Import alias and CLI wrapper |
| `multiple_hma.py` | `src/trading_bot/strategies/multiple_hma.py` | Import alias and CLI wrapper |
| `ping_service.py` | `src/trading_bot/tools/ping_service.py` | Import alias and CLI wrapper |
| `scheduler.py` | `src/trading_bot/utils/scheduler.py` | Import alias |
| `scripts/check_environment.py` | `src/trading_bot/tools/check_environment.py` | Import alias and CLI wrapper |
| `scripts/demo_readonly.py` | `src/trading_bot/tools/demo_readonly.py` | Import alias and CLI wrapper |
| `scripts/diagnose_demo_auth.py` | `src/trading_bot/tools/diagnose_demo_auth.py` | Import alias and CLI wrapper |
| `scripts/integration_roundtrip.py` | `src/trading_bot/tools/roundtrip.py` | Import alias |
| `scripts/live_execution.py` | `src/trading_bot/risk/live_execution.py` | Import alias |
| `scripts/live_risk.py` | `src/trading_bot/risk/live_risk.py` | Import alias |
| `scripts/network_errors.py` | `src/trading_bot/api/network_errors.py` | Import alias |
| `scripts/strategy_costs.py` | `src/trading_bot/risk/strategy_costs.py` | Import alias |
| `scripts/strategy_defaults.py` | `src/trading_bot/config/strategy_defaults.py` | Import alias |
| `scripts/test_offline.py` | `src/trading_bot/tools/check_offline.py` | Import alias and CLI wrapper |
| `scripts/trade_reconciliation.py` | `src/trading_bot/risk/trade_reconciliation.py` | Import alias |
| `scripts/trading_config.py` | `src/trading_bot/config/trading_config.py` | Import alias |
| `sma.py` | `src/trading_bot/strategies/sma.py` | Import alias and CLI wrapper |
| `test_api_key.py` | `src/trading_bot/tools/demo_roundtrip.py` | Import alias and CLI wrapper |
| `test_live_api.py` | `src/trading_bot/tools/live_preflight.py` | Import alias and CLI wrapper |
| `test_strategy.py` | `src/trading_bot/tools/backtest.py` | Import alias and CLI wrapper |
| `tradingPosition.py` | `src/trading_bot/api/models.py` | Import alias |

All production package imports now use `trading_bot.*`. Legacy aliases replace
the importing module with the canonical module in `sys.modules`; old and new
callers therefore share class identity, configuration globals and monkeypatch
points rather than maintaining two copies. Existing automated tests deliberately
continue using legacy imports as compatibility coverage; new tests exercise the
canonical imports and installed commands.

## Other added and modified files

New shared implementation modules and package initializers:

- `src/trading_bot/__init__.py`
- `src/trading_bot/api/__init__.py`
- `src/trading_bot/api/auth_diagnostics.py`
- `src/trading_bot/api/demo_transport.py`
- `src/trading_bot/config/__init__.py`
- `src/trading_bot/config/paths.py`
- `src/trading_bot/risk/__init__.py`
- `src/trading_bot/risk/confirmations.py`
- `src/trading_bot/risk/journal.py`
- `src/trading_bot/risk/legacy_demo.py`
- `src/trading_bot/strategies/__init__.py`
- `src/trading_bot/strategies/backtest.py`
- `src/trading_bot/tools/__init__.py`
- `src/trading_bot/utils/__init__.py`
- `src/trading_bot/utils/files.py`

Other added files:

- `MANIFEST.in`
- `docs/configuration.md`
- `docs/integration-testing.md`
- `docs/refactoring.md`
- `docs/setup.md`
- `docs/trading-safety.md`
- `scripts/backtest.py`
- `scripts/check_offline.py`
- `scripts/demo_roundtrip.py`
- `scripts/live_preflight.py`
- `scripts/ping_service.py`
- `tests/test_backtest.py`
- `tests/test_package_layout.py`

Modified metadata/documentation: `README.md`, `pyproject.toml`, and `uv.lock`.
The project is now an editable src package with 13 console commands, Python >=3.12,
and an isolated setuptools build. The seven runtime dependency versions were
retained. `MANIFEST.in` explicitly includes public checkout tools, docs, tests and
the ECB fixture in the sdist. The wheel contains package modules and metadata.
No existing tests, `.env.example`, fee template, `.gitignore`, fixture or
`test.py` was changed. No tracked file was deleted.

## Architectural decisions

- `api/` contains the client, position model, sanitized network/auth diagnostics
  and prepared DEMO request guard.
- `strategies/` contains unchanged live strategies/indicators, the moving-average
  strategy previously named `main.py`, and a pure historical simulator.
- `risk/` owns execution controls, costs, reconciliation, durable intent,
  confirmation polling and legacy single-BUY ownership helpers.
- `tools/` holds importable CLI implementations; `scripts/` supplies executable
  wrappers. This extra package directory lets installed console commands work
  without depending on a checkout's `scripts` namespace or working directory.
  Shared helpers no longer import the demo/LIVE CLI modules in a cycle.
- `config/paths.py` anchors editable installs to the checkout, preserving exact
  credential/journal paths independently of the working directory. A wheel uses
  a fixed `<sys.prefix>/share/trading-bot` root with no implicit credential search;
  absent configuration fails closed. Developer checkers require the checkout.
- The root fee template is retained for command compatibility; no redundant
  top-level `config/` directory was introduced.
- README now links to setup, configuration, safety and integration documents.
  Existing useful operational instructions/commands are preserved there.

## Confirmed backtest defects and corrections

The old downtrend branch printed SELL but called `buy`. Its long and short P&L
signs were reversed, the signal status never updated, and one shared price field
could not track position direction. Repeated same-side signals alternately opened
and closed positions even though the trend-following code intended a held side.

The simulator now targets BUY or SELL, holds repeated signals, and reverses at the
observed quote: close the old side, realize `(exit - entry) * direction`, then
open the new side. Downtrends use SELL. Only completed positions affect realized
simulation P&L; an open final side and its unrealized mark are reported separately.
No final liquidation is invented. Empty/short/flat histories open no positions;
invalid quotes or periods fail clearly. The legacy buffer adapter is retained.

WMA/HMA calculations now reuse the production indicator implementation. The
three-period rolling warmup and original slope comparison are retained; the
backtest reads only each observed prefix. New tests verify that future quotes
cannot change earlier signals/trades. The historical CLI fetches quotes using
`order_execution=False` and never calls an order method. Obsolete sample-profit
comments were not carried into the corrected simulator; Git retains the originals.

These are idealized one-unit **gross quote-price differences**, not broker cash
settlement. Bid/ask spread, fees, slippage, financing, currency conversion and
latency are not modeled. Same-observation signal/fill prices are an explicit
idealized assumption. Live signal rules and execution behavior are unchanged.

## Compatibility and safety evidence

- Every one of the 26 former module paths aliases its canonical implementation.
  Root `test.py` continues importing the two legacy `scripts.*` modules unchanged.
- Old CLI options, defaults, `--REAL`/`--real`, explicit environment routing,
  account selection and risk flags are exercised by the existing suite.
- Old/new script help and all 13 installed help commands are tested under API,
  HTTP, credential and socket guards, including from a temporary working directory.
- Importing every package module, as well as importing `test.py` under a distinct
  module name, does not authenticate or execute a strategy.
- An AST comparison with HEAD confirmed 18 API/config/risk/strategy/utility
  implementations are equivalent except for imports and ROOT relocation; nine
  extracted transport/journal/confirmation/ownership definitions are AST-identical.
- Shared WMA/HMA output exactly matched the removed duplicate over 36 combinations
  of periods and history lengths; the live indicator warmup convention is unchanged.
- Existing mock regressions cover authentication, separate DEMO/LIVE hosts,
  no fallback, DNS/timeout failures, duplicate/uncertain orders, ownership,
  unrelated positions, costs, limits, stops, locks and transaction reconciliation.

## Validation performed

| Command/check | Result |
| --- | --- |
| `uv sync --python 3.12` | Installed editable `trading-bot==0.1.0`, CPython 3.12.10 |
| `uv sync --locked --offline --check` | Environment current; no changes required |
| `uv pip check --python .venv/bin/python` | Eight installed packages compatible (project plus seven dependencies) |
| `uv run --locked scripts/check_offline.py` | 253 tests: 252 passed, one optional broker-history fixture skipped |
| `uv run --locked scripts/check_environment.py` | Compilation, dependency versions, guarded imports, synthetic indicator/readers and legacy/new/installed help paths passed |
| `uv build --offline` | Source distribution and wheel built successfully |
| `uv pip install --offline --no-deps --target <temporary-dir> dist/trading_bot-0.1.0-py3-none-any.whl` | Wheel installed; every package module imported from an unrelated directory under socket/API guards |
| `git diff --check HEAD` | No whitespace errors |
| Credential and private-file scan of public files and both archives | No credential matches, high-confidence key/token/credential-URL patterns, or private configuration/state paths found |
| Protected hashes and HEAD/index verification | `test.py`, `.env`, prior private integration files and HEAD unchanged; index empty |

The skipped `test_capital_demo_historical_snapshots` requires an absent optional
`tests/fixtures/eurusd_demo_prices.json`. No fixture was fetched and no account
access was attempted. An initial new CLI-test harness had a mutable `sys.argv`
path bug; it was corrected and the full suite rerun successfully. No remaining
failed tests. Offline mocks verify behavior but do not certify broker availability
or real account execution; authenticated integration runs need separate approval.

## Security and preserved local data

No credentials or tokens were printed. `.env` remains ignored and untracked;
private integration directories, local reports, cache/build outputs and virtual
environment remain excluded by the unchanged `.gitignore`. Distribution content
was inspected for private paths and exact configured credential matches. Static
pattern checks are bounded checks, not proof against every possible encoded secret.
The existing private reports and durable trading journals must not be deleted as
part of cosmetic cleanup. Package building and installation used only dependency
infrastructure/local files, never Capital.com account endpoints.

## Remaining recommendations (not implemented)

- Review pre-existing LIVE gate/ownership and journal-lock scope concerns from the
  private audit in a separate safety task. Structural equivalence preserves old
  limitations too; this refactor does not certify or redesign the trading controls.
- Review the existing SMA-specific duplicate separately: its short-history behavior
  differs from the shared validator, so replacing it would change strategy behavior.
- Consider a quote-aware/cost-aware simulation and independent strategy benchmarks
  as a separate feature, with explicit execution timing assumptions.
- Reassess the existing encryption dependency and retire legacy wrappers only after
  external callers and stored operational commands have migrated. No runtime
  dependency or compatibility module was removed during this refactor.
- Do not run default roundtrip or strategy commands merely to check installation;
  use the offline runner, environment checker or `--help` instead.

## Final local Git status

29 modified tracked files; 54 untracked files; zero staged files; zero deletions.
All 83 changed/new files are local. Nothing was committed, pushed or sent as a PR.
HEAD remains the starting commit above.

```text
 M README.md
 M TradingApi.py
 M colors.py
 M ema_hma.py
 M hma.py
 M indicators.py
 M main.py
 M multiple_hma.py
 M ping_service.py
 M pyproject.toml
 M scheduler.py
 M scripts/check_environment.py
 M scripts/demo_readonly.py
 M scripts/diagnose_demo_auth.py
 M scripts/integration_roundtrip.py
 M scripts/live_execution.py
 M scripts/live_risk.py
 M scripts/network_errors.py
 M scripts/strategy_costs.py
 M scripts/strategy_defaults.py
 M scripts/test_offline.py
 M scripts/trade_reconciliation.py
 M scripts/trading_config.py
 M sma.py
 M test_api_key.py
 M test_live_api.py
 M test_strategy.py
 M tradingPosition.py
 M uv.lock
?? MANIFEST.in
?? docs/configuration.md
?? docs/integration-testing.md
?? docs/setup.md
?? docs/trading-safety.md
?? scripts/backtest.py
?? scripts/check_offline.py
?? scripts/demo_roundtrip.py
?? scripts/live_preflight.py
?? scripts/ping_service.py
?? src/trading_bot/__init__.py
?? src/trading_bot/api/__init__.py
?? src/trading_bot/api/auth_diagnostics.py
?? src/trading_bot/api/client.py
?? src/trading_bot/api/demo_transport.py
?? src/trading_bot/api/models.py
?? src/trading_bot/api/network_errors.py
?? src/trading_bot/config/__init__.py
?? src/trading_bot/config/paths.py
?? src/trading_bot/config/strategy_defaults.py
?? src/trading_bot/config/trading_config.py
?? src/trading_bot/risk/__init__.py
?? src/trading_bot/risk/confirmations.py
?? src/trading_bot/risk/journal.py
?? src/trading_bot/risk/legacy_demo.py
?? src/trading_bot/risk/live_execution.py
?? src/trading_bot/risk/live_risk.py
?? src/trading_bot/risk/strategy_costs.py
?? src/trading_bot/risk/trade_reconciliation.py
?? src/trading_bot/strategies/__init__.py
?? src/trading_bot/strategies/backtest.py
?? src/trading_bot/strategies/ema_hma.py
?? src/trading_bot/strategies/hma.py
?? src/trading_bot/strategies/indicators.py
?? src/trading_bot/strategies/moving_average.py
?? src/trading_bot/strategies/multiple_hma.py
?? src/trading_bot/strategies/sma.py
?? src/trading_bot/tools/__init__.py
?? src/trading_bot/tools/backtest.py
?? src/trading_bot/tools/check_environment.py
?? src/trading_bot/tools/check_offline.py
?? src/trading_bot/tools/demo_readonly.py
?? src/trading_bot/tools/demo_roundtrip.py
?? src/trading_bot/tools/diagnose_demo_auth.py
?? src/trading_bot/tools/live_preflight.py
?? src/trading_bot/tools/ping_service.py
?? src/trading_bot/tools/roundtrip.py
?? src/trading_bot/utils/__init__.py
?? src/trading_bot/utils/colors.py
?? src/trading_bot/utils/files.py
?? src/trading_bot/utils/scheduler.py
?? tests/test_backtest.py
?? tests/test_package_layout.py
?? docs/refactoring.md
```

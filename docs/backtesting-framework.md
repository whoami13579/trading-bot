# Unified backtesting framework

HMA and Donchian run on one historical data layer, one event loop, one Decimal
ledger, explicit execution/cost models, one report format and one CLI. Adding a
strategy means writing its pure signal logic and a small adapter. Backtesting is
simulation only: it never authenticates, reads credentials or submits orders.

```bash
uv run trading-bot-backtest --strategy hma --symbol EURUSD --time-frame HOUR --size 100
uv run trading-bot-backtest --strategy donchian --symbol EURUSD --time-frame HOUR --size 100
uv run trading-bot-backtest --strategy grid --grid-lower 1.15 --grid-upper 1.19 --start 2025-09-01 --end 2025-10-01
```

## Problems in the previous architecture

| Area | Before | Consequence |
| --- | --- | --- |
| Event loop | `engine.run` (HMA) and `study_engine.simulate` (Donchian) each had their own completed-close → fill loop | Fill timing, warm-up, financing accrual and snapshots were maintained twice and could drift |
| Strategy interface | HMA: `target(candles)`; Donchian: `target(candles, direction)`, selected with `if strategy.name == "Donchian"` | A strategy's behavior depended on its display name |
| Execution semantics | Differences (warm-up priming, gap resets, boundary liquidation, quote rebuild, rollover financing) were implicit in two code paths | Not visible or configurable; no way to run one strategy under the other's assumptions |
| Data | Provider path (`load_history`) vs snapshot path (`read_snapshot` + `prepare_frames`) wired separately inside each CLI | No common contract; the Donchian CLI could not use provider data and vice versa |
| Download reuse | Cache key depended on each strategy's warm-up | The same range downloaded twice for strategies with different warm-ups |
| CLIs | `cli.py` (HMA only) and `donchian_cli.py` (print only, no files) | No unified entry point; Donchian runs produced no trade/equity exports |
| Reports | `make_report`/`print_report` hard-coded `hma_period`/`direction`; Donchian printed its own table | Inconsistent fields and cost disclosures |
| Research safeguards | Splits, freeze/holdout guards and concentration inside `donchian_study.py` | Not reusable; nothing warned when other runs reused the frozen holdout |

Shared already, and kept: `models.py`, `timeframes.py`, `calendar.py`, `cache.py`,
providers, the `Ledger` and `statistics()`.

## Architecture

```text
strategies/hma.py, donchian.py      pure rules; live runners unchanged
backtesting/
  data         data.py (load_history: providers + cache; load_snapshot: verified snapshot)
               providers/, cache.py, timeframes.py, calendar.py, models.py
  engine       engine.py: Strategy protocol, ExecutionPolicy, Ledger, simulate(), run()
  execution    execution.py: ContinuousExecution, ResearchExecution
               study_engine.py: cost cases, quote rebuild, NY-rollover ledger, frames
  strategies   adapters.py (registry), strategy.py (HMA adapter)
  evaluation   evaluation.py: walk-forward windows, holdout registry, StageRegistry, hashes
               donchian_study.py: preregistered study using those helpers
  reporting    reporting.py: build_report, standard terminal table, JSON/CSV/HTML
  pipeline     runner.py: BacktestRequest -> plan (no I/O) -> load -> simulate -> report
  cli          cli.py (unified; also hma.py --backtest), donchian_cli.py (legacy)
```

Modules stay flat inside `backtesting/` so the Donchian study's code fingerprint
(`code_hashes()`, which hashes `backtesting/*.py`) covers every new module.

### Strategy interface (`engine.Strategy`)

- `name`, `warmup`: completed candles per decision window.
- `target(candles[, position])` returns `"BUY"` (long), `"SELL"` (short) or
  `None` (flat). `candles` holds only completed candles; the newest just closed.
- Position-aware strategies take the current simulated side as a second
  argument. This is resolved once per run from an explicit `position_aware`
  attribute or the `target` signature — never from the strategy name.
- Strategies keep their own indicator state if needed (the deprecated quote-buffer
  adapter does); they never see cash, fills or future candles.

### Execution models

The shared `engine.simulate()` loop is parameterized by an `ExecutionPolicy`, a
quote transform and a ledger. The two existing semantics are explicit models:

| | `continuous` (HMA default) | `research` (Donchian default) |
| --- | --- | --- |
| Decision → fill | completed close → next observed open | same |
| First fill | warm-up close may queue the first scored open | first decision is the first scored close |
| Unexpected active-session gap | position and signals carry on | cancel pending entry, keep held position and its pending exit, reset warm-up (missing candle slots of the series' own timeframe, see below) |
| End of range | position stays open, marked at liquidation-side close | costed synthetic exit at last completed close |
| Fill prices | source bid/ask (indicative OHLC for yfinance) | bid/ask rebuilt around midpoint × cost-case spread multiple (1 pip assumed for midpoint data) |
| Slippage / commission | optional; **unmodeled** unless given | cost case (0.1 / 0.5 pip); commission **assumed 0** |
| Financing | optional linear USD/unit/day | cost case: 0 (historical financing omitted) or 5%/yr at NY 17:00, Wednesday triple |

Either strategy can run under either model (`--execution-model`), on either data
source.

**Gap detection** (`calendar.active_gap(prev_end, next_start, timeframe)`): every
whole candle slot of the series' timeframe between two consecutive candles is a
missing candle. It is an *unexpected* gap if any part of the slot lies inside the
DST-aware New York Sunday 17:00 – Friday 17:00 week, so 1m/5m/15m gaps are
detected and a 4H slot that straddles the Sunday open or Friday close counts.
Scheduled weekend closures are not gaps. Session edges fall on whole New York
hours, so each slot is probed every min(timeframe, 30 minutes); for aligned
30m/1H/4H slots this is exactly the original 30-minute scan. Nothing is filled.
On 1m data, provider no-quote minutes inside the session are missing candles and
therefore reset research windows; `--allow-data-gaps` still only controls whether
the data audit accepts such gaps. A third policy, `LEGACY_OBSERVED_CLOSE`, exists only for the deprecated
`strategies/backtest.py` supplied-price API.

### Data

- `--data-source dukascopy|yfinance`: `load_history` (unchanged validation, gap
  audit, warm-up trimming, atomic integrity-checked cache). The download range
  now uses `max(warmup, 48)` candles of padding, so strategies with warm-ups up to
  48 (HMA16, Donchian 20/10) share one cached download. Larger warm-ups behave
  exactly as before. Existing HMA cache entries remain valid.
- `--data-source snapshot`: `load_snapshot` verifies the snapshot fingerprint and
  applies the study's audited 30m → 1H/4H aggregation (aggregates with missing 30m
  constituents are excluded, never filled). Supports `MINUTE_30`, `HOUR`, `HOUR_4`
  within the snapshot interval. 5m/15m require provider data.
- Every dataset records `data_sha256` of the exact candles used.

## CLI

`trading-bot-backtest` (also `uv run scripts/backtest.py`, `uv run -m
trading_bot.backtesting.cli`). `--strategy` defaults to `hma`, so earlier
`trading-bot-backtest` invocations behave as before.

| Option | Default | Notes |
| --- | --- | --- |
| `--strategy` | `hma` | `hma`, `donchian`, `grid` |
| `--symbol` | `EURUSD` | only EURUSD is verified |
| `--size` | 100 | EUR base units (grid: rejected, use `--level-size`) |
| `--time-frame` | HMA `MINUTE_30`, Donchian/grid `HOUR` | `MINUTE` … `DAY` |
| `--data-source` | HMA/grid `dukascopy`, Donchian `snapshot` | `dukascopy`, `yfinance`, `snapshot` |
| `--snapshot` | `.cache/donchian/eurusd-30m.json.gz` | study snapshot |
| `--start`, `--end`, `--lookback-days` | providers: last 30 complete UTC days; snapshot: 2023-11-01 to 2026-11-01 | UTC, end exclusive |
| `--initial-balance`, `--leverage` | 1000, 1 | USD; simulated collateral |
| `--execution-model` | HMA `continuous`, Donchian `research`, grid `grid` | `grid` only with grid strategies |
| `--commission-per-order`, `--slippage-pips`, `--financing-usd-per-unit-day` | unmodeled | continuous and grid models |
| `--cost-case` | `base` | research model only: `base`, `high`, `financing` |
| `--hma-period`, `--direction` | 16, `BUY` | HMA only |
| `--entry-lookback`, `--exit-lookback` | 20, 10 | Donchian only |
| `--ema-period`, `--grid-lower`, `--grid-upper`, `--grid-intervals`, `--level-size`, `--max-position` | 50, required, required, 10, 100, 500 | grid only; see [grid backtesting](grid-backtesting.md) |
| `--cache-dir`, `--output-dir`, `--refresh-data`, `--allow-data-gaps` | as before | |
| `--allow-holdout-overlap` | off | see research safeguards |

Invalid combinations (another strategy's parameter, a cost option of the other
execution model, a refused holdout reuse, broker/account/risk/scheduling flags)
exit 2 before any data access. Data and simulation failures exit 1 with
`Backtest failed: …` (plus snapshot preparation help for snapshot runs).

Existing commands are kept with their own options and defaults:

```bash
uv run hma.py --backtest                                # = --strategy hma
uv run src/trading_bot/strategies/hma.py --backtest
uv run src/trading_bot/strategies/donchian.py --backtest # = --strategy donchian, print only
```

## Report

Every run prints the same table, then model- and data-specific details:

```text
Strategy, Symbol, Size, Environment, Data source, Timeframe, Historical days,
Period, Candles, Warm-up candles, Initial balance, Final balance, Final equity,
Gross P&L, Net P&L, Total return, Total closed trades, Winning trades,
Losing trades, Win rate, Profit factor, Max drawdown, Realized cash P&L,
Unrealized P&L, Modeled costs, Unmodeled costs, Assumed costs,
Remaining positions, Available funds
— then: Execution model, Realized gross P&L, Unrealized gross P&L,
  Open valuation spread, Last candle end, coverage audit, research counters,
  Estimated total costs, Modeled cash costs, remaining exposure, Rejected entries,
  Holdout overlap, Results
```

Definitions (`engine.pnl_decomposition`; every report asserts the identity below to 1e-18, i.e. up to Decimal rounding):

- **Gross P&L**: midpoint P&L before every cost (indicative-price P&L for data
  without bid/ask) = **Realized gross P&L** (closed trades, entry midpoint to exit
  midpoint) + **Unrealized gross P&L** (an open position from its entry midpoint
  to the last closing midpoint).
- **Modeled costs**: costs actually incurred: executed-leg spread and slippage,
  commission and financing, including those already paid by an open position.
- **Open valuation spread**: the exit half-spread implied by marking an open
  position at its liquidation side (bid for longs, ask for shorts). It has not been
  executed, so it is not a modeled cost; net equity already reflects it.
- **Net P&L** = final equity − initial balance = Gross P&L − Modeled costs − Open
  valuation spread = **Realized cash P&L** (balance change, including costs paid by
  an open position) + **Unrealized P&L** (open position at the liquidation side).
- Flat-ending runs have no open valuation spread, so Gross = Net + Modeled costs.
- **Unmodeled costs** are named and excluded from arithmetic; they are never
  presented as zero. **Assumed costs** lists every explicit assumption (for
  example the research model's zero commission).
- Runs overlapping a registered holdout say so.

`trading-bot-backtest` saves `backtests/<run-id>/report.json`, `trades.csv`,
`trade_details.csv` (signal times, boundary flag, spread/slippage/commission/
financing per trade), `equity.csv` and `equity.html`. `report.json` keeps every
earlier HMA key and meaning and adds `strategy_key`, `parameters`, `environment`,
`data_source`, `quote_basis`, `execution_model`, `execution_policy`,
`assumed_costs`, `summary`, `trade_details`, `holdout_overlap`,
`reproducibility` (`configuration_sha256`, `data_sha256` and the hashed record)
and `notes`.

## Research safeguards (`evaluation.py`)

- `chronological_windows`: expanding-train walk-forward validations plus a final
  holdout; `donchian_study.splits()` now uses it (identical windows).
- `StageRegistry`: one attempt per stage; holdout only after a freeze that
  fingerprints the prepared study and development results; every attempt logged.
  `donchian_study` uses it with the same files and messages.
- Holdout registry: the Donchian study's frozen holdout
  [2026-03-27, 2026-11-01) is registered with the study's **complete**
  preregistered holdout specifications, derived from
  `donchian_study.configurations("holdout")`: strategy parameters, timeframe,
  execution model and cost case, on the frozen snapshot (candle SHA256
  `f7ad8f61…`) with size 100, USD 1,000 and leverage 1. Roles: `primary` (Donchian
  20/10, 1H, base), `primary_cost_sensitivity` (1H high/financing), `robustness`
  (30m and 4H, all cost cases) and `comparator` (HMA16 BUY). Cash is not a strategy.
- Any other Donchian configuration overlapping that holdout (another timeframe,
  execution model, cost case, data source or dataset, parameters, size, balance or
  leverage) is refused unless `--allow-holdout-overlap` is given; the report then
  records `acknowledged_reuse`. The specification is checked before data access
  and the snapshot fingerprint again after it is read. Other strategies and
  periods outside the holdout are not restricted; overlaps are always disclosed with
  `preregistered_role` and `registered_dataset`.
- `canonical_sha256` configuration hashes and data fingerprints in every report.
- Cost sensitivity: `--cost-case base|high|financing`; parameter robustness: the
  study's preregistered neighbours, or explicit CLI parameters outside the holdout.
- The archived study (`backtests/donchian-eurusd-final`) is never rewritten.
  Reproduction re-simulates into separate locations.

## Compatibility notes (intentional, tested changes)

1. **Printed report layout** of `hma.py --backtest`, `scripts/backtest.py` and
   `donchian.py --backtest` follows the standard table. No previous label was
   removed and every numeric value is unchanged. New HMA labels: Size, Environment,
   Gross P&L, Modeled costs, Assumed costs, Execution model, Last candle end,
   Holdout overlap. New Donchian labels: Unmodeled costs, Assumed costs, Execution
   model, Missing 30m source bars, Excluded aggregates, Unexpected gap events,
   Boundary exits, Cancelled gap entries, Gap holding events, Exposure, Estimated
   total costs, Modeled cash costs, Holdout overlap. HMA text changes: `Data source` adds the quote
   basis (`dukascopy (historical bid/ask)`), `Timeframe` adds a short label
   (`MINUTE_30 (30m)`), `Period` adds `(UTC, end exclusive)`. The HMA closing note
   adds the Gross P&L definition.
2. **report.json** gained the additive keys listed above; no key was removed or
   changed. `trade_details.csv` is a new file; `trades.csv`, `equity.csv` and
   `equity.html` are byte-identical for identical inputs.
3. **Shared download padding**: strategies with warm-up below 48 candles now
   download (and cache) the same padded range as HMA16. Only the
   `downloaded_warmup_candles` metadata of such runs differs; used candles and
   results do not.
4. Tests that mocked `backtesting.cli.load_history` now mock
   `backtesting.runner.load_history`, where the loader is called.
5. Review corrections (follow-up commit): Gross P&L for runs ending with an open
   position now includes that position's midpoint move, so it rises by the open
   valuation spread relative to the first PR #2 commit (e.g. HMA default ending
   2026-11-12: $0.1815 → $0.1830). Gross P&L was introduced by PR #2, so no
   released output changes. New lines/keys: Realized gross P&L, Unrealized gross
   P&L, Open valuation spread; `summary.realized_cash_pnl`, `unrealized_pnl`,
   `open_entry_spread_and_slippage`. Holdout disclosures add `preregistered_role`
   and `registered_dataset`.
6. `reporting.make_report` (HMA-only, no remaining callers) was replaced by
   `build_report`; `donchian_cli.data_help` by `study_data.snapshot_help`.
   `study_engine.active_gap` moved to `calendar.py` and is still importable from
   `study_engine`.

## Verification

Pre-refactor baselines were captured before any change and compared after it,
offline, with sockets, credential files and broker imports blocked:

- **Donchian study**: all 201 archived development/holdout runs (20,798 trades)
  re-simulated; `metrics.json`, `trades.csv`, `equity.csv` and the stage rows are
  **byte-identical** to `backtests/donchian-eurusd-final`, before and after.
  `research/donchian/verify_results.py`, run on a copy of the archive, passes and
  reproduces `verification.json` and `simulated_trades.csv` byte-for-byte.
- **Pinned synthetic regressions** (`tests/fixtures/backtest_regression.json.gz`,
  generated by the pre-refactor engines): 3 HMA continuous configurations and 6
  research configurations (Donchian 20/10 × 3 cost cases, Donchian 5/3, HMA ×2)
  match exactly, including Decimal string representations.
- **Existing commands** on cached real data (same inputs before/after):

| Command | Candles | Final equity | Net P&L | Trades | PF | Max DD |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `hma.py --backtest --end 2026-11-12` | 1,054 | $999.9160 | −$0.0840 | 63 | 0.9812 | $1.7430 |
| `strategies/hma.py --backtest --end 2026-11-13` | 1,054 | $999.6610 | −$0.3390 | 61 | 0.9307 | $1.7430 |
| … `--data-source yfinance` | 1,040 | $1,000.6818 | +$0.6818 | 63 | 1.1971 | $1.1001 |
| … `--time-frame MINUTE_5 --allow-data-gaps` | 6,324 | $999.0020 | −$0.9980 | 395 | 0.8962 | $1.7690 |
| … `--direction SELL` | 1,054 | $1,000.5670 | +$0.5670 | 63 | 1.1484 | $1.4170 |
| … `--initial-balance 5000 --size 200` | 1,054 | $4,999.8320 | −$0.1680 | 63 | 0.9812 | $3.4860 |
| … `--time-frame HOUR_4 --start 2026-11-09` | 18 | $999.8790 | −$0.1210 | 1 | 0.0000 | $0.8200 |
| … `--time-frame MINUTE --start 2026-11-05 --allow-data-gaps` | 7,173 | $997.2180 | −$2.7820 | 470 | 0.5147 | $3.0410 |
| … commission 0.05, slippage 0.2, financing 0.0001, leverage 2 | 1,054 | $993.1622 | −$6.8378 | 63 | 0.2343 | $6.9555 |
| `donchian.py --backtest` | 18,674 | $983.8270 | −$16.1730 | 845 | 0.8595 | $22.4650 |
| `donchian.py --backtest --start 2025-01-01 --end 2025-04-01` | 1,515 | $997.9510 | −$2.0490 | 68 | 0.8210 | $4.2990 |
| `donchian.py --backtest --start 2026-03-27 --end 2026-11-01` | 3,741 | $996.5710 | −$3.4290 | 174 | 0.8150 | $6.0840 |

  Every row was identical before and after (report.json values, trades.csv,
  equity.csv, equity.html; Donchian trade lists and equity curves compared
  value-for-value). The unified CLI reproduces each row for the same options;
  `--strategy donchian --cost-case high` gives net −$39.6808 on the same trades.
- **Review corrections**: the 201 study runs, the pinned fixture, the snapshot
  coverage audit and all legacy command numbers are unchanged by the gap,
  reversal-financing, gross P&L and holdout corrections (the study has no
  sub-30m data and no direct reversals). Only Gross P&L of runs ending with an
  open bid/ask position changed, by exactly the open valuation spread.
- **Runtime** (best of 3, same machine): `donchian.py --backtest` 7.09 s → 6.81 s
  (quote rebuild computed once per candle); HMA default 0.52 s → 0.51 s.

## Adding a strategy

1. Write a pure class in `strategies/` with `name`, `warmup` and
   `target(completed_candles[, position])`. No broker imports.
2. Add a `StrategyAdapter` to `backtesting/adapters.py`: parameters (flags,
   defaults), factory, description, signal price and default timeframe/data
   source/execution model.
3. Add regression tests (pinned results, no-look-ahead, CLI defaults).

Data, execution, costs, reports, safeguards and CLI options come for free.

Strategies that manage many orders rather than one target position (the fixed
grid) declare `execution="grid"` in their adapter and run on `GridExecution`
(`backtesting/grid.py`), which reuses the same data, cost options, statistics and
report contract. See [grid backtesting](grid-backtesting.md).

## Other strategies: integration assessment

None were added, because their exact live semantics cannot yet be reproduced
and tested on completed candles without inventing behavior.

- **SMA (`sma.py`) and moving average (`moving_average.py`)**: poll every 60 s and
  push the latest one-minute *open* ask into a rolling list (not completed-candle
  closes). Signals call `createPosition` for the opposite side without closing the
  existing position, so the resulting exposure depends on the account's
  hedging/netting mode; `moving_average.py` also has a blocking initial wait loop.
  Needed: a decision on polling vs completed-candle timing, the open-price input,
  and the broker netting rule, then an adapter plus reversal-semantics tests.
- **EMA + HMA (`ema_hma.py`)**: closest candidate. Single timeframe (default 5m),
  ask closes; EMA(200)/EMA(20) over exactly the last 200 prices (so EMA200 is
  just its SMA seed) and HMA16 over 48. When both agree it opens that side only if
  flat (an existing opposite position is held); when they disagree it closes.
  This maps to a position-aware `target`, but the live runner reads the latest 200
  candles at wall-clock targets, which may include the forming candle. Needed:
  confirm the completed-candle convention (as was accepted for HMA), then pin the
  hold-on-agreement rule and the 200-price EMA window in tests.
- **Multiple HMA (`multiple_hma.py`)**: combines a 4H vote (6 SMAs, 6 EMAs and an
  HMA27 slope over 210 prices; it can return `"Neutral"`) with a 30m HMA20 slope.
  Needed: multi-timeframe support (aligning the latest *completed* 4H candle at
  every 30m decision without look-ahead), a defined `"Neutral"` behavior and
  210-candle warm-up on 4H data.

## Limitations and next steps

- Candle-level execution; no ticks or partial fills. Only the grid model orders
  intrabar limit fills, by a documented assumed candle path.
- EURUSD/USD unit contracts only; Dukascopy/yfinance mappings are EURUSD-only.
- Snapshot data covers the study interval only; Capital.com DEMO history
  acquisition failed (HTTP 404) during the study.
- Financing and commissions are assumptions or unmodeled, not broker schedules.
- No exceptional-holiday calendar; such gaps surface as audit failures or gap events.
- Downloads with warm-ups above 48 candles are cached separately (Dukascopy
  bucket caching still avoids re-downloading most raw data; yfinance does not).
- Next: multi-timeframe support (Multiple HMA), the EMA+HMA adapter once the
  timing convention is confirmed, user-supplied snapshot export from provider data,
  and broker cost schedules.

# Historical HMA backtesting

Backtesting downloads public historical market data and simulates trades locally.
It never loads Capital.com credentials, constructs a broker client, arms execution
gates or writes trading journals. DEMO and LIVE strategy execution remain separate.
No actual trades are part of the validation described here.

## Commands and parameters

```bash
uv sync --locked
uv run hma.py --backtest
uv run hma.py --backtest --time-frame MINUTE_5
uv run hma.py --backtest --data-source yfinance
uv run hma.py --backtest --initial-balance 5000 --size 200
uv run hma.py --backtest --direction SELL --hma-period 20
uv run scripts/backtest.py --end 2026-11-12
```

| Parameter | Default | Meaning |
| --- | --- | --- |
| `--backtest` | off in HMA | Historical simulation instead of online execution |
| `--data-source` | `dukascopy` | Explicit provider; never falls back |
| `--lookback-days` | 30 | Calendar days before end; positive integer |
| `--initial-balance` | 1000 | Initial USD cash |
| `--size` | 100 | EUR base units, not USD spending |
| `--symbol` | `EURUSD` | Only verified EURUSD unit contracts supported |
| `--time-frame`, `--time_frame` | `MINUTE_30` | Completed UTC candle timeframe |
| `--hma-period`, `--hma_period` | 16 | Existing HMA period, integer >= 2 |
| `--direction` | `BUY` | Existing configured-side/flat rule; `SELL` supported |
| `--start` | end minus lookback | Inclusive date or timezone-aware ISO timestamp |
| `--end` | today's UTC midnight | Exclusive end of last complete UTC calendar day |
| `--leverage` | 1 | Explicit simulation collateral ratio, >= 1 |
| `--commission-per-order` | unavailable | Assumed USD charge per entry and exit |
| `--slippage-pips` | unavailable | Assumed adverse slippage per fill; EURUSD pip = 0.0001 |
| `--financing-usd-per-unit-day` | unavailable | Assumed linear debit per EUR/calendar day |
| `--cache-dir` | `.cache/backtests` | Public historical-data cache |
| `--output-dir` | `backtests` | Unique directory for each run's reports |
| `--refresh-data` | off | Replace cached provider data explicitly |
| `--allow-data-gaps` | off | Explicitly accept unexplained missing intervals, never fill them |

Boundaries must align with the requested UTC timeframe and end in the past.
Warm-up data is fetched before start, checked and excluded from performance.
Dates use UTC, even when your workstation uses a different timezone.
Broker account/risk flags, `--REAL`/`--real`, `--read-only`, `--cycles`, `--days`
and scheduling overrides conflict with backtesting and are rejected. Simulation-only
arguments without `--backtest` are rejected to avoid accidentally starting trading.
`scripts/backtest.py` and installed `trading-bot-backtest` are historical-only CLIs.

## Providers and intervals

| Timeframe | Dukascopy source candles | Yahoo source candles | Yahoo approximate retention including warm-up |
| --- | --- | --- | --- |
| `MINUTE` | minute bid/ask | 1m | 30 days, chunks <= 7 days |
| `MINUTE_5` | minute bid/ask | 5m | 60 days |
| `MINUTE_15` | minute bid/ask | 15m | 60 days |
| `MINUTE_30` | minute bid/ask | 15m | 60 days |
| `HOUR` | hourly bid/ask | 1h | 730 days |
| `HOUR_4` | hourly bid/ask | 1h | 730 days |
| `DAY` | hourly bid/ask | 1h | 730 days |

Yahoo restrictions apply to the full padded download, not only the reported period.
For example its one-minute 30-day request cannot include required warm-up and is
rejected; use a shorter lookback. Five/15-minute downloads are split into <=30-day
chunks; hourly downloads into <=60-day chunks. Provider restrictions can change;
empty/rejected chunks cause explicit errors. A fresh request is never silently
shortened to fit retention. Cached complete datasets can be reused after retention
expires, but refreshing them may no longer be possible.

Dukascopy uses candle exports rather than large tick datasets. The implementation
follows the current JSON delta protocol used by maintained
[dukascopy-node](https://github.com/Leo4815162342/dukascopy-node), specifically
its `src/config/data-api.ts`, `src/url-generator` and `src/data-normaliser`
implementations. This is the website export interface at
`jetta.dukascopy.com/v1/candles`, with export-widget request headers, daily minute
and monthly hourly bid/ask buckets. Completed buckets are immutable cache entries;
active buckets use the export `from` parameter. Requests are serial, paced and
bounded by connection/read timeouts. HTTP challenges/rate limits fail explicitly;
there is no challenge bypass or fallback. This website interface is less stable
than a versioned, supported broker API and may require future adapter updates.
The investigated `dukascopy-python` 5.0.0 package raised an import-time NameError,
so it is not included as a dependency.

Yahoo uses maintained [yfinance Ticker.history](https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.history.html)
with `EURUSD=X`, no auto-adjustment, actions or repair. The
[upstream history implementation](https://github.com/ranaroussi/yfinance/blob/main/yfinance/scrapers/history.py)
documents the intraday constraints and 30-minute workaround. We aggregate 15m to
30m ourselves and hourly data to H4/day to guarantee UTC rather than exchange-local
boundaries. Daily downloads consequently share the hourly retention limit.
Yahoo quotes are indicative OHLC, not historical executable bid/ask. Results omit
unknown historical spread and cannot be compared directly with executable quote results.

## Candle integrity and gaps

`Candle.timestamp` is the timezone-aware UTC interval **start**; `end` is exclusive.
`prices` contains OHLC, with separate `bid`/`ask` OHLC when available, symbol,
timeframe, source, first actual observation time and source observation count.
Dukascopy indicator prices are ask closes, matching the existing Capital.com HMA
input. Yahoo uses its indicative closes.

Normalization sorts, removes identical duplicates, rejects conflicting duplicates,
nonfinite/nonpositive/incoherent OHLC, crossed bid/ask and misaligned timestamps.
Aggregation takes first open, maximum high, minimum low and last close separately
for bid and ask. Only completed buckets are eligible. First quote time is retained
for sparse buckets, so execution cannot occur before an available observation.
No candles or prices are forward-filled.

The coverage audit checks the actual performance period and the required warm-up,
not unused download padding. Dukascopy uses the DST-aware New York Sunday 17:00
through Friday 17:00 FX week. Explicit time-delta jumps within complete matching
bid/ask packets establish provider-declared no-quote ranges. These gaps are counted
and skipped without creating tradable prices. A successful HTTP status alone does
not excuse missing buckets, missing sides or unexplained truncation. Yahoo has no
equivalent authoritative quote-absence declaration; both source and target gaps
in its conservative UTC weekday core are checked. Sunday and Friday evening
Yahoo edges are explicitly unverified. Holidays/halts outside the weekend model
may require `--allow-data-gaps`; accepted gaps remain visible in reports.

Reports include requested/actual periods, used/downloaded warm-up counts,
performance candle count, sparse source-slot counts, missing/no-quote counts,
calendar assumptions and SHA256 of normalized used observations. Partial intrabar
quote absence remains visible; OHLC alone cannot prove tick-level completeness.
Cache snapshots are atomic, integrity-checked and versioned; corrupt caches fail
until explicitly refreshed. Neither provider selection nor failures trigger fallback.

## Strategy and accounting

The existing weighted/Hull moving averages remain in `strategies/indicators.py`.
Both live and historical HMA call the same pure slope signal: strictly rising is
BUY, flat/falling is SELL. The requested side is held while its signal matches;
the opposite signal closes to flat, rather than opening the other side. Default
warm-up is exactly 3 * HMA period, matching the live history window.

Only past completed candles enter each signal window. A target calculated at a
close executes at the next actually available candle open. Warm-up can prime the
first performance open, without generating warm-up P&L. Positions persist across
closures; the next observed quote supplies the next execution opportunity. The last
close's pending target is not executed without a subsequent historical open.

For EURUSD size 100 means exposure to 100 EUR. Long entry uses ask and exit bid;
short entry uses bid and exit ask. Gross USD P&L is `(exit - entry) * size` for
longs and `(entry - exit) * size` for shorts. Adverse configured slippage changes
fill prices. Spread and slippage are embedded once, never subtracted again.

Balance = initial cash + realized net cash P&L, including already paid entry fees
and financing for still-open positions. Equity = balance + unrealized liquidation-side
P&L. Margin = fixed entry notional / configured leverage; available funds = equity
minus margin. Margin/notional is collateral, not a trading expense. An entry that
cannot fund margin after commission and immediate spread/slippage is rejected
without a charge. No broker leverage or stop-out policy is invented: negative
available funds during holding are reported, and new underfunded entries rejected.

Commission/financing debits are cash costs. Optional financing accrues over actual
calendar time, including weekends through requested end; it is an assumption, not
a broker swap model. Missing costs are labeled **unmodeled**, not asserted to be
zero; the arithmetic omits them and a total cost estimate is unavailable. Explicit
zero assumptions are allowed and recorded. Executed-leg half-spread and slippage
estimates are reported separately, including entry costs of open positions.

Positions are **not forcibly liquidated** at the end. Balance, equity, unrealized
P&L, margin and remaining exposure are reported separately. Final valuation uses
the last observed completed liquidation-side close; its interval-end timestamp is
retained separately from the requested end if history stops earlier.
Trade statistics use closed net trades; win rate includes breakevens in its
denominator. Profit factor uses positive net trades / absolute negative net trades,
with explicit infinite/undefined states. Drawdown uses observed open/close equity,
not an invented intrabar path or a guaranteed tick-level maximum.

## Architecture and compatibility

```text
src/trading_bot/backtesting/
  __init__.py       Historical package boundary
  models.py         UTC OHLC, Candle and Dataset contracts
  timeframes.py     Normalization and separate bid/ask resampling
  calendar.py       Session/gap coverage audit
  cache.py          Atomic public-data cache and private-state path exclusions
  data.py           Provider selection, warm-up and validated snapshots
  providers/
    __init__.py     Explicit provider factory
    base.py         HistoricalProvider protocol
    dukascopy.py    Candle export decoder and bounded HTTP transport
    yahoo.py        yfinance adapter, chunks and retention validation
  engine.py         Strategy protocol, Decimal ledger and event loop
  strategy.py       Shared HMA signal adapter
  reporting.py      Statistics, JSON/CSV and interactive equity chart
  cli.py            Historical-only argument validation and orchestration
```

Additional strategies implement `Strategy.target(completed_window)` and declare a
warm-up length. Data acquisition and portfolio accounting remain reusable.
`strategies/backtest.py` preserves old supplied-price functions over this same
engine/ledger. That deprecated one-unit quote-buffer interface intentionally retains
its old optimistic same-observation fills and two-sided reversal semantics; it is
not the new historical HMA mode. `test_strategy.py` still maps to the authenticated
read-only `tools/history_probe.py`; `test.py` is unchanged.

The only added direct runtime dependency is `yfinance>=0.2.66,<2` (locked at 1.8.0),
with its locked transitive dependencies. Dukascopy reuses requests. Python >=3.12
and the existing uv/setuptools package layout are preserved.

## Reports and reproducibility

Each ignored `backtests/<UTC-run-id>/` contains `report.json`, `trades.csv`,
`equity.csv` and standalone interactive `equity.html` (no external scripts/assets).
JSON preserves Decimal values as strings; timestamps carry UTC offsets. The report
includes trades, decisions, equity observations, open positions and rejected entries.
Use fixed dates and retain the public cache/data fingerprint to reproduce a run;
an upstream provider can revise its own history after refresh.

Real downloads and full runs were verified over **2026-10-13 00:00 UTC through
2026-11-12 00:00 UTC**, 30 calendar days, HMA16 BUY/flat, 100 EUR, USD1,000:

| Metric | Dukascopy bid/ask | Yahoo indicative |
| --- | --- | --- |
| Performance candles / used warm-up | 1,054 / 48 | 1,040 / 48 |
| Final balance | $999.9250 | $1,000.6818 |
| Final equity | $999.9160 | $1,000.6818 |
| Net P&L including open valuation | -$0.0840 | +$0.6818 |
| Return | -0.0084% | +0.0682% |
| Closed trades | 63 | 63 |
| Winners / losers / breakevens | 24 / 39 / 0 | 26 / 35 / 2 |
| Win rate | 38.0952% | 41.2698% |
| Profit factor | 0.9812 | 1.1971 |
| Observed max drawdown | $1.7430 / 0.1742% | $1.1001 / 0.1099% |
| Remaining simulated positions | 1 BUY, 100 EUR | 1 BUY, 100 EUR |
| Realized / unrealized cash P&L | -$0.0750 / -$0.0090 | +$0.6818 / $0.0000 |

Neither result includes unknown commission, financing or slippage; Yahoo also
omits unknown spread. Both passed strict target-interval checks. Dukascopy reports
77 absent source-minute slots inside used aggregate candles without inventing
quotes. Additional real Dukascopy runs covered MINUTE_5 (6,324 performance candles,
395 closed trades, final balance/equity $999.0020), SELL mode and custom capital/size.

```bash
uv run hma.py --backtest --end 2026-11-12
uv run hma.py --backtest --data-source yfinance --end 2026-11-12
uv run hma.py --backtest --time-frame MINUTE_5 --end 2026-11-12
uv run scripts/check_offline.py
```

Regression tests cover independent long/short cash calculations, spreads, costs,
financing, margin rejection, warm-up, HMA parity, future-prefix invariance, next-open
fills, end exposure, drawdown, provider schemas/errors/retention, UTC resampling,
duplicate/missing candles, caches, CLI defaults/conflicts and report reproducibility.
A fresh interpreter runs a cache-backed CLI under socket, credential/private-state
and broker-import guards. External provider calls alone are replaced in deterministic
tests; the actual strategy, ledger, data validation and reports execute normally.

Remaining accuracy limits are candle-level rather than tick-level execution,
unknown quote timing within source candles, provider calendar/holiday uncertainty,
indicative Yahoo quotes, unmodeled broker-specific costs/stop-outs and no contract
mapping beyond EURUSD/USD. Useful next additions are user-supplied validated data
files, explicit holiday calendars, broker cost schedules and additional strategy
adapters using the existing engine.

A real seven-day Dukascopy `MINUTE` diagnostic (ending 2026-11-12) was rejected in
strict mode because five active source intervals were missing without an explicit
within-packet quote-absence declaration, first 2026-11-05 23:59 UTC. This is an
intentional completeness failure, not silently repaired data. Use
`--allow-data-gaps` only when accepting the reported limitation; missing intervals
stay absent and execution waits for the next available quote. Other historical
periods and holidays may expose similar provider limitations.

## Implementation verification and files

Final offline validation passed **430 tests**, including 36 historical-backtesting
tests, with no failures or skips. `uv sync --locked`, `uv pip check --python
.venv/bin/python`, `uv run scripts/check_environment.py` and `uv build` passed.
The wheel includes all 14 historical package modules and was imported from an
unrelated temporary directory with socket/credential guards. Ruff unused-name
checks and `git diff --check` passed. Independent Decimal calculations from the
real Dukascopy/Yahoo trade CSVs reconciled each trade, final cash/equity/exposure
and observed drawdown; every saved equity point satisfied the accounting identity.

New files are the 14 `backtesting/` modules listed above,
`src/trading_bot/tools/history_probe.py`, `tests/test_historical_backtesting.py`
and this document. Modified files for this task are:

- `.gitignore`, `README.md`, `pyproject.toml`, `uv.lock`.
- `docs/integration-testing.md`, `docs/refactoring.md`.
- `src/trading_bot/strategies/hma.py`, `indicators.py`, `backtest.py`.
- `src/trading_bot/tools/backtest.py`, root `test_strategy.py`.
- `tests/test_backtest.py`, `tests/test_package_layout.py`.

Earlier uncommitted session-lifecycle changes elsewhere in the checkout were
preserved. No files were deleted or renamed. `test.py` stayed byte-for-byte
unchanged (SHA256 `8f47674acd933b2461cac5d04fbe02bbdc4e625e3f659153823abe0c9bb01a0f`).
Generated datasets/reports, `.env` and private trading state are excluded from Git.
No commit, push, Capital.com authentication, actual order or LIVE request was
performed for this historical-backtesting task.

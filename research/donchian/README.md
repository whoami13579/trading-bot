# EURUSD Donchian research

Implemented and evaluated on 2026-11-13. The **primary 1H strategy does not have
enough evidence to warrant DEMO forward testing under the preregistered criteria**.
It lost before costs as well as after costs. No lookback or timeframe was selected
using the holdout. Existing HMA execution, scheduler and trading defaults were
not edited. All fills are simulated; no broker orders were submitted.

## Observed result

EURUSD, fixed 100 EUR positions, USD 1,000 per scored window, unlevered. The four
development validations cover 2025-01-12 through 2026-03-27. The initial training
period starts 2023-11-01; it is unscored. Final holdout is
**[2026-03-27 00:00, 2026-11-01 00:00) UTC**, 219 of 1,096 calendar days.

| Primary 1H 20/10, base costs | Four validations pooled | Final holdout |
|---|---:|---:|
| Trades | 336 | 174 |
| Midpoint gross P&L, USD | -4.7770 | -2.2805 |
| Modeled costs, USD | 2.3310 | 1.1485 |
| Net P&L, USD | -7.1080 | -3.4290 |
| Max observed drawdown, USD | 9.2260 | 6.0840 |
| Net profit factor | 0.8661 | 0.8150 |
| Calendar exposure | 68.33% | 71.44% |
| Higher-cost net P&L, USD | -16.8835 | -8.3898 |
| Base execution + financing sensitivity net, USD | -12.5365 | -6.1973 |

Only one primary validation window was positive. All four nearby 1H settings
lost in aggregate development: 15/10 -$7.953, 25/10 -$3.369, 20/8 -$6.425,
20/12 -$10.372. The top five development winners contributed $10.297 (22.40%
of positive trade P&L); removing them leaves -$17.405. Development exit-year
P&L was -$7.255 in 2025 and +$0.147 in 2026. Full window/year and holdout
concentration results are in [RESULTS.md](RESULTS.md).

30m holdout net was +$3.708 base and -$2.6181 high costs; its development base
net was -$14.328. 4H holdout was -$5.821 base, with only 47 trades (85 in
development), so its trade sample is limited. Its small development gain of
$0.484 became -$5.4954 at high costs. The primary timeframe remains 1H.
HMA16 BUY/flat on the 1H holdout made +$0.977 base and lost $3.528 high costs.
Cash returned zero. These are descriptive comparisons, without significance claims.

## Data, coverage and execution

The initial Capital.com DEMO history acquisition failed with HTTP 404. Acquisition
uses the existing `TradingApi`, explicitly DEMO, wrapped to allow only login and
EURUSD price reads. There is no LIVE option or broker order path in this study.
Cached repository history covered only about one month. The alternate source is
[Dukascopy historical bid/ask data](https://www.dukascopy.com/api/data/get/historical-data-export),
downloaded through the repository's existing decoder/cache and its
`https://jetta.dukascopy.com/v1/candles` interface. Quote levels and spreads can
differ from Capital.com CFD prices; results are not a Capital.com execution replay.
[Capital.com's historical price specification](https://open-api.capital.com/)
defines `snapshotTimeUTC` and separate bid/ask OHLC. The implemented Capital
parser uses those fields, rather than local `snapshotTime`.

The immutable normalized snapshot is
`.cache/donchian/eurusd-30m.json.gz`, SHA256 of canonical candle content:
`f7ad8f615eb2dacd9a79417934f99f09a18da32f3c291f37ed605c4db1eda72a`.
It has 38,020 bars including warm-up from 2023-10-12 00:00 UTC. All timeframes
derive from the same minute bid/ask packets, aggregated first to 30m. Source
prices are ask OHLC for HMA compatibility; Donchian explicitly uses midpoint closes.

| TF | Usable bars in common interval | First candle start UTC | Last candle end UTC | 20/10 trading-time duration |
|---|---:|---|---|---|
| 30m | 37,348 | 2023-11-01 00:00 | 2026-10-30 21:00 | 10h / 5h |
| **1H** | **18,674** | **2023-11-01 00:00** | **2026-10-30 21:00** | **20h / 10h** |
| 4H | 4,820 | 2023-11-01 00:00 | 2026-10-31 00:00 | 80h / 40h |

The common evaluation boundary is 2026-11-01 00:00 UTC for every timeframe;
the market was closed at that boundary. All cover three calendar years closely
enough for the planned comparison. Nine existing incomplete 4H aggregates were
excluded. The 4H last nominal interval ends Saturday 00:00, although its source
quotes stop Friday 21:00; interval-end labels are not exact tick times. Boundary
quote ages in metrics use nominal timeframe interval ends, so see the raw-source
last timestamp when assessing staleness.

Audit of the snapshot including warm-up found zero duplicate/incomplete candles,
230 missing expected active 30m slots, 2,392 sparse 30m bars and 5,470 absent
minute slots inside observed bars. Seven unexpected gap events remain in each
scored timeframe. They include holiday quote absences; the calendar models only
the DST-aware New York Sunday 17:00–Friday 17:00 week. No forward filling or
claims of tick completeness. Full missing timestamps are in `study.json`.

Signals are evaluated after completed closes, with the current close excluded
from both channels. A close used to exit cannot enter the other side; the next
completed close may signal an entry, filled at its following observed open.
Warm-up never places an entry at a window's first open. Unexpected active gaps
cancel pending entries, retain pending exits for the next quote, and reset signal
warm-up. Positions carry through gaps, incurring financing in sensitivity cases.
Scheduled weekend closures retain queued signals for the next actual open.
Observed sparse bars remain usable; missing constituent 30m bars exclude an
aggregate. Execution retains the first actual observation time.

Buys use ask plus adverse slippage; sells use bid minus slippage. Base costs use
historical spread plus 0.1 pip per leg, zero assumed commission, and **unavailable
historical financing omitted**. High costs double each observed spread around
midpoint, use 0.5 pip per leg, and add financing sensitivity. A third scenario
isolates financing at base execution costs. Both sensitivity cases charge 5%
annualized entry midpoint USD notional / 360 at NY 17:00 weekdays, triple on
Wednesday, equally for long/short. This is an assumed stress, not broker history.
No midpoint-only data was used; that code path assumes 1 pip base / 2 pips high
full spread. Spread/slippage are included once in fills and decomposed in exports.

Each scored window starts flat and ends with a costed synthetic liquidation at
the last completed candle close. This valuation may be stale or unavailable as a
real execution; no trade crosses a scored boundary. Rollover and exposure stop
at that synthetic close. Final equity is flat cash. Drawdown observes opens and
closes, and cannot measure intrabar/tick extremes. Aggregate development drawdown
chains validation equity with prior realized P&L while each window's actual
simulation resets to USD 1,000. Exposure is calendar holding time / scored time.

## Single backtest CLI

From the repository root, this verified command runs **one** continuous backtest:

```bash
uv run src/trading_bot/strategies/donchian.py --backtest
uv run src/trading_bot/strategies/donchian.py --help
```

Defaults are fixed at EURUSD, size 100 EUR, DEMO, primary 1H, 20/10 bars,
USD 1,000 initial cash, unlevered, and the documented base costs. The default
scored range is [2023-11-01, 2026-11-01) UTC. It includes the original study's
training period and uses one flat start and one final liquidation, so its totals
differ from the study's separately reset validation/holdout windows. It does not
run the research stages, compare parameters, or change saved research artifacts.

The input must be the normalized, integrity-checked gzip JSON study snapshot
`.cache/donchian/eurusd-30m.json.gz`, including warm-up from 2023-10-12 and history
through 2026-11-01 (scheduled closures allowed). Raw CSVs and provider packets
are not this snapshot format. Use `--snapshot PATH` to select another prepared
snapshot. The CLI validates it with the existing study loader, resamples/audits
with `prepare_frames`, then calls `study_engine.simulate` exactly once with
`DonchianStrategy(20, 10)` and `COSTS[0]`. It does not authenticate, download,
read credentials, or place broker orders.

`--start` and `--end` optionally select an hour-aligned UTC subset of the study
interval. The selected range needs completed 1H candles and at least 20 preceding
completed 1H bars for warm-up. A valid zero-trade run prints the full summary and
exits 0. Missing, corrupt or insufficient data prints an actionable error to
stderr and exits 1; invalid arguments exit 2. Prepare a missing snapshot explicitly:

```bash
uv run -m trading_bot.backtesting.study_data --source capital-demo
# If DEMO history is unavailable, explicitly select the documented alternate:
uv run -m trading_bot.backtesting.study_data --source dukascopy
```

An existing invalid snapshot must be moved aside, or a new `--snapshot PATH`
chosen for preparation and execution. Acquisition does not overwrite snapshots.

Before the CLI fix, the exact command exited **0**, stdout `''`, stderr `''`.
`donchian.py` defined strategy logic only: it had neither an argument parser nor
an `if __name__ == "__main__"` entry point, so it never parsed `--backtest`.
The research entry point was the separate `backtesting.donchian_study` module.
HMA already dispatches `--backtest` through its own `main()`; Donchian now follows
that entry-point convention with an offline single-run adapter. HMA is unchanged.

Verified stdout from the exact command with the existing cached snapshot:

```text
========== BACKTEST REPORT ==========
Strategy:                Donchian (long / short), 20/10 bars
Symbol:                  EURUSD
Size:                    100 EUR
Environment:             DEMO (simulated only)
Data source:             dukascopy (cached historical bid/ask)
Timeframe:               HOUR (1H)
Historical days:         1096.0
Period:                  2023-11-01T00:00:00+00:00 to 2026-11-01T00:00:00+00:00 (UTC, end exclusive)
Candles:                 18674
Warm-up candles:         21
Last candle end:         2026-10-30T21:00:00+00:00
Initial balance:         $1,000.0000
Final balance:           $983.8270
Final equity:            $983.8270
Gross P&L:               $-11.1315
Net P&L:                 $-16.1730
Total return:            -1.6173%
Total closed trades:     845
Winning trades:          303
Losing trades:           542
Win rate:                35.8580%
Profit factor:           0.8595
Max drawdown:            $22.4650 / 2.2331%
Realized cash P&L:       $-16.1730
Unrealized P&L:          $0.0000
Modeled costs:           $5.0415
Remaining positions:     0
Remaining base units:    0
Remaining USD notional:  $0.0000
Available funds:         $983.8270
Rejected entries:        0
Gross P&L uses midpoint prices. All orders are simulated.
Base costs: spread + 0.1 pip/leg slippage; commission assumed zero; historical financing omitted.
=====================================
```

The terminal layout reuses HMA's `reporting.py` currency/percentage formatting,
24-character label columns, report header and closing divider. Metrics with
shared meanings keep HMA's labels and relative order. Account fields come from
the runner's computed statistics. This display update preserves the cached run's
18,674 candles, 845 trades, -$11.1315 gross, $5.0415 modeled costs, -$16.1730 net,
and $22.4650 drawdown. HMA's exact command was run before and after the shared
formatter extraction: its report was identical except for the generated results
directory. Rendering the same saved HMA report/path also matched byte for byte.

Exit status **0**, stderr empty. CLI regression checks use real synthetic
snapshots and fresh interpreters with network, broker imports and credential
reads blocked; they cover aligned fields, HMA field ordering, dollar/percentage
formatting, computed account fields, missing input, insufficient warm-up, zero
trades, and help. Run them with:

```bash
uv run python -m unittest tests.test_donchian_cli -v
uv run scripts/check_offline.py
```

CLI verification passed all **5 targeted tests** and **454 offline tests**.
An explicit missing-snapshot invocation returned status 1, empty stdout, and
stderr identifying the expected snapshot and both preparation commands, without
a traceback. `--help` returned status 0 and documented the supported options and
defaults. The unchanged research entry point also successfully completed a fresh
`prepare` in a temporary directory, without calculating strategy performance.

The CLI fix changes `src/trading_bot/strategies/donchian.py`, adds
`src/trading_bot/backtesting/donchian_cli.py` and `tests/test_donchian_cli.py`,
and updates this README. Strategy rules, study runner, source snapshot and saved
research results are unchanged. Archived prepared study directories still reject
changed source hashes; the reproduction command below uses a fresh directory and
records the current source hashes as before.

## Reproduce

Use the saved snapshot to avoid revisions to upstream history. From repository
root, after the existing `uv sync --locked` setup, use a fresh output directory:

```bash
for stage in prepare development freeze holdout report; do
  .venv/bin/python -m trading_bot.backtesting.donchian_study "$stage" \
    --snapshot .cache/donchian/eurusd-30m.json.gz \
    --output backtests/donchian-reproduction || break
done
.venv/bin/python research/donchian/verify_results.py backtests/donchian-reproduction
.venv/bin/python scripts/check_offline.py
```

The primary study's output is `backtests/donchian-eurusd-final`. Its holdout has
already run once; a repeat in that directory is rejected. The command above is
an exact-configuration reproduction in a new directory, not a new model search.
Code, plan and data hashes are checked against preparation before subsequent
stages. The freeze artifact also binds the development results before holdout.
No parameter changes were made after development or holdout.

Fresh acquisition is explicit and separate (provider revisions may change data):

```bash
.venv/bin/python -m trading_bot.backtesting.study_data --source capital-demo --snapshot /tmp/eurusd-capital.json.gz
# If unavailable, explicitly select the alternate:
.venv/bin/python -m trading_bot.backtesting.study_data --source dukascopy --snapshot /tmp/eurusd-dukascopy.json.gz
```

The Capital DEMO command failed for this run; Dukascopy succeeded. Completed
daily public packets are integrity-checked in `.cache/backtests`. Four concurrent
daily fetches bound memory use; HTTP errors/challenges abort, with no automatic
provider fallback. Existing snapshots cannot be overwritten by acquisition.

## Documentation and local artifacts

- [PLAN.md](PLAN.md): recorded before strategy performance; fixed rules, date
  splitting, costs, search budget, execution and decision criteria.
- [RESULTS.md](RESULTS.md): all primary windows, every pooled nearby setting,
  HMA/cash comparisons, concentration, and the observed holdout separately.
- [verify_results.py](verify_results.py): independently checks exported fills,
  costs, rollover, signals, equity and split containment without rerunning trades.

The normalized snapshot, downloaded packets, generated reports, trade/equity
CSVs, manifests and attempt logs are **local inputs/outputs, excluded from Git**.
They are not included in the PR. A fresh checkout needs the snapshot prepared
with the acquisition command above before either backtest command can run.
The original study outputs remain locally under `backtests/donchian-eurusd-final`;
the reproduction command writes equivalent artifacts under its selected output
directory. That directory contains:

- `study.json` and `freeze.json`: exact data/code/plan fingerprints, coverage
  audit, expanding training dates and pre-holdout freeze.
- `development.csv` and `holdout.csv`: every run with counts, gross/net P&L,
  component costs, drawdown, profit factor and exposure.
- `attempts.jsonl`: all 180 development and 21 holdout attempts. Nearby settings
  never enter holdout.
- `verification.json`: the independent trade/accounting audit. Cost variants
  repeat market trades and are not independent observations.
- `simulated_trades.csv`: combined export with run/window/strategy/cost
  identifiers, signal/fill times, direction, size, prices, financing and boundary
  flags. Individual trade/equity/configuration files are in `runs/`.

The initial study's audit reconciled 201 runs and 20,798 trade records. Optional
review copies of generated CSV/JSON/JSONL files in this directory also stay local
and are ignored. The committed results summary preserves the observed findings.

## Commands actually run

Commands run for this study:

```text
.venv/bin/python -m trading_bot.backtesting.study_data
.venv/bin/python -m trading_bot.backtesting.study_data --source dukascopy
.venv/bin/python -m unittest tests.test_donchian_study -v
.venv/bin/python -m unittest tests.test_donchian_study -q
.venv/bin/python scripts/check_offline.py
.venv/bin/python -m trading_bot.backtesting.donchian_study prepare --output backtests/donchian-eurusd-final
.venv/bin/python -m trading_bot.backtesting.donchian_study development --output backtests/donchian-eurusd-final
.venv/bin/python -m trading_bot.backtesting.donchian_study freeze --output backtests/donchian-eurusd-final
.venv/bin/python -m trading_bot.backtesting.donchian_study holdout --output backtests/donchian-eurusd-final
.venv/bin/python -m trading_bot.backtesting.donchian_study report --output backtests/donchian-eurusd-final
.venv/bin/python research/donchian/verify_results.py
git diff --check
```

The original sequential Dukascopy acquisition was interrupted before any
performance run and replaced with bounded per-day aggregation; its cache was
reused. An initial data-only preparation in `backtests/donchian-eurusd` preceded
the final preparation. A synthetic empty-timeframe test initially exposed an
audit reporting error; it was fixed before development. No actual performance
run failed, no holdout was repeated, and all attempted configurations are retained.

Validation passed **449 offline tests**, including **14 new targeted tests**:
strict/midpoint channels, exit-before-entry, independent long/short base/high cost
arithmetic, next observed opens, warm-up and future-prefix isolation, flat split
boundaries, gap cancellation/exit carry, scheduled closures, DST Wednesday rollover,
snapshot integrity/completed bars, Capital UTC quote parsing, and holdout guards.
The audit additionally verified every exported trade's fills, costs, financing,
equity identity, channel signals and split containment, including a repeat audit
with socket access and credential-file reads blocked. Cache and full generated
outputs remain ignored; generated review copies in this directory stay local.
Only the source, tests, research plan, guide, results summary and verifier belong
in version control.

New implementation files are `src/trading_bot/strategies/donchian.py`,
`src/trading_bot/backtesting/study_data.py`, `study_engine.py`,
`donchian_study.py`, and `tests/test_donchian_study.py`. This directory adds the
plan, documentation, results summary and independent artifact verifier. Shared
terminal formatting in `src/trading_bot/backtesting/reporting.py` is extracted
without changing HMA's rendered report. Generated CSVs use the Python CSV
writer's standard CRLF endings; optional local review copies may use LF endings.

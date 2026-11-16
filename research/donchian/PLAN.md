# Preregistered EURUSD Donchian study

Recorded 2026-11-13 before calculating or inspecting any strategy performance.
This study is separate from all trading runners. EURUSD, 100 EUR fixed size,
USD 1,000 initial cash, unlevered simulated collateral; DEMO history only.
No broker orders, configuration changes, commits or pushes.

## Dates and search budget

Target common UTC interval: [2023-11-01, 2026-11-01), three years.
Acquire at least 25 extra 4H bars before the start for Donchian, 48 for HMA.
Initial training is the first 40% of calendar days; four consecutive validation
windows occupy 40–50%, 50–60%, 60–70%, 70–80%. Boundaries round to UTC days.
Latest approximately 20% (day-alignment rounding only) is the final holdout.
The command must record exact boundaries before simulation. If history cannot
cover this target, record the data-only reason and revised common range before
any performance run; explicitly flag less than three years.

Training expands from the fixed initial start to each validation start. Rules
are fixed, so there is no fitted parameter; earlier bars supply indicator warm-up.
Each scored window starts flat, ignores warm-up entry signals, and ends with a
costed synthetic liquidation at its last completed close. No position or pending
order crosses a scored boundary. Score the holdout once after writing a freeze
artifact. Reproduction uses the frozen snapshot, never refreshes the data.

Primary timeframe is 1H regardless of ranking. Robustness: 30m and UTC-aligned 4H.
Donchian primary 20/10: current midpoint close strictly above/below the preceding
20 closes enters long/short; long exits strictly below preceding 10 minimum,
short exits strictly above preceding 10 maximum. Current close excluded from
both channels. One position; exit decision first, no reversal on that close;
next entry decision may occur on the following completed bar, filled at its next
eligible open. Lookback durations: 30m 10h/5h, 1H 20h/10h, 4H 80h/40h,
measured in trading bars (weekends extend wall-clock durations).

Development only: report all 15/10, 25/10, 20/8, 20/12 runs on all timeframes
and both cost cases, with all four validation windows. No selection or retuning.
Also report current HMA16 BUY/flat (ask-close input, rolling 48 candles, existing
pure signal) and cash (zero yield, zero costs). HMA is a comparator adapted to
completed candles and common execution assumptions, not online runner timing.
No other parameters, instruments or timeframes will be searched.

## Data and costs

Prefer repository cache then existing Capital.com DEMO client; if inaccessible
or inadequate, explicitly document an alternate EURUSD feed. Retain immutable
snapshot plus SHA256, source URLs, retrieval times and acquisition failures.
Validate UTC alignment, duplicate/conflicting stamps, quote pairs, positive OHLC,
completed bars, missing expected active bars, sparse aggregates and gap durations.
Resample one finest-resolution source to all three timeframes where possible.
No forward filling. Weekend closures use New York Sunday 17:00–Friday 17:00,
DST-aware. Holidays are flagged rather than silently treated as scheduled.
Across scheduled closures queue signals for the next observed open. Across
unexpected active gaps cancel pending entry orders, keep positions marked at
last known quote, carry protective exits to the next observed open, reset signal
warm-up, and disclose gap exposure. No invented fills during missing data.

Buy at ask, sell at bid; adverse slippage separately. For midpoint-only data,
assume 1 pip full spread base / 2 pips high. With historical bid/ask preserve
observed spread base, double it around midpoint high. Slippage per leg: base
0.1 pip, high 0.5 pip. Explicit zero commission assumption (not a verified fee).
Historical financing is unavailable unless source supplies it: base assumes zero
and is labeled incomplete; high debits 5% annualized entry midpoint notional at
17:00 New York weekday rollover, triple Wednesday (360-day denominator), both
directions, including closure crossings. This is sensitivity, not broker swap.
Report additional primary 20/10 financing-only sensitivity at base execution
costs with the same 5% financing. Boundary liquidation uses last observed bid/ask
close plus slippage and costs; stale quote age disclosed. It is an evaluation
valuation convention, not a guaranteed executable boundary fill.

## Evaluation criteria fixed in advance

Report USD midpoint gross P&L, fill P&L, net P&L, decomposed spread/slippage/
commission/financing, trade count, net profit factor, observed equity max drawdown
(USD and %), and calendar-time exposure including weekends. Export every trade,
signal/fill timestamps, boundary flags, equity observations, run configuration and
year/window concentration; show top 1/5 winners and P&L excluding top 5 winners.
This is observed candle drawdown, not tick drawdown. Cash is flat USD 1,000.

Evidence sufficient to consider DEMO forward testing requires primary 1H base
net positive in at least 3/4 validations and final holdout; aggregate development
and holdout profit factors >1; at least 100 development and 30 holdout trades;
high-cost aggregate development and holdout net positive; most nearby settings
positive in aggregate development; aggregate development net remains positive
without top five winners. Failure is weak evidence, not a cue to tune holdout.
Robustness rankings never change primary timeframe. No claim of statistical
significance or live profitability follows from these descriptive thresholds.

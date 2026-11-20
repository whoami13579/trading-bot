# Candle-directed fixed-grid backtesting

`--strategy grid` adds a fixed-price EURUSD grid to the unified historical
backtester ([issue #5](https://github.com/whoami13579/trading-bot/issues/5)).
It is a **historical simulation only**: no Capital.com authentication, no
credentials, no broker client imports and no order submission. Every order in
a report is a simulated record. `--REAL`, `--account` and broker risk,
scheduling and read-only options are rejected before any data access, as for
the other strategies.

Code: `strategies/grid.py` (pure rules), `backtesting/grid.py` (order book,
fills, per-lot accounting), `GridExecution` in `backtesting/execution.py`, the
`GRID` adapter in `backtesting/adapters.py`. Tests:
`tests/test_grid_backtesting.py`.

## Reproducible EURUSD example

```bash
uv run trading-bot-backtest --strategy grid --symbol EURUSD --time-frame HOUR \
  --data-source dukascopy --start 2025-09-01 --end 2025-10-01 \
  --ema-period 50 --grid-lower 1.1500 --grid-upper 1.1900 --grid-intervals 40 \
  --level-size 100 --max-position 500 --slippage-pips 0.1
```

Verified on 2026-11-20 with public Dukascopy bid/ask candles. The first run downloaded
and cached the data; a second run read the cache and reproduced every value:

| Item | Value |
| --- | --- |
| Candles / warm-up | 528 scored 1H candles, 50 warm-up; 0 missing or unexplained intervals |
| `data_sha256` | `ccb89368919e88c37f0e149900236db97994b7465cbbea9533c035f69fcab37e` |
| `configuration_sha256` | `fb429a367d5aaf075d1c564908a5ff11c85b9525aeff8e4799559df89c607f4d` |
| Closed lots | 53 (21 grid exits, 32 direction-change flattens), 30 direction changes |
| Net / gross P&L | −$0.6120 / −$0.2665 on $1,000 |
| Max drawdown | $1.7460 (0.1746%) |
| Modeled costs | $0.3455 (spread estimate + 0.1-pip flatten slippage) |
| Peak exposure | 500 EUR in 5 lots ($584.00 entry notional) |
| Ending inventory | flat, long grid active with 5 working entries |

Commission and financing are **unmodeled** in this example (not assumed zero).
Add `--commission-per-order` and `--financing-usd-per-unit-day` to model them.
The result describes one month and one parameter set. It does not show that the
strategy has an edge.

An offline end-to-end run (no network) works on any study-format snapshot. Its
1H bars are aggregated from 30m bid/ask bars, so its numbers differ slightly from
the Dukascopy 1H run (net −$0.5800 rather than −$0.6120 for this period):

```bash
uv run trading-bot-backtest --strategy grid --data-source snapshot \
  --snapshot .cache/donchian/eurusd-30m.json.gz --start 2025-09-01 --end 2025-10-01 \
  --grid-lower 1.1500 --grid-upper 1.1900 --grid-intervals 40
```

## Parameters

| Option | Default | Meaning |
| --- | --- | --- |
| `--ema-period` | 50 | EMA lookback in completed candles (≥ 2) |
| `--grid-lower`, `--grid-upper` | required | Fixed price bounds, `0 < lower < upper` |
| `--grid-intervals` | 10 | Evenly spaced intervals (1–1000); there are `intervals + 1` levels |
| `--level-size` | 100 | EUR base units per grid level (replaces `--size`, which is rejected) |
| `--max-position` | 500 | Cap on open-lot units + working-entry units, in EUR (≥ level size) |
| `--time-frame` | `HOUR` | Any supported timeframe |
| `--data-source` | `dukascopy` | `dukascopy` (bid/ask), `yfinance` (indicative), `snapshot` |
| `--execution-model` | `grid` | The only model for grid strategies, and only for them |
| `--commission-per-order` | unmodeled | USD per **fill** (entry, exit and flatten fills) |
| `--slippage-pips` | unmodeled | Adverse slippage on **market (flatten) fills only** |
| `--financing-usd-per-unit-day` | unmodeled | Linear debit per lot unit per calendar day |
| `--initial-balance`, `--leverage` | 1000, 1 | USD cash; simulated collateral ratio |

Missing bounds, any explicit `--size` (even one equal to `--level-size`),
`--cost-case`, another model or another strategy's
parameters exit 2 before data access. Invalid values (`--ema-period 1`,
`upper <= lower`, `--max-position` below `--level-size`, levels that collapse
after rounding) exit 1 with `Backtest failed: …`.

Levels are `lower + i × (upper − lower) / intervals`, rounded to the 0.00001
EURUSD tick (half-even), and never move during a run.

## Direction rule

- Signal price: the midpoint close when bid/ask exists, otherwise the source close.
- EMA: seeded with the simple mean of the first `ema_period` closes of the loaded
  window, then `ema += 2/(period+1) × (close − ema)`. The loader supplies exactly
  `ema_period` completed warm-up candles before `--start`, so the EMA at the first
  scored candle is fully determined by the request and `data_sha256`.
- The strategy stays **flat** until the EMA exists. A completed close above the EMA
  selects the long grid; below selects the short grid; equal keeps the current
  direction (flat if none).
- The decision on the last warm-up close primes the first scored open (as in HMA
  continuous mode). Decisions are logged per scored close with `signal_close` and `ema`.

## Grid rules

- **Activation.** A new direction is acted on at the next observed open. Its
  reference price is the signal close that selected it. While still flat, a
  later close that confirms the direction becomes the reference.
- **Long grid.** Buy limits at every level strictly below the reference, except the
  top level, which has no exit level above it. A filled lot exits with a sell limit
  one level higher. When that exit fills, the buy is restored at its original level.
- **Short grid.** Sell limits at every level strictly above the reference, except the
  bottom level. A filled lot exits with a buy limit one level lower. When that exit
  fills, the sell is restored at its original level.
- **Direction change.** At the signal close, every working order of the old grid
  (entries and exits) is cancelled. At the next observed open, every old lot is
  closed with a market order: longs sell at bid − slippage, shorts buy at ask +
  slippage. Only after inventory is flat does the new grid activate at that same
  open. A change decided on the final scored close is reported as
  `pending_activation`, with old lots still open and their orders cancelled.
- Each level holds at most one of: nothing, one working entry, or one open lot. That
  makes duplicate fills and replenishments impossible. Invariants are checked after
  every candle.

## Fill rules (deterministic)

| Rule | Behavior |
| --- | --- |
| Quote side | Buys against the **ask** OHLC, sells against the **bid** OHLC (indicative OHLC without bid/ask) |
| Gap-through | A buy whose ask **open** ≤ limit fills at the ask open; a sell whose bid open ≥ limit fills at the bid open (better than the limit, never worse) |
| Touch | Otherwise a buy fills at its limit when ask low ≤ limit; a sell fills at its limit when bid high ≥ limit. Touching counts as a fill; queue position is not modeled |
| Multiple levels in one candle | Every touched eligible order fills at its own price. The midpoint candle picks one path for both quote series: open → nearer extreme (segment 1) → farther extreme (segment 2) → close; ties go low first. Buys below the open are reached only while falling and sells above it only while rising, so the path fixes each touch's segment. Within a segment, fills are ordered by the fraction of that segment the order's own bid or ask series has travelled. Gap fills come first. At the same path point, exits come before entries, then lower order id |
| New orders | Orders seeded or re-admitted at an open may fill in that candle, including a gap at that open. Orders **created by a fill** (a lot's exit, a restored entry) are eligible from the **next** candle, so no lot opens and closes inside one candle |
| Fill time | Gap fills: the candle's observed open time. Touch fills: the candle end, which is when the fill is known |

The path assumption only decides the *order* of fills within one candle, which
matters for funding checks. Prices and fill membership come from the observed
OHLC alone.

## Exposure and funding

- **Position cap:** open-lot units + working-entry units ≤ `--max-position`.
  Exit orders reduce exposure and are not counted. Levels nearest the reference
  are admitted first, and farther levels wait.
- **Admission** (each open, for empty levels of the active grid): requires
  `equity − lot margin − other reservations − commission ≥ level_size × limit / leverage`,
  with equity marked at the observed open. Admitted entries reserve that margin.
- **Fill:** the check is repeated at the fill. Existing lots' financing is first
  accrued up to the fill time, and each interval is charged once even when several
  fills share a timestamp. Open lots are marked at the fill
  price, and the other quote side is estimated from the candle's observed opening
  spread, so only information available at that moment is used. The immediate
  spread loss is included. A failed entry is `rejected`, and its level is retried
  at a later open.
- **Restore:** the entry a closed lot came from is restored only if its
  reservation is funded; otherwise the level is retried at later opens.
- Lot margin is collateral (`size × fill price / leverage`), not an expense.
  `rejected_entries` lists the first rejection per grid, level and reason;
  `funds_rejection_attempts` counts every retry.

## Costs and reporting

The grid reuses the shared data loaders, cache, coverage audit, cost options,
`build_report`/`save_report` and the shared statistics:

- **Spread** is embedded in bid/ask fill prices. The estimate charges each fill half of
  its candle's opening spread. That is exact for open (gap and flatten) fills and an
  estimate for intrabar fills, where the spread at the touch is unobserved.
- **Slippage** applies to market (flatten) fills only. Limit fills are at the limit or better.
- **Commission** is charged per fill. **Financing** accrues linearly per lot from its fill
  time. Unconfigured costs are listed as `unmodeled`, never asserted to be zero.
- Runs are not force-liquidated at the end. Open lots are marked at the last observed
  liquidation-side close (bid for longs, ask for shorts).

Reports add the following to the standard table and JSON:

- realized/unrealized P&L (cash and midpoint-gross)
- trading costs (`trading_costs` = spread estimate + slippage + commission +
  financing, also split into `commission_costs` and `financing_costs`)
- peak exposure (`peak_open_units`, `peak_open_lots`, `peak_open_notional_usd`,
  `peak_committed_units`, `peak_used_margin`). Each is its own running maximum.
  Notional is size × entry price, so a lot replaced at a higher price can raise the
  peak notional without raising peak units
- max drawdown on observed open/close marks
- ending inventory (open lots, working orders, pending activation)
- order counts, activations, direction changes, and grid/flatten exits

`report.json` gains a `grid` section with the levels, activations, ending inventory
and the full order log. Each run directory also gets `grid_orders.csv`.
`trade_details.csv` carries per-lot fields (`lot_id`, levels, fill types,
`exit_reason`), and `equity.csv` adds reserved margin, open units and working orders.
Gross and net P&L reconcile exactly, as for the other strategies.

## Limitations

- OHLC candles hide the true intrabar path. The open → nearer extreme → farther
  extreme → close order is an assumption. A candle that touches a level counts as
  a fill even if the real queue would not have been reached. Use smaller timeframes
  to reduce both effects.
- The spread at an intrabar touch is estimated from the opening spread.
- No partial fills, no broker stop-out (negative available funds are reported, and
  new entries are refused), and no broker swap or commission schedule.
- EURUSD with a USD account only, as for the other strategies.

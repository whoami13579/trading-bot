# Backtesting the order-capable strategies

Every strategy that `tb-trade` can run also has a historical backtest:

```bash
uv run tb-backtest --strategy moving-average
uv run tb-backtest --strategy sma
uv run tb-backtest --strategy hma
uv run tb-backtest --strategy ema-hma
uv run tb-backtest --strategy multiple-hma
uv run tb-backtest --strategy donchian      # historical simulation only
uv run tb-backtest --strategy grid          # historical simulation only
```

This page covers the four strategies that gained a backtest in issue #7:
moving-average, sma, ema-hma and multiple-hma. They run on the
[unified framework](backtesting-framework.md) with the same providers, cache,
coverage audit, Decimal ledger, bid/ask execution, cost options, reports and
reproducibility hashes as HMA. HMA is documented in
[historical backtesting](backtesting.md), Donchian in the
[framework guide](backtesting-framework.md) and Grid in
[grid backtesting](grid-backtesting.md).

## One rule implementation for trading and backtesting

The signal rules live in broker-free modules:

- `trading_bot.strategies.indicators`: SMA, EMA, HMA, `hma_signal`, and the extracted
  `two_ema_signal` and `multiple_signal`.
- `trading_bot.strategies.signals`: `moving_average_side`, `price_side`, `sma_side`,
  the position rules `reversal_target` and `agreement_target`, and every default
  period and timeframe.

The online strategies call these same functions and import their defaults from
`signals`, so the defaults cannot drift apart. The backtest classes in
`trading_bot/backtesting/strategy.py` only convert completed candles into the
price lists that the online code reads.

Online behavior is unchanged. The extracted functions compute the former
expressions exactly, as the offline tests confirm. The tests also drive each
unchanged online loop with a mocked broker over a controlled price series and
check that every online decision equals the backtest's decision on the same
prices (`tests/test_strategy_backtests.py`, `OnlineEquivalenceTests`).

## Strategy rules, parameters and defaults

Prices are Capital.com **ask** prices online. Backtests use the ask side of
Dukascopy bid/ask candles and the indicative price for Yahoo data.

### moving-average

| Item | Online (`moving_average.py`) | Backtest |
| --- | --- | --- |
| Input | Open ask of the newest one-minute candle, polled every 60 s into a 20-price list | Open ask of each completed candle, last 20 |
| Rule | Newest price > mean → BUY; < mean → SELL; equal → no order | Same function (`moving_average_side`) |
| Position | Sends the new side only when it differs from the tracked side (`status`) | Stop and reverse into the new side; hold otherwise |
| Parameters | none (20 and `MINUTE` are fixed) | `--average-period` (20), `--time-frame` (`MINUTE`) |
| Warm-up | 20 prices fetched at start | 20 completed candles |

### sma

| Item | Online (`sma.py`) | Backtest |
| --- | --- | --- |
| Input | Open ask of one-minute candles, polled every 60 s into a 21-price list | Open ask of each completed candle, last 21 |
| Rule | SMA(9) > SMA(21) → BUY; < → SELL; equal → no order | Same rule (`sma_side`) |
| Position | Same `status` rule as moving-average | Stop and reverse; hold otherwise |
| Parameters | none (9, 21 and `MINUTE` are fixed) | `--fast-period` (9), `--slow-period` (21; must exceed fast), `--time-frame` (`MINUTE`) |
| Warm-up | 21 prices fetched at start | 21 completed candles |

### ema-hma

| Item | Online (`ema_hma.py`) | Backtest |
| --- | --- | --- |
| Input | Close ask of the latest 200 (trend) and 48 (HMA) candles at each 5-minute boundary | Close ask of completed candles |
| Trend | EMA(20) < EMA(200) over exactly 200 closes → SELL, else BUY | Same function (`two_ema_signal`) over the same window |
| Trigger | HMA(16) slope over 48 closes: rising → BUY, else SELL | Same function (`hma_signal`) |
| Position | Disagreement → close; agreement → open the agreed side only when flat; an existing position is held, even an opposite one | Same rule (`agreement_target`) |
| Parameters | `--long_period` (200), `--short_period` (20), `--hma_period` (16), `--time_frame` (`MINUTE_5`), `--days` | `--long-period`/`--long_period`, `--short-period`/`--short_period` (must be shorter), `--hma-period`/`--hma_period`, `--time-frame`/`--time_frame` |
| Warm-up | — | max(long, 3 × HMA) = 200 completed candles |

As in the online code, EMA(200) over exactly 200 closes equals its SMA seed.

### multiple-hma

| Item | Online (`multiple_hma.py`) | Backtest |
| --- | --- | --- |
| Signal timeframe | `--short_term` (`MINUTE_30`): HMA(20) slope over 60 closes | `--time-frame` (`MINUTE_30`), same `hma_signal` |
| Long-term vote | 210 `--long_term` (`HOUR_4`) closes: six SMAs and six EMAs (10/20/30/50/100/200) against the latest close, plus an HMA(27) slope. Sum > 1 → BUY, < −1 → SELL, otherwise "Neutral" | Same function (`multiple_signal`) over the last 210 **completed** long-term candles |
| Position | Same agreement rule as ema-hma; "Neutral" never agrees, so it closes | Same rule (`agreement_target`) |
| Parameters | `--hma_period` (20), `--short_term`, `--long_term`, `--days` | `--hma-period`/`--hma_period`, `--long-term`/`--long_term` (a whole multiple ≥ 2 of `--time-frame`), `--time-frame` |
| Warm-up | — | max(3 × HMA, ratio × 212) signal candles: 1,696 30-minute candles for 4H (about 7 calendar weeks) |

**Multi-timeframe alignment.** Long-term candles are aggregated from the
completed signal-timeframe candles already in the decision window. They use the
UTC-aligned OHLC rule of the snapshot frames (`calendar.completed_groups`). A
long-term candle is used only if:

- it had **fully closed** by the decision time, which is the close of the newest
  signal candle, so a 4H candle closing at 04:00 is first used by the decision at
  04:00 and never at 03:30;
- every constituent slot that the FX calendar expects to trade is present. A
  candle with a missing constituent is excluded and never filled. Weekend-edge
  candles count as complete when only their closed-market slots are absent.

If fewer than 210 such candles exist, no decision is made and the position is
held. This matches the online loop, where the indicator raises for lack of
history and the cycle does nothing.

## Execution assumptions and known differences

All four strategies run on the **continuous** execution model by default. A
completed-close decision fills at the next observed open at the ask (BUY) or bid
(SELL), with no forced liquidation at the end. Optional commission, slippage and
financing assumptions apply, and unconfigured costs are reported as unmodeled.
`--execution-model research` also works; `grid` does not. These differences
from the online processes are deliberate and deterministic:

1. **Completed candles only.** The online ema-hma and multiple-hma loops ask the
   broker for the latest candles at each boundary (plus 2 s). Without the
   completed-candle filter that HMA uses, the newest of those candles may still be
   forming; for multiple-hma this includes the 4H candle in progress. The backtest
   never uses a candle, or a higher-timeframe candle, before it has closed. This is
   the convention already accepted for HMA.
2. **Polling strategies decide one candle later.** Online, moving-average and sma
   read the open of the candle that has just started, then order immediately. The
   backtest reads the same open prices but decides at that candle's close and
   fills at the next open. Online polling also drifts by the loop's processing
   time, so it can repeat or skip a minute; the backtest uses each completed
   candle exactly once.
3. **Opposite orders are simulated as reversals.** Online, moving-average and sma
   send a same-size order on the other side without closing the open position.
   The tracked `status` ("Tracks if we are currently BUY or SELL") and the comments
   ("BUY if Fast crosses ABOVE Slow") describe a long/short reversal, which the
   backtest models. On a broker account the result depends on its position mode.
   In netting mode the opposite order flattens rather than reverses. In hedging
   mode it opens an offsetting position with zero net exposure. The LIVE gate's
   single-position and order limits block most such sequences. Read these results
   as the strategy's stated intent, not as a reproduction of any one account mode.
4. **Startup artifacts are not simulated.** moving-average waits after start until
   the price first crosses its average. The first sma iteration repeats the newest
   buffered price, so for 21 minutes one price is counted twice. Both depend on when
   the process starts. The backtest uses clean windows from the first decision.
5. **Scheduling options are not simulated.** `--days 5` waits for the market to
   reopen; historical candles exist only while the market trades. LIVE preflight,
   stop orders, account limits and confirmation timing are not part of any
   backtest.
6. **Periods fixed online are configurable in the backtest.**
   `--average-period`, `--fast-period` and `--slow-period` default to the fixed
   online values. Other values are research settings that the online command
   cannot trade.

## Example commands

```bash
uv run tb-backtest --strategy sma --start 2025-09-01 --end 2025-10-01 --slippage-pips 0.1
uv run tb-backtest --strategy moving-average --average-period 30 --lookback-days 7
uv run tb-backtest --strategy ema-hma --long-period 100 --short-period 10 --time-frame MINUTE_15
uv run tb-backtest --strategy multiple-hma --time-frame MINUTE_30 --long-term HOUR_4 \
  --data-source snapshot --start 2025-09-01 --end 2025-10-01
```

Each option belongs to the strategies that declare it. For example,
`--hma-period` applies to hma (default 16), ema-hma (16) and multiple-hma (20),
and any other strategy rejects it with exit status 2. Out-of-range values exit
with status 1 (`Backtest failed`). Both happen before any data is loaded.

## Reference results (EURUSD, September 2025)

Common settings: Dukascopy bid/ask data, 2025-09-01 to 2025-10-01 (UTC, end
exclusive), USD 1,000, 100 EUR units (grid: 100 per level, cap 500), 0.1 pip
slippage per leg, and each strategy's default parameters and timeframe. Spread
comes from the historical bid/ask. Commission and financing are **unmodeled**.
Execution is continuous, including Donchian, whose own default is the research
model on the study snapshot, and grid, which runs on the grid model. The window
lies outside the frozen Donchian holdout (2026-03-27 to 2026-11-01).

| Strategy | Timeframe | Trades | Gross P&L | Net P&L | Return | Max drawdown | Profit factor | Ending equity | Modeled costs |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| moving-average | 1m | 3,819 | 0.6730 | −26.5380 | −2.6538% | 26.8860 | 0.52 | 973.4620 | 27.2095 |
| sma | 1m | 1,663 | −0.1930 | −11.8060 | −1.1806% | 12.4680 | 0.66 | 988.1940 | 11.6115 |
| hma | 30m | 72 | −0.5085 | −1.1280 | −0.1128% | 2.0100 | 0.78 | 998.8720 | 0.6180 |
| ema-hma | 5m | 426 | 0.0360 | −2.9530 | −0.2953% | 4.0350 | 0.75 | 997.0470 | 2.9875 |
| multiple-hma | 30m + 4H | 61 | −0.3370 | −0.7420 | −0.0742% | 1.4460 | 0.82 | 999.2580 | 0.4050 |
| donchian | 1H | 22 | 1.5715 | 1.4380 | 0.1438% | 1.4450 | 1.80 | 1,001.4380 | 0.1320 |
| grid | 1H | 15 | −0.0790 | −0.1670 | −0.0167% | 1.2790 | 0.88 | 999.8330 | 0.0880 |

Amounts are USD. Modeled costs are the executed spread plus slippage; no cash
costs were configured. Trades are closed trades. Net P&L marks an open position
at its liquidation side, and one was open at the end for moving-average, sma,
hma, ema-hma and donchian.

The strategies trade different timeframes and position rules, so these figures
show coverage and costs, not a fair ranking. The one-minute strategies trade
thousands of times, and spread alone exceeds their gross result. These are
default parameters, not optimized ones; they do not indicate future
performance.

| Strategy | data_sha256 | configuration_sha256 |
| --- | --- | --- |
| moving-average | `0133e22249042c17d3005585064cc5c0f592a79f1c4e607c3127c50819e944f4` | `f380de7fd440f482a99e0232d923415b0f0e7df1f73ecb6749845089778bfd92` |
| sma | `1a877cc298772c04e00cc0e85621651cf6772c49714a528b31d4cadc9958bc78` | `59dc5fa17c9cafc15206e0ad702f67e6f6cabee3fb9ad9933986e23c11c38f3c` |
| hma | `63497a33561a413158898a73540f4b385717af74c17e42b229114bd7835799d9` | `360dc2845a785f329b5b6b4a462ecafc393490b17be11bca960a3ab41b1e4772` |
| ema-hma | `373e47533a1823d5e0c130ebd1af1cd1b6b5001ff1b882fd3aad4031c398550c` | `888f1fb4a435c4b71147b74560a0894ac5465d7880f1796bca5ed780c94627f9` |
| multiple-hma | `2a63640f62a4077d2ef4ca6c5652b40e927f335ce6bc14a3a4f41cd4ed57fc3d` | `c98dce197f3bcd33b392c4fa7c849ae2727ea0e7b67b5d9f07ce2753e38723b9` |
| donchian | `84dd463852fa186e0d08a8e986dc14eb44eaa455217dcd853748329cccdd5a76` | `611a1faf25c0d8b6f17fac1e803276afc356b7dc06ddbf9837c263683adad15c` |
| grid | `688b751ab450a48df1aa03f2d9366871d9868c539d5fe4550e55360a5eca3148` | `226352888cb6c3c8d2b9c58ea35d1c7f1c226a8dffcd71f7c91511f989007067` |

Reproduce a row with:

```bash
uv run tb-backtest --strategy sma --symbol EURUSD --data-source dukascopy \
  --start 2025-09-01 --end 2025-10-01 --initial-balance 1000 --size 100 --slippage-pips 0.1
# donchian: add --execution-model continuous; grid: omit --size
```

Rerunning the same command produces an identical report. HMA, Donchian and Grid
produced identical reports, including both hashes, before and after this change.

## Limitations

- These are candle-level simulations with no intrabar path, latency or partial
  fills. One-minute results are very sensitive to spread and slippage.
- Long-term candles come from the signal timeframe. With Yahoo data, its weekend
  edges are checked against the Dukascopy FX calendar, so edge candles may be
  excluded.
- The study snapshot holds 30-minute data. One-minute and five-minute strategies
  need `--data-source dukascopy` (or `yfinance`, which keeps only about 7 days of
  one-minute history).
- The differences listed above (reversal semantics, startup artifacts,
  completed-candle timing) are modeling choices. Change them only together with
  their tests.

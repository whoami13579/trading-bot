# Controlled DEMO validation plan: shared single-position execution

Issue #9. This plan has **not been executed**. Running it places DEMO orders and
needs separate authorization from the account holder. Offline tests passed when
it was written; they do not show LIVE readiness, and no part of this plan
authorizes LIVE trading.

## Scope and common rules

One strategy at a time, in this order: hma, ema-hma, multiple-hma, sma,
moving-average. Each run uses the DEMO endpoint only (never `--REAL`/`--real`).

| Limit | Value |
| --- | --- |
| Instrument | EURUSD only |
| Size | 100 (the verified minimum used by every strategy default) |
| Strategy-owned positions | at most 1 (engine invariant) |
| Maximum exposure | one 100-unit EURUSD position, about 110–120 USD notional |
| Orders per strategy run | at most 2 (one entry, one exit); sma/moving-average: at most 1 |
| Concurrent strategy processes | 1 per account |
| Run length | `--cycles` bounded, as listed per strategy |

### Required account conditions (check before every run)

1. DEMO account enabled, active and selected; `tb-demo-readonly` passes.
2. **No open positions and no working orders on EURUSD** (other instruments are
   ignored by the strategies, but keep the account empty for clarity). Any
   EURUSD position blocks entries by design.
3. No other strategy process running (the DEMO ownership record is per process).
4. Note the account mode (`hedgingMode` in account preferences) in the log;
   repeat the sma/moving-average reversal step on a netting and a hedging
   account if both are available.
5. A second terminal with the Capital.com DEMO web platform open to verify
   positions independently.

### Read-only preflight (every strategy)

```bash
uv run tb-trade --strategy KEY --read-only --cycles 2   # plus the run's strategy options
```

Pass: two `DEMO read-only cycle` lines per run with HTTP 200 for every probe,
`session validated` or `renewed`, no position read, no order on the account, exit 0.

### Evidence to keep

Terminal stdout/stderr, the start/end time in UTC, the account mode, and
screenshots of the DEMO position list before and after. Runtime logs use fixed
reason codes and mask the account ID to its last four characters; still review
the output before sharing it. Never copy `.env` or tokens into the record.

### Manual reconciliation procedure

Use whenever a run stops with `Trading stopped`, `order outcome may be
uncertain`, `acknowledged order(s) are not yet confirmed`, or a non-zero
`strategy_owned_positions` at exit:

1. Do not restart the strategy.
2. On the DEMO platform, list EURUSD positions and working orders and the
   account activity for the run's time window.
3. Match each order the log reports (`Order acknowledged operation=...`) to the
   activity. Close any position the run opened, by hand.
4. Confirm the account has no EURUSD positions or working orders.
5. Record the outcome, then restart only if the plan continues.

### Global failure criteria (stop the plan, file an issue)

- Any order beyond the step's expected order count, or any duplicate order.
- Any close of a position the run did not open.
- An order after a `stale_boundary`, `order_confirmation_pending`,
  `Entry blocked` or `Trading paused` message for that boundary.
- An opposite market order while a position is open.
- Any credential, token, account or deal identifier in the output.
- Any automatic resumption of a stopped (uncertain) run.

## Per-strategy steps

`--days 7` keeps weekend runs simple; run during FX market hours.

### 1. hma

Options: `--time-frame MINUTE` (one-minute candles and schedule, so transitions
happen within the run), default `--hma-period 16` and `--direction BUY`.

| Step | Action | Expected transition | Pass |
| --- | --- | --- | --- |
| 1 | Read-only preflight | — | as above |
| 2 | `tb-trade --strategy hma --time-frame MINUTE --cycles 30` | flat → entry_pending → open (on a BUY slope) → exit_pending → flat (on SELL) | `order submitted`, `Order confirmed operation=open`, `Position ownership recorded`, then `close position`, `Order confirmed operation=close`; platform matches; at most 2 orders |
| 3 | Open a manual EURUSD BUY on the platform, then run 10 cycles | no entry; manual position untouched | `Positions not owned by this process positions=1`, `Entry blocked`; manual position still open; close it by hand afterwards |
| 4 | Network: disconnect Wi-Fi/WSL network for ~3 minutes during a run with an open owned position | pause → recover → resume | `Trading paused`, `Session reauthenticated`, `Broker state reconciled`, `Scheduled trading resumed`; no duplicate order; missed boundaries not replayed |
| 5 | Shutdown: Ctrl+C while waiting, with an owned position | stop, no order | exit 130, `Shutdown complete ... strategy_owned_positions=1`; position still open; reconcile by hand |
| 6 | Restart after step 5 without closing | no adoption | `Entry blocked`; no close of the earlier position; then close by hand |

### 2. ema-hma

Options: `--time_frame MINUTE_5`. Signal inputs still end with the forming
candle (#11); record decisions for comparison only.

| Step | Action | Pass |
| --- | --- | --- |
| 1 | Read-only preflight (two probes per cycle: 200 and 48 candles) | as above |
| 2 | `--cycles 24` (2 hours) | entry on agreement, close on disagreement, each confirmed; ≤ 2 orders |
| 3 | Manual EURUSD position present for 6 cycles | never closed; entries blocked |
| 4 | Network outage ~10 minutes | pause/resume as hma step 4 (EMA-HMA used to stop on any outage) |
| 5 | Ctrl+C during the wait | as hma step 5 |

### 3. multiple-hma

Options: `--short_term MINUTE_30 --long_term HOUR_4 --days 7`. The 4H vote still
includes the 4H candle in progress (#11).

| Step | Action | Pass |
| --- | --- | --- |
| 1 | Read-only preflight (210 HOUR_4 and 60 MINUTE_30 candles) | as above |
| 2 | `--cycles 16` (8 hours) | at most one entry and one exit, each confirmed |
| 3 | Manual position, 2 cycles | never closed; entries blocked |
| 4 | Ctrl+C during the wait | as hma step 5 |

### 4. sma

| Step | Action | Pass |
| --- | --- | --- |
| 1 | Read-only preflight (21 MINUTE candles) | as above |
| 2 | `--cycles 120` until the first reversal | one confirmed entry; at the first opposite signal: `Trading stopped reason=reversal_requires_confirmed_close`, **no second order**, owned position still open, exit non-zero |
| 3 | Reconcile: close the position by hand | account flat |
| 4 | Repeat step 2 on the other account mode (netting/hedging) if available | identical outcome |

### 5. moving-average

As sma, plus: no order during the startup wait (the first decision comes one
boundary after the newest open first crosses the 20-open mean). Expect at most
one order per run until #10.

## Success

A strategy passes when every step passes, the platform's position history
matches the log, the account is flat at the end and no global failure criterion
occurred. Record the result per strategy in the PR or issue. LIVE remains
subject to the existing LIVE preflight, verified fee evidence and the account
holder's explicit authorization.

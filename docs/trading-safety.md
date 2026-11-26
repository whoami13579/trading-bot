# Trading safety

## Automated strategy execution

All order-capable strategies share these defaults: **EURUSD, size 100, DEMO**.
The preferred command is `tb-trade`; it dispatches to the same strategy
implementations as the original commands:

| Strategy | Preferred command | Original commands |
| --- | --- | --- |
| Moving average | `tb-trade --strategy moving-average` | `trading-bot-moving-average`, `main.py` |
| SMA | `tb-trade --strategy sma` | `trading-bot-sma`, `sma.py` |
| HMA | `tb-trade --strategy hma` | `trading-bot-hma`, `hma.py` |
| EMA/HMA | `tb-trade --strategy ema-hma` | `trading-bot-ema-hma`, `ema_hma.py` |
| Multiple HMA | `tb-trade --strategy multiple-hma` | `trading-bot-multiple-hma`, `multiple_hma.py` |
| Single SELL order | — (not a strategy) | `test.py` |

**`test.py` submits an order; it is not an offline test.** Existing SMA, EMA, HMA,
Multiple HMA calculations, signal rules, periods and timeframes are unchanged.
Donchian and Grid are historical simulations only: `tb-trade --strategy donchian`
and `tb-trade --strategy grid` are rejected in every mode before any strategy,
credential or broker code is loaded. Use `tb-backtest` for them.

```bash
# DEMO
uv run tb-trade --strategy hma

# LIVE
uv run tb-trade --strategy hma --REAL
```

The same invocation works with the other strategies and the original commands
(`.venv/bin/python hma.py --REAL` remains equivalent). `--real` remains an alias
for `--REAL`. All accept optional instrument and size overrides:

```bash
uv run tb-trade --strategy sma --symbol EURUSD --size 100
uv run tb-trade --strategy multiple-hma --REAL --symbol EURUSD --size 100
```

These are online strategy commands and can place orders when preflight succeeds.
`--help` exits before credentials or network access. Use the offline test runner
for validation, not a running strategy.

`tb-trade` changes nothing between the command line and the strategy. It requires
exactly one `--strategy` and refuses unknown or simulation-only strategies,
backtesting options, and environment spellings other than `--REAL`/`--real`.
It then calls the strategy's existing entry point once with the remaining
arguments. `tb-trade` has no broker, retry or order code. Every strategy entry
point runs the same online lifecycle, the
[shared single-position execution](shared-execution.md) (issue #9), and every
LIVE run goes through the same LIVE gate. The strategies still differ in their
signal inputs and in what their decisions ask for (below).
See the [CLI guide](cli.md).

### Safety capabilities by strategy

Verified against `execution/single_position.py`, the strategy modules, the client
and the LIVE gate, and covered by offline tests with mocked brokers. "LIVE gate"
means `risk/live_execution.py`: it checks account, environment, fees, exposure,
margin, charges and drawdown, requires a broker stop, permits at most one owned
position, verifies confirmations, keeps the process lock and the durable intent
journal, never retries, and stops on any uncertainty. Offline tests are not a
DEMO or LIVE validation; see the [DEMO validation plan](demo-validation-plan.md).

Shared by all five order-capable strategies (moving-average, sma, hma, ema-hma,
multiple-hma):

| Capability | Behavior |
| --- | --- |
| Scheduling | UTC candle boundaries (+2 s); missed boundaries skipped, never replayed; each boundary at most once |
| Stale-signal guard | `--max-signal-delay` (`CycleGuard`): at cycle start, before the signal, immediately before every order; in LIVE also inside the gate and after rate limiting, immediately before the send |
| Position state | the broker's position list each decision, never the requested orders |
| What it closes | only the position this process opened, as confirmed by the broker (DEMO: confirmation; LIVE: the gate's journal). Unowned positions are never closed or adopted |
| Entries | only while flat and no unowned position is open on the instrument |
| Reversal (BUY to SELL or back) | never an opposite order: trading stops with the owned position open (`reversal_requires_confirmed_close`, issue #10) |
| DEMO order confirmation | `PendingOrders`: confirmation and position visibility before the next decision; polled between boundaries; 300 s deadline stops |
| Duplicate prevention | pending tracking, broker positions, and a stop on any unexpected order response |
| Transport failure, interruption or exception during an order | stops; outcome reported uncertain; never retried |
| Rejected or mismatched confirmation | stops for manual reconciliation (existing policy) |
| Network outage | DEMO: bounded read retries, pause, resume with a re-verified client and broker reconciliation; LIVE: stop |
| Session expiry | validated or renewed each boundary and every 5 minutes while waiting (`prepare_trading_cycle`) |
| Graceful shutdown | SIGINT/SIGTERM deferred during an order request; never closes positions; exit reports owned positions and pending orders |
| Restart | DEMO owns nothing after a restart: earlier positions block entries and are never adopted; unconfirmed orders warn at exit. LIVE: the journal blocks restart until reconciled |
| `--read-only` | DEMO session/history diagnostics; the client refuses every mutation |
| LIVE gate | yes, bound to the cycle deadline |

Strategy-specific:

| | HMA | EMA-HMA | Multiple-HMA | SMA | Moving average |
| --- | --- | --- | --- | --- | --- |
| Schedule | `--time-frame` (30 min) or `--schedule-time-frame` | `--time_frame` (5 min) | `--short_term` (30 min) | 1 min | 1 min |
| Signal input | completed candles only; a missing expected candle skips the cycle | newest candles, ending with the forming one (#11) | newest candles; the 4H vote includes the 4H candle in progress (#11) | newest 21 ask opens (the just-started candle's open is a known price) | newest 20 ask opens, after a startup wait |
| Decision | configured side / flat | agreement holds that side; disagreement is flat | agreement holds that side; disagreement or Neutral is flat | stop-and-reverse: stops at the first reversal (#10) | as SMA |
| Market hours | `--days` (7) | `--days` (7) | `--days` (5: waits while closed) | always | always |

Consequences:

- In **DEMO**, HMA, EMA-HMA and Multiple-HMA no longer close positions they did
  not open (they closed every position on the instrument before issue #9).
- SMA and moving average no longer send an opposite order on a signal change.
  They stop at the first reversal in DEMO and LIVE, leaving the owned position
  open, until issue #10 adds a confirmed close-then-open.
- Run one strategy per instrument and account: a second strategy's position is
  unowned for the first and blocks its entries.

Donchian and Grid have no online mode (planned in #12 and #13).

| Selection | Environment | Endpoint |
| --- | --- | --- |
| No flag | DEMO (default) | `https://demo-api-capital.backend-capital.com` |
| `--REAL` / `--real` | LIVE, real money | `https://api-capital.backend-capital.com` |

These endpoints are documented in the [official API reference](https://open-api.capital.com/).
Both environments read the same permission-600 `.env`. Authentication failures
stop the selected environment, with no automatic switching or fallback. Session
tokens remain private to their client and environment, and redirects are disabled.

### Automatic account, market and cost checks

At startup and before every LIVE entry, the gate retrieves and checks the active
enabled LIVE CFD account, market status and directional restrictions, instrument
specifications, size increments, leverage, available margin, positions, working
orders, quote spread and available BUY/SELL funding data. It never switches
accounts or changes account preferences. Hedging mode must already be enabled to
prevent netting into unrelated positions. Existing account positions or working
orders block new entries. Multiple eligible LIVE accounts still require
`--account` to identify the already-active account; no selection is guessed.

The documented broker market API exposes bid/offer and overnight funding data,
but does not provide a complete account-specific commission and other-fee schedule.
The cost collector reports available information and **never treats absent fees
as zero**. If the required account-specific charges or holding-cost budget cannot
be verified, LIVE preflight stops with
`live_account_fee_schedule_unavailable_from_api_no_verified_evidence`.
Thus the simple LIVE command can start unattended preflight without implying
that the account is ready to execute orders.

An existing `.live-integration/fees.json` can automatically provide previously
reviewed evidence for missing fees; `--fee-profile PATH` remains an optional path
override. No file is created, altered, or marked verified automatically. Evidence
must pass the existing permission-600, account fingerprint, legal entity,
classification, currency, instrument, official-source and seven-day freshness
checks. Strategy holding-cost evidence also needs
`strategy_holding_costs_verified: true` and a reviewed nonnegative
`strategy_funding_allowance_usd`. That allowance is included in estimated charges.
It is a reviewed cost allowance, not an invented API fee. Unknown, stale, pending
or incomplete evidence blocks execution. The example profile is unverified and
cannot enable trading. This fee data contains no credentials; `.env` remains the
single credential file.

### Execution protections

At most one strategy-owned position may be open. Entry count, cumulative estimated
costs, exposure, available margin, planned stop loss and observed equity drawdown
are checked before submitting another entry. Every LIVE entry includes a broker
stop, and the accepted order, affected position, size, direction and actual stop
are verified. Closing a verified owned position remains available when entry
limits are reached. No other position, including another position on EURUSD, is
closed by the strategy.

Before each mutation the gate durably records intent under the account-specific
`.live-integration/strategies/` directory. An account lock blocks concurrent
strategy processes. Timeouts, interrupted submissions and unresolved confirmations
stop execution without automatically repeating the order. Pending or still-owned
positions in a previous journal block restart until manually reconciled with the
actual account. Never clear unresolved state simply to retry an order. Session or
account changes invalidate the gate and require a new automated preflight.

Network outages: every order-capable strategy retries read-only GETs with bounded
backoff, and in DEMO pauses and resumes after re-authentication and broker
reconciliation. Trade-changing
requests are never retried. An uncertain order outcome stops the bot for manual
reconciliation, and LIVE never resumes automatically. A DEMO order acknowledged with HTTP
200 blocks further strategy orders until the broker confirms it and the position list
reflects it. The confirmation is polled between boundaries, and trading stops for manual
reconciliation if the order is unresolved 300 seconds after it was acknowledged. This
holds across reconnection but not across a process restart. Every exit with such an order
warns to reconcile before restarting. HMA signals use only the completed candle expected
for each boundary (EMA-HMA and Multiple-HMA: issue #11). The LIVE gate rechecks
`--max-signal-delay` after its broker reads, immediately before sending. An expired cycle sends nothing, leaves no pending intent and
uses no order or charge allowance. See [network recovery](network-recovery.md).

Ctrl+C stops the process without liquidating positions. In every order-capable strategy,
Ctrl+C or SIGTERM during an order request is deferred until that request returns. Broker stops remain in
place; review remaining exposure on Capital.com. Prices can slip or gap and fees
or holding costs can exceed estimates. Entry-time risk limits do not guarantee a
maximum loss or run a background liquidation service. A broker stop that closes a
position can leave a journal requiring manual reconciliation before a new entry.

`ping_service.py` and `test_strategy.py` remain read-only despite supporting the
environment selectors. The latter simulates historical signals and never arms an
execution gate. DEMO-specific diagnostics, utility modules and `tests/` remain
nontrading. The separate `test_live_api.py --execute-live-trade` integration test
retains its deliberate one-off limits and terminal confirmation; it is not the
normal automated strategy path described here.

## LIVE fee review and transaction-charge limit

The older `test_live_api.py` checker requires an explicit `--max-charges` cap, including estimated
roundtrip spread, commissions, conversion and other applicable transaction charges.
It checks all three limits before terminal confirmation and again after refreshing
the market/account snapshot. A missing cap, unknown fee, non-USD account, exceeded
limit or increased post-confirmation cost prevents submission.

The 2026-11-09 fee review refreshed LIVE data using only authentication and GETs.
The account's legal entity and retail/professional/wholesale classification remain
unverified. `.live-integration/fees.pending.json` is a private, unverified worksheet;
it does not enable trading. Match the applicable official schedule to the account,
complete all numeric fee fields and evidence, then prepare the active private
`.live-integration/fees.json`. Do not mark a generic tariff as verified for an
unknown entity or assume unknown regional charges are zero. The profile must be
mode 600 and specify legal entity, client classification and EURUSD scope.

Safe refresh with the requested USD limits (no orders):

```bash
.venv/bin/python test_live_api.py --environment live --max-margin 5 --max-exposure 120 --max-charges 1
```

After full fee verification, the account holder can run the same command with
`--execute-live-trade` from their own interactive terminal. It still requires the
fresh exact confirmation phrase; automated entry of that phrase is inappropriate.
No LIVE trade was submitted during this preparation. The execution report records
confirmed BUY/closing prices and size; absent broker-reported margin or actual
spread remains unavailable rather than being inferred from estimates. Missing
closing price prevents a complete-success result after closure is verified.

## Structural refactor boundary

All live signal rules, API payloads, timeouts, default limits, endpoint selection,
account verification, locks, intent journals, ownership checks and reconciliation
paths retain their existing behavior. Core modules now live under `trading_bot.api`,
`trading_bot.config` and `trading_bot.risk`. Compatibility wrappers contain no
second implementation. This migration is not a new certification of trading
safety; use offline mocks before separately authorized account testing.

The HMA backtest is a separate offline simulator. Its corrected direction and
position accounting do not alter the live HMA strategy. It models gross quote
price differences for one unit, with no cash conversion, costs or forced final
liquidation. Do not treat its output as reconciled broker P&L.

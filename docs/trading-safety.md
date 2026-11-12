# Trading safety

## Automated strategy execution

All six order-capable strategies share these defaults: **EURUSD, size 100, DEMO**:
`main.py`, `sma.py`, `hma.py`, `multiple_hma.py`, `ema_hma.py`, and `test.py`.
**`test.py` submits an order; it is not an offline test.** Existing SMA, EMA, HMA,
Multiple HMA calculations, signal rules, periods and timeframes are unchanged.

```bash
# DEMO
.venv/bin/python hma.py

# LIVE
.venv/bin/python hma.py --REAL
```

The same invocation works with the other five scripts. `--real` remains an alias
for `--REAL`. All six accept optional instrument and size overrides:

```bash
.venv/bin/python sma.py --symbol EURUSD --size 100
.venv/bin/python multiple_hma.py --REAL --symbol EURUSD --size 100
```

These are online strategy commands and can place orders when preflight succeeds.
`--help` exits before credentials or network access. Use the offline test runner
for validation, not a running strategy.

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

Ctrl+C stops the process without liquidating positions. Broker stops remain in
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

# Trading bot development setup

Capital.com strategy scripts. Use Python **3.12**, selected by `.python-version`,
and install dependencies from the existing `uv.lock`. The package metadata permits
Python >=3.10; this development setup and its offline checker target 3.12.

## Ubuntu / WSL2 setup

Verified on Ubuntu 26.04.1 LTS, x86_64, WSL2, using Bash, Git 2.53.0,
uv 0.12.11, and CPython 3.12.10. The existing checkout is
`/home/chu/code/trading-bot`; no additional clone is needed.

```bash
cd /home/chu/code/trading-bot
export PATH="$HOME/.local/bin:$PATH"
export UV_CACHE_DIR=/tmp/trading-bot-uv-cache
uv --version
uv python list --only-installed
uv sync --frozen
.venv/bin/python --version
```

uv and Python 3.12 are already installed on this machine and were reused. uv finds
Python at `/home/chu/.local/share/uv/python/cpython-3.12.10-linux-x86_64-gnu/bin/python3.12`.
The system `python3` is 3.14.4; use `.venv/bin/python` or `uv run` for this project.
There is no need to change the system Python, install system pip, or activate the
virtual environment.

For a fresh Linux machine without uv, follow the
[official uv installation instructions](https://docs.astral.sh/uv/getting-started/installation/).
If Python 3.12 is absent, run `uv python install 3.12`, then `uv sync --frozen`.
[uv detects existing Python installations](https://docs.astral.sh/uv/guides/install-python/).
The first sync downloads locked packages from PyPI. It does not contact Capital.com.
`--frozen` preserves `uv.lock`; do not regenerate it as part of setup.

The cache override makes uv usable within the restricted development workspace,
where the default home cache is read-only. It applies only to the current shell.
If uv reports a hardlink fallback across filesystems, copying is harmless; set
`export UV_LINK_MODE=copy` to silence that warning. No OS packages or build tools
were needed: all locked dependencies installed from wheels.

## Configuration

Offline checks and `--help` require **no credentials and no `.env` file**.
Both DEMO and LIVE use this checkout's same private `.env`. `.env.example` contains
only placeholders. Create it only if absent, then edit locally:

```bash
cd /home/chu/code/trading-bot
umask 077
cp --no-clobber .env.example .env
chmod 600 .env
git check-ignore -v .env
```

Only these case-sensitive variables are accepted:

| Variable | Purpose |
| --- | --- |
| `X-CAP-API-KEY` | Shared Capital.com API key |
| `identifier` | Capital.com login identifier |
| `password` | Custom API-key password |
| `CST` | Legacy name; ignored, leave blank |
| `X_SECURITY_TOKEN` | Legacy name; ignored, leave blank |

No account ID or environment selector belongs in `.env`. Credentials are read from
this exact file using the dotenv parser without interpolation, parent-directory
search, or shell-variable overrides. **Do not `source .env`**: the API key name
contains hyphens. Missing, ambiguous, duplicate, placeholder, unexpected, or insecure
configuration is rejected. No credential values are logged or changed.

Select the endpoint in code with `TradingApi(environment="demo")` (the default)
or explicitly `TradingApi(environment="live")`. The legacy explicit
`real_account=True` argument remains supported; conflicting selectors are rejected.
Each client has a fixed endpoint and fresh in-memory session tokens. Legacy
`load_keys()` / `write_keys()` are compatibility no-ops: unlabelled `.env` tokens
cannot safely identify their environment, so they are never used or saved.
The two guarded integration CLIs below also start with empty, independent tokens.
`.gitignore` excludes private configuration and both integration state directories.

## Safe offline validation

After syncing, run:

```bash
cd /home/chu/code/trading-bot
export UV_CACHE_DIR=/tmp/trading-bot-uv-cache
uv sync --frozen --offline --check
uv pip check --python .venv/bin/python
.venv/bin/python scripts/check_environment.py
```

The checker verifies Python 3.12 and all seven locked package versions, compiles
all project Python files, imports dependencies and safe project modules, checks
every `TradingApi` constructor call statically, and exercises indicators and legacy
price readers with synthetic data. It runs all ten strategy/reader/integration CLI `--help` paths
with API construction, HTTP requests, network connections, dotenv loading, and
sleep blocked. It never imports or executes `main.py` or `test.py`; those files
are compiled and inspected as text only. No account access is needed.

You can also inspect the supported CLIs individually without starting a loop:

```bash
uv run --offline --no-sync hma.py --help
uv run --offline --no-sync multiple_hma.py --help
uv run --offline --no-sync ema_hma.py --help
uv run --offline --no-sync ping_service.py --help
```

`uv --offline` prevents uv from downloading packages; it does **not** prevent a
Python program from accessing the network. These help commands are safe because
argument parsing exits on `--help` before configuration or API construction.
Use the checker above for validation with network access blocked in Python.

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

### Configurable risk defaults

`--REAL` is the explicit authorization for automated LIVE execution. Strategy
startup displays a real-money warning and performs preflight; it does **not** read
terminal input or require an exact phrase. Essential missing data still blocks
execution. Configure unattended runs in `scripts/strategy_defaults.py`, or use
optional CLI overrides:

| Control | Default | Optional override |
| --- | --- | --- |
| Maximum estimated margin | USD 5 | `--max-margin` |
| Maximum notional exposure | USD 120 | `--max-exposure` |
| Maximum cumulative estimated roundtrip charges per process | USD 1 | `--max-charges` |
| Maximum observed equity drawdown from the process's peak | USD 2 | `--max-loss` |
| Maximum opening orders per process | 1 | `--max-orders` |
| Broker stop distance in instrument price units | 0.001 | `--stop-distance` |

These policy defaults do not establish that a trade is affordable or safe for a
particular account. Broker size increments, stop restrictions, available funds,
leverage, market status, costs and all limits must pass current checks. Invalid
sizes, nonfinite numbers and nonpositive risk limits are rejected. Existing LIVE
contract support remains **EURUSD, size 100, USD accounts**; other `--symbol` and
`--size` values remain available in DEMO, and unsupported LIVE mappings stop before
execution. The CLI never changes an unsupported size or instrument silently.

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

## DNS failures and safe connectivity checks

During the 2026-11-11 investigation, the DEMO hostname resolved and guarded DEMO
login, time and ping checks returned HTTP 200. The earlier `socket.gaierror`
occurred before authentication; its exact transient DNS cause could not be
reproduced. WSL's generated resolver was preserved, and `.env` was not modified.
Machine-specific diagnostic reports remain private and are excluded from Git.

For an unauthenticated, read-only connectivity check:

```bash
getent hosts demo-api-capital.backend-capital.com
curl --noproxy '*' --connect-timeout 5 --max-time 20 --fail --silent --show-error \
  https://demo-api-capital.backend-capital.com/api/v1/time
```

`test_api_key.py` remains order-capable by default. To authenticate and preflight
without any orders, use **only** its DEMO read-only mode:

```bash
uv run test_api_key.py --read-only
```

`test.py` submits a SELL after successful authentication; do not use it to test
connectivity. Network failures stop the API client with sanitized DNS, timeout,
proxy, TLS or connection diagnostics, distinct from HTTP 401. Requests use a
5-second connection timeout and 20-second read inactivity timeout; libc DNS
resolution is governed by the system resolver, not an absolute 5-second deadline.
Network exceptions and position-changing requests are never automatically retried. A stopped
client clears session state and blocks all subsequent requests, including orders
and reauthentication. Restore connectivity and start a fresh client explicitly;
reconcile any uncertain order outcome first. Existing ping session refresh and
bounded GET confirmation polling are retained.

`TradingUnavailable` inherits `SystemExit` so existing strategy loops cannot
swallow it with `except Exception` and continue with stale data. CLI/report
boundaries catch it explicitly and return failure. Missing market data is never
converted into a trading signal or an empty price series after an HTTP failure.

## Demo connection checks and automated tests

During previous read-only DEMO testing, login initially returned HTTP 401 before a DEMO account existed. After
that account was created, one independent session POST succeeded with HTTP 200
and both token headers using the unchanged credentials. Subsequent read-only
integration checks passed for ping, the active demo CFD account, EURUSD market
rules, 210 MINUTE_30 candles, and positions. No orders were sent.

Run the automated suite with all HTTP and socket connections blocked:

```bash
cd /home/chu/code/trading-bot
.venv/bin/python scripts/test_offline.py
```

This runner discovers only `tests/`, blocks dotenv credential loading, and never
executes strategy entry points. Tests cover API response mocks, session refresh,
request safety, synthetic signals, and archived public ECB daily EUR/USD prices.
ECB daily reference rates are separate from Capital.com intraday quotes. The
Capital.com snapshot test is explicitly skipped until a demo snapshot is obtained.

After separately authorizing the next read-only validation stage, the following
makes **online, read-only demo requests**, including a login; it still cannot
place, change, or close orders:

```bash
.venv/bin/python scripts/demo_readonly.py
.venv/bin/python scripts/test_offline.py
```

The connection checker reads only this project's `.env`, ignoring ambient shell
credentials and parent dotenv files. Its final-request guard permits HTTPS only
to `demo-api-capital.backend-capital.com`, with an exact method/path allowlist:
session creation and the requested read-only endpoints. Redirects and environment
proxies are disabled; TLS verification, a 5-second connection timeout and a
20-second read timeout are enforced.
Requests are spaced at least 1.1 seconds apart, with a 20-request cap.
It never calls `write_keys()` and verifies `.env` contents remain unchanged.

Only sanitized pass/fail flags and HTTP statuses are saved in
`DEMO_API_RESULTS.json`; credential values, session tokens, account details,
response bodies, and exception messages are excluded. If all history requests
succeed, price-only snapshots are saved under `tests/fixtures/` for offline
replay. Running this checker with rejected credentials exits with status 1.

Session fixes keep fresh tokens in memory, explicitly mark plaintext API-password
authentication, validate both response token headers, and standardize ping returns.
Two-hour history sampling now returns its computed samples. Indicator guards reject
invalid periods and inadequate history; valid-data formulas and signal rules are
unchanged. `test.py` remains unsuitable for testing and must not be executed.

Offline tests verify the session request contract. Checker preflight flags
parsing hazards without exposing values, and response diagnostics distinguish
known codes from unknown/missing codes and non-JSON responses. Automatic `.netrc`
authentication is disabled before request preparation. Offline tests cover those
guards.
Additional online requests require their own authorization.

For independent request construction without any network request, run:

```bash
.venv/bin/python scripts/diagnose_demo_auth.py
```

This compares PreparedRequest JSON bytes with a stdlib construction and prints
only flags. Its optional `encryption-key` and `session` probe modes use direct
`http.client` transport and refuse network access without the explicit
`--approve-demo-request` flag. The latest explicitly authorized session probe
succeeded after DEMO account creation.

## BUY and SELL integration test

`test_api_key.py` is an order-executing integration test. It defaults to **EURUSD,
size 100, DEMO**. It authenticates and verifies the active account and instrument,
opens one BUY, confirms acceptance and the exact resulting position, closes that
position, and verifies closure. Only after that cycle succeeds does it perform
one SELL (short) and close that exact position. A successful HTTP order response
alone is insufficient; the [broker's confirmation API](https://open-api.capital.com/)
identifies affected positions separately from the order ID.

```bash
# DEMO: submits one BUY/close and one SELL/close
.venv/bin/python test_api_key.py

# LIVE: real money; one terminal confirmation after risk preflight
.venv/bin/python test_api_key.py --REAL

# Read-only credential/account/market checks; no orders
.venv/bin/python test_api_key.py --read-only
```

`--real` remains an alias, and the legacy `--environment live` explicit selector
is supported. Both environments use the same unchanged permission-600 `.env`.
There is no endpoint fallback, automatic account switching, token sharing, or
mutation retry. Imports, `--help`, and the offline test runner do not start this
test. **The default invocation now places DEMO trades**; use `--read-only` for
credential validation. The old `--execute-demo-trade` flag is accepted as a
compatibility alias for the same two-roundtrip DEMO test.

`--symbol` and `--size` remain optional overrides. Size is validated against the
broker's minimum, maximum and increment; it is never rounded or replaced with
the broker minimum. Forex overrides use the supported contract mapping. LIVE
risk calculations support USD-quoted currency pairs with unit contracts and USD
accounts; unsupported mappings fail before any order. A non-default LIVE size
also requires matching `position_size` scope in verified fee evidence. Overrides
remain subject to the risk caps and stop-distance restrictions.

For LIVE, preflight verifies the account, instrument, available margin, exposure,
market direction restrictions, funding, applicable fees, stop distance and risk
limits for both directions. Risk defaults are the shared USD 5 margin, USD 120
exposure, USD 1 cumulative estimated charges and USD 2 drawdown limits, with a
0.001 price-unit broker stop. Optional `--max-margin`, `--max-exposure`,
`--max-charges`, `--max-loss`, and `--stop-distance` overrides remain available.
The integration test's opening-order cap is fixed at **two: one BUY and one SELL**.
The integration test supports both **hedging** and **normal (non-hedging)**
accounts. `hedgingMode` must be a known boolean and remain unchanged during the
test; no account preference is updated. BUY and SELL are sequential roundtrips,
with verified BUY closure before any SELL opening, so simultaneous opposite
positions are unnecessary. Existing EURUSD positions or working orders block
entry in either mode. The existing conservative account-wide rule also blocks
other open positions or working orders. Multiple LIVE
accounts require explicit selection of the already-active account with `--account`.

Positions and working orders are rechecked after risk preflight immediately before
each opening. In normal mode, positions are read again after the working-order
check to catch an intervening fill. The account mode is rechecked before entry
and position management. A confirmation must prove exactly one newly opened
position; a netted/amended/closed affected deal is not adopted as test ownership.
The close uses `DELETE /positions/{dealId}` for that proven position, verifies its
identity and size again, and confirms its absence before proceeding. It never
uses a new opposite-direction order to close the BUY.

[Capital.com's hedging help](https://help.capital.com/hc/en-us/articles/26358080503698-How-to-enable-hedging-mode)
describes simultaneous BUY and SELL positions as the hedging feature. The
[API reference](https://open-api.capital.com/) documents reading trading mode with
`GET /accounts/preferences`, confirmation with `GET /confirms/{dealReference}`,
and closing an exact position by ID. Capital Com Bel's published
[execution policy, section 4](https://trade.capital.com/docs/order_execution_policy_v2_0_nbrb_capital_com.pdf)
describes normal-mode opposite orders as reducing, closing or reversing existing
exposure; its same-direction positions can also coexist. These distinctions are
why existing target exposure is blocked instead of requiring hedging. The policy
is entity-specific; the integration test continues to verify actual API responses
and refuses ambiguous outcomes.

Keep other EURUSD trading activity stopped while this controlled test runs.
Account locks coordinate local integration runs, but API inventory reads and
order submission are not atomic against another app or person trading the account.
Unknown mode, detected concurrent exposure, a changed position, or an uncertain
order stops further mutations and requires reconciliation. The separate automated
LIVE strategy-loop gate retains its existing hedging requirement; this change
applies to the integration test.

LIVE missing fee data or unverified evidence still blocks execution. An existing
verified `.live-integration/fees.json` is reused, or select evidence with
`--fee-profile`; no fees are invented. Fees must cover the requested instrument,
account, size and planned holding costs. After preflight, type `LIVE` once in an
interactive terminal within 60 seconds to confirm both roundtrips. There is no
second confirmation before SELL. Checks are refreshed before each entry, so a
new risk failure can stop the test after BUY has safely closed.

The report displays execution, broker transaction P/L, fee cashflows and exposure
independently. Each side distinguishes confirmed closure from an order whose
outcome is uncertain and a side never attempted. Missing BUY reconciliation still
stops the workflow before SELL. That case prints `Overall: INCOMPLETE`, preserves
the successful BUY closure and returns status 1. `PASS` covers execution and trade
cashflow attribution; fee completeness is always reported separately. Missing
execution prices or unattributable P/L prevents a full PASS even after closure.

Transactions match the exact position `dealId`, because `reference` can contain
a separate ledger ID. A reference fallback is used only when no `dealId` is
supplied; explicit foreign IDs, currencies or markets cannot override that match.
Processed zero cashflows are valid. Five bounded history reads use explicit UTC
`from`/`to` windows rather than a moving ten-minute window. Fees remain separately
reported and incomplete when records are absent. No account balance change is
attributed to the test. Quote spread estimates are in account currency; they are
already embedded in fill-price P/L and must not be subtracted a second time.

The final account-wide positions snapshot is separate from the current run's
owned positions. A missing/failed check is `UNKNOWN`, never an assumed empty list.
Close confirmations must identify exactly the owned, fully closed position with
the correct opposite direction and size; an echoed reference alone is insufficient.

Safely display the saved result without credentials, network requests or orders:

```bash
uv run test_api_key.py --report-only
```

Saved position data is historical, and this command does not fetch new financial
records or overwrite the original report. Account-specific investigation reports
and recovered cashflow records remain private and are excluded from Git.
`test.py` still submits one SELL when run; its output now distinguishes submission
from fill confirmation and explicitly states that it does not close the position.

A lock and durable intent journal under `.demo-integration/` or
`.live-integration/` prevent concurrent integration tests and retries after
uncertain outcomes. Results are saved privately in `buy-sell-results.json` in the
selected directory. The deleted root-level reports are not recreated. A known
owned position is closed once even if post-open price or stop verification fails.
If a close fails or is uncertain, the script stops, never proceeds to SELL, and
prominently reports the known remaining position or an unknown-exposure warning.
No unrelated position is closed. A failed or interrupted run can require manual
reconciliation; never erase an unresolved journal simply to retry. Quotes, stops
and estimated costs cannot guarantee a maximum loss or successful closure.

Run mocked coverage without any network access using
`.venv/bin/python scripts/test_offline.py`. No actual DEMO or LIVE orders were
submitted while implementing this workflow.

## LIVE preflight and user-run test

`test_live_api.py` and `test_api_key.py` use the same permission-600 `.env`.
`test_live_api.py` defaults to DEMO read-only checks, and `--REAL` or `--real`
selects its read-only LIVE checker. `test_api_key.py` defaults to the BUY/SELL
trading workflow above; add `--read-only` for diagnostic use. The legacy `--environment live` explicit selector remains supported.
Conflicting selectors are rejected, and authentication failures never reroute. The following commands perform authentication and reads only:

```bash
# Explicit DEMO read-only checks
.venv/bin/python test_api_key.py --read-only
# Explicit LIVE selection; orders remain disabled.
.venv/bin/python test_live_api.py --REAL
```

The LIVE preflight checks authentication tokens, ping, session/account identity,
enabled LIVE CFD accounts, balance, available funds, leverage, EURUSD restrictions,
positions and working orders. It never switches accounts. Fixed official HTTPS
hosts, matching login/session/account IDs, active environment availability, and
any returned demo flag establish the selected environment. A CFD product type
alone is insufficient. There is no fallback between endpoints.

Account IDs come from `GET /accounts` and the active account from `GET /session`.
A single eligible active account needs no configured ID. If several eligible LIVE
accounts exist, the report lists masked accounts and selection keys; trading is
blocked until `--account SELECTION_KEY` explicitly selects the active account.
Selecting a different or unavailable account aborts; activate the intended account
through the platform before rerunning. Account selection is checked again before
execution. Strategy CLIs accept `--REAL`, `--real` and `--account` and use the shared
LIVE execution gate described above. Strategy loops must not be run as credential
validation: use the read-only integration checkers instead.

Requests use certificate verification, finite timeouts, no redirects/retries or
ambient proxy/netrc authentication, and at least 1.1 seconds between calls. Private
LIVE results/state are under Git-ignored `.live-integration/`; DEMO uses
`.demo-integration/`.
Successful read-only API checks are reported separately from trade readiness.

The older single-BUY `test_live_api.py` execution path still requires `--REAL` (or the legacy
`--environment live`), `--execute-live-trade`,
user-chosen `--max-margin`, `--max-exposure`, and `--max-charges` in USD (USD accounts only), a verified private fee profile,
explicit account selection when needed, and an exact fresh phrase typed in a TTY.
Unknown risk data, existing exposure/orders, exceeded limits or unresolved prior
state block execution. It permits one EURUSD BUY of size 100 and a position-specific
DELETE of only its confirmed new position; no separate SELL is used.

Quotes, margin and costs are estimates. A market fill can slip; margin or a stop
does not cap possible loss. Uncertain outcomes stop mutation attempts and trigger
read-only reconciliation. No automatic opening/closing retries are permitted.
No real-money orders were submitted during shared-configuration validation.

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

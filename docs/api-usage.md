# Capital.com API usage

The client implements all 36 REST operations and five WebSocket destinations in the
[coverage matrix](api-coverage.md), based on the [official reference](https://open-api.capital.com/).
The matrix distinguishes implementation, offline tests, DEMO verification, and intentional
LIVE policy restrictions. The DEMO verifier writes local JSON reports with request
statuses and cleanup evidence; generated reports are excluded from version control.
No strategy rules were added or changed.

## Authentication and environment selection

```python
from trading_bot.api import TradingApi

api = TradingApi(environment="demo", auto_authenticate=False)
if api.createNewSession() is None:
    raise RuntimeError("DEMO authentication failed")
account = api.verify_account_environment()  # Discovers the active enabled CFD account.
api.pin_session_identity(account)  # For a custom scheduled loop, call prepare_trading_cycle() before each cycle.
accounts = api.getAllAccounts()
preferences = api.getAccountPreferences()
# Do not log api credentials, api.cst, api.x_security_token, or arbitrary server payloads.
```

The default is DEMO. `environment="live"` explicitly selects the LIVE host for reads;
`real_account=True` remains compatible. Conflicting selectors raise an error. There is
no fallback between hosts. Credentials come from this checkout's private mode-600 `.env`
using the existing non-interpolating loader, or all three explicit constructor arguments.
Tokens live in memory and are never saved. Importing the package does not load credentials
or connect. Optional encrypted login is `api.createNewSession(encryptedPassword=True)`:
it retrieves the public key and follows the reference's RSA PKCS#1 v1.5 example, including
its inner Base64 encoding. The prose calls this AES, but the published executable example
uses RSA; this implementation follows that example.

Scheduled strategies validate their pinned account/session before each cycle and renew
conservatively after nine minutes of inactivity. See [scheduled session lifecycle](session-lifecycle.md)
for recovery rules, LIVE gate behavior and HMA read-only scheduling options.

REST sessions expire after ten minutes of inactivity. `api.pingService()` keeps the session
active and retains the legacy single login refresh for an unauthorized ping; refresh
invalidates a LIVE execution gate. Other authentication failures latch the client instead
of replaying requests. `api.logOutOfTheCurrentSession()` clears tokens even if logout
fails; an empty successful logout response is supported. LIVE logout is deliberately
blocked by the same transport policy as other newly covered LIVE mutations.

## Read examples

```python
# Historical EURUSD data: the legacy (payload, HTTP status) return shape is retained.
history, status = api.getHistoricalPrices(
    "EURUSD", "HOUR", 100,
    from_="2026-11-01T00:00:00", to="2026-11-05T00:00:00",
)
prices = history["prices"]  # Raw documented bid/ask OHLC fields are preserved.

search = api.searchMarkets("EURUSD")
all_markets = api.getAllMarkets()
several = api.getMarketsDetails(["EURUSD", "GOLD"])
details = api.getSingleMarketDetails("EURUSD")
positions, status = api.getAllPositions()
working_orders = api.getAllWorkingOrders()
sentiment = api.getClientSentimentForMarkets(["EURUSD", "GOLD"])
eurusd_sentiment = api.getClientSentimentForMarket("EURUSD")
```

Other reads: `getServerTime()`, `getSessionDetails()`, `getAccountPreferences()`,
`getAllTopLevelMarketCategories()`, `getAllCategorySubNodes(nodeId, limit=None)`,
`getsinglePosition(dealId)`, `getPositionOrderConfirmation(dealReference)`,
`getAllWatchlists()`, `getSingleWatchlist(watchlistId)`.

History accepts keyword-only `from_`, `to`, `lastPeriod`. Activity additionally accepts
`detailed`, `dealId`, and `filter` (FIQL); transactions accept `type` from the documented
transaction types. The client validates dates, ranges, bounds and enum values and encodes
query values using requests. FIQL is forwarded as an encoded string and its full grammar
is validated by the broker. Activity ranges are limited to one day and `lastPeriod` to
86400 seconds. Prices accept `from_`, `to`, all eight documented resolutions and 1–1000
values. Market batches support at most 50 epics; category node limit is at most 500.
The reference documents no pagination parameters, so none are sent. If both market
`searchTerm` and `epics` are supplied, the broker gives searchTerm precedence.

## Explicit mutations

| Impact | Authorization | Behavior |
| --- | --- | --- |
| Existing position open/close | Existing DEMO behavior; LIVE requires the existing execution gate | Preserves all LIVE preflight, fees, exposure, margin, loss, stops, ownership, journal and reconciliation checks |
| Position update | `position-update`, bound to the selected deal ID | DEMO only; full documented stop/profit fields |
| Working order create | `working-order` without resource ID | DEMO only; LIMIT/STOP, level, goodTillDate and stop/profit settings |
| Working order update/cancel | `working-order`, bound to the selected deal ID | DEMO only; update supports level, expiry and stop/profit settings, not undocumented size/type changes |
| Account switch | `account-switch` | DEMO only; validates the enabled target, consumes rotated session headers, verifies the resulting account, invalidates old grants and streams |
| Preference edits | `preferences` | DEMO only; sends only explicitly supplied leverage/hedging changes; validates leverage against available choices |
| Balance adjustment | `demo-balance` | DEMO host and account verified again; amount −400000 to 400000; resulting balance at most 100000 |
| Watchlist edits | `watchlist` | DEMO only; explicit authorization separate from trading; name length 1–20 |
| Logout | Calling the session logout method | DEMO only under the current mutation policy |

Grants are client-, token-, session generation-, account-, impact- and optionally
resource-bound. Account identity is rechecked immediately before new mutations. A grant
for creating an order does not authorize editing or canceling an existing one. Selecting
an existing resource is explicit caller intent; applications must additionally prove
ownership before automating cleanup. The integration verifier does this through accepted
confirmations, baseline comparison, and size/direction/instrument checks.

```python
# Isolated DEMO watchlist example; the grant itself changes nothing.
grant = api.authorize_mutation("watchlist")
created = api.createWatchlist("DEMO research", ["EURUSD"], authorization=grant)
watchlist_id = created["watchlistId"]
api.addMarketToWatchlist(watchlist_id, "GOLD", authorization=grant)
api.removeMarketFromWatchlist(watchlist_id, "GOLD", authorization=grant)
api.deleteWatchlist(watchlist_id, authorization=grant)

# Explicitly selected position; verify it is yours before editing.
# grant = api.authorize_mutation("position-update", resource_id=owned_deal_id)
# submitted = api.updatePosition(owned_deal_id, stopDistance=0.01, authorization=grant)
# confirmation = api.getPositionOrderConfirmation(submitted["dealReference"])
```

Position create/update and working-order create/update accept the documented
`guaranteedStop`, `trailingStop`, `stopLevel`, `stopDistance`, `stopAmount`, `profitLevel`,
`profitDistance`, `profitAmount`. Incompatible combinations are rejected. Broker dealing
rules, product restrictions (including real-stock stop restrictions) and hedging/guaranteed
stop restrictions remain authoritative. Explicit false values are preserved.

New LIVE working-order writes, position updates, account switches, preferences, balances,
watchlists and logout intentionally fail before HTTP transmission. The existing LIVE
risk policy only validates its established EURUSD open/close workflow; authentication or
a generic boolean cannot safely authorize different impacts. These operations have full
REST implementations for DEMO and mocked contracts, but no newly approved LIVE execution
policy. This is a documented limitation, not a claim of unrestricted LIVE completeness.

## WebSocket quotes

```python
from trading_bot.api import CapitalWebSocketClient

with CapitalWebSocketClient(api, max_reconnects=3) as stream:
    stream.subscribe_quotes(["EURUSD"])
    for _ in range(10):
        event = stream.receive(timeout=30)
        if event and event["destination"] == "quote":
            quote = event["payload"]
            print(quote["epic"], quote["bid"], quote["ofr"])
    stream.unsubscribe_quotes(["EURUSD"])
```

## WebSocket OHLC

```python
with CapitalWebSocketClient(api) as stream:
    stream.subscribe_ohlc(["EURUSD"], ["MINUTE", "HOUR"], type="classic")
    stream.subscribe_ohlc(["EURUSD"], ["MINUTE"], type="heikin-ashi")
    for _ in range(10):
        event = stream.receive(timeout=30)
        if event and event["destination"] == "ohlc.event":
            bar = event["payload"]
            print(bar["epic"], bar["resolution"], bar["type"], bar["c"])
    stream.unsubscribe_ohlc(["EURUSD"], resolutions=["HOUR"], types=["classic"])
    stream.unsubscribe_ohlc(["EURUSD"])  # All remaining resolutions/types for that epic.
```

The official shared streaming URL is
`wss://api-streaming-capital.backend-capital.com/connect`; authentication tokens determine
the account/environment. The client does not derive or invent a DEMO streaming hostname.
All outbound messages carry authenticated cst/securityToken fields and unique correlation
IDs. Subscriptions are committed only after matching PROCESSED acknowledgments. The client
supports both candle types and all eight REST resolutions. One caller must own the
synchronous stream's operations; concurrent receive/subscribe calls are not supported.

The combined unique-instrument limit across quotes and candles is 40. Subscriptions are
deduplicated, including overlapping candle resolutions. `receive()` sends application
pings every 300 seconds while polled; call `ping()` yourself if you are not polling.
Protocol-level WebSocket pings alone do not renew the Capital.com application session.
Acknowledgments have a finite timeout and intervening events use a bounded buffer.
Errors, malformed events and expired sessions never cause automatic login. Disconnections
can restore only previously acknowledged subscriptions for the same account/session,
with exponential backoff and a lifetime limit of three reconnect attempts by default.
An account switch, token rotation or new login requires a new streaming client. Recovery
never submits orders or changes accounts. Transport logging is disabled for this stream,
and inbound credential echoes are redacted before returning events or acknowledgments.

## Errors, rates and uncertain executions

`TradingUnavailable` remains a `SystemExit` subclass so legacy strategy loops cannot
swallow an availability failure and continue trading. It exposes fixed `reason`, optional
`status_code`, allowlisted `broker_code`, and `outcome_uncertain`. It never exposes raw
response text or the original network exception in its message. `ValueError` rejects local
invalid parameters and `PermissionError` rejects unauthorized impacts. `StreamError`
contains fixed streaming diagnostics. Successful REST responses must be JSON objects;
logout additionally accepts an empty response. Documented confirmation 404 means not yet
available and returns `None` for bounded polling.

All HTTP traffic is HTTPS with certificate verification, timeouts and redirects disabled.
Requests are paced across clients in this process: below 10/second per login identifier,
login below 1/second per API key, DEMO position/order creation at most 1000/hour, and
DEMO top-up at most 100/day per verified account. The broker enforces aggregate quotas
across other processes/API keys. Separate processes need a shared coordinator if used
concurrently. HTTP 429 fails closed without replay. No order or account mutation is
blindly retried, including timeouts, redirects, malformed replies and server errors.

A returned `dealReference` is a submission acknowledgment, not proof of execution.
Poll `getPositionOrderConfirmation()` with a bounded deadline and verify `dealStatus`,
reference, affected deals, instrument, direction, size, and the actual position/order.
A stopped client remains stopped. Reconciliation after a timeout needs a fresh verified
read-only session and the saved intent/reference plus current positions/orders/history;
do not submit another order just because a response was lost. The existing LIVE gate
and integration tools retain their durable journals and reconciliation requirements.

## Running tests

```bash
uv sync --locked
uv run trading-bot-check-offline
uv run python -m unittest discover -s tests -p test_api_coverage.py
uv run python -m unittest discover -s tests -p test_websocket_client.py
uv run python -m unittest discover -s tests -p test_api_verification_safety.py
uv build
```

The offline runner blocks socket connections, requests sends and credential loading.
`test.py` is an order-capable strategy entry point; it remains unchanged and must not be
used as an offline test runner.

```bash
# Read-only broker verification, using a separate report to preserve prior trade evidence:
uv run python -m trading_bot.tools.api_integration --encrypted-login --report docs/demo-api-readonly-verification.json

# Explicit opt-in: one minimum-size EURUSD BUY and close, one distant short-lived LIMIT
# order/update/cancel, and an isolated temporary watchlist/create/edit/delete:
uv run python -m trading_bot.tools.api_integration --execute-demo
```

The verifier checks the final prepared DEMO host before every HTTP transmission, and
requires exact single-use mutation intent recorded durably before sending. It verifies
DEMO account identity before every trading mutation, refuses existing EURUSD exposure,
uses the minimum broker size, tracks confirmed affected IDs, and verifies cleanup.
The working order has a three-minute expiry as an additional bound. A private mode-600
journal under `.demo-integration/api-verification/` blocks a subsequent trading run if
anything remains unresolved; do not delete that journal to retry. Unsupported/rejected
broker operations may stop verification safely and must be investigated separately.
The report records attempts, statuses, cleanup and unresolved/uncertain operations.
No account switch, preference update or balance adjustment is needed for this test.
LIVE mutations are tested only with mocks.

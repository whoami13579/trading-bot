# Scheduled strategy authentication

Capital.com documents a ten-minute inactivity lifetime for REST session tokens in its
[official reference](https://open-api.capital.com/). A successful login at strategy startup
does not keep the session alive while the process sleeps until a candle boundary.

Before this fix, `src/trading_bot/strategies/hma.py:57–58` in commit `6fe8c96` waited until
the next scheduled minute and then called `load_keys()`. That compatibility method is
intentionally a no-op: `.env` must never be a session-token cache. The first subsequent
HMA call was `calculate_hma_result()` (`strategies/indicators.py:119`), which requests
`hma_period * 3` prices. With the production defaults the failing request is:

```text
GET /api/v1/prices/EURUSD?resolution=MINUTE_30&max=48
```

Both CST and X-SECURITY-TOKEN headers were constructed from the in-memory startup session.
After inactivity they could be stale, and the common HTTP client correctly stopped on
401. Its existing refresh path belonged to `pingService()`, which HMA did not call between
the wait and history retrieval. Loading credentials from disk would not fix this lifecycle.

## Cycle boundary

The shared factory verifies the initial account and pins its environment, exact base URL
and account ID. HMA, EMA/HMA and Multiple HMA call `prepare_trading_cycle()` after the
scheduler and any market-opening wait, before calculating signals. SMA calls it before
each latest-price cycle; the moving-average strategy calls it before updating its buffer.
The shared ping utility also uses this path once its account is pinned. During HMA-family
scheduler waits, the scheduler invokes this read-only validation every five minutes.
Healthy sessions therefore keep their tokens and existing LIVE gate without re-login.
The final cycle check remains necessary after suspension, long market-opening waits,
revocation or an unexpected expiration.

The API tracks the monotonic time of successful authenticated REST requests. Public time
and encryption-key requests do not extend this timer. At a cycle boundary:

1. A previously stopped client remains stopped. Availability failures and uncertain order
   outcomes cannot be cleared by renewal.
2. If inactivity reaches 540 seconds, create one new session on the already-selected host.
   This leaves a safety margin before the documented 600-second deadline. Shorter cycles
   validate with an authenticated ping and keep a healthy session alive without re-login.
3. A 401 from that dedicated read-only ping allows one renewal. A 403, timeout, rate limit,
   malformed response or connection failure stops without attempting login recovery.
4. Recheck session details and enabled account discovery against the pinned environment,
   exact URL and account ID. Reject changes; never send `PUT /session` to switch back.
5. Verify a renewed session with a fresh ping. A second 401 is terminal. Resume DEMO
   strategy work only after these checks pass.

There is no automatic replay of history requests or trading mutations. A 401 received
outside the cycle probe still stops the client. This deliberately confines recovery to
an operation with no execution outcome to reconcile. Failed renewal clears the tokens
and disarms execution. Interrupted requests also invalidate the client and propagate the
interrupt; catching the interrupt cannot make a subsequent cycle resubmit an order.

## LIVE behavior

The selected environment never changes and no fallback host is attempted. After renewal,
an existing LIVE execution gate remains disarmed. The scheduled cycle stops with
`live_session_renewed_requires_preflight`, before any signal or order. The gate object,
old token binding, ownership, pending intent, order/cost/loss limits, account lock and
journal remain intact. Renewal neither calls `arm()` nor replaces the gate with one that
has fresh counters. Reconciliation and a new full preflight are required before trading
can resume. In particular, an owned or uncertain position must not be discarded to make
a restart pass. Read-only LIVE clients have no gate to rearm and can renew for reads;
this change was tested on LIVE only with mocks.

This conservative policy means an unattended LIVE strategy may stop if a session
actually needs renewal despite scheduled keepalives. It is preferable to implicitly reauthorizing real-money execution after tokens
change. No LIVE broker requests are needed to verify the behavior.

## HMA scheduling and safe verification

The production candle and scheduling default remains **MINUTE_30**. Existing
`--time_frame` remains supported, with `--time-frame` as an alias. Both select candles
and, unless overridden, the corresponding schedule. The new `--schedule-time-frame`
changes only wake-up frequency, leaving candle resolution and HMA signals unchanged.
Supported minute schedules are MINUTE, MINUTE_5, MINUTE_15 and MINUTE_30. The existing
two-second post-boundary settling delay is retained.

Verify the actual HMA loop without computing signals or submitting orders:

```bash
uv run hma.py --read-only --schedule-time-frame MINUTE --cycles 3
```

This checks three scheduled boundaries using the normal MINUTE_30 history. `--read-only`
is a DEMO-only diagnostic mode and rejects LIVE flags before authentication. It does not
call any indicator/signal function or any position/order mutation. Authentication failures
stop immediately; malformed read-only results also exit with a fixed failure message.
`--cycles` bounds the number of scheduled attempts. Omitting `--read-only` runs the normal
order-capable strategy, so retain it when checking authentication alone.

A faster bounded diagnostic uses one-second read-only cycles without waiting for clock
boundaries:

```bash
uv run python -m trading_bot.tools.session_diagnostics --cycles 3 --interval-seconds 1
```

To reproduce real inactivity and verify renewal in the same isolated session:

```bash
uv run python -m trading_bot.tools.session_diagnostics --idle-seconds 610 --probe-expired --cycles 3
```

The last command intentionally probes the original history GET after the idle wait
(at most two read-only network attempts if the first receives no HTTP response),
without going through the production client's fail-closed latch. It then invokes the
fixed cycle boundary and performs three history reads. This exception exists only in
the read-only diagnostic: production strategy calls never bypass the latch. The final
prepared-request guard allows only the exact DEMO host, session creation, and GETs for
ping, session details, accounts and EURUSD history. Login attempts and total requests are
bounded. Reports contain HTTP methods, paths, statuses and header-presence booleans;
credentials, tokens and account IDs are excluded. No credentials or private trading-state
files are written.

Do not run another login probe concurrently with the inactivity check, because that could
confound session-expiration evidence. There is no need to run the existing order-capable
integration tools to verify authentication.

## Regression tests

```bash
uv run python -m unittest discover -s tests -p test_session_lifecycle.py
uv run python -m unittest discover -s tests -p test_scheduled_sessions.py
uv run trading-bot-check-offline
```

The controlled broker/clock tests reproduce the original expired history GET with both
headers present, then verify renewal before history retrieval; new headers; healthy short
cycles without unnecessary login; repeated long cycles; account/host/environment drift;
401/403; failed login; timeouts; interruption; mutation non-replay; preserved LIVE gates;
and scheduling/read-only CLI behavior. Existing strategy signal calculations are unchanged.

## Earlier execution verification scope

This section records the earlier investigation under its separate execution
authorization. No trades were repeated during the publication review below.

After separate explicit authorization, a controlled DEMO test opened one minimum-size
EURUSD BUY (100 units) following cycle validation and closed only its confirmed affected
position. Both acceptance and closure were confirmed, baseline positions were preserved,
and no test exposure remained. The test used the existing guarded DEMO transport and a
new temporary journal outside the repository; existing private trading state was untouched.
This was a controlled API execution check, separate from the read-only expiration probe.
It did not run an unbounded automated strategy or modify account preferences.

Intermittent WSL DNS failures interrupted the first real probes before any order was
submitted. Diagnostic-only processes subsequently used previously OS-resolved DEMO IPv4
addresses, independently checked with the broker's public time endpoint, while retaining
the exact original URL, TLS SNI and certificate verification. No DNS workaround was added
to the application or system settings. Production DNS/network failures continue to stop
safely and remain separate from HTTP 401/session-expiration handling.

## Verification on 2026-11-12

The isolated real DEMO session reproduced the exact problem after **610.0 seconds**:
the original history GET returned **401**, although both original authentication headers
were present and unchanged. A single new `POST /session` returned 200. Session/account
verification passed against the original pinned identity, followed by three history GETs
returning 200 with 48 prices each. Only the first cycle renewed; the following two reused
the healthy session. All 18 HTTP requests stayed on the DEMO host. The diagnostic computed
no signals and sent no trading mutations. This confirms session expiration, rather than
missing headers or incorrect credentials, on the reproduced path.

Original offline result: **394 passed, zero failures/skips**. The targeted suites contain
31 lifecycle tests and 22 scheduling/diagnostic tests, in addition to 341 existing tests.
The first targeted run exposed four mutation-authorization subcase errors after a client
had stopped; adding the availability check to authorization issuance fixed these and
preserves the fail-closed exception contract. Earlier real probes were interrupted or
blocked by connection/DNS failures and did not establish an HTTP authentication outcome;
the isolated certificate-verified run above supersedes them. No failed execution attempt
submitted an order. The separate authorized EURUSD BUY/close test passed, with minimum
size 100 and confirmed absence of test exposure afterward.

Validation commands included:

```bash
uv run trading-bot-check-offline
uv run python -m unittest discover -s tests -p test_session_lifecycle.py
uv run python -m unittest discover -s tests -p test_scheduled_sessions.py
uv run python -m compileall -q src/trading_bot
uv build
uv run hma.py --help
uv run python -m trading_bot.tools.session_diagnostics --help
git diff --check
```

Broker verification used the diagnostic command above and an actual HMA MINUTE-scheduled
read-only cycle with the MINUTE_30 candle default. A temporary wrapper applied the
in-memory DNS workaround only to these diagnostic processes; the wrapper is not part of
the application. The controlled execution check reused the existing `DemoTransport`,
`TradeJournal` and `legacy_demo.execute_trade` helpers with a new `/tmp` journal. It
performed one opening and one exact position DELETE, with confirmation and inventory
checks on both sides. `test.py` and existing credentials/private trading-state files were
not modified. No LIVE requests or mutations were performed, and no changes were committed
or pushed.

## Publication review

The remaining 17 session-lifecycle files were reviewed separately from the
historical-backtesting commit `ea3debdc867d761e8f686c065211889cc6d81a95`.
The review confirmed that renewal keeps the pinned host, environment and account;
never switches accounts; and leaves an existing renewed LIVE execution gate
disarmed. The existing gate, ownership, pending intent, counters, lock and journal
are preserved. Stopped or interrupted requests cannot be revived by a cycle check,
and no position/order operation is automatically replayed.

Current validation: **430 offline tests passed, zero failures/skips**, including
31 lifecycle tests, 22 scheduling/diagnostic tests and 36 historical-backtesting
tests. The environment checker, wheel/sdist build and whitespace checks passed.
HMA, SMA, EMA/HMA, Multiple HMA and Moving Average retain their existing signal
logic; historical mode still returns before broker authentication. HMA's
production default remains MINUTE_30.

Two fresh allowlisted DEMO read-only diagnostics were attempted with the
610-second expiration probe configured. Both authenticated successfully with
`POST /session` HTTP 200. Session discovery succeeded in both; account discovery
succeeded in the first attempt. Intermittent DNS resolution then failed on the
first attempt's history GET and the second attempt's account GET. Both clients
stopped safely before reaching the idle wait; no scheduled cycles completed.
These attempts verify login and network-failure handling, but **do not freshly
verify real expiration or renewal**. The earlier successful reproduction above
remains historical evidence; the current expiration/renewal checks are automated
broker/clock regressions. No DNS workaround or production retry was added.

No DEMO orders, LIVE API requests, account mutations, credential writes or private
journal writes occurred during this review. Actual trading was not rerun and
LIVE renewal/gate behavior remains verified only with mocks.

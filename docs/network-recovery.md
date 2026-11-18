# Network retries, pause and recovery (HMA)

Before this change, a single transient DNS failure stopped the scheduled HMA strategy
for good, for example on the cycle ping `GET /api/v1/ping` with
`exception_category=dns_resolution_failed`. `TradingApi._request` turned every transport
exception into a permanent, fail-closed stop: `TradingUnavailable`, a `SystemExit`
subclass that legacy `except Exception` loops cannot swallow. It also cleared the session
tokens and latched the client so no later request could be sent. The HMA loop did not
handle that stop, so the process exited and needed a manual restart.

The fail-closed latch is intentionally unchanged. Instead:

1. **Read-only GET requests** may be retried a few times inside one request.
2. If the retry budget runs out, HMA **pauses**. It probes connectivity, then builds a
   **new** client that re-authenticates, re-verifies the original account and reconciles
   broker state before resuming. The stopped client is never revived.
3. Missed candle boundaries are **skipped**, never replayed.

This applies to `hma.py` / `trading-bot-hma`, including `--read-only`. SMA, EMA/HMA,
Multiple HMA, Moving Average and the diagnostic/integration tools keep their existing
single-attempt, fail-closed behavior.

## 1. Read-only request retries

| Retried (GET only) | Never retried |
| --- | --- |
| DNS resolution failure | HTTP 401 / 403 (authentication or permission) |
| Connection failure or reset | TLS/certificate failures (possible interception) |
| Connection, read or other timeout | Unclassified request errors, HTTP 400/404/500 |
| Proxy connection failure | Invalid or non-JSON responses |
| HTTP 429, 502, 503, 504 | **Every** POST, PUT and DELETE, including login and logout |

The default is 3 retries after the first attempt, with delays near **2s, 4s and 8s**
(exponential backoff, ±20% jitter, each delay capped at 30s). A read is sent at most
four times; there is no unbounded retry inside a request. Every attempt still passes
the local rate limiter. Each retry and the final exhaustion are logged.

Retries are opt-in per client. `TradingApi.configure_read_retries(policy)` enables them,
and HMA enables them after startup. Any other `TradingApi` keeps one attempt per request.

## 2. Paused state and recovery (DEMO)

When a read's retry budget is exhausted, or a transient failure ends a cycle in some
other way (for example a 503 during session renewal), HMA follows these steps:

1. **Classify.** The stop is examined *before* any recovery:
   * An uncertain trade-changing request stops the bot (see §4).
   * A non-transient reason stops the bot: authentication, permission, identity change,
     malformed response, TLS or interrupt.
   * A LIVE session stops the bot (see §5).
   * `--no-network-recovery` stops the bot.
2. **Pause.** No signals are calculated and no orders are sent. The log records
   `Trading paused reason=... recovery_interval_seconds=...`.
3. **Probe** every `--recovery-interval` seconds (default 60, ±10% jitter) with the
   public `GET /api/v1/time`. The probe sends no credentials and makes one attempt.
   **No login is attempted while the network is down**, so an outage does not
   accumulate login attempts.
4. **Re-authenticate** after a successful probe. A brand-new client with empty tokens
   makes one `POST /session` to the same DEMO host. It then rediscovers the session and
   enabled accounts and requires the **same account ID** pinned at startup. Accounts are
   never switched. A login rejected with 401/403 stops recovery immediately; it does not
   loop on bad credentials. A login that fails with a transient error waits for the next
   probe.
5. **Reconcile broker state.** `GET /positions` and `GET /workingorders` are read and
   validated, and the counts are logged: total, strategy-symbol positions and working
   orders. Malformed data stops the bot. HMA derives every decision from these live broker
   records at its next cycle, so no position or order is reconstructed from memory.
   HMA never creates, changes or cancels working orders; existing working orders on the
   symbol are reported as a warning.
6. **Resume.** The new client, which keeps the same retry policy, replaces the stopped
   one. The scheduler then waits for the **next future boundary**.

`--max-outage SECONDS` stops the bot once the pause has lasted that long. The default of 0
waits indefinitely, which suits unattended operation. While paused, the bot logs every
failed probe, so a long outage is visible in the logs.

A transient failure **during startup**, before any trade, follows the same pause/probe
loop using the normal startup factory. Authentication failures at startup still stop
immediately.

## 3. Schedule integrity

* The scheduler only targets future boundaries, so a boundary missed while paused is
  never executed late. `wait_until_targets()` now returns the boundary it waited for, as
  an aware UTC datetime.
* Each boundary is executed **at most once**. A boundary that is not later than the last
  executed one is skipped, which covers a clock that steps backwards.
* A cycle is **stale** once more than `--max-signal-delay` seconds (default 120), or one
  full schedule interval, have passed since its boundary. Staleness is checked when the
  cycle starts, before the signal is calculated, and again immediately before each order
  request. A stale cycle calculates no signal and submits nothing. This also covers a host
  that slept or suspended (common on WSL laptops) and long renewals. With `--days 5`, the
  first boundary after a weekend market-open wait is skipped rather than traded late.
* Boundaries compare as full UTC datetimes, so recovery across midnight, month or year
  ends schedules normally.
* The loop is single-threaded: keepalive pings, cycles and recovery never overlap.
* `--cycles N` still counts attempted boundaries. A boundary that failed and caused a pause
  counts.

## 4. Trading safety guarantees

* **Trade-changing requests are never retried**: opening, closing or updating positions,
  and creating, updating or deleting working orders. The retry policy is consulted only
  for `GET`, and a second check rejects any other method.
* A transport failure or HTTP error on a trade-changing request marks the outcome
  **uncertain**, and the existing latch blocks every later request. The supervisor checks
  for this first and **stops without recovery**:

  ```text
  Trading stopped reason=read_timeout environment=DEMO: a trade-changing request has an
  uncertain outcome. It is never resubmitted automatically. Manual reconciliation of
  positions and working orders is required before restart.
  ```

  The DEMO HMA path receives no deal reference when the send itself fails, so the result
  cannot be proven from confirmations. Pausing and auto-resuming would risk a duplicate,
  so the bot stops instead. `TradingApi.recovered_client()` also refuses to build a
  replacement after an uncertain outcome.
* The existing single-send protections are unchanged. These include the local order quota,
  the LIVE gate's durable pending-intent journal, confirmation polling without resubmission
  and ownership checks.
* Ctrl+C or SIGTERM during an order request is **deferred until that single request
  returns**, so stopping never creates an uncertain order. A second signal forces an
  immediate interrupt; the order is then reported as uncertain.

## 5. LIVE

LIVE never pauses and resumes automatically. A new LIVE session always needs the full LIVE
preflight and a fresh execution gate, and the existing policy is never to reauthorize
real-money execution implicitly. After LIVE read retries are exhausted, the strategy stops
exactly as before:

```text
Trading stopped reason=dns_resolution_failed environment=LIVE: LIVE trading never resumes
automatically; restart the strategy to run the full LIVE preflight
```

`recovered_client()` raises `PermissionError` for LIVE, even if called directly. Bounded
**read-only** retries do apply to LIVE GETs such as pings, account and market reads,
position checks and order confirmation polling. They never touch the session, tokens or
the execution gate, and LIVE `POST`/`DELETE` sends inside the gate are never retried.
LIVE behavior is verified with mocks only.

## 6. Configuration

Defaults are in `src/trading_bot/config/strategy_defaults.py`. Every value can be
overridden on the command line without code changes:

| Option | Default | Meaning |
| --- | --- | --- |
| `--network-retries N` | 3 | Retries per read after the first attempt (0–10; 0 disables) |
| `--network-retry-delay S` | 2.0 | Seconds before the first retry; doubles per retry |
| `--network-retry-max-delay S` | 30.0 | Cap for one retry delay |
| `--network-retry-jitter F` | 0.2 | ±fraction of randomization per delay (0–0.99) |
| `--recovery-interval S` | 60.0 | Seconds between connectivity probes while paused (1–3600) |
| `--max-outage S` | 0 | Stop after this many paused seconds; 0 = wait indefinitely |
| `--max-signal-delay S` | 120.0 | Skip a cycle that starts later than this after its boundary |
| `--no-network-recovery` | off | Stop on an outage instead of pausing |

`--network-retries 0 --no-network-recovery` restores the previous fail-closed behavior.
Invalid values are rejected before credentials are loaded or any request is made.

## 7. Logging

Runtime events go to **stderr** with UTC timestamps. Strategy output such as
`>>> BUY order submitted` stays on stdout. Logs contain only fixed reason codes,
environment, endpoint templates, counts and durations. They never contain API keys,
identifiers, passwords, CST/X-SECURITY-TOKEN values, account or deal IDs, URLs, query
strings or exception text. An application that configures `logging` itself keeps its
own handlers.

| Event | Example |
| --- | --- |
| Transport failure | `API transport failure ... endpoint=/api/v1/ping ... exception_category=dns_resolution_failed` |
| Retry and backoff | `API read retry scheduled ... retry=1 max_retries=3 delay_seconds=2.17` |
| Budget exhausted | `API read retries exhausted ... attempts=4` |
| Paused | `Trading paused reason=dns_resolution_failed environment=DEMO phase=scheduled ...` |
| Still down | `Connectivity not yet recovered reason=... attempt=3 outage_seconds=44` |
| Reauthenticated | `Session reauthenticated environment=DEMO; account identity and environment match ...` |
| Reconciled | `Broker state reconciled positions=0 symbol_positions=0 working_orders=0 ...` |
| Recovered | `Connectivity recovered environment=DEMO attempt=5 outage_seconds=78` |
| Resumed | `Scheduled trading resumed environment=DEMO; missed boundaries are skipped ...` |
| Skipped cycle | `Scheduled cycle skipped reason=stale_boundary stage=order ...` |
| Unrecoverable | `Trading stopped reason=authentication_failed_http_401 ...` |

## 8. Stopping and restarting safely

* **Stop:** press Ctrl+C or send SIGTERM. The exit code is 130 or 143. The bot finishes an
  in-flight order request first and never closes positions on exit; broker stops (LIVE)
  remain in place. The final log line says whether an order outcome is uncertain.
* **Restart after a network stop** (for example `--max-outage` reached, or recovery
  disabled): restarting is safe once connectivity is back. HMA reads broker positions
  every cycle and opens a position only when none exists for the symbol.
* **Restart after an uncertain order outcome:** first check Capital.com for positions and
  working orders on the symbol, and confirm whether the last open/close took effect.
  DEMO HMA has no durable journal, so a restart does not remember the uncertainty. LIVE
  restart is blocked by the gate's journal until it is reconciled; never clear that state
  just to retry an order.
* **Prolonged outage:** the bot stays paused, probing every interval and logging each
  attempt. It never trades on stale data. When connectivity returns, it re-authenticates,
  reconciles and continues from the next boundary. Use `--max-outage` if an external
  supervisor such as systemd should restart or alert instead.

## 9. Verification

```bash
uv run --locked scripts/check_offline.py                       # full offline suite
uv run python -m unittest tests.test_network_recovery          # issue #3 tests
uv run hma.py --read-only --schedule-time-frame MINUTE --cycles 3   # DEMO, no signals/orders
```

`tests/test_network_recovery.py` drives the real `TradingApi`, scheduler, supervisor and
HMA loop against a stateful offline broker on a deterministic clock. It covers:

* temporary and exhausted DNS failures
* timeouts and connection resets
* authentication errors
* prolonged outages and `--max-outage`
* reconciliation of existing positions
* ambiguous open and close outcomes
* duplicate prevention
* skipped and stale candles
* recovery across midnight
* shutdown during recovery
* log redaction

On 2026-11-18 a DEMO `--read-only` run (MINUTE schedule, 4 cycles) simulated a 100-second
in-process DNS outage. This used a diagnostic wrapper outside the repository; no DNS
workaround is in the application. The cycle ping failed 4 times with backoff
2.17s/4.45s/6.73s. The bot paused and failed 4 public probes without any login. On
probe 5 it re-authenticated to the same account, reconciled 0 positions and 0 working
orders, skipped the missed 13:45 boundary, and completed cycles 3 and 4. No signals were
calculated, no orders were sent and no LIVE request was made.

## 10. Limitations

* Only HMA uses pause/resume. The other strategies keep the previous fail-closed behavior.
* LIVE never auto-resumes (by design, see §5).
* DEMO HMA keeps no durable journal. An uncertain outcome stops the process, and a manual
  restart relies on the operator's reconciliation.
* Recovery does not validate candle timestamps. Freshness is enforced through boundary age
  and live retrieval at the cycle time, and the HMA signal rule itself is unchanged.
* A second HMA process on the same DEMO account is not prevented. The LIVE gate's account
  lock still applies to LIVE.
* The scheduler still uses local wall-clock minutes. Around a DST fall-back, the repeated
  local hour's boundaries are rejected as already executed rather than run twice.

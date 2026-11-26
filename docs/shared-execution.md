# Shared single-position online execution

Issue #9. Every order-capable strategy (moving-average, sma, hma, ema-hma,
multiple-hma) runs one online lifecycle,
`src/trading_bot/execution/single_position.py`. A strategy module supplies only
its market-data reads and a pure decision. Scheduling, signal freshness,
broker-confirmed position state, ownership, order confirmation, network recovery,
session renewal, graceful shutdown and LIVE gating are shared.

Offline tests do not establish LIVE readiness. No strategy is certified for real
money by this change. See the [DEMO validation plan](demo-validation-plan.md).

## Architecture

```text
tb-trade --strategy KEY            (tools/trade.py: selection only, no broker code)
    |
strategies/registry.py             (capabilities; resolves the strategy main())
    |
strategies/<key>.py                (options, defaults, signal inputs, pure decision)
    |  SinglePositionStrategy(key, symbol, size, schedule, signal, target, probes, days)
    v
execution/single_position.py       (SinglePositionExecution)
    +-- utils/scheduler.wait_until_targets     UTC candle boundaries, keepalive, polling
    +-- utils/network_recovery.CycleGuard      one run per boundary, never stale
    +-- utils/network_recovery.PendingOrders   DEMO confirmation + position visibility
    +-- utils/network_recovery.NetworkSupervisor  DEMO pause/resume; LIVE never resumes
    +-- utils/network_recovery.GracefulShutdown   SIGINT/SIGTERM deferred during an order
    +-- Ownership                               DEMO: from confirmations; LIVE: gate record
    +-- risk/live_execution.LiveExecution       LIVE gate, bound to the cycle deadline
    |
api/client.TradingApi              (Capital.com REST; read retries; mutation guards)
```

Nothing above was copied: the engine calls the existing classes. The only
changes to them are a confirmation callback on `PendingOrders` (to record
ownership) and `TradingApi.forbid_mutations()` (to back `--read-only` at the
client). The engine is imported only by online entry points, never by
backtesting.

## Strategy decision interface

```python
SinglePositionStrategy(
    key="hma", symbol="EURUSD", size=100,
    schedule=[0, 30],                     # minute boundaries
    signal=lambda api, boundary: ...,     # market-data reads; None = unavailable
    target=lambda value, side: ...,       # pure: BUY, SELL, FLAT or HOLD
    probes=(("MINUTE_30", 48),),          # history reads of --read-only cycles
    days=7,                               # 5: wait while the FX market is closed
)
```

- `signal(api, boundary)` reads market data for the cycle boundary and returns
  any strategy-specific value. **None means the signal is unavailable** (a
  missing completed candle, short history, the moving-average startup wait): the
  cycle ends with no position read and no order.
- `target(value, side)` is pure. `side` is the verified side of the
  strategy-owned position (`"BUY"`, `"SELL"`, or None when it owns none). It
  returns:
  - `BUY` / `SELL`: hold a position on that side (open one if flat);
  - `FLAT`: hold no position (close the owned one);
  - `HOLD`: keep whatever is verified (no order).

  Anything else stops trading (`invalid_strategy_target`).

The decisions reuse the rules shared with the backtests in
`strategies/signals.py`:

| Strategy | Signal value | Target |
| --- | --- | --- |
| hma | `calculate_hma_result(..., boundary)`: completed candles only | `configured_side_target(signal, direction) or FLAT` |
| ema-hma | `(EMA trend, HMA trigger)` from the newest candles | `agreement_target(trend, trigger, side) or FLAT` |
| multiple-hma | `(4H vote, 30m HMA trigger)` from the newest candles | `agreement_target(vote, trigger, side) or FLAT` |
| sma | newest 21 ask opens | `reversal_target(sma_side(...), side) or HOLD` |
| moving-average | newest 20 ask opens, after the startup wait | `reversal_target(moving_average_side(...), side) or HOLD` |

Multiple timeframes: `signal` may read any number of timeframes.
`utils/candles.completed_closes(payload, timeframe, boundary)` works for any
resolution and returns only candles that have completed by the boundary, so a 4H
candle is usable from the boundary at which it closes and never before
(`tests/test_single_position_execution.py::MultiTimeframeTests`). The boundary is
the one `CycleGuard` accepted, so a strategy never reads "now".

## One cycle

```text
wait for the next boundary      keepalive every 5 min; DEMO confirmations polled
(days == 5: wait for open)      confirmations still polled while closed
CycleGuard.start(boundary)      already executed or stale -> skip
prepare_trading_cycle()         ping/renew/verify account; failures stop or pause
--read-only: history probes, then next boundary (nothing below runs)
PendingOrders.resolve()         unconfirmed DEMO order -> skip (no decision)
CycleGuard.fresh("signal")
value = strategy.signal(...)    None -> skip
verified = broker positions     ownership reconciled (see below)
target = strategy.target(value, verified.side)
transition:
  target == verified            nothing
  FLAT, owned position          resolve(attempts=3), fresh("order"), close it
  BUY/SELL, flat, no unowned    resolve(), fresh("order"), open it
  BUY/SELL, flat, unowned open  entry blocked (warning, no order)
  BUY<->SELL                    stop: reversal_requires_confirmed_close
```

`--max-signal-delay` is checked at cycle start, before the signal, immediately
before each order and, in LIVE, by the gate after its broker reads and again
by the client after rate limiting, immediately before the HTTP request. A
boundary missed during an outage or a long wait is never replayed: a signal
that is too old never becomes valid after reconnecting.

## Execution states

`SinglePositionExecution.status()` reports the in-memory state without a broker
request: `phase`, `desired` (the latest decision), `verified` (the last broker-
verified owned side), `owned_positions`, `pending_entries`, `pending_exits` and
`outcome_uncertain`.

```text
            open acknowledged (HTTP 200)          confirmed + listed
   flat  ------------------------------> entry_pending ---------------> open
    ^                                        |                           |
    |      close confirmed + no longer listed|   close acknowledged       |
    +------------------------------ exit_pending <------------------------+
    |                                        |
    |   owned position no longer listed (broker stop, manual close): DEMO releases
    |   ownership and is flat; LIVE stops (journal must be reconciled)
    |
 any state -> halted: uncertain or unexpected order outcome, rejected or
              mismatched confirmation, confirmation deadline, reversal request,
              inconsistent ownership, unknown target
```

| State | Meaning | Orders allowed |
| --- | --- | --- |
| desired | What the latest decision asked for | — |
| verified | Owned side from the broker's position list | — |
| entry_pending / exit_pending | DEMO: HTTP 200 received, broker confirmation or position list not yet consistent | none; no decision is made |
| open (confirmed entry) | Confirmation names the deal and it is listed on the instrument | close only, or hold |
| flat (confirmed exit) | Confirmation accepted and the deal no longer listed | entry, if no unowned position on the instrument |
| rejected | Confirmation `REJECTED`/not `ACCEPTED` or details differ | none: trading stops (existing policy) |
| uncertain | Transport failure, interruption, unexpected status or exception during the request | none: trading stops, never retried |

## Order confirmation lifecycle

An HTTP request succeeding, HTTP 200, a deal reference or a strategy request is
never treated as a fill.

- **DEMO.** `createPosition`/`closePosition` HTTP 200 records a pending entry in
  `PendingOrders` before a deferred shutdown can take effect. It stays pending,
  across client replacement, until `/confirms/{dealReference}` is `ACCEPTED`
  with the same reference, instrument, direction, size and exactly one `OPENED`
  affected deal, and the position list shows that deal (open) or no longer shows
  it (close). It is polled between boundaries. After 300 s
  (`ORDER_CONFIRMATION_TIMEOUT`) trading stops. Any other outcome
  (`REJECTED`, mismatch, missing reference) stops trading for manual
  reconciliation.
- **LIVE.** The gate confirms synchronously inside `execute()` (five checks
  1.1 s apart), verifies the opened position and broker stop, or verifies the
  closure, and records it in the durable journal. `PendingOrders` is disabled.
- **Both.** Any non-200 return, exception or interruption from an order request
  is an uncertain outcome: trading stops (`order_response_unexpected`,
  `order_outcome_uncertain`, or the client's `TradingUnavailable` with
  `mutation=True`). Order-changing requests are never retried.

## Position ownership

The strategy closes only positions it can prove it opened.

- **DEMO.** A deal becomes owned only when `PendingOrders` reconciles its
  confirmation: the confirmation names the deal and the position list shows it
  on the instrument. A position with the same instrument, side and size is
  never adopted (`test_ownership_is_never_inferred_from_a_matching_instrument_and_direction`).
  Ownership is kept in memory, so it survives client replacement after an
  outage but not a restart.
- **LIVE.** The engine reads the gate's journal-backed `owned` record and never
  edits it. The client already lists only gate-owned positions in LIVE.
- **Unowned positions on the instrument** (manual, another strategy, an earlier
  run) are never closed or adopted. While any is open, entries are blocked with
  a warning: the account would no longer hold only the strategy's position, and
  on a netting account the order could change the other position. Positions on
  other instruments are ignored. In LIVE the gate already refuses entries while
  any position or working order exists.
- **Broker-side closures** (stop, manual close, margin): in DEMO the owned deal
  is no longer listed, ownership is released and the strategy is flat; it may
  enter again. In LIVE the journal still records the position, so the engine
  stops (`live_owned_position_closed_outside_strategy`) instead of waiting for
  the gate to refuse the next entry.
- **Opposite orders.** The engine closes with `closePosition(dealId)` and never
  relies on an opposite market order: depending on the account mode that would
  close, hedge or be refused. A requested reversal stops trading (below).
- **Restart.** DEMO starts owning nothing, so a position left by an earlier run
  blocks entries until it is closed or managed on Capital.com; it is never
  adopted automatically. LIVE refuses to start while the previous journal has a
  pending intent or an owned position. Every exit reports
  `strategy_owned_positions` and states that DEMO ownership is not kept.

### Change from earlier DEMO behavior

Before issue #9, HMA, EMA-HMA and Multiple-HMA closed **every** position on the
instrument in DEMO, including positions they had not opened. They now close only
their own. Entries were, and still are, blocked while any position is open on
the instrument. LIVE behavior was already owned-only.

## Reversals and issue #10

The moving-average and SMA decisions are stop-and-reverse. The single-position
engine has no confirmed close-then-open, and it never sends an opposite order
over an open position. When the decision moves from one side to the other, the
engine stops trading with `reversal_requires_confirmed_close` and leaves the
owned position open (DEMO without a broker stop; LIVE with its broker stop).

Previously DEMO sent the opposite market order (netting: the account went flat;
hedging: offsetting positions accumulated) and LIVE stopped inside the gate at
that order (`live_open_order_limit_reached`). The new outcome is the same in
every account mode and in LIVE, and no order is sent. This is the minimum
safety change Issue #9 needs; deciding and implementing confirmed reversals (or
long/flat) is issue #10. The backtest warning for these strategies says so.

## Network recovery and sessions

Unchanged policies, now for every strategy (see [network recovery](network-recovery.md)
and [session lifecycle](session-lifecycle.md)):

- read-only GETs: bounded retries (`--network-retries`, backoff, jitter);
- trade-changing requests: one attempt, never retried;
- DEMO outage: pause, probe every `--recovery-interval`, build a fresh client
  that must log in, rediscover and match the pinned account, reconcile positions
  and working orders and recheck pending orders, then resume at the next future
  boundary; `--max-outage` bounds the pause; an unconfirmed order bounds it to its
  deadline;
- authentication, permission, identity or malformed-response failures stop;
- an uncertain order outcome stops without recovery;
- LIVE never resumes automatically: a new LIVE session needs a restart and the
  full preflight;
- sessions are validated or renewed at each boundary (`prepare_trading_cycle`)
  and every 5 minutes while waiting.

## Graceful shutdown

SIGINT/SIGTERM while waiting or reading stop at once (exit 130/143). During an
order request they are deferred until that one request returns, and an HTTP-200
acknowledgement is recorded first; a second signal forces an immediate interrupt,
reported as an uncertain outcome. Stopping never sends an order and never closes
positions. Every exit reports owned positions and pending orders; unconfirmed
DEMO orders print a reconciliation warning.

## Command-line options

Every order-capable strategy accepts the shared execution options
(`trading_config.add_execution_arguments`), validated before credentials are
loaded:

| Option | Meaning |
| --- | --- |
| `--read-only` | DEMO only. Session and history reads per boundary; never calculates signals, reads positions or places orders. The client refuses mutations (`forbid_mutations`), also after recovery |
| `--cycles N` | Stop after N attempted boundaries |
| `--network-retries`, `--network-retry-delay`, `--network-retry-max-delay`, `--network-retry-jitter` | Read-only GET retries |
| `--recovery-interval`, `--max-outage`, `--no-network-recovery` | DEMO pause/resume |
| `--max-signal-delay` | Stale-cycle limit, seconds |

Strategy-specific options are unchanged (see the [CLI guide](cli.md)).

## Remaining strategy-specific differences

| Strategy | Signal input | Known online/backtest difference |
| --- | --- | --- |
| hma | completed candles; missing expected candle skips | LIVE limits only |
| ema-hma | newest candles, ending with the forming one | forming candle (#11) |
| multiple-hma | newest candles; 4H vote includes the 4H candle in progress | forming candles (#11) |
| sma | newest 21 ask opens at one-minute boundaries | reversal stops (#10); opens one candle earlier than the simulation |
| moving-average | newest 20 ask opens, after the startup wait | as sma; the backtest has no startup wait |

## Adding a single-position strategy

1. Put the pure rule in `strategies/` (shared with its backtest adapter).
2. In the strategy's `main()`: build the parser with `add_trading_arguments`,
   the strategy's own options and `add_execution_arguments`; call
   `check_execution_arguments` and `network_policies(args)` before anything
   authenticates.
3. Build `SinglePositionStrategy` with a `signal` that reads only completed
   candles for the boundary (`completed_closes`) and returns None when the
   expected candle is missing, and a pure `target`.
4. Call `run(strategy, args, connect=..., wait=...)`. Do not add a loop, sleep,
   order call or position read to the module
   (`tests/test_shared_execution_safety.py` enforces this).
5. Register the DEMO/LIVE modes and `trading_options` in the registry only after
   the tests and a DEMO validation exist.

### Donchian (#12)

`DonchianStrategy.target(candles, direction)` is already position-aware and
pure. Its online module needs a `signal` returning completed candles (or None),
a `target` mapping None to `FLAT`, and a confirmed reversal through flat. The
last part depends on #10: today the engine refuses a direct reversal, which is
safe but stops at the first long-to-short change.

### Grid (#13)

Grid is multi-order and must not use this engine. It can reuse
`wait_until_targets`, `CycleGuard`, `NetworkSupervisor` (with its own pending
tracker), `GracefulShutdown`, `prepare_trading_cycle`, the read-retry policy,
`completed_closes`, `forbid_mutations` and `add_network_arguments`. Its working
orders, lots, ownership and LIVE risk policy need their own model; the
single-position restriction is not relaxed for it.

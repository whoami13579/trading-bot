# Configuration

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

On the command line, `uv run tb-trade --strategy NAME` selects DEMO, and only an
explicit `--REAL` or `--real` selects LIVE; `tb-trade` refuses `--live`, `--demo`
and `--environment` rather than guessing. The legacy strategy commands use the same
two flags. Historical backtesting (`tb-backtest`) never selects an environment and
refuses `--REAL`, `--real` and `--account`. See the [CLI guide](cli.md).

Select the endpoint in code with `TradingApi(environment="demo")` (the default)
or explicitly `TradingApi(environment="live")`. The legacy explicit
`real_account=True` argument remains supported; conflicting selectors are rejected.
Each client has a fixed endpoint and fresh in-memory session tokens. Legacy
`load_keys()` / `write_keys()` are compatibility no-ops: unlabelled `.env` tokens
cannot safely identify their environment, so they are never used or saved.
The two guarded integration CLIs below also start with empty, independent tokens.
`.gitignore` excludes private configuration and both integration state directories.

### Configurable risk defaults

`--REAL` is the explicit authorization for automated LIVE execution. Strategy
startup displays a real-money warning and performs preflight; it does **not** read
terminal input or require an exact phrase. Essential missing data still blocks
execution. Configure unattended runs in `src/trading_bot/config/strategy_defaults.py`, or use
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


### Network retry and recovery (HMA)

`tb-trade --strategy hma` (and the legacy `hma.py`/`trading-bot-hma`) accepts
`--network-retries` (default 3), `--network-retry-delay` (2s),
`--network-retry-max-delay` (30s), `--network-retry-jitter` (0.2),
`--recovery-interval` (60s), `--max-outage` (0 = unlimited), `--max-signal-delay`
(120s) and `--no-network-recovery`. The defaults live in
`src/trading_bot/config/strategy_defaults.py`. Retries apply to read-only GETs only.
See [network recovery](network-recovery.md) for the full behavior.

## Package paths

Shared configuration lives in `src/trading_bot/config/trading_config.py`; editable
strategy defaults live in `src/trading_bot/config/strategy_defaults.py`. The old
`scripts/trading_config.py` and `scripts/strategy_defaults.py` imports alias those
same modules, so there is one implementation and one set of defaults.

The fee evidence template remains at `live_fee_profile.example.json` to preserve
existing commands. No extra top-level `config/` directory is needed: Python
configuration belongs in the package, and private operational configuration stays
in its existing ignored locations.

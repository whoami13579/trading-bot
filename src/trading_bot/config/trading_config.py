"""Shared, non-interpolating credential loading. Never load or persist session tokens."""

from pathlib import Path
import unicodedata

from dotenv.parser import parse_stream
from trading_bot.config import strategy_defaults as defaults

from trading_bot.config.paths import ROOT
DEMO_URL = "https://demo-api-capital.backend-capital.com"
LIVE_URL = "https://api-capital.backend-capital.com"
CREDENTIAL_KEYS = ("X-CAP-API-KEY", "identifier", "password")
ALLOWED_KEYS = frozenset((*CREDENTIAL_KEYS, "CST", "X_SECURITY_TOKEN"))


class ConfigurationError(ValueError):
    """Messages are fixed codes and never include configuration values."""


def endpoint(environment="demo"):
    if environment not in ("demo", "live"):
        raise ConfigurationError("explicit_demo_or_live_environment_required")
    return LIVE_URL if environment == "live" else DEMO_URL


def load_credentials(path=None):
    """Read exactly this checkout's .env, ignoring process environment and legacy tokens.

    parse_stream preserves literal passwords without shell or dotenv interpolation.
    No search in parent directories, global environment mutation, or token cache writes.
    """
    path = Path(path) if path is not None else ROOT / ".env"
    if not path.is_file():
        raise ConfigurationError("shared_credentials_file_missing")
    if path.is_symlink() or path.stat().st_mode & 0o777 != 0o600:
        raise ConfigurationError("shared_credentials_require_private_regular_file_mode_600")
    with path.open(encoding="utf-8") as stream:
        bindings = list(parse_stream(stream))
    if any(b.error for b in bindings):
        raise ConfigurationError("shared_credentials_parse_error")
    named = [b for b in bindings if b.key is not None]
    if any(b.key not in ALLOWED_KEYS for b in named):
        raise ConfigurationError("unexpected_shared_configuration_variable")
    if len({b.key for b in named}) != len(named):
        raise ConfigurationError("duplicate_shared_configuration_variable")
    for binding in named:
        tail = binding.original.string.split("=", 1)[-1].lstrip()
        if (binding.key in CREDENTIAL_KEYS and not tail.startswith(("'", '"'))
                and any(marker in tail for marker in (" #", "\t#"))):
            raise ConfigurationError("ambiguous_unquoted_credential_comment")
    values = {b.key: b.value for b in named}
    for key in CREDENTIAL_KEYS:
        value = values.get(key)
        if (not isinstance(value, str) or not value or value != value.strip()
                or value.lower().startswith(("your_", "enter_", "replace_"))
                or (value.startswith("<") and value.endswith(">"))
                or "****" in value or "••••" in value or "${" in value
                or any(unicodedata.category(ch).startswith("C") for ch in value)):
            raise ConfigurationError("invalid_or_placeholder_shared_credentials")
    return {key: values[key] for key in CREDENTIAL_KEYS}


def add_environment_arguments(parser):
    parser.allow_abbrev = False  # Real-money selection must use an exact supported flag.
    parser.add_argument("--REAL", "--real", dest="real", action="store_true",
                        help="Select Capital.com LIVE (real money); default: DEMO")
    parser.add_argument("--account", help="Active account ID or selection key; never switches accounts")


def integration_environment(argv):
    """Route the two host-restricted integration checkers without a fallback."""
    import argparse
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--REAL", "--real", dest="real", action="store_true")
    parser.add_argument("--environment", choices=("demo", "live"))
    args, _ = parser.parse_known_args(argv)
    if args.real and args.environment == "demo":
        parser.error("--REAL/--real conflicts with --environment demo")
    return "live" if args.real else (args.environment or "demo")


def add_trading_arguments(parser):
    """The same LIVE execution controls on every order-capable strategy."""
    from decimal import Decimal

    add_environment_arguments(parser)
    parser.add_argument("--symbol", default=defaults.SYMBOL, help="Instrument epic (default: EURUSD)")
    parser.add_argument("--size", type=positive_size, default=defaults.SIZE, help="Position size (default: 100)")
    for option in ("margin", "exposure", "charges", "loss"):
        parser.add_argument(f"--max-{option}", type=Decimal,
                            default=getattr(defaults, "MAX_" + option.upper()),
                            help=f"Override LIVE {option} limit in USD (default: %(default)s)")
    parser.add_argument("--max-orders", type=int, default=defaults.MAX_ORDERS,
                        help="LIVE opening-order limit per run (default: %(default)s)")
    parser.add_argument("--stop-distance", type=Decimal, default=defaults.STOP_DISTANCE,
                        help="LIVE broker stop distance in price units (default: %(default)s)")
    parser.add_argument("--fee-profile", type=Path, default=ROOT / ".live-integration/fees.json",
                        help="Optional existing verified evidence for fees absent from the broker API")


def positive_size(value):
    import argparse
    import math
    try:
        size = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("size must be a finite positive number") from None
    if not math.isfinite(size) or size <= 0:
        raise argparse.ArgumentTypeError("size must be a finite positive number")
    return size


def create_trading_api(args, *, symbol=None, size=None, order_execution=True):
    """Authenticate only the selected host, verify it, then optionally arm LIVE orders."""
    from trading_bot.api.client import TradingApi

    environment = "live" if args.real else "demo"
    if args.real:
        print("\n!!! LIVE TRADING — REAL MONEY AT RISK !!!\n")
    if args.real and order_execution:
        from trading_bot.risk.live_execution import LiveExecution
        LiveExecution.validate_options(args, symbol, size)
    api = TradingApi(environment=environment, account=args.account, auto_authenticate=False)
    if api.createNewSession() is None:
        from trading_bot.api.network_errors import TradingUnavailable
        if isinstance(getattr(api, "_unavailable", None), str):
            raise TradingUnavailable(api._unavailable) from None
        raise ConfigurationError(f"{environment}_authentication_failed_no_fallback")
    account = api.verify_account_environment()
    print(f"Authenticated {environment.upper()} account ****{account['accountId'][-4:]}")
    if args.real and order_execution:
        gate = LiveExecution(api, args, symbol, size)
        try:
            gate.arm()
        except BaseException:
            gate.release()
            raise
        api._live_execution = gate
    return api

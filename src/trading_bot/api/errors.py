"""Value-free broker error identifiers from the official reference's error examples."""

KNOWN_BROKER_ERRORS = frozenset(
    """
error.account.limit.reached error.invalid.account error.invalid.accountId error.invalid.amount
error.invalid.details error.invalid.from error.invalid.leverage.value error.invalid.limit
error.invalid.resolution error.invalid.stoploss.maxvalue error.invalid.stoploss.minvalue
error.invalid.takeprofit.maxvalue error.invalid.takeprofit.minvalue error.invalid.watchlist
error.not-found.dealId error.not-found.dealReference error.not-found.epic error.not-found.watchlist
error.not-in-future.goodTillDate error.null.api.key error.null.epic error.null.searchTerm
error.size.createwatchlistrequest.name error.too-many.requests
""".split()
)


def broker_error_code(response) -> str | None:
    """Never forward arbitrary server text, parameter values, or error messages."""
    try:
        value = response.json()
    except (ValueError, TypeError):
        return None
    code = value.get("errorCode") if isinstance(value, dict) else None
    if not isinstance(code, str):
        return None
    code = code.split(":", 1)[0]
    return code if code in KNOWN_BROKER_ERRORS else None

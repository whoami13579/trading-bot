"""Pure, conservative reconciliation helpers. No credentials or network operations."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from trading_bot.risk.live_risk import IntegrationError, dec, number, require

FEE_TYPES = frozenset(("TRADE_COMMISSION", "FX_COMMISSION", "TRADE_COMMISSION_GSL", "SWAP"))


class ReconciliationPending(IntegrationError):
    """Execution may be complete even though its financial records are incomplete."""


def transaction_window(start, now=None):
    """Use explicit UTC dates so a delayed check does not lose the trade at 600s."""
    now = now or datetime.now(timezone.utc)
    beginning = datetime.fromisoformat(start.replace("Z", "+00:00")) - timedelta(seconds=60)
    require(beginning.tzinfo is not None and now.tzinfo is not None, "history_window_requires_utc")
    return {"from": beginning.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "to": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")}


def valid_transaction_query(query):
    if set(query) != {"from", "to"} or any(len(v) != 1 for v in query.values()):
        return False
    try:
        start, end = (datetime.strptime(query[k][0], "%Y-%m-%dT%H:%M:%S") for k in ("from", "to"))
        return timedelta(0) <= end - start <= timedelta(days=1)
    except (ValueError, TypeError):
        return False


def summarize_transactions(rows, position_id, references, currency, epic):
    """Prefer the position dealId; transaction.reference is a separate ledger ID.

    A reference fallback is only allowed when no dealId is supplied. Never let
    a matching reference override an explicitly different position or currency.
    """
    require(isinstance(rows, list), "invalid_settlement_response")
    matched, seen = [], {}
    for item in rows:
        if not isinstance(item, dict) or item.get("status") != "PROCESSED" or item.get("currency") != currency:
            continue
        if item.get("instrumentName") not in (None, epic):
            continue
        deal_id = item.get("dealId")
        if (deal_id != position_id if deal_id is not None else item.get("reference") not in references):
            continue
        kind = item.get("transactionType")
        if kind not in FEE_TYPES | {"TRADE"}:
            continue
        amount = dec(item.get("size"))  # For transaction history this is cash, not position units.
        reference = item.get("reference")
        if reference is not None:
            key = (reference, kind, deal_id)
            if key in seen:
                require(seen[key] == amount, "conflicting_transaction_cashflows")
                continue
            seen[key] = amount
        matched.append((kind, amount))
    trades = [amount for kind, amount in matched if kind == "TRADE"]
    fees = {}
    for kind, amount in matched:
        if kind in FEE_TYPES:
            fees[kind] = str(dec(fees.get(kind, "0")) + amount)
    return {"realized_profit_loss": str(sum(trades, Decimal("0"))) if trades else None,
            "pnl_reconciliation_status": "ATTRIBUTED" if trades else "PENDING",
            "reported_fee_cashflows": fees or None,
            "fee_reconciliation_status": "REPORTED_PARTIAL" if fees else "UNVERIFIED",
            "fee_completeness_verified": False,
            "matched_trade_transactions": len(trades),
            "pnl_source": "processed_position_matched_transactions" if trades else None}


def validate_close_confirmation(value, position_id, epic, direction, size):
    """An echoed p_ reference alone does not prove which position was closed."""
    require(value.get("dealStatus") == "ACCEPTED" and value.get("status") == "CLOSED"
            and value.get("epic") == epic, "test_close_not_confirmed")
    affected = value.get("affectedDeals")
    require(isinstance(affected, list) and len(affected) == 1
            and affected[0].get("dealId") == position_id
            and affected[0].get("status") == "FULLY_CLOSED", "test_close_confirmation_wrong_position")
    require(value.get("direction") == ("SELL" if direction == "BUY" else "BUY")
            and dec(value.get("size")) == dec(size), "test_close_confirmation_direction_or_size_mismatch")


def fx_price_metrics(market, currency, epic, size, *, direction=None, opening=None, closing=None):
    """Only the verified FX unit/quote-currency mapping; never guess conversions."""
    instrument, snapshot = market["instrument"], market["snapshot"]
    result = {"estimated_roundtrip_spread_cost": None, "price_based_pnl_estimate": None,
              "account_currency": currency, "spread_price": None, "spread_pips": None,
              "size_base_units": None, "pip_value_account_currency": None}
    if (len(epic) != 6 or not epic.isalpha() or not epic.endswith("USD")
            or instrument.get("type") != "CURRENCIES" or instrument.get("epic") != epic
            or instrument.get("currency") != currency or currency != epic[3:]
            or instrument.get("lotSize") != 1 or snapshot.get("scalingFactor") != 1):
        return result
    quantity = number(size)
    bid, ask = number(snapshot.get("bid")), number(snapshot.get("offer"))
    require(ask >= bid, "invalid_demo_quote")
    pip = Decimal("0.0001")
    result.update(estimated_roundtrip_spread_cost=str(quantity * (ask - bid)),
                  spread_price=str(ask - bid), spread_pips=str((ask - bid) / pip),
                  size_base_units=f"{quantity} {epic[:3]}", pip_value_account_currency=str(quantity * pip))
    if opening is not None and closing is not None and direction in ("BUY", "SELL"):
        change = number(closing) - number(opening)
        result["price_based_pnl_estimate"] = str(quantity * change * (1 if direction == "BUY" else -1))
    return result

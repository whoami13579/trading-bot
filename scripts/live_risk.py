"""Shared broker validation and verified LIVE risk/fee requirements. No network I/O."""

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import time
from urllib.parse import urlsplit

EPIC, SIZE = "EURUSD", Decimal("100")


class IntegrationError(RuntimeError):
    """Only fixed, non-secret diagnostic codes are used as messages."""


def require(condition, code):
    if not condition:
        raise IntegrationError(code)


def number(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise IntegrationError("invalid_numeric_response") from None
    require(result.is_finite() and result > 0, "invalid_numeric_response")
    return result


def validate_market(payload, *, epic=EPIC):
    instrument, rules, snapshot = payload["instrument"], payload["dealingRules"], payload["snapshot"]
    require(instrument["epic"] == epic and instrument["type"] == "CURRENCIES", "unexpected_market")
    minimum = number(rules["minDealSize"]["value"])
    maximum = number(rules["maxDealSize"]["value"])
    increment = number(rules["minSizeIncrement"]["value"])
    require(minimum <= maximum and Decimal(str(float(minimum))) == minimum, "unsupported_order_size")
    unit = rules["minDealSize"]["unit"]
    require(unit in ("POINTS", "CONTRACTS", "SHARES", "AMOUNT"), "unknown_size_unit")
    require(rules["maxDealSize"]["unit"] == unit and rules["minSizeIncrement"]["unit"] == unit,
            "inconsistent_size_units")
    status = snapshot["marketStatus"]
    require(status in ("TRADEABLE", "CLOSED", "EDITS_ONLY", "OFFLINE", "SUSPENDED", "ON_AUCTION"), "unknown_market_status")
    preference = rules["marketOrderPreference"]
    require(preference in ("AVAILABLE_DEFAULT_ON", "AVAILABLE_DEFAULT_OFF", "NOT_AVAILABLE"), "unknown_market_order_preference")
    modes = snapshot.get("marketModes", [])
    require(isinstance(modes, list) and all(mode in ("REGULAR", "CLOSE_ONLY", "LONG_ONLY", "SHORT_ONLY") for mode in modes), "unknown_market_mode")
    summary = {"epic": epic, "instrument_type": "CURRENCIES", "market_status": status,
               "minimum_size": str(minimum), "size_unit": unit, "maximum_size": str(maximum),
               "size_increment": str(increment), "market_order_preference": preference,
               "market_modes": modes, "guaranteed_stop_allowed": instrument.get("guaranteedStopAllowed") is True,
               "tradeable": status == "TRADEABLE" and preference != "NOT_AVAILABLE" and "REGULAR" in modes}
    # Only documented numeric restrictions leave memory; exclude arbitrary server text.
    summary["distance_restrictions"] = {name: {"unit": rule["unit"], "value": str(number(rule["value"]))}
        for name in ("minGuaranteedStopDistance", "minStopOrProfitDistance", "maxStopOrProfitDistance")
        if isinstance(rule := rules.get(name), dict) and rule.get("unit") in ("POINTS", "PERCENTAGE")}
    return minimum, summary


def dec(value, minimum=None):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise IntegrationError("invalid_numeric_risk_information") from None
    require(number.is_finite() and (minimum is None or number >= minimum), "invalid_numeric_risk_information")
    return number


def fingerprint(account_id):
    return hashlib.sha256(account_id.encode()).hexdigest()


def risk_estimate(market, account, preferences, *, epic=EPIC, size=SIZE):
    size = number(size)
    instrument, snapshot, rules = market["instrument"], market["snapshot"], market["dealingRules"]
    minimum, summary = validate_market(market, epic=epic)
    increment = dec(rules["minSizeIncrement"]["value"], Decimal("0.00000001"))
    require(summary["tradeable"] and minimum <= size <= dec(rules["maxDealSize"]["value"])
            and (size - minimum) % increment == 0, "live_requested_size_not_tradeable")
    require(len(epic) == 6 and epic.isalpha() and epic.endswith("USD"), "unverified_live_contract_mapping")
    require(instrument.get("currency") == "USD" and dec(instrument.get("lotSize")) == 1
            and dec(snapshot.get("scalingFactor")) == 1, "unverified_live_contract_mapping")
    bid, ask = dec(snapshot["bid"], Decimal("0.00000001")), dec(snapshot["offer"], Decimal("0.00000001"))
    require(ask >= bid, "invalid_live_quote")
    require("delayTime" in snapshot and dec(snapshot["delayTime"]) == 0, "live_quote_delayed_or_delay_unknown")
    leverage = dec(preferences["leverages"]["CURRENCIES"]["current"], Decimal("1"))
    require(instrument.get("marginFactorUnit") == "PERCENTAGE", "unsupported_live_margin_unit")
    factor = dec(instrument.get("marginFactor"), Decimal("0.00000001")) / 100
    require(factor <= 1, "unsupported_live_margin_factor")
    currency = account["currency"]
    require(currency in ("USD", "EUR"), "account_currency_conversion_not_implemented")
    require(currency == "USD" or epic == "EURUSD", "account_currency_conversion_not_implemented")
    to_account = Decimal("1") if currency == "USD" else 1 / bid  # Conservative debit conversion.
    notional = size * ask * to_account
    # Both broker instrument rate and current category leverage are exposed. Never understate either.
    instrument_margin = notional * factor
    leverage_margin = notional / leverage
    margin = max(instrument_margin, leverage_margin)
    overnight = instrument.get("overnightFee", {})
    funding = {"reported_buy_rate": str(dec(overnight["longRate"])) if overnight.get("longRate") is not None else None,
               "next_charge_at_utc": datetime.fromtimestamp(overnight["swapChargeTimestamp"] / 1000, timezone.utc).isoformat()
               if type(overnight.get("swapChargeTimestamp")) is int else None,
               "charge_interval_minutes": str(dec(overnight["swapChargeInterval"], Decimal("1")))
               if overnight.get("swapChargeInterval") is not None else None,
               "applies_if_held_through_rollover": True}
    return {"epic": epic, "direction": "BUY", "size": str(size), "base_currency_units": f"{size} {epic[:3]}",
            "account_currency": currency, "bid": str(bid), "ask": str(ask), "spread": str(ask - bid),
            "notional_usd": str(size * ask), "estimated_notional_account_currency": str(notional),
            "current_leverage": str(leverage), "instrument_margin_percent": str(factor * 100),
            "margin_from_instrument_rate": str(instrument_margin), "margin_from_account_leverage": str(leverage_margin),
            "estimated_margin_account_currency": str(margin), "margin_method": "higher_of_published_rate_and_current_leverage",
            "estimated_roundtrip_spread_cost": str(size * (ask - bid) * to_account),
            "pip_value_usd": str(size / 10000), "market": summary,
            "overnight_funding": funding,
            "margin_is_not_maximum_loss": True, "market_fill_price_not_guaranteed": True}


def verified_costs(path, risk, account_id, market, *, epic=EPIC):
    require(path.is_file(), "verified_live_fee_profile_missing")
    require(not path.is_symlink() and path.stat().st_mode & 0o777 == 0o600, "live_fee_profile_requires_private_regular_file")
    profile = json.loads(path.read_text())
    require(profile.get("verified_for_this_account") is True and profile.get("account_fingerprint") == fingerprint(account_id)
            and profile.get("currency") == risk["account_currency"], "live_fee_profile_not_account_verified")
    require(isinstance(profile.get("legal_entity"), str) and bool(profile["legal_entity"].strip())
            and profile.get("client_classification") in ("retail", "professional", "wholesale")
            and profile.get("instrument") == epic, "live_fee_profile_account_scope_unverified")
    if dec(risk["size"]) != SIZE:
        require(dec(profile.get("position_size")) == dec(risk["size"]), "live_fee_profile_size_scope_unverified")
    reviewed = date.fromisoformat(profile["reviewed_at"])
    require(0 <= (datetime.now(timezone.utc).date() - reviewed).days <= 7, "live_fee_profile_stale")
    source = urlsplit(profile["source_url"])
    require(source.scheme == "https" and bool(source.hostname) and
            (source.hostname == "capital.com" or source.hostname.endswith(".capital.com")
             or source.hostname in ("help.capitalccuk.com", "help.capitalccau.com", "help.capitalccbah.com")),
            "live_fee_source_not_official")
    commission = dec(profile["commission_per_side"], Decimal("0"))
    other_charges = dec(profile["other_transaction_charges_roundtrip"], Decimal("0"))
    conversion_rate = dec(profile["conversion_fee_percent"], Decimal("0")) / 100
    require(conversion_rate <= 1, "invalid_live_conversion_fee")
    require(risk["account_currency"] != "USD" or conversion_rate == 0, "conversion_fee_not_applicable_to_same_currency")
    # Refuse a planned immediate roundtrip near rollover; no assumption that an outage cannot extend holding time.
    overnight = market["instrument"].get("overnightFee", {})
    require(isinstance(overnight.get("swapChargeTimestamp"), int), "live_funding_schedule_unknown")
    seconds = (overnight["swapChargeTimestamp"] / 1000) - time.time()
    require(seconds > 120, "live_funding_rollover_too_close_or_stale")
    long_rate = dec(overnight["longRate"])
    spread = dec(risk["estimated_roundtrip_spread_cost"], Decimal("0"))
    conversion = spread * conversion_rate
    return {"fee_schedule_verified_by_user": True, "commission_roundtrip": str(commission * 2),
            "estimated_conversion_cost_on_flat_price_roundtrip": str(conversion),
            "other_transaction_charges_roundtrip": str(other_charges),
            "estimated_roundtrip_cost": str(spread + commission * 2 + conversion + other_charges),
            "next_funding_at_utc": datetime.fromtimestamp(overnight["swapChargeTimestamp"] / 1000, timezone.utc).isoformat(),
            "reported_buy_funding_rate": str(long_rate),
            "planned_funding_cost": "0", "funding_if_held_past_rollover_unknown": True,
            "actual_slippage_and_final_cost_not_guaranteed": True}


def check_limits(context, max_margin, max_exposure, max_charges=None):
    require(context["phase1_passed"], "live_phase1_incomplete")
    require(max_margin is not None and max_exposure is not None and max_charges is not None,
            "explicit_margin_exposure_and_charge_limits_required")
    require(all(dec(limit) > 0 for limit in (max_margin, max_exposure, max_charges)), "positive_live_limits_required")
    risk = context["risk"]
    require(context["account"]["currency"] == risk["account_currency"] == "USD", "usd_account_required_for_usd_limits")
    require(dec(risk["estimated_margin_account_currency"]) <= dec(max_margin), "live_margin_limit_exceeded")
    require(dec(risk["estimated_notional_account_currency"]) <= dec(max_exposure), "live_exposure_limit_exceeded")
    require(dec(risk["costs"]["estimated_roundtrip_cost"], Decimal("0")) <= dec(max_charges), "live_transaction_charge_limit_exceeded")
    require(dec(context["account"]["balance"]["available"]) >= dec(risk["estimated_margin_account_currency"]) + dec(risk["costs"]["estimated_roundtrip_cost"]),
            "insufficient_live_available_balance")

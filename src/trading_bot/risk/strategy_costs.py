"""Retrieve available broker cost data; refuse to turn missing fee fields into zero.

The documented market API exposes spread and overnightFee, but no complete
account-specific commission/other-charge schedule. Existing verified evidence can
fill that gap; no tariff, account classification, or fee is guessed or created.
"""

from decimal import Decimal
import json
from pathlib import Path

from trading_bot.risk.live_risk import IntegrationError, dec, require, verified_costs


def available_costs(market, risk):
    overnight = market["instrument"].get("overnightFee", {})
    return {
        "source": "authenticated_broker_market_api",
        "estimated_roundtrip_spread_cost": risk["estimated_roundtrip_spread_cost"],
        "account_currency": risk["account_currency"],
        "reported_buy_funding_rate": (str(dec(overnight["longRate"]))
                                      if overnight.get("longRate") is not None else None),
        "reported_sell_funding_rate": (str(dec(overnight["shortRate"]))
                                       if overnight.get("shortRate") is not None else None),
        "next_funding_timestamp_ms": (overnight["swapChargeTimestamp"]
                                      if type(overnight.get("swapChargeTimestamp")) is int else None),
        "funding_interval_minutes": (str(dec(overnight["swapChargeInterval"], Decimal("1")))
                                     if overnight.get("swapChargeInterval") is not None else None),
        "missing_account_fee_information": ["commission", "other_transaction_charges", "strategy_holding_cost_budget"],
        "complete": False,
    }


def strategy_costs(market, risk, account_id, fee_path=None):
    """Refresh API data on every call and reuse optional, already-verified evidence."""
    observed = available_costs(market, risk)
    require(observed["reported_buy_funding_rate"] is not None
            and observed["reported_sell_funding_rate"] is not None, "live_funding_rates_unavailable")
    if fee_path is None or not Path(fee_path).is_file():
        print(json.dumps({"available_live_cost_information": observed}))
        raise IntegrationError("live_account_fee_schedule_unavailable_from_api_no_verified_evidence")
    # Revalidate scope, permissions, legal entity, classification, date and official
    # source every time; an unverified/pending profile can never enable execution.
    path = Path(fee_path)
    costs = verified_costs(path, risk, account_id, market, epic=risk["epic"])
    profile = json.loads(path.read_text())
    require(profile.get("strategy_holding_costs_verified") is True,
            "strategy_holding_costs_require_verified_fee_profile")
    funding = dec(profile.get("strategy_funding_allowance_usd"), Decimal("0"))
    costs.update(source="broker_market_api_and_verified_account_evidence",
                 available_broker_costs=observed,
                 strategy_funding_allowance_usd=str(funding), planned_funding_cost=str(funding),
                 estimated_roundtrip_cost=str(dec(costs["estimated_roundtrip_cost"]) + funding))
    return costs

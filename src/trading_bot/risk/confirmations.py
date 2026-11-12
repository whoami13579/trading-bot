"""Bounded confirmation polling and position response validation."""
import re
import time
from trading_bot.risk.live_risk import IntegrationError, require

def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value), "invalid_deal_identifier")
    return value

def positions(api):
    payload, status = api.getAllPositions()
    require(status == 200 and isinstance(payload.get("positions"), list), "invalid_positions_response")
    return payload["positions"]

def confirm(api, reference, transport):
    reference = identifier(reference)
    transport.references.add(reference)
    for _ in range(5):
        value = api.getPositionOrderConfirmation(reference)
        if value is None or value.get("dealStatus") in ("PENDING", "UNKNOWN"):
            time.sleep(1.1)
            continue
        require(value.get("dealReference") == reference, "confirmation_reference_mismatch")
        return value
    raise IntegrationError("confirmation_unresolved_no_mutation_retry")

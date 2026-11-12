"""Shared strategy defaults. Risk policy is configuration, never a source of fee facts.

Edit these values to configure unattended runs; CLI overrides remain available.
USD risk limits apply only to the verified LIVE USD-account contract mapping.
"""

from decimal import Decimal

SYMBOL = "EURUSD"
SIZE = 100
MAX_MARGIN = Decimal("5")
MAX_EXPOSURE = Decimal("120")
MAX_CHARGES = Decimal("1")  # Cumulative estimated roundtrip charges per process.
MAX_LOSS = Decimal("2")  # Equity drawdown limit; entry-time checks, not guaranteed liquidation.
MAX_ORDERS = 1  # Opening orders per process; closing an owned position remains allowed.
STOP_DISTANCE = Decimal("0.001")  # Price units; broker restrictions are always checked.

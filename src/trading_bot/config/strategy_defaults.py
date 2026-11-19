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

# Network retry and recovery (HMA scheduled strategy). CLI overrides: see
# add_network_arguments in trading_config.py and docs/network-recovery.md.
# Retries apply only to read-only GET requests; trade-changing requests are never retried.
NETWORK_RETRIES = 3  # Additional attempts per read after the first: delays near 2s, 4s, 8s.
NETWORK_RETRY_DELAY = 2.0  # Seconds before the first retry; doubles for each later retry.
NETWORK_RETRY_MAX_DELAY = 30.0  # Upper bound for one backoff delay, in seconds.
NETWORK_RETRY_JITTER = 0.2  # Each delay is randomized by up to +/-20%.
RECOVERY_INTERVAL = 60.0  # Seconds between connectivity checks while paused (DEMO only).
MAX_OUTAGE = 0.0  # Seconds paused before giving up; 0 waits indefinitely.
MAX_SIGNAL_DELAY = 120.0  # Skip a cycle whose boundary is older than this, in seconds.
# DEMO: an acknowledged order must be confirmed and visible in positions within this many
# seconds; until then no strategy order is sent, afterwards trading stops for manual review.
# The deadline is monotonic and independent of the candle schedule.
ORDER_CONFIRMATION_TIMEOUT = 300.0
ORDER_CONFIRMATION_POLL_INTERVAL = 10.0  # Seconds between read-only checks while waiting.

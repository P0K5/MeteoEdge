"""Hardcoded config for the spike. Promote to env vars in the full build."""
from pathlib import Path

# Universe
# Spec V1 nominally targets 4 majors; widened here for the spike's observation phase.
# Mid-caps print higher and more variable funding, so the spike actually exercises
# the open/accrue/close machinery instead of sitting idle on majors.
UNIVERSE = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "ARBUSDT",
    "XRPUSDT", "OPUSDT", "SUIUSDT", "AAVEUSDT",
]

# Polling cadence
POLL_INTERVAL_SECONDS = 60

# Strategy thresholds (mirror funding-edge-spec.md §7.1–§7.2)
ENTRY_THRESHOLD_BPS = 3.0           # 0.03% per 8h funding interval
EXIT_THRESHOLD_BPS = 1.0
ENTRY_BASIS_CEILING_BPS = 20.0
EMERGENCY_BASIS_BPS = 100.0
PERSISTENCE_LOOKBACK_HOURS = 72     # require rate > threshold in 60% of last 72h
PERSISTENCE_FRACTION_MIN = 0.60
MIN_MINUTES_TO_NEXT_FUNDING = 30

# Fee model (round-trip, matches spec §7.4)
SPOT_TAKER_FEE_BPS = 7.5            # 0.075% with BNB discount
PERP_TAKER_FEE_BPS = 5.0            # 0.050%
# Round-trip: entry + exit on both legs = 2 * (7.5 + 5.0) = 25 bps
ROUND_TRIP_FEE_BPS = 2 * (SPOT_TAKER_FEE_BPS + PERP_TAKER_FEE_BPS)

# Virtual hedge sizing (notional only — not real capital)
VIRTUAL_NOTIONAL_USD = 500.0

# Holding behaviour
MAX_HOLD_HOURS = 14 * 24            # 14-day max, matches spec
TARGET_HOLD_HOURS = 3 * 24          # 3-day target — long enough to amortise round-trip fees

# Output paths
LOG_DIR = Path("logs")
SIGNALS_CSV = LOG_DIR / "signals.csv"
CYCLES_CSV = LOG_DIR / "cycles.csv"
SNAPSHOTS_JSONL = LOG_DIR / "snapshots.jsonl"
OPEN_HEDGES_JSON = LOG_DIR / "open_hedges.json"

# HTTP
HTTP_TIMEOUT_SECONDS = 15
USER_AGENT = "FundingEdge-Spike/0.1 (contact: you@example.com)"

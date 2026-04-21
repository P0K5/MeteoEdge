"""Hardcoded config for the spike. Promote to env vars in the full build."""
from pathlib import Path

# Kalshi
KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_API_KEY_ID = "2bec40e9-65b9-49a5-a59f-5cb73dfb46cf"  # read-only access is enough for the spike
KALSHI_PRIVATE_KEY_PATH = Path("keys/kalshi_private.pem")

# Stations: (METAR code, latitude, longitude, city name, NWS station for climate report)
STATIONS = [
    ("KNYC", 40.7789, -73.9692, "New York",    "KNYC"),
    ("KORD", 41.9742, -87.9073, "Chicago",     "KORD"),
    ("KMIA", 25.7953, -80.2901, "Miami",       "KMIA"),
    ("KAUS", 30.1944, -97.6700, "Austin",      "KAUS"),
    ("KLAX", 33.9425, -118.4081, "Los Angeles", "KLAX"),
]

# Kalshi series tickers for daily high temperature markets.
# Event tickers are constructed as {SERIES}-{YYMONDD} (e.g. KXHIGHNY-26APR21).
KALSHI_TEMP_SERIES = {
    "KNYC": "KXHIGHNY",   # confirmed
    "KORD": "KXHIGHCHI",  # TODO: verify from kalshi.com URL
    "KMIA": "KXHIGHMIA",  # TODO: verify from kalshi.com URL
    "KAUS": "KXHIGHAUS",  # TODO: verify from kalshi.com URL
    "KLAX": "KXHIGHLAX",  # TODO: verify from kalshi.com URL
}

# Polling cadence
POLL_INTERVAL_SECONDS = 300  # 5 minutes

# Strategy thresholds
MIN_EDGE_CENTS = 3.0
MIN_CONFIDENCE_YES = 0.80  # for YES-side trades
MAX_CONFIDENCE_YES_FOR_NO = 0.20  # for NO-side trades (1 - confidence_no >= 0.8)
MIN_MINUTES_TO_SETTLEMENT = 15

# Historical climb rates: 95th percentile additional rise (°F) from time-of-day to end-of-day.
# These are hand-seeded approximations. In the full build, compute from 5 years of METAR.
# Keyed by (station, local_hour_of_day, month_number).
# Spike: use a simple default and override per station where we have priors.
DEFAULT_CLIMB_LOOKUP = {
    # hour_of_day -> max additional rise expected (°F) by p95
    10: 8.0, 11: 7.0, 12: 6.0, 13: 5.0, 14: 4.0,
    15: 3.0, 16: 2.0, 17: 1.0, 18: 0.5, 19: 0.0,
    20: 0.0, 21: 0.0, 22: 0.0, 23: 0.0,
}

# Forecast uncertainty (stddev in °F) for the bayesian prior on "undetermined" brackets
FORECAST_STDDEV_F = 2.0

# Output paths
LOG_DIR = Path("logs")
CANDIDATES_CSV = LOG_DIR / "candidates.csv"
SNAPSHOTS_JSONL = LOG_DIR / "snapshots.jsonl"
SETTLEMENTS_CSV = LOG_DIR / "settlements.csv"

# HTTP
HTTP_TIMEOUT_SECONDS = 15
USER_AGENT = "MeteoEdge-Spike/0.1 (contact: you@example.com)"

"""Hardcoded config for the Polymarket spike. Promote to env vars in the full build."""
from pathlib import Path

# Polymarket APIs (no authentication required for read-only access)
POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_API = "https://clob.polymarket.com"

# Polymarket Gamma API tag filter for weather/temperature markets.
# This is how the daily city temperature markets are discovered — the Gamma
# API ignores text search params (`q`, `keyword`), but `tag_id` works.
POLYMARKET_WEATHER_TAG_ID = "84"

# Stations: (METAR code, latitude, longitude, Polymarket city name, resolution station)
# City names match Polymarket's question text exactly ("highest temperature in <city>").
# Resolution stations are extracted from market descriptions — these are the airports
# Polymarket uses for settlement, so the spike fetches METAR from the same source.
STATIONS = [
    ("KLGA", 40.7790,  -73.8740,  "New York City", "KLGA"),  # LaGuardia
    ("KORD", 41.9742,  -87.9073,  "Chicago",       "KORD"),
    ("KMIA", 25.7953,  -80.2901,  "Miami",         "KMIA"),
    ("KAUS", 30.1944,  -97.6700,  "Austin",        "KAUS"),
    ("KLAX", 33.9425,  -118.4081, "Los Angeles",   "KLAX"),
    ("KDAL", 32.8470,  -96.8517,  "Dallas",        "KDAL"),  # Dallas Love Field
    ("KATL", 33.6367,  -84.4281,  "Atlanta",       "KATL"),
    ("KBKF", 39.7017,  -104.7517, "Denver",        "KBKF"),  # Buckley AFB (Polymarket's choice)
    ("KHOU", 29.6454,  -95.2789,  "Houston",       "KHOU"),  # Houston Hobby
    ("KSFO", 37.6213,  -122.3790, "San Francisco", "KSFO"),
    ("KSEA", 47.4502,  -122.3088, "Seattle",       "KSEA"),
]

# Polling cadence
POLL_INTERVAL_SECONDS = 300  # 5 minutes

# Strategy thresholds
MIN_EDGE_CENTS = 3.0
MIN_CONFIDENCE_YES = 0.80       # for YES-side trades
MAX_CONFIDENCE_YES_FOR_NO = 0.20  # for NO-side trades (1 - confidence_no >= 0.8)
MIN_MINUTES_TO_SETTLEMENT = 15

# Historical climb rates: p95 additional rise (°F) from time-of-day to end-of-day.
# Hand-seeded approximations. In the full build, compute from 5 years of METAR.
# Hours 0-9 reflect the full diurnal range still ahead (daily min typically 4-7am).
DEFAULT_CLIMB_LOOKUP = {
    0: 25.0, 1: 25.0, 2: 25.0, 3: 24.0, 4: 23.0, 5: 21.0,
    6: 18.0, 7: 15.0, 8: 12.0, 9: 10.0,
    10: 8.0, 11: 7.0, 12: 6.0, 13: 5.0, 14: 4.0,
    15: 3.0, 16: 2.0, 17: 1.0, 18: 0.5, 19: 0.0,
    20: 0.0, 21: 0.0, 22: 0.0, 23: 0.0,
}

# Forecast uncertainty (stddev in °F) for the Bayesian prior on undetermined brackets
FORECAST_STDDEV_F = 2.0

# Output paths
LOG_DIR = Path("logs")
CANDIDATES_CSV = LOG_DIR / "candidates.csv"
SNAPSHOTS_JSONL = LOG_DIR / "snapshots.jsonl"
SETTLEMENTS_CSV = LOG_DIR / "settlements.csv"

# HTTP
HTTP_TIMEOUT_SECONDS = 15
USER_AGENT = "MeteoEdge-Spike/0.3 (contact: you@example.com)"

# Optional: enrich market prices with live CLOB orderbook data per bracket.
# Adds ~2 API calls per matched bracket per poll. Off by default to keep polls fast;
# `outcomePrices` from Gamma is usually within 1¢ of the live mid for liquid markets.
ENABLE_CLOB_ENRICHMENT = False

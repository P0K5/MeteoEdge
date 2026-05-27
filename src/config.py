"""Unified config for Polymarket weather arbitrage. Environment vars override defaults."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # Load .env from cwd (or parent dirs) into os.environ

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
# Polymarket uses for settlement, so we fetch METAR from the same source.
# KBKF (Denver) and KDAL (Dallas) removed — 0% win rate, excluded until fixed.
STATIONS = [
    # KLGA (NYC) removed — 31% win rate, -44% ROI on May 12 paper data
    # KAUS (Austin) removed — 38% win rate, -10% ROI on May 12 paper data
    # KSEA (Seattle) removed — 21% win rate, -66% ROI on May 14 paper data (marine layer)
    # KSFO removed — marine layer causes unreliable forecasts on both YES and NO sides
    ("KORD", 41.9742,  -87.9073,  "Chicago",       "KORD"),
    ("KMIA", 25.7953,  -80.2901,  "Miami",         "KMIA"),
    ("KLAX", 33.9425,  -118.4081, "Los Angeles",   "KLAX"),
    ("KATL", 33.6367,  -84.4281,  "Atlanta",       "KATL"),
    ("KHOU", 29.6454,  -95.2789,  "Houston",       "KHOU"),  # Houston Hobby
]

# Station timezone mapping (used for local time conversions at each location)
STATION_TZ = {
    "KORD": "America/Chicago",
    "KMIA": "America/New_York",
    "KLAX": "America/Los_Angeles",
    "KATL": "America/New_York",
    "KHOU": "America/Chicago",
}

# Strategy thresholds (env var overrides)
MIN_EDGE_CENTS = float(os.getenv("MIN_EDGE_CENTS", "15.0"))
MIN_PRICE_CENTS = int(os.getenv("MIN_PRICE_CENTS", "60"))  # below 60¢ ROI is negative (0-50% win rate)
MIN_CONFIDENCE_YES = 0.85       # for YES-side trades
MAX_CONFIDENCE_YES_FOR_NO = 0.15  # for NO-side trades (1 - confidence_no >= 0.85)

# YES trades disabled: 51.9% win rate over 3 live days (vs 94.2% for NO).
# Re-enable once ≥7 days of settlements validate YES accuracy.
ENABLE_YES_TRADES = os.getenv("ENABLE_YES_TRADES", "false").lower() == "true"
MIN_MINUTES_TO_SETTLEMENT = 15
# Daily temperature markets resolve within 24h — reject anything beyond this window.
# Without this cap the scanner evaluates tomorrow's markets against today's METAR data,
# producing spurious 100% NO confidence (today's observed high makes future brackets
# look impossible when they are not).
MAX_MINUTES_TO_SETTLEMENT = int(os.getenv("MAX_MINUTES_TO_SETTLEMENT", "1440"))  # 24 hours

# Polling cadence (env var override)
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))  # 5 minutes default

# Risk management limits (all configurable via env vars)
STARTING_CAPITAL_EUR = float(os.getenv("STARTING_CAPITAL_EUR", "500.0"))
RISK_DAILY_LOSS_LIMIT_EUR = float(os.getenv("RISK_DAILY_LOSS_LIMIT_EUR", "50.0"))
RISK_MAX_OPEN_POSITIONS = int(os.getenv("RISK_MAX_OPEN_POSITIONS", "15"))
RISK_DRAWDOWN_STOP_PCT = float(os.getenv("RISK_DRAWDOWN_STOP_PCT", "0.15"))
RISK_MIN_LIQUIDITY = int(os.getenv("RISK_MIN_LIQUIDITY", "50"))

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
LIVE_TRADES_JSONL = LOG_DIR / "live_trades.jsonl"
POSITION_SNAPSHOTS_JSONL = LOG_DIR / "position_snapshots.jsonl"

# Live execution
POLYMARKET_HOST = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
POSITION_SIZE_EUR = float(os.getenv("POSITION_SIZE_EUR", "5.0"))
# 2% buffer covers Polymarket taker fees (price-dependent, highest ~2% at extreme prices)
POSITION_SIZE_WITH_FEES = POSITION_SIZE_EUR * 1.02

# Take-profit: exit when market bid reaches (predicted_price - buffer).
# The model snapshot is frozen at entry time and cannot validate further price
# movement, so we lock in the captured edge and recycle capital into the next trade.
TAKE_PROFIT_BUFFER_CENTS = int(os.getenv("TAKE_PROFIT_BUFFER_CENTS", "2"))

# HTTP
HTTP_TIMEOUT_SECONDS = 15
USER_AGENT = "MeteoEdge/1.0 (Polymarket weather-arbitrage research bot; contact: andre.freixo.santos@gmail.com)"

# Optional: enrich market prices with live CLOB orderbook data per bracket.
# Adds ~2 API calls per matched bracket per poll. Off by default to keep polls fast;
# `outcomePrices` from Gamma is usually within 1¢ of the live mid for liquid markets.
ENABLE_CLOB_ENRICHMENT = os.getenv("ENABLE_CLOB_ENRICHMENT", "false").lower() == "true"

"""Unified config for Polymarket weather arbitrage. Environment vars override defaults."""
import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()  # Load .env from cwd (or parent dirs) into os.environ

# Polymarket APIs (no authentication required for read-only access)
POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_API = "https://clob.polymarket.com"

# Polymarket Gamma API tag filter for weather/temperature markets.
# This is how the daily city temperature markets are discovered — the Gamma
# API ignores text search params (`q`, `keyword`), but `tag_id` works.
POLYMARKET_WEATHER_TAG_ID = "84"

# Stations: (METAR code, latitude, longitude, Polymarket city name, resolution station,
#            unit, timezone)
# City names match Polymarket's question text exactly ("highest temperature in <city>").
# Resolution stations are extracted from market descriptions — these are the airports
# Polymarket uses for settlement, so we fetch METAR from the same source.
# unit is "F" for US stations (Polymarket labels in °F) and "C" for all others.
# KBKF (Denver) and KDAL (Dallas) removed — 0% win rate, excluded until fixed.
STATIONS = [
    # KLGA (NYC) removed — 31% win rate, -44% ROI on May 12 paper data
    # KAUS (Austin) removed — 38% win rate, -10% ROI on May 12 paper data
    # KSEA (Seattle) removed — 21% win rate, -66% ROI on May 14 paper data (marine layer)
    # KSFO removed — marine layer causes unreliable forecasts on both YES and NO sides
    ("KORD", 41.9742,  -87.9073,  "Chicago",       "KORD", "F", "America/Chicago"),
    ("KMIA", 25.7953,  -80.2901,  "Miami",         "KMIA", "F", "America/New_York"),
    ("KLAX", 33.9425, -118.4081,  "Los Angeles",   "KLAX", "F", "America/Los_Angeles"),
    ("KATL", 33.6367,  -84.4281,  "Atlanta",       "KATL", "F", "America/New_York"),
    ("KHOU", 29.6454,  -95.2789,  "Houston",       "KHOU", "F", "America/Chicago"),   # Houston Hobby
    # International — validated by shadow loop (≥5 trades, 100% win rate, ≥3 days).
    # Polymarket labels these in °C; the scanner converts bracket boundaries to °F
    # before passing them to the envelope model, which remains entirely °F-native.
    ("RKSI", 37.4602,  126.4407,  "Seoul",         "RKSI", "C", "Asia/Seoul"),        # 11/11 100% shadow
    ("WMKK",  2.7456,  101.7099,  "Kuala Lumpur",  "WMKK", "C", "Asia/Kuala_Lumpur"),# 9/9 100% shadow
    ("RKPK", 35.1795,  128.9382,  "Busan",         "RKPK", "C", "Asia/Seoul"),        # 9/10 90% shadow
    # ZSPD (Shanghai) removed — shadow trades averaged ~57c entry price, below MIN_PRICE_CENTS=60;
    # shadow validation does not apply to live conditions. Re-evaluate when ≥5 trades at ≥60c.
    ("ZGSZ", 22.6393,  113.8108,  "Shenzhen",      "ZGSZ", "C", "Asia/Shanghai"),     # 12/12 100% shadow
    ("WSSS",  1.3644,  103.9915,  "Singapore",     "WSSS", "C", "Asia/Singapore"),    # 9/9 100% shadow
    ("MPMG",  8.9734,  -79.5556,  "Panama City",   "MPMG", "C", "America/Panama"),    # 10/10 100% shadow
]

# Station timezone mapping (used for local time conversions at each location)
STATION_TZ = {
    "KORD": "America/Chicago",
    "KMIA": "America/New_York",
    "KLAX": "America/Los_Angeles",
    "KATL": "America/New_York",
    "KHOU": "America/Chicago",
    "RKSI": "Asia/Seoul",
    "WMKK": "Asia/Kuala_Lumpur",
    "RKPK": "Asia/Seoul",
    "ZGSZ": "Asia/Shanghai",
    "WSSS": "Asia/Singapore",
    "MPMG": "America/Panama",
}

# Per-station active local-hour window [start, end). Outside this window the
# scanner skips the station — METARs before the start hour are usually
# yesterday's heat-tail (see May 27 KHOU bug: 78.98°F at 03:43 CDT was a
# carryover from the prior day's peak), and after the end hour the daily high
# is locked in. The start hour also feeds compute_daily_high's `min_local_hour`,
# so observations before it are filtered when computing today's running high.
# Add new entries here when introducing stations on non-US timezones; the bot
# polls continuously and each station is evaluated independently in its own
# local day.
STATION_ACTIVE_HOURS = {
    "KORD": (6, 23),
    "KMIA": (6, 23),
    "KLAX": (6, 23),
    "KATL": (6, 23),
    "KHOU": (6, 23),
    "RKSI": (11, 23),   # delayed from 6: early entries (09-11 local) cause bracket blanketing
    "WMKK": (6, 23),
    "RKPK": (11, 23),   # delayed from 6: same rationale as RKSI (Seoul climate)

    "ZGSZ": (11, 23),   # delayed from 6: narrow 1C brackets cause blanketing (same as RKSI)
    "WSSS": (6, 23),
    "MPMG": (6, 23),
}

# Strategy thresholds (env var overrides)
MIN_EDGE_CENTS = float(os.getenv("MIN_EDGE_CENTS", "15.0"))
# Live ledger showed high-edge entries are adversely selected. Tightened from 25c
# to 20c on 2026-06-04: 5/6 post-fix losses had edge 21-25c, and June 3 showed
# a clear pattern of low-priced NO (high claimed edge) losing while higher-priced
# entries on the same station/day won. Override via env to experiment.
MAX_EDGE_CENTS = float(os.getenv("MAX_EDGE_CENTS", "20.0"))
MIN_PRICE_CENTS = int(os.getenv("MIN_PRICE_CENTS", "60"))  # below 60¢ ROI is negative (0-50% win rate)
MIN_CONFIDENCE_YES = 0.85       # for YES-side trades
# NO-side entry threshold: only enter when model's p(YES) is at or below this.
# Calibration on 30 trustworthy trades (May 24-29, with SELL pnl or yes_won
# field) showed the model's predicted_NO band of 85-95c had 40-64% real
# win rate — essentially noise.  The 95-99c and 100c bands had 67-90% real
# WR, the only bands with measurable signal.  Tightening from 0.15 (allow
# entries down to predicted_NO=85c) to 0.05 (only at predicted_NO>=95c)
# restricts the bot to the calibrated regime. Override via env to experiment.
MAX_CONFIDENCE_YES_FOR_NO = float(os.getenv("MAX_CONFIDENCE_YES_FOR_NO", "0.05"))

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

# Stop-loss: sell NO position when bid drops to or below this level.
# Calibrated on June 3 position snapshots: 3 losers all crossed 55c before
# collapsing to 1c; 6 winners on the same day all stayed above 60c (closest
# was 62c on KATL 76-77F which recovered to 95c). 55c avoids that false
# positive while catching confirmed collapses. Override via env to experiment.
STOP_LOSS_NO_BID_CENTS = int(os.getenv("STOP_LOSS_NO_BID_CENTS", "55"))

# HTTP
HTTP_TIMEOUT_SECONDS = 15
USER_AGENT = "MeteoEdge/1.0 (Polymarket weather-arbitrage research bot; contact: andre.freixo.santos@gmail.com)"

# Optional: enrich market prices with live CLOB orderbook data per bracket.
# Adds ~2 API calls per matched bracket per poll. Off by default to keep polls fast;
# `outcomePrices` from Gamma is usually within 1¢ of the live mid for liquid markets.
ENABLE_CLOB_ENRICHMENT = os.getenv("ENABLE_CLOB_ENRICHMENT", "false").lower() == "true"


# ------------------------------------------------------------------
# Source Priority Configuration
# ------------------------------------------------------------------

@lru_cache(maxsize=None)
def _load_source_priority() -> dict:
    config_path = Path(__file__).parent.parent / "config" / "source_priority.yaml"
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def get_source_priority(city: str) -> list[dict]:
    return _load_source_priority().get(city, [])

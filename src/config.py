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
    ("RKSI", 37.4602,  126.4407,  "Seoul",         "RKSI", "C", "Asia/Seoul"),        # entries disabled via DISABLED_STATIONS — June forecast busts (#201)
    ("WMKK",  2.7456,  101.7099,  "Kuala Lumpur",  "WMKK", "C", "Asia/Kuala_Lumpur"),# 9/9 100% shadow
    ("RKPK", 35.1795,  128.9382,  "Busan",         "RKPK", "C", "Asia/Seoul"),        # 9/10 90% shadow
    # ZSPD (Shanghai) removed — shadow trades averaged ~57c entry price, below MIN_PRICE_CENTS=60;
    # shadow validation does not apply to live conditions. Re-evaluate when ≥5 trades at ≥60c.
    ("ZGSZ", 22.6393,  113.8108,  "Shenzhen",      "ZGSZ", "C", "Asia/Shanghai"),     # 12/12 100% shadow
    ("WSSS",  1.3644,  103.9915,  "Singapore",     "WSSS", "C", "Asia/Singapore"),    # 9/9 100% shadow
    ("MPMG",  8.9734,  -79.5556,  "Panama City",   "MPMG", "C", "America/Panama"),    # 10/10 100% shadow

    # --- Archive shadow candidates (issue #274) — shadow-only by default.
    # All are included in SHADOW_STATIONS_ARCHIVE below.
    # Hong Kong intentionally OMITTED — resolves against Hong Kong Observatory
    # (weather.gov.hk), not a standard ICAO METAR site. Requires a custom
    # scraper before it can be added. Track in a follow-up issue.

    # Europe
    ("EGLC", 51.5053,    0.0553,  "London",        "EGLC", "C", "Europe/London"),
    ("LFPB", 48.9694,    2.4414,  "Paris",         "LFPB", "C", "Europe/Paris"),
    ("LIMC", 45.6306,    8.7281,  "Milan",         "LIMC", "C", "Europe/Rome"),
    ("EFHK", 60.3172,   24.9633,  "Helsinki",      "EFHK", "C", "Europe/Helsinki"),
    ("EPWA", 52.1657,   20.9671,  "Warsaw",        "EPWA", "C", "Europe/Warsaw"),
    ("LTFM", 41.2611,   28.7416,  "Istanbul",      "LTFM", "C", "Europe/Istanbul"),
    # Ankara resolves against LTAC (Cubuk, north of city centre) per Polymarket descriptions
    ("LTAC", 40.1378,   32.9988,  "Ankara",        "LTAC", "C", "Europe/Istanbul"),

    # Asia / Pacific
    ("RJTT", 35.5494,  139.7798,  "Tokyo",         "RJTT", "C", "Asia/Tokyo"),
    ("RCSS", 25.0697,  121.5519,  "Taipei",        "RCSS", "C", "Asia/Taipei"),
    ("ZSPD", 31.1443,  121.8083,  "Shanghai",      "ZSPD", "C", "Asia/Shanghai"),
    ("ZGGG", 23.3924,  113.2988,  "Guangzhou",     "ZGGG", "C", "Asia/Shanghai"),
    ("ZHHH", 30.7838,  114.2081,  "Wuhan",         "ZHHH", "C", "Asia/Shanghai"),
    ("ZSJN", 36.8572,  117.2161,  "Jinan",         "ZSJN", "C", "Asia/Shanghai"),
    ("ZHCC", 34.5197,  113.8408,  "Zhengzhou",     "ZHCC", "C", "Asia/Shanghai"),
    ("RPLL", 14.5086,  121.0194,  "Manila",        "RPLL", "C", "Asia/Manila"),

    # MENA
    ("LLBG", 32.0114,   34.8867,  "Tel Aviv",      "LLBG", "C", "Asia/Jerusalem"),
    ("OEJN", 21.6796,   39.1565,  "Jeddah",        "OEJN", "C", "Asia/Riyadh"),

    # Latin America
    ("SBGR",-23.4356,  -46.4731,  "Sao Paulo",     "SBGR", "C", "America/Sao_Paulo"),

    # Oceania
    ("NZWN",-41.3272,  174.8053,  "Wellington",    "NZWN", "C", "Pacific/Auckland"),
]


def station_city(cfg) -> str:
    """Return the Polymarket city name from a STATIONS entry (index 3)."""
    return cfg[3]


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
    # Archive shadow candidates (issue #274)
    "EGLC": "Europe/London",
    "LFPB": "Europe/Paris",
    "LIMC": "Europe/Rome",
    "EFHK": "Europe/Helsinki",
    "EPWA": "Europe/Warsaw",
    "LTFM": "Europe/Istanbul",
    "LTAC": "Europe/Istanbul",
    "RJTT": "Asia/Tokyo",
    "RCSS": "Asia/Taipei",
    "ZSPD": "Asia/Shanghai",
    "ZGGG": "Asia/Shanghai",
    "ZHHH": "Asia/Shanghai",
    "ZSJN": "Asia/Shanghai",
    "ZHCC": "Asia/Shanghai",
    "RPLL": "Asia/Manila",
    "LLBG": "Asia/Jerusalem",
    "OEJN": "Asia/Riyadh",
    "SBGR": "America/Sao_Paulo",
    "NZWN": "Pacific/Auckland",
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
    # Archive shadow candidates (issue #274) — default 6-23 local window
    "EGLC": (6, 23),
    "LFPB": (6, 23),
    "LIMC": (6, 23),
    "EFHK": (6, 23),
    "EPWA": (6, 23),
    "LTFM": (6, 23),
    "LTAC": (6, 23),
    "RJTT": (6, 23),
    "RCSS": (6, 23),
    "ZSPD": (11, 23),   # same narrow-bracket rationale as ZGSZ (Asia/Shanghai)
    "ZGGG": (11, 23),   # same narrow-bracket rationale as ZGSZ (Asia/Shanghai)
    "ZHHH": (11, 23),   # same narrow-bracket rationale as ZGSZ (Asia/Shanghai)
    "ZSJN": (11, 23),   # same narrow-bracket rationale as ZGSZ (Asia/Shanghai)
    "ZHCC": (11, 23),   # same narrow-bracket rationale as ZGSZ (Asia/Shanghai)
    "RPLL": (6, 23),
    "LLBG": (6, 23),
    "OEJN": (6, 23),
    "SBGR": (6, 23),
    "NZWN": (6, 23),
}

# Issue #669: stations whose METAR persistence must run 24/7, independent of
# STATION_ACTIVE_HOURS, because the climb-table builder needs early
# local-morning observations that the scanner's active-hours gate otherwise
# thins out (RKSI/RKPK start at local 11:00; the Chinese stations at local
# 11:00 too -- see STATION_ACTIVE_HOURS above).
#
# This does NOT touch STATION_ACTIVE_HOURS itself or build_weather_for_scanning()
# -- that gate exists to prevent the scanner from opening new *entries* on
# overnight carryover (the KHOU 2026-05-27 incident) and must not be regressed.
# It only adds a second, narrow METAR fetch+persist pass
# (persist_metar_for_climb_stations() in src/weather/builder.py) that runs
# every poll for these stations regardless of local hour or open-position
# status, mirroring the precedent set by build_weather_for_pricing() (#425)
# for the same "scanner gate is right for trading, wrong for data collection"
# reason.
#
# RKSI and RKPK are included even though Seoul/Busan already have a 24/7 AMOS
# feed (see config/source_priority.yaml) -- METAR remains a useful redundant
# feed for the climb builder's get_canonical_station_feeds() union, and the
# extra persistence is a no-op cost (METAR is fetched every poll anyway via
# the shared metars_cache).
CLIMB_BUILDER_24H_METAR_STATIONS: "frozenset[str]" = frozenset({
    "RKSI", "RKPK", "ZGSZ", "ZGGG", "ZHHH", "ZHCC", "ZSPD",
})

# Strategy thresholds (env var overrides)
MIN_EDGE_CENTS = float(os.getenv("MIN_EDGE_CENTS", "15.0"))
# Live ledger showed high-edge entries are adversely selected. Tightened from 25c
# to 20c on 2026-06-04: 5/6 post-fix losses had edge 21-25c, and June 3 showed
# a clear pattern of low-priced NO (high claimed edge) losing while higher-priced
# entries on the same station/day won. Override via env to experiment.
MAX_EDGE_CENTS = float(os.getenv("MAX_EDGE_CENTS", "20.0"))
# Ground-truth audit 2026-07-07 (#644): NO bought at 60-69c had a 46% win rate
# vs ~65% breakeven (-47.70 EUR on 28 trades) — the model is most confidently
# wrong exactly where it disagrees hardest with the market. Floor raised 60→70.
# The DB bot_config value (currently 75) is authoritative at runtime.
MIN_PRICE_CENTS = int(os.getenv("MIN_PRICE_CENTS", "70"))
MIN_CONFIDENCE_YES = 0.85       # for YES-side trades
# NO-side entry threshold: only enter when model's p(YES) is at or below this.
# Calibration on 30 trustworthy trades (May 24-29, with SELL pnl or yes_won
# field) showed the model's predicted_NO band of 85-95c had 40-64% real
# win rate — essentially noise.  The 95-99c and 100c bands had 67-90% real
# WR, the only bands with measurable signal.  Tightening from 0.15 (allow
# entries down to predicted_NO=85c) to 0.05 (only at predicted_NO>=95c)
# restricts the bot to the calibrated regime. Override via env to experiment.
MAX_CONFIDENCE_YES_FOR_NO = float(os.getenv("MAX_CONFIDENCE_YES_FOR_NO", "0.05"))

MIN_MINUTES_TO_SETTLEMENT = 15
# Daily temperature markets resolve within 24h — reject anything beyond this window.
# Without this cap the scanner evaluates tomorrow's markets against today's METAR data,
# producing spurious 100% NO confidence (today's observed high makes future brackets
# look impossible when they are not).
MAX_MINUTES_TO_SETTLEMENT = int(os.getenv("MAX_MINUTES_TO_SETTLEMENT", "1440"))  # 24 hours

# Polling cadence (env var override)
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))  # 5 minutes default

# Freshness thresholds by source (VALUES ARE IN SECONDS despite the _MIN suffix
# in the name -- legacy naming. FreshnessMonitor reads these directly as seconds
# and multiplies by 3 for the CRITICAL tier.)
#
# Production override: each source can be overridden via env var
# FRESHNESS_<SOURCE>_SEC (e.g. FRESHNESS_METAR_SEC=3600) without touching the
# in-code defaults. The defaults below are kept tight because the unit tests
# (src/tests/test_freshness_monitor.py) hard-code them. The deployed bot
# should set env vars to match real feed cadences -- otherwise routine
# 5-minute METAR delays generate hundreds of CRITICAL log lines per hour
# (observed 2026-06-21: ~100 CRITICAL/hour with default 180s metar threshold).
#
# Recommended production env values (all in seconds):
#   FRESHNESS_METAR_SEC=3600         # 1h: METAR cadence ~30-60min
#   FRESHNESS_AMOS_SEC=1800          # 30min: AMOS cadence ~30min
#   FRESHNESS_MSS_SEC=900            # 15min: MSS cadence ~1min documented
#   FRESHNESS_JMA_AMEIDAS_SEC=1800   # 30min: JMA AMeDAS cadence ~10min, bursts to 30
FRESHNESS_THRESHOLDS_MIN: dict[str, int] = {
    "metar": int(os.getenv("FRESHNESS_METAR_SEC", "180")),
    "amos": int(os.getenv("FRESHNESS_AMOS_SEC", "90")),
    "mss": int(os.getenv("FRESHNESS_MSS_SEC", "15")),
    "jma_ameidas": int(os.getenv("FRESHNESS_JMA_AMEIDAS_SEC", "60")),
}
# Default threshold (in seconds) for sources not listed above
FRESHNESS_THRESHOLD_DEFAULT_MIN = int(os.getenv("FRESHNESS_DEFAULT_SEC", "180"))

# Risk management limits (all configurable via env vars)
STARTING_CAPITAL_EUR = float(os.getenv("STARTING_CAPITAL_EUR", "500.0"))
RISK_DAILY_LOSS_LIMIT_EUR = float(os.getenv("RISK_DAILY_LOSS_LIMIT_EUR", "50.0"))
RISK_MAX_OPEN_POSITIONS = int(os.getenv("RISK_MAX_OPEN_POSITIONS", "15"))
RISK_DRAWDOWN_STOP_PCT = float(os.getenv("RISK_DRAWDOWN_STOP_PCT", "0.15"))
RISK_MIN_LIQUIDITY = int(os.getenv("RISK_MIN_LIQUIDITY", "50"))

# Issue #611: intentional bracket re-entry must be an explicit config decision.
# false (default): once ANY live order exists today for (station, ticker, side, day)
#   -- filled, sold, or timeout attempt -- the entry gate in run.py blocks every
#   further order on that bracket for the rest of the day. Timeout attempts count
#   deliberately: repeated timeout retries were part of the observed stacking
#   (7x KATL 98-99F on 07-02, 5 timeout WMKK attempts on 07-03).
# true: re-entry after an exit is allowed, but the gate still blocks while an
#   OPEN position exists for the token -- never stack on an open position.
LIVE_ALLOW_BRACKET_REENTRY = os.getenv("LIVE_ALLOW_BRACKET_REENTRY", "false").lower() == "true"

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
SIZING_MODE: str = os.getenv("SIZING_MODE", "flat")

# Take-profit: exit when market bid reaches (predicted_price - buffer).
# The model snapshot is frozen at entry time and cannot validate further price
# movement, so we lock in the captured edge and recycle capital into the next trade.
TAKE_PROFIT_BUFFER_CENTS = int(os.getenv("TAKE_PROFIT_BUFFER_CENTS", "2"))

# Per-station take-profit buffer overrides.  Keys are ICAO codes; values are
# integers (¢).  Set via env: TAKE_PROFIT_BUFFER_CENTS_KORD=3 etc.
# Falls back to TAKE_PROFIT_BUFFER_CENTS when no station-specific override exists.
def get_take_profit_buffer_cents(station: str) -> int:
    """Return the take-profit buffer (¢) for *station*.

    Checks for a station-specific env var (TAKE_PROFIT_BUFFER_CENTS_{STATION})
    first; falls back to the global TAKE_PROFIT_BUFFER_CENTS.
    """
    env_key = f"TAKE_PROFIT_BUFFER_CENTS_{station.upper()}"
    raw = os.getenv(env_key)
    if raw is not None:
        try:
            return int(raw)
        except (ValueError, TypeError):
            pass
    return TAKE_PROFIT_BUFFER_CENTS

# Forced pre-settlement exit: when a position is within this many minutes of
# settlement resolution AND the NO bid has depth >= STOP_LOSS_MIN_DEPTH_SHARES,
# cross the spread and close rather than holding to settlement.
# Live data (2026-06-07..06-16) shows hold-to-settlement is net-negative
# (−€1.72 at 64% win rate) while early exits are strongly positive
# (+€29.79 at 90% win rate).
# Set to 0 to reproduce today's behaviour (disabled).
FORCE_EXIT_MINUTES_TO_SETTLEMENT = int(
    os.getenv("FORCE_EXIT_MINUTES_TO_SETTLEMENT", "60")
)

# Stop-loss: model-based, NOT price-based. Backtest of 130 settled positions
# (May 27 - Jun 11, position_snapshots.jsonl replay) showed every bid-threshold
# stop is net harmful (bid<=entry-15: -65.6 EUR; bid<=55c: -58.3 EUR vs hold)
# because winners routinely dip to 10-30c on intraday noise before recovering
# to 99c. The model trigger (live fair value below avg entry) is ~EV-neutral
# but cuts the full-stake loss tail by ~1/3: it exits early at high bids,
# before the market reprices. See issue #177 for the full table.
# Trigger: fair_value_now < avg_entry for STOP_LOSS_CONSECUTIVE_POLLS polls.
# Floor: only sell while bid >= STOP_LOSS_MIN_BID_CENTS -- below that the
# salvage value is too small vs the recovery odds (strikes are kept, so a
# bid recovery while the model still disagrees sells immediately).
STOP_LOSS_MIN_BID_CENTS = int(os.getenv("STOP_LOSS_MIN_BID_CENTS", "40"))
STOP_LOSS_CONSECUTIVE_POLLS = int(os.getenv("STOP_LOSS_CONSECUTIVE_POLLS", "2"))
# Thin-book guard: require this many shares at the best bid before stopping
# out -- a 1-share spoof quote must not trigger an exit.
STOP_LOSS_MIN_DEPTH_SHARES = float(os.getenv("STOP_LOSS_MIN_DEPTH_SHARES", "10"))
# Stop-loss sells are priced through the bid by this many cents so the order
# crosses immediately even if the top of book ticks down between the orderbook
# fetch and the post. Unfilled remainders are cancelled, never left resting.
STOP_LOSS_SELL_AGGRESSION_CENTS = int(os.getenv("STOP_LOSS_SELL_AGGRESSION_CENTS", "2"))

# Stop-loss safety guards (hotfix 2026-06-13 after the model cut 3 likely wins
# in one day -- WSSS 91-93, MPMG 88-90, KATL 90-91 all overshot bracket but
# fair_value_now briefly crashed on intraday temp spikes).
#
# Proximity guard: don't fire while the running daily high is more than this
# many F below bracket_low. The model occasionally panics when temp climbs a
# couple of degrees in a few minutes; if the bracket is still 1-2F away, the
# fair-value crash is usually a false alarm and the bid recovers within the
# next polls. Set to a large negative number to disable.
STOP_LOSS_MIN_BRACKET_PROXIMITY_F = float(
    os.getenv("STOP_LOSS_MIN_BRACKET_PROXIMITY_F", "0.5")
)
# Overshoot guard: don't fire when an available forecast (NWS, else secondary)
# predicts the daily high will exceed bracket_high. NO wins on overshoot, so a
# model dip while the temp climbs through the bracket is the path to a win,
# not a loss. Disabled for "or above" brackets where overshoot is impossible.
STOP_LOSS_RESPECT_FORECAST_OVERSHOOT = (
    os.getenv("STOP_LOSS_RESPECT_FORECAST_OVERSHOOT", "true").lower() == "true"
)

# Entry margin filter: skip NO entries when the bracket sits within this many
# degrees F of max(forecast high, current running high). Margin-bucket analysis
# of 117 confirmed outcomes (May 27 - Jun 11): 0-2F margin = 27% loss rate,
# -11.62 EUR; 2-4F margin = 4% loss rate, +20.69 EUR. With ~+1 EUR wins vs
# -5 EUR losses, the <2.5F zone is pure bleed. See issue #200.
MIN_FORECAST_BRACKET_MARGIN_F = float(os.getenv("MIN_FORECAST_BRACKET_MARGIN_F", "2.5"))

# Interim overconfidence guardrail (issue #305). Clamps p_yes to [1-cap, cap]
# so the system never treats a bracket as a certainty. Setting to 1.0 reproduces
# pre-guardrail behaviour exactly. Remove/loosen once EMOS (#70) is promoted.
MODEL_PROB_CAP = float(os.getenv("MODEL_PROB_CAP", "0.95"))
# Envelope stddev floor as a fraction of the climb still to come (issue #652):
# effective_stddev = max(forecast_stddev, fraction * (max_env - current_high)).
# Prevents near-certain morning claims about a high that is mostly unrealized.
ENVELOPE_SIGMA_CLIMB_FRACTION = float(os.getenv("ENVELOPE_SIGMA_CLIMB_FRACTION", "0.5"))

# Feature flag (issue #448, epic #70 Phase 2 / #445): when true AND
# WeatherState.ensemble_sigma_f is populated (per-station GEFS ensemble
# spread -- the estimator lives in src/model/ensemble_sigma.py; wiring it
# onto WeatherState is a follow-up issue, #449/#665), true_probability_yes
# and the EMOS-shadow serving path use it as the forecast stddev instead of
# the fixed FORECAST_STDDEV_F. Default OFF: this PR only plumbs the field
# and its consumption path -- it must not change what gets served live.
# Promote per station only after shadow data validates the estimator.
USE_ENSEMBLE_SIGMA = os.getenv("USE_ENSEMBLE_SIGMA", "false").lower() == "true"

# EMOS deployment mode: 'legacy' | 'emos_shadow' | 'emos_primary'
# Per-city mode is read from the emos_calibration table; this is the fallback
# when no calibration row exists for a city.
EMOS_DEFAULT_MODE: str = os.environ.get("EMOS_DEFAULT_MODE", "legacy")

# Stations excluded from new entries (observations keep collecting).
# RKSI: June forecast busts of +5.4 to +12.4F produced 4 losses (10W/4L,
# -7.16 EUR net) -- the worst station of the month. See issue #201.
#
# SHADOW_STATIONS: shadow both sides (DISABLED_STATIONS kept as alias for back-compat).
_shadow_both_raw = os.getenv("SHADOW_STATIONS") or os.getenv("DISABLED_STATIONS", "RKSI")
SHADOW_STATIONS: "set[str]" = {s.strip().upper() for s in _shadow_both_raw.split(",") if s.strip()}

# Archive shadow candidates added in issue #274.  Shadow-only stations — no live orders
# until their shadow performance is validated.  Add their ICAO codes to
# SHADOW_STATIONS (via the env var) to suppress live trading, or use
# SHADOW_STATIONS_ARCHIVE directly in scripts that need to enumerate them.
# Hong Kong was intentionally omitted from the archive import — it resolves
# against Hong Kong Observatory (weather.gov.hk), which requires a custom
# scraper. It will be added in a dedicated follow-up issue.
SHADOW_STATIONS_ARCHIVE: "frozenset[str]" = frozenset({
    # Europe
    "EGLC", "LFPB", "LIMC", "EFHK", "EPWA", "LTFM", "LTAC",
    # Asia / Pacific
    "RJTT", "RCSS", "ZSPD", "ZGGG", "ZHHH", "ZSJN", "ZHCC", "RPLL",
    # MENA
    "LLBG", "OEJN",
    # Latin America
    "SBGR",
    # Oceania
    "NZWN",
})

# Alias kept so existing code referencing DISABLED_STATIONS still works.
DISABLED_STATIONS: "set[str]" = SHADOW_STATIONS

# Per-side shadow env vars: only shadow the named side.
SHADOW_STATIONS_YES: "set[str]" = {
    s.strip().upper() for s in os.getenv("SHADOW_STATIONS_YES", "").split(",") if s.strip()
}
SHADOW_STATIONS_NO: "set[str]" = {
    s.strip().upper() for s in os.getenv("SHADOW_STATIONS_NO", "").split(",") if s.strip()
}

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


def get_canonical_station_feeds(station: str) -> list[str]:
    """Return all DB ``station`` keys under which *station*'s observations are stored.

    High-cadence collectors (MSS, AMOS, JMA) persist rows under the Polymarket
    city name (e.g. ``"Singapore"``), while METAR persists under the ICAO code
    (e.g. ``"WSSS"``).  Querying only the ICAO key misses the denser city-keyed
    rows, leading to daily-high computations that can miss intra-30-min peaks.

    This helper resolves a station ICAO to the full list of DB keys that may
    contain observations for that physical station.  The list is ordered
    high-cadence first (city name) so callers can iterate in priority order.

    For stations without a city-keyed high-cadence feed the list contains only
    the ICAO code itself — no behaviour change for those stations.

    Example::

        get_canonical_station_feeds("WSSS")  # → ["Singapore", "WSSS"]
        get_canonical_station_feeds("KORD")  # → ["KORD"]

    Args:
        station: ICAO code (e.g. ``"WSSS"``).

    Returns:
        Ordered list of DB station keys, high-cadence (city-named) first.
    """
    # Build the ICAO → city mapping once from STATIONS (tuple index 0 = ICAO, index 3 = city).
    _icao_to_city: dict[str, str] = {s[0]: s[3] for s in STATIONS}
    city = _icao_to_city.get(station)
    if city is None:
        return [station]

    # Check whether a non-metar (high-cadence) source is configured for this city.
    # If source_priority.yaml has an entry for the city and any source is NOT metar,
    # a city-keyed feed exists → prepend the city name.
    sources = get_source_priority(city)
    has_hf_feed = any(s["source"] != "metar" for s in sources)
    if has_hf_feed:
        return [city, station]
    return [station]


def is_training_eligible(city: str) -> bool:
    """Return whether *city* is eligible for use in training-data construction.

    Reads ``source_priority.yaml`` via :func:`get_source_priority`.  A city is
    ineligible only if at least one of its entries explicitly sets
    ``training_eligible: false`` (e.g. dead/low-cadence feeds flagged in the
    2026-07-01 audit — see issue #558).  Cities with no entry at all in
    ``source_priority.yaml``, or whose entries omit the field, are eligible
    by default.

    Args:
        city: Polymarket city name (e.g. ``"Jinan"``).

    Returns:
        False if any source entry for *city* sets ``training_eligible: false``,
        True otherwise (including for cities absent from source_priority.yaml).
    """
    sources = get_source_priority(city)
    return not any(s.get("training_eligible") is False for s in sources)


# ------------------------------------------------------------------
# DB-backed parameter store — config keys, defaults, and live access
# ------------------------------------------------------------------

# All editable config keys with their hardcoded defaults.
# These must NOT include credentials, API keys, or STARTING_CAPITAL_EUR.
CONFIG_DEFAULTS: "dict[str, str | int | float | bool]" = {
    "MIN_EDGE_CENTS": 15.0,
    "MAX_EDGE_CENTS": 20.0,
    "MIN_PRICE_CENTS": 70,
    "MIN_CONFIDENCE_YES": 0.85,
    "MAX_CONFIDENCE_YES_FOR_NO": 0.05,
    "MIN_FORECAST_BRACKET_MARGIN_F": 2.5,
    "EMOS_DEFAULT_MODE": "legacy",
    "EMOS_MIN_SAMPLES_SHADOW": 35,  # Reserved for future shadow-entry gate (currently unused — no read location)
    "EMOS_MIN_SAMPLES_PROMOTION": 60,
    "DAILY_LOSS_LIMIT_EUR": 50.0,
    "MAX_OPEN_POSITIONS": 15,
    "DRAWDOWN_STOP_PCT": 0.15,
    "MIN_MARKET_LIQUIDITY_SHARES": 50.0,
    "POSITION_SIZE_EUR": 5.0,
    "SIZING_MODE": "flat",
    "TAKE_PROFIT_BUFFER_CENTS": 2,
    "STOP_LOSS_MIN_BID_CENTS": 40,
    "STOP_LOSS_CONSECUTIVE_POLLS": 2,
    "STOP_LOSS_MIN_DEPTH_SHARES": 10.0,
    "POLL_INTERVAL_SECONDS": 300,
    "MAX_MINUTES_TO_SETTLEMENT": 1440,
    "MIN_MINUTES_TO_SETTLEMENT": 15,
    "FORCE_EXIT_MINUTES_TO_SETTLEMENT": 60,
    "ZERO_EVAL_WATCHDOG_CONSECUTIVE_TICKS": 4,  # Fire alert after 4 consecutive zero-evaluation ticks with markets available (~20 min at 5-min poll cadence)
    # Alert when MAX(model_forecast_log.logged_at) is older than this many
    # hours (issue #717). The capture timer's four daily runs (~06/12/18/21 UTC)
    # leave a designed overnight gap of ~8.3h between the ~21:46 UTC write and
    # the next ~06:05 UTC run; the default must clear that gap or it false-alerts
    # every night (issue #726). 10h clears it with margin while still catching a
    # genuinely dead job the same morning it should have run.
    "FORECAST_CAPTURE_STALENESS_THRESHOLD_HOURS": 10.0,
    # Shadow-only YES thresholds — applied on the YES shadow path only.
    # These are intentionally looser than the live YES gates so the shadow loop
    # can collect data without risking live orders.  The NO side is unaffected.
    "SHADOW_MIN_EDGE_CENTS_YES": 3.0,
    "SHADOW_MIN_CONFIDENCE_YES": 0.55,
    "SHADOW_MIN_PRICE_CENTS_YES": 20,
    "MODEL_PROB_CAP": 0.95,
    "ENVELOPE_SIGMA_CLIMB_FRACTION": 0.5,
    # Feature flag (issue #448): use WeatherState.ensemble_sigma_f (GEFS
    # ensemble spread) instead of the fixed FORECAST_STDDEV_F when available.
    # Default off -- no live behaviour change until a station is promoted.
    "USE_ENSEMBLE_SIGMA": False,
    # Stage 1 of issue #551: rank/prioritize candidates using the uncapped (raw)
    # model probability instead of scan order. Default off -- entry gates always
    # consume the capped p_yes regardless of this flag; only which candidate is
    # preferred for execution (when capital/risk slots are limited) changes.
    "RANK_ON_RAW_PROB": False,
    # Residual bias correction (issue #307)
    "MAX_RESIDUAL_MAE_F_FOR_LIVE": 8.0,
    "RESIDUAL_WINDOW_DAYS": 30,
    "RESIDUAL_MIN_SAMPLES": 10,
    "RESIDUAL_MAX_CORRECTION_F": 5.0,
    "RESIDUAL_CORRECTION_ENABLED": True,
    # DEB master switch — set True to activate DEB weight computation and consumption
    "DEB_ENABLED": False,
    # DEB cold-start fractions for HRRR and NBM (issue #435)
    "DEB_HRRR_COLD_START_FRACTION": 0.4,
    "DEB_NBM_COLD_START_FRACTION": 0.4,
    # DEB cold-start fractions for ECMWF and ICON (issue #442)
    "DEB_ECMWF_COLD_START_FRACTION": 0.5,
    "DEB_ICON_COLD_START_FRACTION": 0.5,
    # DEB group weight cap for the noaa_us channel group (NWS + HRRR + NBM)
    "DEB_GROUP_WEIGHT_CAP": 0.7,
    # Active forecast stack — controls which ingestion channels are live.
    # baseline: NWS + open_meteo only
    # hrrr_nbm: adds HRRR and NBM for US stations
    # intl_ecmwf_icon: adds ECMWF and ICON-EU for international stations
    # full: all channels active
    "FORECAST_STACK": "baseline",
    # Active sigma source for EMOS retraining/serving (issue #449) — which
    # emos_calibration track (keyed by forecast_source AND sigma_source) a
    # retrain writes to and a reader (get_city_mode/apply_emos) reads from.
    # fixed:    train/serve sigma_raw as the constant FORECAST_STDDEV_F,
    #           ignoring any persisted per-row model_forecast_log.sigma_f —
    #           the historical default this codebase used before per-row
    #           sigma existed.
    # ensemble: train/serve sigma_raw from the persisted sigma_f (falls back
    #           to FORECAST_STDDEV_F when a row has none), pairing with the
    #           USE_ENSEMBLE_SIGMA serving path's state.ensemble_sigma_f
    #           (issue #448) so d is no longer fit against a near-constant
    #           input.
    # Default 'fixed' -- no live behaviour change until an operator flips
    # this AND a sigma_source='ensemble' retrain has been promoted.
    "EMOS_SIGMA_SOURCE": "fixed",
    # Statistical promotion bar (issue #559) — advisory shadow→live tooling.
    # Supersedes issue #80's old thresholds (>=5 trades / 100% WR / >=3 days).
    # A station+side is "eligible" iff settled shadow trades >= this minimum
    # AND the Wilson lower bound of its win rate exceeds the break-even win
    # rate implied by its avg entry price + the fee model (src/strategy/fee.py).
    # This tool is advisory only: it never auto-promotes and never touches the
    # live entry gate.
    "PROMOTION_MIN_SETTLED_TRADES": 30,
    "PROMOTION_WILSON_CONFIDENCE": 0.95,
    # Next-day evaluation (issue #687): once a station's own today-market is
    # past MIN_MINUTES_TO_SETTLEMENT (or absent), allow evaluating its next
    # market instead of hard-skipping it via the wrong_date gate. Default off
    # -- scanner behaviour is byte-for-byte identical to today while this is
    # False. When on, next-day candidates are shadow-logged only (is_next_day=1)
    # via the existing log_candidate()/insert_candidate() path -- no live
    # entries from next-day evaluation under any circumstance in this design.
    "NEXT_DAY_EVALUATION": False,
    # LOW-direction ("lowest temperature in") market scanning (issue #733
    # rollback decision, 2026-07-17): the bot focuses on daily-HIGH markets
    # only. LOW was shadow-only from day one (#455) and produced a
    # disproportionate bug trail (#554, #610, permanent settle zombies) for
    # 55 shadow rows of output. Off by default; flipping this back on
    # restores the previous shadow-only LOW scan unchanged.
    "ENABLE_LOW_MARKETS": False,
    # Fallback sigma multiplier for next-day evaluation when no EMOS lead bin
    # covers the market's lead time (issue #687 amendment 2): effective sigma
    # = FORECAST_STDDEV_F * NEXT_DAY_SIGMA_MULTIPLIER. 1.5 is a starting
    # estimate, not a fitted value -- only applies to the fully-unfitted case;
    # a matched calibration bin's own sigma is used unchanged.
    "NEXT_DAY_SIGMA_MULTIPLIER": 1.5,
}

# Maps each FORECAST_STACK value to the set of model tags whose rows should be
# averaged (EQUAL weights) to form the ensemble μ during EMOS training. The
# EMOS SERVING path consumes the same equal-weight mean of the same feeds
# (emos_mode.emos_serving_mu, issue #658 train/serve parity) — NOT the
# DEB-weighted/intraday-corrected μ used by the legacy envelope path.
FORECAST_STACK_MODELS: dict[str, frozenset] = {
    "baseline":        frozenset({"nws", "open_meteo"}),
    "hrrr_nbm":        frozenset({"nws", "open_meteo", "hrrr", "nbm"}),
    "intl_ecmwf_icon": frozenset({"nws", "open_meteo", "ecmwf", "icon"}),
    "full":            frozenset({"nws", "open_meteo", "hrrr", "nbm", "ecmwf", "icon", "gefs"}),
}


def seed_config(db) -> None:
    """Seed bot_config from env vars / hardcoded defaults on first run.

    For each key in CONFIG_DEFAULTS:
    - If no DB row exists: seed from env var (if set) or hardcoded default.
    - If a row already exists: leave it alone — DB is authoritative.

    Call once at process start before the first poll cycle.
    """
    for key, default in CONFIG_DEFAULTS.items():
        existing = db.get_config(key)
        if existing is None:
            # First run — seed from env var if set, otherwise use hardcoded default
            value = os.getenv(key, str(default))
            db.set_config(key, value)


def seed_station_overrides(db) -> None:
    """Seed station_overrides on first run for shadow-mode stations.

    Idempotent: existing rows are never overwritten — DB is authoritative.
    Only called once per startup.

    RKSI — shadows both YES and NO sides (issue #288).
    Epic-C low-side shadow cities (issue #457) — seeded with low_no_enabled=0
    (shadow-only); promoted to 1 after ≥14 days validation per city.
    """
    if db.get_station_override("RKSI") is None:
        db.set_station_override("RKSI", yes_enabled=False, no_enabled=False, low_no_enabled=False)

    # Epic-C low-side shadow rollout (issue #457): ensure rows exist for the 6 cities.
    # DB migration adds low_no_enabled=0 to all existing rows automatically; this only
    # creates missing rows so the DB tracks them explicitly.
    for station in ("EGLC", "LFPB", "RJTT", "ZSPD", "KMIA"):
        if db.get_station_override(station) is None:
            db.set_station_override(station, yes_enabled=False, no_enabled=False, low_no_enabled=False)


def get_live_config(db) -> dict:
    """Return current bot_config values as a typed dict.

    Reads all rows from the bot_config table and casts each value to its
    expected Python type.  Falls back to CONFIG_DEFAULTS for any key not
    yet seeded (should not happen after seed_config() runs, but safe).
    """
    raw = db.get_all_config()
    result: dict = {}
    for key, default in CONFIG_DEFAULTS.items():
        raw_val = raw.get(key, str(default))
        if isinstance(default, bool):
            result[key] = raw_val.lower() in ("true", "1", "yes")
        elif isinstance(default, int):
            try:
                result[key] = int(raw_val)
            except (ValueError, TypeError):
                result[key] = default
        elif isinstance(default, float):
            try:
                result[key] = float(raw_val)
            except (ValueError, TypeError):
                result[key] = default
        else:
            # str (covers EMOS_DEFAULT_MODE)
            result[key] = raw_val
    return result


# ------------------------------------------------------------------
# GRIB / HRRR cache configuration
# ------------------------------------------------------------------
# These params are intentionally minimal and kept separate from the DB-backed
# CONFIG_DEFAULTS store because they control infrastructure (disk layout, TTL)
# rather than trading strategy.  They follow the same env-var-override pattern
# used throughout this file and are read live by src/data/grib_cache.py.

# TTL for on-disk GRIB2 slices (hours).  HRRR runs hourly; 6h keeps two full
# model cycles in cache while preventing unbounded disk growth.
GRIB_CACHE_TTL_HOURS: float = float(os.getenv("GRIB_CACHE_TTL_HOURS", "6.0"))

# Directory for cached GRIB2 slices.  Relative to the working directory (i.e.
# the repo root when running normally).  Override via env to point at fast
# local storage or a shared NFS mount.
GRIB_CACHE_DIR: str = os.getenv("GRIB_CACHE_DIR", ".grib_cache")

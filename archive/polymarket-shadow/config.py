"""
Shadow-loop config. Same shape as polymarket-spike/config.py but with:
  - 50 cities (11 US + 39 non-US) instead of 11
  - unit field per station ("F" for US, "C" for non-US)
  - timezone field per station
  - region field for cohort analysis
  - forecast_source per station (nws | open-meteo)

The model lives in envelope.py and is unit-aware. Climb-rate lookup
is still US-tuned — that is intentional: the whole point of running
the shadow loop is to gather data to recalibrate per region.
"""
from pathlib import Path

POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_API = "https://clob.polymarket.com"
POLYMARKET_WEATHER_TAG_ID = "84"

# Station tuple:
#   (icao, lat, lon, polymarket_city_name, resolution_station,
#    unit, timezone, region, forecast_source)
#
# Coordinates are airport-approximate. Polymarket city names match the
# 'highest/lowest temperature in <city>' question text exactly.

STATIONS = [
    # --- US (11) — same as live spike, kept here so shadow predictions
    # for US cities can be compared directly against live trades
    ("KLGA", 40.7790,  -73.8740,  "New York City", "KLGA", "F", "America/New_York",    "us", "nws"),
    ("KORD", 41.9742,  -87.9073,  "Chicago",       "KORD", "F", "America/Chicago",     "us", "nws"),
    ("KMIA", 25.7953,  -80.2901,  "Miami",         "KMIA", "F", "America/New_York",    "us", "nws"),
    ("KAUS", 30.1944,  -97.6700,  "Austin",        "KAUS", "F", "America/Chicago",     "us", "nws"),
    ("KLAX", 33.9425, -118.4081,  "Los Angeles",   "KLAX", "F", "America/Los_Angeles", "us", "nws"),
    ("KDAL", 32.8470,  -96.8517,  "Dallas",        "KDAL", "F", "America/Chicago",     "us", "nws"),
    ("KATL", 33.6367,  -84.4281,  "Atlanta",       "KATL", "F", "America/New_York",    "us", "nws"),
    ("KBKF", 39.7017, -104.7517,  "Denver",        "KBKF", "F", "America/Denver",      "us", "nws"),
    ("KHOU", 29.6454,  -95.2789,  "Houston",       "KHOU", "F", "America/Chicago",     "us", "nws"),
    ("KSFO", 37.6213, -122.3790,  "San Francisco", "KSFO", "F", "America/Los_Angeles", "us", "nws"),
    ("KSEA", 47.4502, -122.3088,  "Seattle",       "KSEA", "F", "America/Los_Angeles", "us", "nws"),

    # --- Europe (7)
    # EDDM (Munich) removed — 50% win, -50% calibration drift, 6 trades, 3 days
    # EHAM (Amsterdam) removed — 80% win but -19% calibration drift; 0% on first day
    # LEMD (Madrid) removed — 50% win, -49% calibration drift, 6 trades, 4 days
    ("EGLC", 51.5053,    0.0553,  "London",        "EGLC", "C", "Europe/London",       "eu", "open-meteo"),
    ("LFPB", 48.9694,    2.4414,  "Paris",         "LFPB", "C", "Europe/Paris",        "eu", "open-meteo"),
    ("LIMC", 45.6306,    8.7281,  "Milan",         "LIMC", "C", "Europe/Rome",         "eu", "open-meteo"),
    ("EFHK", 60.3172,   24.9633,  "Helsinki",      "EFHK", "C", "Europe/Helsinki",     "eu", "open-meteo"),
    ("EPWA", 52.1657,   20.9671,  "Warsaw",        "EPWA", "C", "Europe/Warsaw",       "eu", "open-meteo"),
    ("UUWW", 55.5915,   37.2615,  "Moscow",        "UUWW", "C", "Europe/Moscow",       "eu", "open-meteo"),
    ("LTFM", 41.2611,   28.7416,  "Istanbul",      "LTFM", "C", "Europe/Istanbul",     "eu", "open-meteo"),

    # --- Asia/Pacific (13)
    # ZBAA (Beijing) removed — 50% win, -50% calibration drift, 4 days
    # ZSQD (Qingdao) removed — 67% win but inconsistent (0/2 on first day), -33% calib drift
    # ZUCK (Chongqing) removed — 60% win, losses first two days, -40% drift
    # OPKC (Karachi) removed — 71% win but loses on 2 of 5 days, -22% calib drift
    ("RJTT", 35.5494,  139.7798,  "Tokyo",         "RJTT", "C", "Asia/Tokyo",          "asia", "open-meteo"),
    ("RKSI", 37.4602,  126.4407,  "Seoul",         "RKSI", "C", "Asia/Seoul",          "asia", "open-meteo"),
    ("RKPK", 35.1795,  128.9382,  "Busan",         "RKPK", "C", "Asia/Seoul",          "asia", "open-meteo"),
    ("RCSS", 25.0697,  121.5519,  "Taipei",        "RCSS", "C", "Asia/Taipei",         "asia", "open-meteo"),
    ("ZSPD", 31.1443,  121.8083,  "Shanghai",      "ZSPD", "C", "Asia/Shanghai",       "asia", "open-meteo"),
    ("ZGGG", 23.3924,  113.2988,  "Guangzhou",     "ZGGG", "C", "Asia/Shanghai",       "asia", "open-meteo"),
    ("ZGSZ", 22.6393,  113.8108,  "Shenzhen",      "ZGSZ", "C", "Asia/Shanghai",       "asia", "open-meteo"),
    ("ZUUU", 30.5786,  103.9471,  "Chengdu",       "ZUUU", "C", "Asia/Shanghai",       "asia", "open-meteo"),
    ("ZHHH", 30.7838,  114.2081,  "Wuhan",         "ZHHH", "C", "Asia/Shanghai",       "asia", "open-meteo"),
    ("ZSJN", 36.8572,  117.2161,  "Jinan",         "ZSJN", "C", "Asia/Shanghai",       "asia", "open-meteo"),
    ("ZHCC", 34.5197,  113.8408,  "Zhengzhou",     "ZHCC", "C", "Asia/Shanghai",       "asia", "open-meteo"),
    ("RPLL", 14.5086,  121.0194,  "Manila",        "RPLL", "C", "Asia/Manila",         "asia", "open-meteo"),

    # --- Southeast Asia / South Asia (3)
    # VILK (Lucknow) removed — 67% win, losses on 2 of 4 days
    ("WSSS",  1.3644,  103.9915,  "Singapore",     "WSSS", "C", "Asia/Singapore",      "asia", "open-meteo"),
    ("WMKK",  2.7456,  101.7099,  "Kuala Lumpur",  "WMKK", "C", "Asia/Kuala_Lumpur",   "asia", "open-meteo"),

    # --- MENA (2)
    ("LLBG", 32.0114,   34.8867,  "Tel Aviv",      "LLBG", "C", "Asia/Jerusalem",      "mena", "open-meteo"),
    ("OEJN", 21.6796,   39.1565,  "Jeddah",        "OEJN", "C", "Asia/Riyadh",         "mena", "open-meteo"),

    # --- Latin America (3)
    # CYYZ (Toronto) removed — 50% win, -24.5% ROI, -50% calibration drift, 4 days
    # MMMX (Mexico City) removed — 0 settled tickers in 5 days (market closes mid-UTC-night, never polled to within 5h)
    # FACT (Cape Town) removed — 50% win, -3.5% ROI, -50% calibration drift
    ("SBGR",-23.4356,  -46.4731,  "Sao Paulo",     "SBGR", "C", "America/Sao_Paulo",   "latam", "open-meteo"),
    ("SAEZ",-34.8222,  -58.5358,  "Buenos Aires",  "SAEZ", "C", "America/Argentina/Buenos_Aires", "latam", "open-meteo"),
    ("MPMG",  8.9734,  -79.5556,  "Panama City",   "MPMG", "C", "America/Panama",      "latam", "open-meteo"),

    # --- Oceania (1)
    ("NZWN",-41.3272,  174.8053,  "Wellington",    "NZWN", "C", "Pacific/Auckland",    "oceania", "open-meteo"),

    # Ankara resolves against LTAC (Cubuk, north of city center) per Polymarket descriptions
    ("LTAC", 40.1378,   32.9988,  "Ankara",        "LTAC", "C", "Europe/Istanbul",     "eu", "open-meteo"),

    # Hong Kong intentionally OMITTED — resolves against Hong Kong Observatory
    # (weather.gov.hk), not an ICAO METAR site. Would need a custom scraper.
]

# Polling cadence — same as live spike
POLL_INTERVAL_SECONDS = 300  # 5 minutes

# Strategy thresholds (matching live spike for direct comparison on US cohort)
MIN_EDGE_CENTS = 3.0
MIN_CONFIDENCE_YES = 0.80
MAX_CONFIDENCE_YES_FOR_NO = 0.20
MIN_MINUTES_TO_SETTLEMENT = 15

# Climb lookup is in Fahrenheit (US-tuned). envelope.py converts to
# Celsius at lookup time for non-US stations. This is admittedly crude
# — real climate-specific tables are a recalibration task once we have
# enough shadow data to fit them.
DEFAULT_CLIMB_LOOKUP_F = {
    0: 25.0, 1: 25.0, 2: 25.0, 3: 24.0, 4: 23.0, 5: 21.0,
    6: 18.0, 7: 15.0, 8: 12.0, 9: 10.0,
    10: 8.0, 11: 7.0, 12: 6.0, 13: 5.0, 14: 4.0,
    15: 3.0, 16: 2.0, 17: 1.0, 18: 0.5, 19: 0.0,
    20: 0.0, 21: 0.0, 22: 0.0, 23: 0.0,
}

# Forecast uncertainty (stddev) — same unit as the station
FORECAST_STDDEV_F = 2.0
FORECAST_STDDEV_C = FORECAST_STDDEV_F / 1.8

# Output paths — kept separate from live spike's logs/
LOG_DIR = Path("logs_shadow")
SNAPSHOTS_JSONL = LOG_DIR / "snapshots.jsonl"
CANDIDATES_CSV = LOG_DIR / "candidates.csv"

# HTTP
HTTP_TIMEOUT_SECONDS = 15
USER_AGENT = "MeteoEdge-Shadow/0.1 (paper-trading, no execution)"

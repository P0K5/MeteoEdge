# MeteoEdge — MVP Spike (Observe-Only)

**Purpose:** Validate the core premise of the MeteoEdge strategy before investing in the full build.
**Mode:** Observe-only. No orders placed. No real money at risk.
**Duration:** 1 weekend to build, 5-10 trading days to run.
**Exit criterion:** ≥55% of flagged high-confidence candidates resolve profitably. If not, stop and revisit the spec.

---

## 1. What This Spike Does

Every 5 minutes, for one trading day at a time, across 5 US cities:

1. Pulls current METAR observations for KNYC, KORD, KMIA, KAUS, KLAX
2. Pulls current NWS hourly forecast for each station
3. Pulls all open Kalshi daily-high-temperature bracket markets
4. Computes the "physical envelope" of achievable daily highs given what's already observed
5. Computes a true probability for each bracket
6. Compares to Kalshi's ask price and flags any bracket with edge ≥ 3¢ and confidence ≥ 80%
7. Logs every flagged candidate to a CSV

At the end of each trading day, the settlement checker pulls the official NWS Daily Climate Report result and records whether each flagged candidate would have won.

After 5-10 days, you compute the hit rate. If it's above 55%, proceed to the full build. If not, the premise needs rethinking.

## 2. What This Spike Deliberately Skips

- **No order placement.** Observation only.
- **No database.** Everything in CSV and JSON files.
- **No services.** Run manually or via cron.
- **No dashboard.** Just log files and a report script.
- **No LLM sanity check.** The full spec adds this; the spike tests whether the rules engine alone produces a real edge.
- **No Kalshi authentication for trading endpoints.** Public market data only.
- **No fractional Kelly or position sizing.** Not placing trades.
- **Hardcoded config.** City list, thresholds, and polling interval are constants in the script.

If the spike fails, none of the above would have saved it. If it succeeds, you know the edge exists before building the infrastructure around it.

## 3. Prerequisites

- Python 3.11+
- A Kalshi account with API credentials for read access (Settings → API → create key pair)
- Internet access to `aviationweather.gov`, `api.weather.gov`, and `api.elections.kalshi.com`

Install:

```bash
pip install httpx pandas python-dateutil pytz astral cryptography
```

## 4. Project Layout

```
meteoedge-spike/
├── spike.py                  # main polling loop
├── settle.py                 # end-of-day settlement checker
├── report.py                 # computes hit rate from the CSVs
├── config.py                 # hardcoded config
├── kalshi_client.py          # thin Kalshi API wrapper
├── envelope.py               # envelope + edge math
├── logs/
│   ├── candidates.csv        # every flagged trade candidate
│   ├── snapshots.jsonl       # raw market + weather snapshots (for debugging)
│   └── settlements.csv       # actual outcomes once NWS publishes
└── keys/
    └── kalshi_private.pem    # chmod 600
```

## 5. The Code

### 5.1 `config.py`

```python
"""Hardcoded config for the spike. Promote to env vars in the full build."""
from pathlib import Path

# Kalshi
KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_API_KEY_ID = "YOUR_KEY_ID_HERE"  # read-only access is enough for the spike
KALSHI_PRIVATE_KEY_PATH = Path("keys/kalshi_private.pem")

# Stations: (METAR code, latitude, longitude, city name, NWS station for climate report)
STATIONS = [
    ("KNYC", 40.7789, -73.9692, "New York",    "KNYC"),
    ("KORD", 41.9742, -87.9073, "Chicago",     "KORD"),
    ("KMIA", 25.7953, -80.2901, "Miami",       "KMIA"),
    ("KAUS", 30.1944, -97.6700, "Austin",      "KAUS"),
    ("KLAX", 33.9425, -118.4081, "Los Angeles", "KLAX"),
]

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
```

### 5.2 `kalshi_client.py`

```python
"""Minimal Kalshi API wrapper. Read-only endpoints for the spike."""
import base64
import time
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from config import KALSHI_API_BASE, KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH, HTTP_TIMEOUT_SECONDS, USER_AGENT


def _load_private_key():
    with open(KALSHI_PRIVATE_KEY_PATH, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _sign_request(method: str, path: str) -> dict:
    """Kalshi RSA-PSS request signing. Returns headers."""
    private_key = _load_private_key()
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method.upper()}{path}".encode("utf-8")

    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )

    return {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }


def get_weather_events() -> list[dict]:
    """Return all open weather events. The spike filters to daily-high only."""
    path = "/events?status=open&category=Climate&with_nested_markets=true&limit=200"
    url = f"{KALSHI_API_BASE}{path}"
    headers = _sign_request("GET", path)
    r = httpx.get(url, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
    r.raise_for_status()
    return r.json().get("events", [])


def get_orderbook(ticker: str) -> dict:
    path = f"/markets/{ticker}/orderbook"
    url = f"{KALSHI_API_BASE}{path}"
    headers = _sign_request("GET", path)
    r = httpx.get(url, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
    r.raise_for_status()
    return r.json()
```

### 5.3 `envelope.py`

```python
"""Envelope and edge calculations. This is the heart of the strategy."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import erf, sqrt
from config import DEFAULT_CLIMB_LOOKUP, FORECAST_STDDEV_F


@dataclass
class WeatherState:
    station: str
    now_local: datetime
    sunset_local: datetime
    current_high_f: float
    current_high_time: datetime
    latest_temp_f: float
    latest_temp_time: datetime
    forecast_high_f: float | None


@dataclass
class Bracket:
    ticker: str
    low_f: float   # inclusive lower bound
    high_f: float  # inclusive upper bound
    yes_ask_cents: int
    yes_ask_size: int
    no_ask_cents: int
    no_ask_size: int


def p_normal_between(low: float, high: float, mean: float, stddev: float) -> float:
    """P(low <= X <= high) for X ~ N(mean, stddev^2)."""
    def cdf(x): return 0.5 * (1 + erf((x - mean) / (stddev * sqrt(2))))
    return max(0.0, min(1.0, cdf(high) - cdf(low)))


def expected_additional_rise(station: str, now_local: datetime) -> float:
    """Look up the p95 additional daily high rise possible from `now` to end of day."""
    hour = now_local.hour
    # Temps functionally cannot rise after sunset for daily-high purposes
    if hour >= 20:
        return 0.0
    return DEFAULT_CLIMB_LOOKUP.get(hour, 0.0)


def compute_envelope(state: WeatherState) -> tuple[float, float]:
    """Return (min_plausible_high, max_plausible_high) for the rest of the day."""
    min_high = state.current_high_f
    additional = expected_additional_rise(state.station, state.now_local)
    # Peak-from-here plus p95 rise gives the upper envelope
    max_high = max(
        state.current_high_f,
        state.latest_temp_f + additional,
    )
    return min_high, max_high


def true_probability_yes(bracket: Bracket, state: WeatherState) -> float:
    """
    Compute P(daily high falls in this bracket).
    Returns values in [0, 1].
    """
    lo, hi = bracket.low_f, bracket.high_f
    min_env, max_env = compute_envelope(state)

    # Bracket entirely below current daily high: impossible as the *daily high*
    # (the high already exceeded this range)
    if hi < state.current_high_f:
        return 0.0

    # Bracket entirely above max envelope: impossible
    if lo > max_env:
        return 0.0

    # Bracket fully contains current high and max envelope is within it: certain
    if lo <= state.current_high_f and hi >= max_env:
        return 1.0

    # Otherwise: bayesian estimate around the forecast high
    forecast_mean = state.forecast_high_f if state.forecast_high_f is not None else (
        (state.current_high_f + max_env) / 2
    )
    # Clip the forecast to the envelope — reality can't exceed the envelope
    forecast_mean = max(min_env, min(max_env, forecast_mean))
    return p_normal_between(lo, hi, forecast_mean, FORECAST_STDDEV_F)
```

### 5.4 `spike.py`

```python
"""Main polling loop. Run this script during trading hours."""
import csv
import json
import time
from datetime import datetime, timezone
from dateutil import parser as dtparse
import httpx
from astral import LocationInfo
from astral.sun import sun
import pytz

from config import (
    STATIONS, POLL_INTERVAL_SECONDS, MIN_EDGE_CENTS,
    MIN_CONFIDENCE_YES, MAX_CONFIDENCE_YES_FOR_NO,
    MIN_MINUTES_TO_SETTLEMENT, LOG_DIR, CANDIDATES_CSV,
    SNAPSHOTS_JSONL, HTTP_TIMEOUT_SECONDS, USER_AGENT,
)
from envelope import Bracket, WeatherState, true_probability_yes
from kalshi_client import get_weather_events


# --- Rough fee approximation for the spike.
# Full spec requires the actual fee schedule; this is good enough for candidate flagging.
def estimate_fee_cents(price_cents: int) -> float:
    """Rough: 0.07 * price * (1 - price) per contract, 1¢ floor, in cents."""
    p = price_cents / 100.0
    return max(1.0, 7.0 * p * (1 - p))


# --- Data fetchers
def fetch_metar(station: str) -> dict | None:
    url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json&hours=2"
    try:
        r = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT_SECONDS)
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        return data[0]  # most recent observation first
    except Exception as e:
        print(f"[metar] {station} error: {e}")
        return None


def fetch_all_metars_today(station: str) -> list[dict]:
    url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json&hours=24"
    try:
        r = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT_SECONDS)
        r.raise_for_status()
        return r.json() or []
    except Exception as e:
        print(f"[metar-day] {station} error: {e}")
        return []


def fetch_nws_forecast_high(lat: float, lon: float) -> float | None:
    """Use the points API to discover the hourly forecast URL, then pull today's high."""
    try:
        points_url = f"https://api.weather.gov/points/{lat},{lon}"
        r = httpx.get(points_url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT_SECONDS)
        r.raise_for_status()
        forecast_url = r.json()["properties"]["forecastHourly"]
        r2 = httpx.get(forecast_url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT_SECONDS)
        r2.raise_for_status()
        periods = r2.json()["properties"]["periods"]
        # Filter to periods ending today local
        highs = [p["temperature"] for p in periods[:18] if p.get("temperatureUnit") == "F"]
        return max(highs) if highs else None
    except Exception as e:
        print(f"[nws] {lat},{lon} error: {e}")
        return None


# --- Local time helpers
STATION_TZ = {
    "KNYC": "America/New_York",
    "KORD": "America/Chicago",
    "KMIA": "America/New_York",
    "KAUS": "America/Chicago",
    "KLAX": "America/Los_Angeles",
}


def now_local(station: str) -> datetime:
    return datetime.now(pytz.timezone(STATION_TZ[station]))


def sunset_local(station: str, lat: float, lon: float) -> datetime:
    local_tz = pytz.timezone(STATION_TZ[station])
    loc = LocationInfo(station, "US", STATION_TZ[station], lat, lon)
    s = sun(loc.observer, date=datetime.now(local_tz).date(), tzinfo=local_tz)
    return s["sunset"]


# --- Running daily high
def compute_daily_high(metars: list[dict], tz_name: str) -> tuple[float, datetime] | None:
    tz = pytz.timezone(tz_name)
    today_local_date = datetime.now(tz).date()
    best_temp, best_time = None, None
    for m in metars:
        temp_c = m.get("temp")
        obs_time_str = m.get("reportTime") or m.get("obsTime")
        if temp_c is None or obs_time_str is None:
            continue
        try:
            obs_time = dtparse.parse(obs_time_str)
            if obs_time.tzinfo is None:
                obs_time = obs_time.replace(tzinfo=timezone.utc)
            obs_local = obs_time.astimezone(tz)
            if obs_local.date() != today_local_date:
                continue
            temp_f = (float(temp_c) * 9 / 5) + 32
            if best_temp is None or temp_f > best_temp:
                best_temp, best_time = temp_f, obs_local
        except Exception:
            continue
    if best_temp is None:
        return None
    return best_temp, best_time


# --- Kalshi market parsing
def parse_bracket_from_market(m: dict) -> Bracket | None:
    """Parse a Kalshi daily-high-temp market into a Bracket. Schema varies; inspect first."""
    ticker = m.get("ticker")
    # The bracket bounds are encoded in the subtitle / rules_primary.
    # Kalshi typically uses phrasings like "between 82 and 84°" or ">=85°".
    sub = (m.get("subtitle") or m.get("yes_sub_title") or "").strip()
    # Heuristic range parser — refine once you see real data
    import re
    m_range = re.search(r"(\d{1,3})\s*(?:-|to|–)\s*(\d{1,3})", sub)
    m_gte = re.search(r"(?:>=|≥|or (?:above|more|higher))\s*(\d{1,3})", sub)
    m_lte = re.search(r"(?:<=|≤|or (?:below|less|lower))\s*(\d{1,3})", sub)

    if m_range:
        lo, hi = float(m_range.group(1)), float(m_range.group(2))
    elif m_gte:
        lo, hi = float(m_gte.group(1)), 200.0
    elif m_lte:
        lo, hi = -50.0, float(m_lte.group(1))
    else:
        return None

    return Bracket(
        ticker=ticker,
        low_f=lo,
        high_f=hi,
        yes_ask_cents=m.get("yes_ask") or 99,
        yes_ask_size=m.get("yes_ask_size") or 0,
        no_ask_cents=m.get("no_ask") or 99,
        no_ask_size=m.get("no_ask_size") or 0,
    )


def is_daily_high_market(event: dict, market: dict) -> tuple[bool, str | None]:
    """Identify daily-high-temperature markets and extract the station code."""
    title = (event.get("title") or "").lower()
    sub_title = (event.get("sub_title") or "").lower()
    if "high" not in title and "high" not in sub_title:
        return False, None
    for station, _, _, city, _ in STATIONS:
        if city.lower() in title or city.lower() in sub_title:
            return True, station
    return False, None


# --- Main loop
def append_candidate(row: dict):
    LOG_DIR.mkdir(exist_ok=True)
    new_file = not CANDIDATES_CSV.exists()
    with open(CANDIDATES_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new_file:
            w.writeheader()
        w.writerow(row)


def append_snapshot(snap: dict):
    LOG_DIR.mkdir(exist_ok=True)
    with open(SNAPSHOTS_JSONL, "a") as f:
        f.write(json.dumps(snap, default=str) + "\n")


def minutes_to_settlement(market: dict) -> float:
    close_str = market.get("close_time") or market.get("expiration_time")
    if not close_str:
        return 9999
    close = dtparse.parse(close_str)
    if close.tzinfo is None:
        close = close.replace(tzinfo=timezone.utc)
    return (close - datetime.now(timezone.utc)).total_seconds() / 60


def poll_once():
    ts = datetime.now(timezone.utc).isoformat()
    print(f"\n=== Poll at {ts} ===")

    # 1. Build weather state per station
    weather: dict[str, WeatherState] = {}
    for station, lat, lon, city, _ in STATIONS:
        metars = fetch_all_metars_today(station)
        latest = metars[0] if metars else None
        if not latest:
            continue
        high = compute_daily_high(metars, STATION_TZ[station])
        if not high:
            continue
        high_f, high_time = high
        try:
            latest_temp_f = (float(latest["temp"]) * 9 / 5) + 32
            latest_time = dtparse.parse(latest.get("reportTime") or latest["obsTime"])
            if latest_time.tzinfo is None:
                latest_time = latest_time.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        forecast_high = fetch_nws_forecast_high(lat, lon)
        weather[station] = WeatherState(
            station=station,
            now_local=now_local(station),
            sunset_local=sunset_local(station, lat, lon),
            current_high_f=high_f,
            current_high_time=high_time,
            latest_temp_f=latest_temp_f,
            latest_temp_time=latest_time,
            forecast_high_f=forecast_high,
        )
        print(f"[{station}] current_high={high_f:.1f}°F latest={latest_temp_f:.1f}°F forecast={forecast_high}")

    # 2. Pull Kalshi events and scan
    try:
        events = get_weather_events()
    except Exception as e:
        print(f"[kalshi] error: {e}")
        return

    for event in events:
        for market in event.get("markets", []):
            is_daily, station = is_daily_high_market(event, market)
            if not is_daily or station not in weather:
                continue
            mins_left = minutes_to_settlement(market)
            if mins_left < MIN_MINUTES_TO_SETTLEMENT:
                continue
            bracket = parse_bracket_from_market(market)
            if not bracket:
                continue

            state = weather[station]
            p_yes = true_probability_yes(bracket, state)
            fee = estimate_fee_cents(min(bracket.yes_ask_cents, bracket.no_ask_cents))

            ev_yes = p_yes * 100 - bracket.yes_ask_cents - fee
            ev_no = (1 - p_yes) * 100 - bracket.no_ask_cents - fee

            candidate = None
            if ev_yes >= MIN_EDGE_CENTS and p_yes >= MIN_CONFIDENCE_YES:
                candidate = ("YES", ev_yes, bracket.yes_ask_cents, p_yes)
            elif ev_no >= MIN_EDGE_CENTS and p_yes <= MAX_CONFIDENCE_YES_FOR_NO:
                candidate = ("NO", ev_no, bracket.no_ask_cents, 1 - p_yes)

            snap = {
                "ts": ts, "station": station, "ticker": bracket.ticker,
                "bracket_low": bracket.low_f, "bracket_high": bracket.high_f,
                "yes_ask": bracket.yes_ask_cents, "no_ask": bracket.no_ask_cents,
                "current_high": state.current_high_f, "latest_temp": state.latest_temp_f,
                "forecast_high": state.forecast_high_f, "p_yes": round(p_yes, 4),
                "ev_yes": round(ev_yes, 2), "ev_no": round(ev_no, 2),
                "minutes_to_settlement": round(mins_left, 1),
            }
            append_snapshot(snap)

            if candidate:
                side, edge, price, confidence = candidate
                row = {**snap, "flagged_side": side, "flagged_edge": round(edge, 2),
                       "flagged_price": price, "flagged_confidence": round(confidence, 4)}
                append_candidate(row)
                print(f"  ** FLAGGED {bracket.ticker} {side} @ {price}¢ edge={edge:.2f}¢ p={confidence:.2%}")


def main():
    print("MeteoEdge spike starting. Observe-only mode.")
    print("Ctrl-C to stop. Logs under ./logs/")
    while True:
        try:
            poll_once()
        except KeyboardInterrupt:
            print("\nStopping.")
            break
        except Exception as e:
            print(f"[loop] unhandled error: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
```

### 5.5 `settle.py`

```python
"""Run once a day after NWS publishes the Daily Climate Report (typically ~9am local next day)."""
import csv
from datetime import date, timedelta
import httpx
from config import STATIONS, LOG_DIR, CANDIDATES_CSV, SETTLEMENTS_CSV, USER_AGENT


def fetch_daily_climate_high(station: str, target_date: date) -> float | None:
    """
    Pull the NWS Daily Climate Report max temperature.
    The climate product is text; we parse the "MAXIMUM TEMPERATURE" line.
    In practice for the spike, cross-check against METAR 24h max as a fallback.
    """
    # Fallback: use the daily METAR max for the target date as "truth"
    url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json&hours=48"
    try:
        r = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        r.raise_for_status()
        data = r.json() or []
    except Exception as e:
        print(f"[settle] {station} error: {e}")
        return None

    import pytz
    from dateutil import parser as dtparse
    from spike import STATION_TZ
    tz = pytz.timezone(STATION_TZ[station])
    best = None
    for m in data:
        temp_c = m.get("temp")
        obs = m.get("reportTime") or m.get("obsTime")
        if temp_c is None or obs is None:
            continue
        try:
            t = dtparse.parse(obs)
            if t.tzinfo is None:
                t = t.replace(tzinfo=pytz.UTC)
            if t.astimezone(tz).date() != target_date:
                continue
            temp_f = (float(temp_c) * 9 / 5) + 32
            if best is None or temp_f > best:
                best = temp_f
        except Exception:
            continue
    return best


def settle_yesterday():
    """For each candidate from yesterday, record whether it would have won."""
    if not CANDIDATES_CSV.exists():
        print("No candidates to settle.")
        return

    yesterday = date.today() - timedelta(days=1)
    print(f"Settling for {yesterday}")

    # Pull truth per station once
    truth = {}
    for station, _, _, _, _ in STATIONS:
        h = fetch_daily_climate_high(station, yesterday)
        if h is not None:
            truth[station] = h
            print(f"  {station} daily high = {h:.1f}°F")

    # Read candidates, match on date, compute outcomes
    new_file = not SETTLEMENTS_CSV.exists()
    with open(CANDIDATES_CSV) as f_in, open(SETTLEMENTS_CSV, "a", newline="") as f_out:
        reader = csv.DictReader(f_in)
        writer = None
        for row in reader:
            ts = row["ts"][:10]  # YYYY-MM-DD
            if ts != yesterday.isoformat():
                continue
            station = row["station"]
            if station not in truth:
                continue
            actual = truth[station]
            lo, hi = float(row["bracket_low"]), float(row["bracket_high"])
            yes_won = lo <= actual <= hi
            won = yes_won if row["flagged_side"] == "YES" else not yes_won

            # P&L in cents per contract ignoring fees (approximation for the spike)
            if row["flagged_side"] == "YES":
                pnl = (100 - float(row["flagged_price"])) if yes_won else -float(row["flagged_price"])
            else:
                pnl = (100 - float(row["flagged_price"])) if (not yes_won) else -float(row["flagged_price"])

            out = {**row, "actual_high": actual, "yes_won": yes_won,
                   "candidate_won": won, "pnl_cents": round(pnl, 2)}
            if writer is None:
                writer = csv.DictWriter(f_out, fieldnames=list(out.keys()))
                if new_file:
                    writer.writeheader()
            writer.writerow(out)

    print(f"Wrote settlements to {SETTLEMENTS_CSV}")


if __name__ == "__main__":
    settle_yesterday()
```

### 5.6 `report.py`

```python
"""Summarize hit rate and P&L after N days. Run any time after settlements accumulate."""
import csv
import statistics
from config import SETTLEMENTS_CSV


def main():
    if not SETTLEMENTS_CSV.exists():
        print("No settlements yet.")
        return

    rows = list(csv.DictReader(open(SETTLEMENTS_CSV)))
    # Deduplicate: if the same ticker was flagged multiple times, take first flag of the day
    seen = set()
    unique = []
    for r in rows:
        key = (r["ts"][:10], r["ticker"], r["flagged_side"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)

    n = len(unique)
    if n == 0:
        print("Zero unique flagged candidates.")
        return

    wins = sum(1 for r in unique if r["candidate_won"] in ("True", "true", True))
    hit_rate = wins / n
    pnls = [float(r["pnl_cents"]) for r in unique]
    total_pnl = sum(pnls)
    avg_pnl = statistics.mean(pnls)
    stdev_pnl = statistics.pstdev(pnls) if n > 1 else 0.0

    by_station = {}
    for r in unique:
        by_station.setdefault(r["station"], []).append(r)

    print(f"\n=== MeteoEdge Spike Report ===")
    print(f"Unique flagged candidates: {n}")
    print(f"Wins: {wins}  Hit rate: {hit_rate:.2%}")
    print(f"Total P&L (cents, pre-fee): {total_pnl:+.1f}")
    print(f"Avg P&L per trade: {avg_pnl:+.2f}¢  stdev: {stdev_pnl:.2f}¢")
    print(f"\nBy station:")
    for station, items in sorted(by_station.items()):
        w = sum(1 for r in items if r["candidate_won"] in ("True", "true", True))
        print(f"  {station}: {len(items)} flagged, {w} won ({w/len(items):.1%})")

    print(f"\n{'='*40}")
    if hit_rate >= 0.55 and n >= 30:
        print(f"GREEN LIGHT: hit rate {hit_rate:.2%} >= 55% on {n} candidates. Proceed to full build.")
    elif hit_rate >= 0.55:
        print(f"PROVISIONAL GREEN: hit rate OK but n={n} is below 30. Run more days.")
    else:
        print(f"RED LIGHT: hit rate {hit_rate:.2%} < 55%. Do not proceed. Revisit spec.")


if __name__ == "__main__":
    main()
```

## 6. How to Run

### Day 0 — one-time setup
```bash
cd meteoedge-spike
python -m venv .venv && source .venv/bin/activate
pip install httpx pandas python-dateutil pytz astral cryptography
mkdir -p logs keys
chmod 700 keys
# Drop your Kalshi private key into keys/kalshi_private.pem
chmod 600 keys/kalshi_private.pem
# Edit config.py with your key ID
```

### Each trading day
```bash
# Start the poller at market open, let it run until close
# US weather markets are active during local daytime hours.
# Portugal is UTC+0/+1, US Eastern is UTC-5/-4, so US noon = 17:00-18:00 local for you.
# Practically: start by early US morning (late Portugal morning), kill by late US evening.
python spike.py
```

### The morning after
```bash
python settle.py
python report.py
```

## 7. Decision Criteria

After 5-10 trading days of clean data, run `report.py`:

| Outcome | Decision |
|---|---|
| n ≥ 30 candidates AND hit rate ≥ 55% AND total P&L positive | **Green light.** Proceed to the full build per the spec. |
| n ≥ 30 AND hit rate 50-55% | **Yellow light.** Extend to 15-20 days. Look for per-station patterns — maybe 2 cities have real edge and 3 don't. |
| n ≥ 30 AND hit rate < 50% | **Red light.** Core premise is wrong. Do not build. Investigate: is the envelope too aggressive? Is the fee approximation off? Are you flagging too many certainty-near-1 trades where the remaining edge is fee-eaten? |
| n < 30 after 10 days | Flagging is too restrictive. Either lower `MIN_EDGE_CENTS` to 2 or lower `MIN_CONFIDENCE_YES` to 0.75 and try another 5 days. If still < 30, the thresholds used in the full build will also starve — revisit the strategy. |

## 8. Known Spike Limitations

These are accepted for the spike and addressed in the full spec:

- Fee estimate is a rough formula, not the actual Kalshi schedule
- Settlement uses METAR 24h max as a proxy for the NWS Daily Climate Report; occasionally diverges by 1°F
- `parse_bracket_from_market` is heuristic and may miss unusual bracket phrasings — inspect flagged markets manually
- No handling of DST transitions, holidays, or Kalshi market-specific resolution quirks
- No retry/backoff on HTTP errors — if a poll fails, that window is skipped
- Single-threaded; if Kalshi is slow, polls can back up (acceptable for 5-minute cadence)
- The historical climb lookup is hand-seeded; the full build derives it from 5 years of real METAR

## 9. What to Hand Off to the Full Build

After the spike concludes, package these artifacts for the agent team:

1. `logs/candidates.csv` — every flagged trade over the spike period
2. `logs/settlements.csv` — actual outcomes
3. `logs/snapshots.jsonl` — full market + weather state at each poll (gold for backtest calibration)
4. A short written retro: which stations/brackets/times worked, which didn't, what surprised you

This becomes the seed dataset for Epic 7 (Backtest Harness) and the initial calibration of the envelope's climb-rate lookup table.

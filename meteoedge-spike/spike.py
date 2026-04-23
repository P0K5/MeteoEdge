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
from kalshi_client import get_weather_events, get_orderbook


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
def parse_bracket_from_market(m: dict) -> "Bracket | None":
    """Parse a Kalshi daily-high-temp market into a Bracket. Schema varies; inspect first."""
    import re
    ticker = m.get("ticker")
    if not ticker:
        return None

    # Bracket bounds are encoded in the subtitle. Kalshi uses phrasings like
    # "Between 82 and 84", "85 or above", "79 or below", ">=85", "<=79".
    sub = (m.get("subtitle") or m.get("yes_sub_title") or "").strip()

    # Range: "82-84", "82 to 84", "82–84", "82 and 84", "82° to 84°", "between 82 and 84"
    m_range = re.search(r"(\d{1,3})\s*°?\s*(?:-|to|–|and)\s*(\d{1,3})", sub)
    # GTE: ">=85", "≥85"
    m_gte_explicit = re.search(r"(?:>=|≥)\s*(\d{1,3})", sub)
    # GTE: "85 or above/more/higher", "85 and above"
    m_gte_or = re.search(r"(\d{1,3})(?:\s*°F?)?\s+(?:or|and)\s+(?:above|more|higher)", sub, re.IGNORECASE)
    # GTE: "above 85"
    m_gte_bare = re.search(r"\babove\s+(\d{1,3})", sub, re.IGNORECASE)
    # LTE: "<=79", "≤79"
    m_lte_explicit = re.search(r"(?:<=|≤)\s*(\d{1,3})", sub)
    # LTE: "79 or below/less/lower", "79 and below"
    m_lte_or = re.search(r"(\d{1,3})(?:\s*°F?)?\s+(?:or|and)\s+(?:below|less|lower)", sub, re.IGNORECASE)
    # LTE: "below 79"
    m_lte_bare = re.search(r"\bbelow\s+(\d{1,3})", sub, re.IGNORECASE)

    if m_range:
        lo, hi = float(m_range.group(1)), float(m_range.group(2))
    elif m_gte_explicit:
        lo, hi = float(m_gte_explicit.group(1)), 200.0
    elif m_gte_or:
        lo, hi = float(m_gte_or.group(1)), 200.0
    elif m_gte_bare:
        lo, hi = float(m_gte_bare.group(1)), 200.0
    elif m_lte_explicit:
        lo, hi = -50.0, float(m_lte_explicit.group(1))
    elif m_lte_or:
        lo, hi = -50.0, float(m_lte_or.group(1))
    elif m_lte_bare:
        lo, hi = -50.0, float(m_lte_bare.group(1))
    else:
        if sub:
            print(f"[parse] unparseable subtitle for {ticker}: {repr(sub[:80])}")
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


CITY_ALIASES: dict[str, list[str]] = {
    "New York":    ["new york", "nyc"],
    "Chicago":     ["chicago"],
    "Miami":       ["miami"],
    "Austin":      ["austin"],
    "Los Angeles": ["los angeles", "la"],
}

def is_daily_high_market(event: dict, market: dict) -> tuple[bool, "str | None"]:
    """Identify daily-high-temperature markets and extract the station code."""
    title = (event.get("title") or "").lower()
    sub_title = (event.get("sub_title") or "").lower()
    if "high" not in title and "high" not in sub_title:
        return False, None
    for station, _, _, city, _ in STATIONS:
        for alias in CITY_ALIASES.get(city, [city.lower()]):
            if alias in title or alias in sub_title:
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
    try:
        close = dtparse.parse(close_str)
        if close.tzinfo is None:
            close = close.replace(tzinfo=timezone.utc)
        return (close - datetime.now(timezone.utc)).total_seconds() / 60
    except Exception:
        return 9999


def poll_once():
    ts = datetime.now(timezone.utc).isoformat()
    print(f"\n=== Poll at {ts} ===")

    # 1. Build weather state per station
    weather: dict[str, WeatherState] = {}
    for station, lat, lon, city, _ in STATIONS:
        metars = fetch_all_metars_today(station)
        latest = metars[0] if metars else None
        if not latest:
            print(f"[{station}] no METAR data available, skipping")
            continue
        high = compute_daily_high(metars, STATION_TZ[station])
        if not high:
            print(f"[{station}] could not compute daily high, skipping")
            continue
        high_f, high_time = high
        try:
            latest_temp_c = latest.get("temp")
            if latest_temp_c is None:
                print(f"[{station}] latest METAR missing temp field, skipping")
                continue
            latest_temp_f = (float(latest_temp_c) * 9 / 5) + 32
            obs_time_str = latest.get("reportTime") or latest.get("obsTime")
            if obs_time_str is None:
                print(f"[{station}] latest METAR missing time field, skipping")
                continue
            latest_time = dtparse.parse(obs_time_str)
            if latest_time.tzinfo is None:
                latest_time = latest_time.replace(tzinfo=timezone.utc)
        except Exception as e:
            print(f"[{station}] METAR parse error: {e}, skipping")
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
        print(f"[kalshi] error fetching events: {e}. Skipping this poll.")
        return

    print(f"[kalshi] {len(events)} temperature events fetched")
    n_markets = n_daily = n_brackets = n_flagged = 0
    for event in events:
        for market in event.get("markets", []):
            n_markets += 1
            try:
                is_daily, station = is_daily_high_market(event, market)
                if not is_daily or station not in weather:
                    continue
                n_daily += 1
                mins_left = minutes_to_settlement(market)
                if mins_left < MIN_MINUTES_TO_SETTLEMENT:
                    continue
                bracket = parse_bracket_from_market(market)
                if not bracket:
                    continue
                n_brackets += 1

                # Enrich bracket with real-time prices from the orderbook.
                # yes_dollars/no_dollars are BID levels (sorted ascending, [-1] = best bid).
                # In a binary market: YES ask = 100 - best NO bid, NO ask = 100 - best YES bid.
                try:
                    ob = get_orderbook(bracket.ticker)
                    book = ob.get("orderbook_fp", {})
                    yes_lvls = book.get("yes_dollars") or []
                    no_lvls = book.get("no_dollars") or []
                    if no_lvls:
                        bracket.yes_ask_cents = 100 - round(float(no_lvls[-1][0]) * 100)
                        bracket.yes_ask_size = round(float(no_lvls[-1][1]))
                    if yes_lvls:
                        bracket.no_ask_cents = 100 - round(float(yes_lvls[-1][0]) * 100)
                        bracket.no_ask_size = round(float(yes_lvls[-1][1]))
                except Exception as e:
                    print(f"[orderbook] {bracket.ticker}: {e}")

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
                    n_flagged += 1
                    side, edge, price, confidence = candidate
                    row = {**snap, "flagged_side": side, "flagged_edge": round(edge, 2),
                           "flagged_price": price, "flagged_confidence": round(confidence, 4)}
                    append_candidate(row)
                    print(f"  ** FLAGGED {bracket.ticker} {side} @ {price}¢ edge={edge:.2f}¢ p={confidence:.2%}")

            except Exception as e:
                ticker = market.get("ticker", "unknown")
                print(f"[market] error processing {ticker}: {e}, skipping")
                continue
    print(f"[scan] {n_markets} markets total, {n_daily} daily-high matched, {n_brackets} brackets parsed, {n_flagged} flagged")


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

"""
Shadow loop — paper trading only. Identical structure to the live spike,
with two deliberate omissions:

  1. NO order execution path. There is no Polymarket trading client,
     no API keys, no CLOB write calls. The process literally cannot
     place an order even if a code bug tried to.
  2. NO live-CLOB orderbook enrichment. Snapshots use Gamma's
     `outcomePrices` directly. Keeps polls fast across 50 cities.

Outputs one row per (city × bracket × poll) to logs_shadow/snapshots.jsonl
with predicted probability, ask prices, and notional EV. Settlement is
reconciled offline via a future settlement reconciler (post-MVP).
"""
import csv
import json
import re
import time
from datetime import datetime, timezone

import httpx
import pytz
from astral import LocationInfo
from astral.sun import sun
from dateutil import parser as dtparse

from config import (
    CANDIDATES_CSV,
    HTTP_TIMEOUT_SECONDS,
    LOG_DIR,
    MAX_CONFIDENCE_YES_FOR_NO,
    MIN_CONFIDENCE_YES,
    MIN_EDGE_CENTS,
    MIN_MINUTES_TO_SETTLEMENT,
    POLL_INTERVAL_SECONDS,
    SNAPSHOTS_JSONL,
    STATIONS,
    USER_AGENT,
)
from envelope import Bracket, WeatherState, true_probability_yes
from forecast import fetch_forecast_high
from polymarket_client import get_weather_markets


# --- Station lookup by Polymarket city name (lowercase)
# Tuple: (station_code, lat, lon, unit, timezone, region, forecast_source)
CITY_TO_META: dict[str, tuple[str, float, float, str, str, str, str]] = {
    city.lower(): (icao, lat, lon, unit, tz, region, src)
    for (icao, lat, lon, city, _resolution, unit, tz, region, src) in STATIONS
}


# --- METAR fetcher (works globally for any ICAO code)
def fetch_all_metars_today(station: str) -> list[dict]:
    url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json&hours=24"
    try:
        r = httpx.get(url, headers={"User-Agent": USER_AGENT},
                      timeout=HTTP_TIMEOUT_SECONDS)
        r.raise_for_status()
        return r.json() or []
    except Exception as e:
        print(f"[metar] {station} error: {e}")
        return []


# --- Time helpers
def now_local(tz_name: str) -> datetime:
    return datetime.now(pytz.timezone(tz_name))


def sunset_local(station: str, lat: float, lon: float, tz_name: str) -> datetime:
    tz = pytz.timezone(tz_name)
    loc = LocationInfo(station, "?", tz_name, lat, lon)
    s = sun(loc.observer, date=datetime.now(tz).date(), tzinfo=tz)
    return s["sunset"]


# --- Daily-high computation in station unit
def compute_daily_high(
    metars: list[dict], tz_name: str, unit: str
) -> tuple[float, datetime] | None:
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
            temp_native = float(temp_c) if unit == "C" else (float(temp_c) * 9 / 5 + 32)
            if best_temp is None or temp_native > best_temp:
                best_temp, best_time = temp_native, obs_local
        except Exception:
            continue
    if best_temp is None:
        return None
    return best_temp, best_time


# --- Polymarket market parsing
def identify_city(market: dict) -> tuple[bool, str | None]:
    """
    Match 'highest temperature in <city>' against the configured city list.
    Lowest-temperature markets are skipped — the envelope model is built
    around the daily high. They'll be a separate feature.
    """
    q = (market.get("question") or "").lower()
    if "highest temperature in" not in q:
        return False, None
    for city_lower in CITY_TO_META:
        if f"highest temperature in {city_lower}" in q:
            return True, city_lower
    return False, None


def _decode_json_string(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


# Bracket label patterns — match both °F and °C variants
_LABEL_LTE = re.compile(
    r"(\d{1,3})\s*°?\s*([FC])?\s+or\s+(?:below|less|lower|under)",
    re.IGNORECASE,
)
_LABEL_GTE = re.compile(
    r"(\d{1,3})\s*°?\s*([FC])?\s+or\s+(?:above|more|higher|over)",
    re.IGNORECASE,
)
_LABEL_BETWEEN = re.compile(
    r"between\s+(\d{1,3})\s*(?:and|to|-|–)\s*(\d{1,3})\s*°?\s*([FC])?",
    re.IGNORECASE,
)
_LABEL_RANGE_DASH = re.compile(
    r"(\d{1,3})\s*[-–]\s*(\d{1,3})\s*°?\s*([FC])",
    re.IGNORECASE,
)


def _floor_for_unit(unit: str) -> float:
    return -50.0 if unit == "F" else -50.0


def _ceiling_for_unit(unit: str) -> float:
    return 200.0 if unit == "F" else 100.0


def parse_bracket_from_market(market: dict, unit: str) -> Bracket | None:
    """
    Parse a Polymarket bracket using `groupItemTitle`. `unit` is the
    station's native unit — used as a fallback when the label doesn't
    include an explicit ° symbol, and validated against the label when
    it does (mismatched units are rejected).
    """
    condition_id = (
        market.get("conditionId") or market.get("condition_id") or market.get("id")
    )
    if not condition_id:
        return None

    label = (market.get("groupItemTitle") or market.get("question") or "").strip()
    if not label:
        return None

    def _resolve_unit(label_unit: str | None) -> str | None:
        if not label_unit:
            return unit
        lu = label_unit.upper()
        if lu != unit:
            return None
        return lu

    if (m := _LABEL_LTE.search(label)):
        u = _resolve_unit(m.group(2))
        if u is None: return None
        lo, hi = _floor_for_unit(u), float(m.group(1))
    elif (m := _LABEL_GTE.search(label)):
        u = _resolve_unit(m.group(2))
        if u is None: return None
        lo, hi = float(m.group(1)), _ceiling_for_unit(u)
    elif (m := _LABEL_BETWEEN.search(label)):
        u = _resolve_unit(m.group(3))
        if u is None: return None
        lo, hi = float(m.group(1)), float(m.group(2))
    elif (m := _LABEL_RANGE_DASH.search(label)):
        u = _resolve_unit(m.group(3))
        if u is None: return None
        lo, hi = float(m.group(1)), float(m.group(2))
    else:
        return None

    outcomes = _decode_json_string(market.get("outcomes"), [])
    prices = _decode_json_string(market.get("outcomePrices"), [])
    token_ids = _decode_json_string(market.get("clobTokenIds"), [])

    yes_idx = next((i for i, o in enumerate(outcomes) if str(o).lower() == "yes"), 0)
    no_idx = next((i for i, o in enumerate(outcomes) if str(o).lower() == "no"), 1)

    def _safe_price(idx: int) -> float:
        try:
            return float(prices[idx])
        except (IndexError, TypeError, ValueError):
            return 0.5

    yes_price = _safe_price(yes_idx)
    no_price = _safe_price(no_idx)
    yes_token = token_ids[yes_idx] if len(token_ids) > yes_idx else None
    no_token = token_ids[no_idx] if len(token_ids) > no_idx else None

    return Bracket(
        ticker=condition_id,
        unit=u,
        low=lo,
        high=hi,
        yes_ask_cents=max(1, min(99, round(yes_price * 100))),
        yes_ask_size=0,
        no_ask_cents=max(1, min(99, round(no_price * 100))),
        no_ask_size=0,
        yes_token_id=yes_token,
        no_token_id=no_token,
    )


# --- Fee estimate (same heuristic as live spike, for parity)
def estimate_fee_cents(price_cents: int) -> float:
    p = price_cents / 100.0
    return max(1.0, 7.0 * p * (1 - p))


# --- Output helpers
def append_snapshot(snap: dict) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    with open(SNAPSHOTS_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(snap, default=str, ensure_ascii=False) + "\n")


def append_candidate(row: dict) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    new_file = not CANDIDATES_CSV.exists()
    with open(CANDIDATES_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new_file:
            w.writeheader()
        w.writerow(row)


def minutes_to_settlement(market: dict) -> float:
    close_str = (
        market.get("endDate") or market.get("end_date_iso")
        or market.get("endDateIso") or market.get("close_time")
    )
    if not close_str:
        return 9999.0
    try:
        close = dtparse.parse(str(close_str))
        if close.tzinfo is None:
            close = close.replace(tzinfo=timezone.utc)
        return (close - datetime.now(timezone.utc)).total_seconds() / 60
    except Exception:
        return 9999.0


# --- Main poll
def poll_once() -> None:
    ts = datetime.now(timezone.utc).isoformat()
    print(f"\n=== Shadow poll at {ts} ===")

    weather: dict[str, WeatherState] = {}
    station_meta: dict[str, tuple[str, float, float, str, str, str, str]] = {}

    # 1. Build weather state per configured station
    for city_lower, meta in CITY_TO_META.items():
        icao, lat, lon, unit, tz_name, region, src = meta
        station_meta[city_lower] = meta

        metars = fetch_all_metars_today(icao)
        if not metars:
            print(f"[{icao}] no METAR data, skipping ({region})")
            continue

        latest = metars[0]
        high = compute_daily_high(metars, tz_name, unit)
        if not high:
            print(f"[{icao}] cannot compute daily high, skipping ({region})")
            continue
        high_native, high_time = high

        try:
            latest_temp_c = latest.get("temp")
            if latest_temp_c is None:
                continue
            latest_temp_native = (
                float(latest_temp_c) if unit == "C"
                else float(latest_temp_c) * 9 / 5 + 32
            )
            obs_time_str = latest.get("reportTime") or latest.get("obsTime")
            if obs_time_str is None:
                continue
            latest_time = dtparse.parse(obs_time_str)
            if latest_time.tzinfo is None:
                latest_time = latest_time.replace(tzinfo=timezone.utc)
        except Exception as e:
            print(f"[{icao}] METAR parse error: {e}, skipping")
            continue

        forecast = fetch_forecast_high(lat, lon, unit, src, tz_name)

        weather[city_lower] = WeatherState(
            station=icao,
            unit=unit,
            now_local=now_local(tz_name),
            sunset_local=sunset_local(icao, lat, lon, tz_name),
            current_high=high_native,
            current_high_time=high_time,
            latest_temp=latest_temp_native,
            latest_temp_time=latest_time,
            forecast_high=forecast,
        )
        unit_sym = f"°{unit}"
        fc_str = f"{forecast:.1f}{unit_sym}" if forecast is not None else "n/a"
        print(
            f"[{region}/{icao}] high={high_native:.1f}{unit_sym} "
            f"latest={latest_temp_native:.1f}{unit_sym} forecast={fc_str}"
        )

    # 2. Fetch Polymarket weather markets
    try:
        all_markets = get_weather_markets()
    except Exception as e:
        print(f"[polymarket] fetch error: {e}, skipping this poll")
        return

    print(f"[polymarket] {len(all_markets)} weather-tagged markets fetched")
    n_temp = n_brackets = n_flagged = 0

    for market in all_markets:
        try:
            matched, city_lower = identify_city(market)
            if not matched or city_lower not in weather:
                continue
            n_temp += 1

            mins_left = minutes_to_settlement(market)
            if mins_left < MIN_MINUTES_TO_SETTLEMENT:
                continue

            state = weather[city_lower]
            bracket = parse_bracket_from_market(market, state.unit)
            if not bracket:
                continue
            n_brackets += 1

            p_yes = true_probability_yes(bracket, state)
            fee = estimate_fee_cents(min(bracket.yes_ask_cents, bracket.no_ask_cents))
            ev_yes = p_yes * 100 - bracket.yes_ask_cents - fee
            ev_no = (1 - p_yes) * 100 - bracket.no_ask_cents - fee

            candidate = None
            if ev_yes >= MIN_EDGE_CENTS and p_yes >= MIN_CONFIDENCE_YES:
                candidate = ("YES", ev_yes, bracket.yes_ask_cents, p_yes)
            elif ev_no >= MIN_EDGE_CENTS and p_yes <= MAX_CONFIDENCE_YES_FOR_NO:
                candidate = ("NO", ev_no, bracket.no_ask_cents, 1 - p_yes)

            icao, lat, lon, unit, tz_name, region, src = station_meta[city_lower]
            snap = {
                "ts": ts,
                "region": region,
                "city": city_lower,
                "station": icao,
                "unit": unit,
                "ticker": bracket.ticker,
                "bracket_low": bracket.low,
                "bracket_high": bracket.high,
                "yes_ask": bracket.yes_ask_cents,
                "no_ask": bracket.no_ask_cents,
                "current_high": state.current_high,
                "latest_temp": state.latest_temp,
                "forecast_high": state.forecast_high,
                "p_yes": round(p_yes, 4),
                "ev_yes": round(ev_yes, 2),
                "ev_no": round(ev_no, 2),
                "minutes_to_settlement": round(mins_left, 1),
            }
            append_snapshot(snap)

            if candidate:
                n_flagged += 1
                side, edge, price, conf = candidate
                row = {
                    **snap,
                    "flagged_side": side,
                    "flagged_edge": round(edge, 2),
                    "flagged_price": price,
                    "flagged_confidence": round(conf, 4),
                }
                append_candidate(row)
                print(
                    f"  ** [{region}] FLAGGED {bracket.ticker[:14]}… "
                    f"{side} @ {price}¢ edge={edge:.2f}¢ p={conf:.2%}"
                )

        except Exception as e:
            mid = (market.get("conditionId") or market.get("id") or "unknown")[:14]
            print(f"[market] {mid}…: {e}, skipping")
            continue

    print(
        f"[scan] {len(all_markets)} fetched, {n_temp} city-matched, "
        f"{n_brackets} parsed, {n_flagged} flagged"
    )


def main() -> None:
    print("MeteoEdge-Polymarket SHADOW loop starting. No execution path.")
    print(f"Tracking {len(STATIONS)} cities. Logs under ./logs_shadow/")
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

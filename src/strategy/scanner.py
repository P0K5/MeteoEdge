"""Market scanner — identifies mispriced Polymarket temperature brackets.

For each market fetched from Polymarket, this module:
1. Checks if it's a 'highest temperature in <city>' market for a configured station
2. Parses the bracket (e.g., '82-84°F') into a Bracket object
3. Computes true P(yes) using the weather envelope model
4. Computes expected value for YES and NO sides
5. Returns Candidate objects for any market with edge >= MIN_EDGE_CENTS
"""
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from dateutil import parser as dtparse

from src.config import (
    STATIONS, MIN_EDGE_CENTS, MAX_EDGE_CENTS, MIN_PRICE_CENTS, MIN_CONFIDENCE_YES,
    MAX_CONFIDENCE_YES_FOR_NO, ENABLE_YES_TRADES, MIN_MINUTES_TO_SETTLEMENT,
    ENABLE_CLOB_ENRICHMENT,
)
from src.model.envelope import Bracket, WeatherState, true_probability_yes, compute_envelope
from src.data.polymarket import get_orderbook
from src.data.taf_disruption import check_taf_disruption
from src.strategy.fee import estimate_fee_cents


# Map Polymarket city name (lowercase) → METAR station code
POLYMARKET_CITY_TO_STATION: dict[str, str] = {
    city.lower(): station for station, _, _, city, *_ in STATIONS
}

# Reverse map: METAR station code → Polymarket city name
STATION_TO_CITY: dict[str, str] = {
    station: city for station, _, _, city, *_ in STATIONS
}


def is_highest_temp_market(market: dict) -> tuple[bool, "str | None"]:
    """
    Return (True, station_code) if this is a 'Will the highest temperature in
    <city> be <bracket> on <date>?' market for one of our configured cities.
    Lowest-temperature markets are explicitly skipped — the envelope model
    is built around the daily high, not the daily low.
    """
    q = (market.get("question") or "").lower()
    if "highest temperature in" not in q:
        return False, None
    for city, station in POLYMARKET_CITY_TO_STATION.items():
        if f"highest temperature in {city}" in q:
            return True, station
    return False, None


def _decode_json_string(value, default):
    """Polymarket Gamma API returns several fields as JSON-encoded strings."""
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


# Bracket label patterns. Each regex captures the optional unit character [FC] so
# we can detect °C labels and convert bracket boundaries to °F before they enter
# the envelope model (which is entirely °F-native). US markets label in °F;
# all non-US Polymarket markets label in °C.
_LABEL_LTE = re.compile(
    r"(\d{1,3})\s*°?\s*([FC])?\s+or\s+(?:below|less|lower|under)", re.IGNORECASE
)
_LABEL_GTE = re.compile(
    r"(\d{1,3})\s*°?\s*([FC])?\s+or\s+(?:above|more|higher|over)", re.IGNORECASE
)
_LABEL_BETWEEN = re.compile(
    r"between\s+(\d{1,3})\s*(?:and|to|-|–)\s*(\d{1,3})\s*°?\s*([FC])?", re.IGNORECASE
)
# Range-dash requires an explicit unit character to avoid ambiguity with bare integers.
_LABEL_RANGE_DASH = re.compile(r"(\d{1,3})\s*[-–]\s*(\d{1,3})\s*°\s*([FC])", re.IGNORECASE)
# Bare single-value: "21°C" or "82°F". Polymarket Asian markets list each possible
# integer daily high as its own market option. Treat as 1-unit-wide bracket [N, N+1).
_LABEL_EXACT = re.compile(r"^\s*(\d{1,3})\s*°\s*([FC])\s*$", re.IGNORECASE)


def _to_f(val: float, unit_char: "str | None") -> float:
    """Convert val to °F if unit_char is 'C', otherwise return as-is."""
    if unit_char and unit_char.upper() == "C":
        return val * 9 / 5 + 32
    return val


def parse_bracket_from_market(market: dict) -> "Bracket | None":
    """
    Parse a Polymarket bracket market into a Bracket using the `groupItemTitle`
    field (e.g. "55°F or below", "between 28-30°C", "92°F or above"). Falls
    back to the question text when groupItemTitle is missing.
    All bracket boundaries are normalised to °F before storage — the envelope
    model is entirely °F-native.
    """
    condition_id = market.get("conditionId") or market.get("condition_id") or market.get("id")
    if not condition_id:
        return None

    label = (market.get("groupItemTitle") or market.get("question") or "").strip()
    if not label:
        return None

    if (m := _LABEL_LTE.search(label)):
        unit = m.group(2)
        lo, hi = -58.0, _to_f(float(m.group(1)), unit)
    elif (m := _LABEL_GTE.search(label)):
        unit = m.group(2)
        lo, hi = _to_f(float(m.group(1)), unit), 392.0
    elif (m := _LABEL_BETWEEN.search(label)):
        unit = m.group(3)
        lo, hi = _to_f(float(m.group(1)), unit), _to_f(float(m.group(2)), unit)
    elif (m := _LABEL_RANGE_DASH.search(label)):
        unit = m.group(3)
        lo, hi = _to_f(float(m.group(1)), unit), _to_f(float(m.group(2)), unit)
    elif (m := _LABEL_EXACT.search(label)):
        unit = m.group(2)
        val = float(m.group(1))
        lo, hi = _to_f(val, unit), _to_f(val + 1, unit)
    else:
        print(f"[parse] unparseable label: {label[:60]!r}")
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
        low_f=lo,
        high_f=hi,
        yes_ask_cents=max(1, min(99, round(yes_price * 100))),
        yes_ask_size=0,
        no_ask_cents=max(1, min(99, round(no_price * 100))),
        no_ask_size=0,
        yes_token_id=yes_token,
        no_token_id=no_token,
    )


def minutes_to_settlement(market: dict) -> float:
    close_str = (
        market.get("endDate")
        or market.get("end_date_iso")
        or market.get("endDateIso")
        or market.get("close_time")
    )
    if not close_str:
        return 9999
    try:
        close = dtparse.parse(str(close_str))
        if close.tzinfo is None:
            close = close.replace(tzinfo=timezone.utc)
        return (close - datetime.now(timezone.utc)).total_seconds() / 60
    except Exception:
        return 9999


def _enrich_from_clob(bracket: Bracket) -> None:
    """Overwrite bracket ask prices with live CLOB data.

    Only called when ENABLE_CLOB_ENRICHMENT=True. Adds ~2 API calls per bracket.
    """
    if not ENABLE_CLOB_ENRICHMENT:
        return

    if bracket.yes_token_id:
        try:
            ob = get_orderbook(bracket.yes_token_id)
            asks = ob.get("asks") or []
            if asks:
                best = min(float(a["price"]) for a in asks)
                bracket.yes_ask_cents = max(1, min(99, round(best * 100)))
                bracket.yes_ask_size = sum(max(0, int(float(a["size"]))) for a in asks[:3])
        except Exception as e:
            print(f"[clob] YES {bracket.ticker[:14]}…: {e}")

    if bracket.no_token_id:
        try:
            ob = get_orderbook(bracket.no_token_id)
            asks = ob.get("asks") or []
            if asks:
                best = min(float(a["price"]) for a in asks)
                bracket.no_ask_cents = max(1, min(99, round(best * 100)))
                bracket.no_ask_size = sum(max(0, int(float(a["size"]))) for a in asks[:3])
        except Exception as e:
            print(f"[clob] NO {bracket.ticker[:14]}…: {e}")


@dataclass
class Candidate:
    """A market where our model sees a tradeable edge."""
    station: str
    bracket: Bracket
    side: str           # "YES" or "NO"
    edge_cents: float   # expected value in cents (must be >= MIN_EDGE_CENTS)
    price_cents: int    # ask price in cents for the flagged side
    confidence: float   # p_yes for YES side, 1-p_yes for NO side
    p_yes: float        # raw model probability (always P(YES))
    ev_yes: float       # expected value of buying YES
    ev_no: float        # expected value of buying NO
    minutes_to_settlement: float
    market: dict        # raw market dict (for logging, do not mutate)
    taf_disruption: bool = field(default=False)  # TEMPO/PROB TS/SH/FG overlap


def scan_markets(
    weather: dict[str, WeatherState],
    markets: list[dict],
    db=None,
) -> tuple[list[Candidate], list[dict]]:
    """Scan all Polymarket markets against current weather states.

    Args:
        weather: dict mapping station code → WeatherState (only stations we have data for)
        markets: list of raw market dicts from get_weather_markets()
        db: optional Database instance — when provided, TAF disruption is checked per candidate

    Returns:
        (candidates, all_snapshots) where:
        - candidates: markets where edge >= MIN_EDGE_CENTS for YES or NO side
        - all_snapshots: every evaluated market as a snapshot dict (for logging)
    """
    candidates: list[Candidate] = []
    snapshots: list[dict] = []
    ts = datetime.now(timezone.utc).isoformat()

    for market in markets:
        try:
            is_temp, station = is_highest_temp_market(market)
            if not is_temp or station not in weather:
                continue

            mins_left = minutes_to_settlement(market)
            if mins_left < MIN_MINUTES_TO_SETTLEMENT:
                continue

            # Only trade markets that settle today (UTC). Markets closing on a future
            # date use tomorrow's weather — our METAR data is only valid for today.
            end_str = (
                market.get("endDate") or market.get("end_date_iso")
                or market.get("endDateIso") or market.get("close_time") or ""
            )
            if end_str:
                try:
                    end_dt = dtparse.parse(str(end_str))
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    today_utc = datetime.now(timezone.utc).date()
                    if end_dt.date() != today_utc:
                        continue
                except Exception:
                    pass

            bracket = parse_bracket_from_market(market)
            if not bracket:
                continue

            if ENABLE_CLOB_ENRICHMENT:
                _enrich_from_clob(bracket)

            state = weather[station]
            p_yes = true_probability_yes(bracket, state, mins_left)
            fee = estimate_fee_cents(min(bracket.yes_ask_cents, bracket.no_ask_cents))

            ev_yes = p_yes * 100 - bracket.yes_ask_cents - fee
            ev_no = (1 - p_yes) * 100 - bracket.no_ask_cents - fee

            snap = {
                "ts": ts, "station": station, "ticker": bracket.ticker,
                "bracket_low": bracket.low_f, "bracket_high": bracket.high_f,
                "yes_ask": bracket.yes_ask_cents, "no_ask": bracket.no_ask_cents,
                "current_high": state.current_high_f, "latest_temp": state.latest_temp_f,
                "forecast_high": state.forecast_high_f, "p_yes": round(p_yes, 4),
                "ev_yes": round(ev_yes, 2), "ev_no": round(ev_no, 2),
                "minutes_to_settlement": round(mins_left, 1),
            }
            snapshots.append(snap)

            # Check for a tradeable edge
            candidate = None
            skipped_reason = None
            if (ENABLE_YES_TRADES and ev_yes >= MIN_EDGE_CENTS and p_yes >= MIN_CONFIDENCE_YES
                    and bracket.yes_ask_cents >= MIN_PRICE_CENTS):
                if ev_yes > MAX_EDGE_CENTS:
                    skipped_reason = f"YES edge {ev_yes:.2f}¢ > MAX_EDGE_CENTS={MAX_EDGE_CENTS}¢ (adverse selection)"
                else:
                    candidate = Candidate(
                        station=station, bracket=bracket, side="YES",
                        edge_cents=ev_yes, price_cents=bracket.yes_ask_cents,
                        confidence=p_yes, p_yes=p_yes,
                        ev_yes=ev_yes, ev_no=ev_no,
                        minutes_to_settlement=mins_left, market=market,
                    )
            elif (ev_no >= MIN_EDGE_CENTS and p_yes <= MAX_CONFIDENCE_YES_FOR_NO
                    and bracket.no_ask_cents >= MIN_PRICE_CENTS):
                if ev_no > MAX_EDGE_CENTS:
                    skipped_reason = f"NO edge {ev_no:.2f}¢ > MAX_EDGE_CENTS={MAX_EDGE_CENTS}¢ (adverse selection)"
                else:
                    candidate = Candidate(
                        station=station, bracket=bracket, side="NO",
                        edge_cents=ev_no, price_cents=bracket.no_ask_cents,
                        confidence=1 - p_yes, p_yes=p_yes,
                        ev_yes=ev_yes, ev_no=ev_no,
                        minutes_to_settlement=mins_left, market=market,
                    )

            if candidate and db:
                city = STATION_TO_CITY.get(station, "")
                if city:
                    now_utc = datetime.now(timezone.utc)
                    peak_start = now_utc.isoformat()
                    peak_end = (now_utc + timedelta(minutes=mins_left)).isoformat()
                    taf_flag = check_taf_disruption(city, db, peak_start, peak_end)
                    candidate.taf_disruption = taf_flag
                    if taf_flag:
                        factor = float(os.getenv("TAF_DISRUPTION_CONFIDENCE_FACTOR", "0.85"))
                        candidate.confidence *= factor
                        print(
                            f"  [cue] {city} taf_disruption=True "
                            f"confidence={candidate.confidence:.2f}"
                        )

            if candidate:
                candidates.append(candidate)
                label = market.get("groupItemTitle") or f"{bracket.low_f:.0f}-{bracket.high_f:.0f}°F"
                end_date = market.get("endDate") or market.get("end_date_iso") or "?"
                print(
                    f"  ** FLAGGED [{station}] {label} {candidate.side} @ "
                    f"{candidate.price_cents}¢ edge={candidate.edge_cents:.2f}¢ "
                    f"p={candidate.confidence:.2%} closes={str(end_date)[:10]}"
                )
            elif skipped_reason:
                label = market.get("groupItemTitle") or f"{bracket.low_f:.0f}-{bracket.high_f:.0f}°F"
                print(f"  -- SKIPPED [{station}] {label}: {skipped_reason}")

        except Exception as e:
            mid = (market.get("conditionId") or market.get("id") or "unknown")[:16]
            print(f"[market] error processing {mid}…: {e}, skipping")
            continue

    return candidates, snapshots

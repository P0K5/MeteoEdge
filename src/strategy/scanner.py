"""Market scanner — identifies mispriced Polymarket temperature brackets.

For each market fetched from Polymarket, this module:
1. Checks if it's a 'highest temperature in <city>' market for a configured station
2. Parses the bracket (e.g., '82-84°F') into a Bracket object
3. Computes true P(yes) using the weather envelope model
4. Computes expected value for YES and NO sides
5. Returns Candidate objects for any market with edge >= MIN_EDGE_CENTS
"""
import json
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from dateutil import parser as dtparse

from src.config import (
    STATIONS, MIN_EDGE_CENTS, MAX_EDGE_CENTS, MIN_PRICE_CENTS, MIN_CONFIDENCE_YES,
    MAX_CONFIDENCE_YES_FOR_NO, ENABLE_YES_TRADES, MIN_MINUTES_TO_SETTLEMENT,
    ENABLE_CLOB_ENRICHMENT, MIN_FORECAST_BRACKET_MARGIN_F, DISABLED_STATIONS,
    SHADOW_STATIONS, SHADOW_STATIONS_YES, SHADOW_STATIONS_NO,
    CONFIG_DEFAULTS, get_live_config, MODEL_PROB_CAP,
)
from src.model.envelope import Bracket, WeatherState, true_probability_yes, compute_envelope
from src.model.emos_mode import get_city_mode, apply_emos, _check_ready_for_promotion
from src.model.residual_correction import compute_residual_stats
from src.data.polymarket import get_orderbook
from src.data.taf_disruption import check_taf_disruption
from src.strategy.fee import estimate_fee_cents

log = logging.getLogger(__name__)


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
        lo, hi = -50.0, _to_f(float(m.group(1)), unit)
    elif (m := _LABEL_GTE.search(label)):
        unit = m.group(2)
        lo, hi = _to_f(float(m.group(1)), unit), 200.0
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
        log.warning("[parse] unparseable label: %r", label[:60])
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


def _enrich_from_clob(bracket: Bracket, orderbooks: "dict[str, dict] | None" = None) -> None:
    """Overwrite bracket ask prices with live CLOB data.

    Only called when ENABLE_CLOB_ENRICHMENT=True.

    Args:
        bracket: The bracket to enrich in-place.
        orderbooks: Optional pre-fetched dict mapping token_id → orderbook.
            When provided the cached result is used directly (no HTTP call).
            When ``None`` (default) a fresh ``get_orderbook()`` call is made
            per token, preserving backward-compatible single-market behaviour.
    """
    if not ENABLE_CLOB_ENRICHMENT:
        return

    if bracket.yes_token_id:
        try:
            if orderbooks is not None:
                ob = orderbooks.get(bracket.yes_token_id) or {}
            else:
                ob = get_orderbook(bracket.yes_token_id)
            asks = ob.get("asks") or []
            if asks:
                best = min(float(a["price"]) for a in asks)
                bracket.yes_ask_cents = max(1, min(99, round(best * 100)))
                bracket.yes_ask_size = sum(max(0, int(float(a["size"]))) for a in asks[:3])
        except Exception as e:
            log.warning("[clob] YES %s...: %s", bracket.ticker[:14], e)

    if bracket.no_token_id:
        try:
            if orderbooks is not None:
                ob = orderbooks.get(bracket.no_token_id) or {}
            else:
                ob = get_orderbook(bracket.no_token_id)
            asks = ob.get("asks") or []
            if asks:
                best = min(float(a["price"]) for a in asks)
                bracket.no_ask_cents = max(1, min(99, round(best * 100)))
                bracket.no_ask_size = sum(max(0, int(float(a["size"]))) for a in asks[:3])
        except Exception as e:
            log.warning("[clob] NO %s...: %s", bracket.ticker[:14], e)


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
    shadow: bool = field(default=False)           # True when ENABLE_YES_TRADES=False


def no_entry_margin_gap(bracket: Bracket, state: WeatherState) -> float | None:
    """Distance in °F between the bracket and the expected daily high, for NO entries.

    Returns None when the gate does not apply:
    - no forecast or running-high data available
    - the forecast sits inside the bracket (historically 7% loss rate — the
      envelope model prices these adequately)
    - the running daily high already exceeds the bracket top (the daily max
      cannot decrease, so NO can no longer lose)
    """
    forecast = state.forecast_high_f if state.forecast_high_f is not None else state.secondary_forecast_f
    current = state.current_high_f
    bases = [v for v in (forecast, current) if v is not None]
    if not bases:
        return None
    base = max(bases)
    if base < bracket.low_f:
        # Bracket above: at risk if the temp climbs into it
        return bracket.low_f - base
    if bracket.high_f < 200 and base > bracket.high_f:
        if current is not None and current > bracket.high_f:
            return None
        return base - bracket.high_f
    return None


def scan_markets(
    weather: "dict[str, WeatherState]",
    markets: list,
    db=None,
    orderbooks: "dict[str, dict] | None" = None,
) -> "tuple[list[Candidate], list[dict]]":
    """Scan all Polymarket markets against current weather states.

    Args:
        weather: dict mapping station code → WeatherState (only stations we have data for)
        markets: list of raw market dicts from get_weather_markets()
        db: optional Database instance — when provided, TAF disruption is checked per candidate
        orderbooks: optional pre-fetched dict mapping token_id → orderbook dict.
            When provided, CLOB enrichment reads from this dict instead of
            making individual HTTP calls (one call per token, batched upstream).
            When ``None``, falls back to per-bracket ``get_orderbook()`` calls.

    Returns:
        (candidates, all_snapshots) where:
        - candidates: markets where edge >= MIN_EDGE_CENTS for YES or NO side
        - all_snapshots: every evaluated market as a snapshot dict (for logging)
    """
    candidates: list[Candidate] = []
    snapshots: list[dict] = []
    ts = datetime.now(timezone.utc).isoformat()
    skip_reason_counts: Counter = Counter()  # Track skip reasons for final summary

    # Read shadow YES thresholds from live config (DB-backed) so they can be
    # tuned via the dashboard without restarting the bot.  Fall back to
    # CONFIG_DEFAULTS when db is unavailable (tests, CLI runs without a DB).
    if db is not None:
        _live = get_live_config(db)
        shadow_yes_edge_min  = float(_live.get("SHADOW_MIN_EDGE_CENTS_YES",  CONFIG_DEFAULTS["SHADOW_MIN_EDGE_CENTS_YES"]))
        shadow_yes_conf_min  = float(_live.get("SHADOW_MIN_CONFIDENCE_YES",  CONFIG_DEFAULTS["SHADOW_MIN_CONFIDENCE_YES"]))
        shadow_yes_price_min = int(_live.get("SHADOW_MIN_PRICE_CENTS_YES", CONFIG_DEFAULTS["SHADOW_MIN_PRICE_CENTS_YES"]))
    else:
        shadow_yes_edge_min  = float(CONFIG_DEFAULTS["SHADOW_MIN_EDGE_CENTS_YES"])
        shadow_yes_conf_min  = float(CONFIG_DEFAULTS["SHADOW_MIN_CONFIDENCE_YES"])
        shadow_yes_price_min = int(CONFIG_DEFAULTS["SHADOW_MIN_PRICE_CENTS_YES"])

    for market in markets:
        try:
            is_temp, station = is_highest_temp_market(market)
            if not is_temp:
                # Market is not a highest-temp market; don't even count it
                continue
            if station not in weather:
                # It's a highest-temp market but we have no weather data for this station
                skip_reason_counts["not_highest_temp"] += 1
                continue

            mins_left = minutes_to_settlement(market)
            if mins_left < MIN_MINUTES_TO_SETTLEMENT:
                label = market.get("groupItemTitle") or market.get("question", "")[:40]
                log.debug("[%s] -- SKIPPED %s: closes in %.1f min < %d min",
                          station, "outside_window", mins_left, MIN_MINUTES_TO_SETTLEMENT)
                skip_reason_counts["outside_window"] += 1
                continue

            # Only trade markets that settle today (UTC). Markets closing on a future
            # date use tomorrow's weather — our METAR data is only valid for today.
            end_str = (
                market.get("endDate") or market.get("end_date_iso")
                or market.get("endDateIso") or market.get("close_time") or ""
            )
            wrong_date_skipped = False
            if end_str:
                try:
                    end_dt = dtparse.parse(str(end_str))
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    today_utc = datetime.now(timezone.utc).date()
                    if end_dt.date() != today_utc:
                        label = market.get("groupItemTitle") or market.get("question", "")[:40]
                        log.debug("[%s] -- SKIPPED %s: closes %s != today (%s)",
                                  station, "wrong_date", end_dt.date(), today_utc)
                        skip_reason_counts["wrong_date"] += 1
                        wrong_date_skipped = True
                except Exception:
                    pass

            if wrong_date_skipped:
                continue

            bracket = parse_bracket_from_market(market)
            if not bracket:
                label = market.get("groupItemTitle") or market.get("question", "")[:40]
                log.debug("[%s] -- SKIPPED %s: %s", station, "bracket_parse_fail", label)
                skip_reason_counts["bracket_parse_fail"] += 1
                continue

            if ENABLE_CLOB_ENRICHMENT:
                _enrich_from_clob(bracket, orderbooks=orderbooks)

            state = weather[station]
            city = STATION_TO_CITY.get(station, station)

            # EMOS mode switching: determine mode for this city and optionally
            # apply linear bias correction to forecast_mean and forecast_stddev.
            emos_mode_used = "legacy"
            emos_stddev_override = None
            if db is not None:
                mode = get_city_mode(city, db)
                if mode == "emos_shadow":
                    # Shadow: compute calibrated params for logging only; serve legacy probs.
                    # forecast_mean and sigma used by true_probability_yes are not modified.
                    from src.config import FORECAST_STDDEV_F
                    _mu_cal, _sigma_cal = apply_emos(
                        state.corrected_mu_f or state.deb_mu_f or state.forecast_high_f or 0.0,
                        FORECAST_STDDEV_F,
                        city, db,
                    )
                    log.debug(
                        "[emos] shadow city=%s legacy_mu=%.2f emos_mu=%.2f"
                        " legacy_sigma=%.2f emos_sigma=%.2f",
                        city,
                        state.corrected_mu_f or state.deb_mu_f or state.forecast_high_f or 0.0,
                        _mu_cal,
                        FORECAST_STDDEV_F,
                        _sigma_cal,
                    )
                    emos_mode_used = "emos_shadow"
                elif mode == "emos_primary":
                    if not _check_ready_for_promotion(city, db):
                        log.warning(
                            "[emos] city=%s emos_primary but ready_for_promotion=0"
                            " — falling back to legacy",
                            city,
                        )
                        emos_mode_used = "legacy"
                    else:
                        from src.config import FORECAST_STDDEV_F
                        _mu_raw = (
                            state.corrected_mu_f
                            or state.deb_mu_f
                            or state.forecast_high_f
                            or 0.0
                        )
                        _mu_cal, _sigma_cal = apply_emos(_mu_raw, FORECAST_STDDEV_F, city, db)
                        # Inject calibrated values into a patched state so
                        # true_probability_yes uses the EMOS-corrected mean.
                        # We do this by temporarily wrapping: pass corrected_mu_f
                        # and a stddev override without mutating the shared state.
                        from dataclasses import replace as _dc_replace
                        state = _dc_replace(state, corrected_mu_f=_mu_cal)
                        emos_stddev_override = _sigma_cal
                        emos_mode_used = "emos_primary"

            if emos_stddev_override is not None:
                p_yes = true_probability_yes(bracket, state, mins_left, forecast_stddev=emos_stddev_override)
            else:
                p_yes = true_probability_yes(bracket, state, mins_left)
            raw_p_yes = p_yes
            p_yes = min(max(p_yes, 1.0 - MODEL_PROB_CAP), MODEL_PROB_CAP)
            fee = estimate_fee_cents(min(bracket.yes_ask_cents, bracket.no_ask_cents))

            ev_yes = p_yes * 100 - bracket.yes_ask_cents - fee
            ev_no = (1 - p_yes) * 100 - bracket.no_ask_cents - fee

            snap = {
                "ts": ts, "station": station, "ticker": bracket.ticker,
                "bracket_low": bracket.low_f, "bracket_high": bracket.high_f,
                "yes_ask": bracket.yes_ask_cents, "no_ask": bracket.no_ask_cents,
                "current_high": state.current_high_f, "latest_temp": state.latest_temp_f,
                "forecast_high": state.forecast_high_f, "p_yes": round(p_yes, 4),
                "raw_p_yes": round(raw_p_yes, 4), "capped_p_yes": round(p_yes, 4),
                "ev_yes": round(ev_yes, 2), "ev_no": round(ev_no, 2),
                "minutes_to_settlement": round(mins_left, 1),
                "emos_mode": emos_mode_used,
            }
            snapshots.append(snap)

            # Check for a tradeable edge
            candidate = None
            skipped_reason = None
            label = market.get("groupItemTitle") or f"{bracket.low_f:.0f}-{bracket.high_f:.0f}°F"

            # Determine per-side shadow status from DB override or env fallback
            station_override = db.get_station_override(station) if db else None
            if station_override is not None:
                yes_enabled = station_override["yes_enabled"]
                no_enabled = station_override["no_enabled"]
            else:
                # Fall back to env vars: shadow if in SHADOW_STATIONS or per-side set
                yes_enabled = (station not in SHADOW_STATIONS) and (station not in SHADOW_STATIONS_YES)
                no_enabled = (station not in SHADOW_STATIONS) and (station not in SHADOW_STATIONS_NO)

            # ENABLE_YES_TRADES=False forces YES shadow on all stations regardless of yes_enabled
            shadow_yes = (not yes_enabled) or (not ENABLE_YES_TRADES)
            shadow_no = not no_enabled

            # Use looser shadow thresholds on the YES shadow path so the
            # shadow loop can collect data.  The NO branch is untouched.
            _yes_edge = shadow_yes_edge_min if shadow_yes else MIN_EDGE_CENTS
            _yes_conf = shadow_yes_conf_min if shadow_yes else MIN_CONFIDENCE_YES
            _yes_price = shadow_yes_price_min if shadow_yes else MIN_PRICE_CENTS

            if ev_yes >= _yes_edge and p_yes >= _yes_conf and bracket.yes_ask_cents >= _yes_price:
                if ev_yes > MAX_EDGE_CENTS:
                    skipped_reason = "max_edge"
                    log.debug("[%s] -- SKIPPED %s: %s edge=%.2f¢ > MAX=%.2f¢",
                              station, skipped_reason, "YES", ev_yes, MAX_EDGE_CENTS)
                    skip_reason_counts[skipped_reason] += 1
                else:
                    candidate = Candidate(
                        station=station, bracket=bracket, side="YES",
                        edge_cents=ev_yes, price_cents=bracket.yes_ask_cents,
                        confidence=p_yes, p_yes=p_yes,
                        ev_yes=ev_yes, ev_no=ev_no,
                        minutes_to_settlement=mins_left, market=market,
                        shadow=shadow_yes,
                    )
            elif ev_no >= MIN_EDGE_CENTS and p_yes <= MAX_CONFIDENCE_YES_FOR_NO and bracket.no_ask_cents >= MIN_PRICE_CENTS:
                margin_gap = no_entry_margin_gap(bracket, state)
                if ev_no > MAX_EDGE_CENTS:
                    skipped_reason = "max_edge"
                    log.debug("[%s] -- SKIPPED %s: %s edge=%.2f¢ > MAX=%.2f¢",
                              station, skipped_reason, "NO", ev_no, MAX_EDGE_CENTS)
                    skip_reason_counts[skipped_reason] += 1
                elif margin_gap is not None and margin_gap < MIN_FORECAST_BRACKET_MARGIN_F:
                    skipped_reason = "margin_gate"
                    log.debug("[%s] -- SKIPPED %s: bracket %.1f-%.1fF within %.1fF of expected high (< %.1fF)",
                              station, skipped_reason, bracket.low_f, bracket.high_f,
                              margin_gap, MIN_FORECAST_BRACKET_MARGIN_F)
                    skip_reason_counts[skipped_reason] += 1
                else:
                    # MAE gate (issue #307): suppress live NO entries when rolling MAE
                    # exceeds MAX_RESIDUAL_MAE_F_FOR_LIVE. Shadow continues so data
                    # keeps accumulating while the station is gated.
                    _mae_suppressed = False
                    if db is not None and not shadow_no:
                        _res_stats = compute_residual_stats(city, db)
                        if _res_stats is not None and _res_stats.live_suppressed:
                            shadow_no = True
                            _mae_suppressed = True
                            log.info(
                                "[%s] MAE gate: rolling_mae=%.2f°F > threshold → "
                                "forcing NO to shadow (live suppressed)",
                                station, _res_stats.rolling_mae,
                            )
                    candidate = Candidate(
                        station=station, bracket=bracket, side="NO",
                        edge_cents=ev_no, price_cents=bracket.no_ask_cents,
                        confidence=1 - p_yes, p_yes=p_yes,
                        ev_yes=ev_yes, ev_no=ev_no,
                        minutes_to_settlement=mins_left, market=market,
                        shadow=shadow_no,
                    )
            else:
                # YES gates passed but NO gate failed (or both edges below MIN_EDGE_CENTS).
                # YES branch is now handled above unconditionally, so only NO failures
                # and low-edge cases reach here.
                if ev_no >= MIN_EDGE_CENTS:
                    if p_yes > MAX_CONFIDENCE_YES_FOR_NO:
                        skipped_reason = "confidence_gate"
                        log.debug("[%s] -- SKIPPED %s: p_yes=%.4f > MAX=%.4f",
                                  station, skipped_reason, p_yes, MAX_CONFIDENCE_YES_FOR_NO)
                    elif bracket.no_ask_cents < MIN_PRICE_CENTS:
                        skipped_reason = "min_edge"
                        log.debug("[%s] -- SKIPPED %s: NO price=%.0f¢ < MIN=%.0f¢",
                                  station, skipped_reason, bracket.no_ask_cents, MIN_PRICE_CENTS)
                    else:
                        skipped_reason = "min_edge"
                    skip_reason_counts[skipped_reason] += 1
                else:
                    # Both edges below MIN_EDGE_CENTS
                    skipped_reason = "min_edge"
                    log.debug("[%s] -- SKIPPED %s: max(%.2f¢, %.2f¢) < MIN=%.2f¢",
                              station, skipped_reason, ev_yes, ev_no, MIN_EDGE_CENTS)
                    skip_reason_counts[skipped_reason] += 1

            if candidate and db:
                _taf_city = STATION_TO_CITY.get(station, "")
                if _taf_city:
                    now_utc = datetime.now(timezone.utc)
                    peak_start = now_utc.isoformat()
                    peak_end = (now_utc + timedelta(minutes=mins_left)).isoformat()
                    taf_flag = check_taf_disruption(_taf_city, db, peak_start, peak_end)
                    candidate.taf_disruption = taf_flag
                    if taf_flag:
                        factor = float(os.getenv("TAF_DISRUPTION_CONFIDENCE_FACTOR", "0.85"))
                        candidate.confidence *= factor
                        log.info(
                            "  [cue] %s taf_disruption=True confidence=%.2f",
                            _taf_city, candidate.confidence,
                        )

            if candidate:
                candidates.append(candidate)
                label = market.get("groupItemTitle") or f"{bracket.low_f:.0f}-{bracket.high_f:.0f}°F"
                end_date = market.get("endDate") or market.get("end_date_iso") or "?"
                log.info(
                    "  ** FLAGGED [%s] %s %s @ %sc edge=%.2fc p=%.2f%% closes=%s",
                    station, label, candidate.side, candidate.price_cents,
                    candidate.edge_cents, candidate.confidence * 100, str(end_date)[:10],
                )

        except Exception as e:
            mid = (market.get("conditionId") or market.get("id") or "unknown")[:16]
            log.warning("[market] error processing %s...: %s, skipping", mid, e, exc_info=True)
            continue

    # Log summary at INFO level
    num_flagged = len(candidates)
    total_markets = len(markets)
    counts_str = ", ".join(f"{count} {reason}" for reason, count in skip_reason_counts.most_common())
    log.info("[scan] %d markets: %d flagged, %s", total_markets, num_flagged, counts_str)

    return candidates, snapshots

"""Market scanner — identifies mispriced Polymarket temperature brackets.

For each market fetched from Polymarket, this module:
1. Checks if it's a 'highest temperature in <city>' or 'lowest temperature in <city>' market
2. Parses the bracket (e.g., '82-84°F') into a Bracket object
3. Computes true P(yes) using the weather envelope model (high) or low-side model (low)
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
from typing import Literal
from dateutil import parser as dtparse

from src.config import (
    STATIONS, MIN_EDGE_CENTS, MAX_EDGE_CENTS, MIN_PRICE_CENTS, MIN_CONFIDENCE_YES,
    MAX_CONFIDENCE_YES_FOR_NO, MIN_MINUTES_TO_SETTLEMENT,
    ENABLE_CLOB_ENRICHMENT, MIN_FORECAST_BRACKET_MARGIN_F, DISABLED_STATIONS,
    SHADOW_STATIONS, SHADOW_STATIONS_YES, SHADOW_STATIONS_NO,
    CONFIG_DEFAULTS, get_live_config, MODEL_PROB_CAP, FORECAST_STDDEV_F,
    ENVELOPE_SIGMA_CLIMB_FRACTION,
)
from src.model.envelope import (
    Bracket, WeatherState, true_probability_yes, compute_envelope,
    next_day_probability_yes,
)
from src.model.emos_mode import (
    get_city_mode, emos_serving_mu, _check_ready_for_promotion, resolve_sigma_raw,
    _select_emos_row,
)
from src.model.residual_correction import compute_residual_stats
from src.data.polymarket import get_orderbook
from src.data.taf_disruption import check_taf_disruption
from src.data.open_meteo import fetch_open_meteo_with_spread, fetch_gfs_with_spread
from src.strategy.fee import estimate_fee_cents

log = logging.getLogger(__name__)

# Station -> (lat, lon), for next-day forecast fetches (issue #687). Built
# from the same STATIONS tuples as POLYMARKET_CITY_TO_STATION/STATION_TO_CITY
# below.
STATION_COORDS: dict[str, tuple[float, float]] = {
    station: (lat, lon) for station, lat, lon, *_ in STATIONS
}


# Map Polymarket city name (lowercase) → METAR station code
POLYMARKET_CITY_TO_STATION: dict[str, str] = {
    city.lower(): station for station, _, _, city, *_ in STATIONS
}

# Reverse map: METAR station code → Polymarket city name
STATION_TO_CITY: dict[str, str] = {
    station: city for station, _, _, city, *_ in STATIONS
}

# Low-side city alias map.  Polymarket uses different city name conventions on
# "lowest temperature in" markets vs "highest temperature in" markets (e.g.
# "NYC" vs "New York City", "London" vs a specific airport alias).  Build this
# map explicitly — do NOT auto-derive from POLYMARKET_CITY_TO_STATION.
POLYMARKET_CITY_TO_STATION_LOW: dict[str, str] = {
    "chicago":       "KORD",
    "miami":         "KMIA",
    "los angeles":   "KLAX",
    "atlanta":       "KATL",
    "houston":       "KHOU",
    "seoul":         "RKSI",
    "kuala lumpur":  "WMKK",
    "busan":         "RKPK",
    "shenzhen":      "ZGSZ",
    "singapore":     "WSSS",
    "panama city":   "MPMG",
    # Low-side markets use shortened names for some cities
    "nyc":           "KJFK",   # Polymarket uses "NYC" on low-side markets
    "new york":      "KJFK",
    "london":        "EGLC",
    "paris":         "LFPB",
    "tokyo":         "RJTT",
    "shanghai":      "ZSPD",
}

# Reverse map for low-side: station → city display name
STATION_TO_CITY_LOW: dict[str, str] = {v: k.title() for k, v in POLYMARKET_CITY_TO_STATION_LOW.items()}


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


def is_lowest_temp_market(market: dict) -> tuple[bool, "str | None"]:
    """Return (True, station_code) for 'lowest temperature in <city>' markets.

    Uses POLYMARKET_CITY_TO_STATION_LOW which has explicit city aliases
    for low-side markets (Polymarket uses different names on the low side).
    """
    q = (market.get("question") or "").lower()
    if "lowest temperature in" not in q:
        return False, None
    for city, station in POLYMARKET_CITY_TO_STATION_LOW.items():
        if f"lowest temperature in {city}" in q:
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


def _fetch_next_day_forecast(station: str, lead_hours: float) -> "tuple[float, float | None] | None":
    """Fetch (mu_f, sigma_f) for a station's next-day forecast (issue #687).

    Follows the same open_meteo -> gfs fallback convention as
    src/scripts/capture_forecasts.py: try the multi-model spread fetch first
    (both mu and cross-model sigma); if unavailable, fall back to the
    single-model GFS fetch (mu only -- sigma is always None there, since a
    single deterministic run has no cross-member spread to compute).

    Returns None when no station coordinates are configured or neither
    fetcher returns a usable forecast (e.g. all upstream requests failed).
    """
    coords = STATION_COORDS.get(station)
    if coords is None:
        return None
    lat, lon = coords
    result = fetch_open_meteo_with_spread(lat, lon, lead_hours=round(lead_hours))
    if result is not None:
        return result
    return fetch_gfs_with_spread(lat, lon, lead_hours=round(lead_hours))


def _resolve_next_day_mu_sigma(
    station: str, city: str, lead_hours: float, db, next_day_sigma_multiplier: float,
) -> "tuple[float, float] | None":
    """Resolve (mu, sigma) for a next-day candidate (issue #687).

    Implements the round-2 review's binding calibration consistency rule:
    per evaluation, use the matched EMOS lead-bin row's full (a, b, c, d), or
    none of it -- never a calibrated sigma paired with an uncalibrated mu.

    - If a fitted lead bin covers ``lead_hours``: mu = a + b*mu_stack,
      sigma = c + d*sigma_stack (exactly the transform EMOS serving would
      apply at that lead -- see src.model.emos_mode.apply_emos).
    - Otherwise: raw mu_stack (uncalibrated) with
      sigma = FORECAST_STDDEV_F * NEXT_DAY_SIGMA_MULTIPLIER (issue #687
      amendment 2 -- the same-day fallback FORECAST_STDDEV_F alone is tuned
      too tight for 12-36h lead).

    Returns None when no next-day forecast is available at all for the
    station (caller should skip the candidate, same as any other
    forecast-unavailable condition).
    """
    fetched = _fetch_next_day_forecast(station, lead_hours)
    if fetched is None:
        return None
    mu_stack, sigma_stack = fetched

    row = _select_emos_row(city, db, lead_hours * 60.0) if db is not None else None
    if row is not None:
        a, b, c, d = row["a"], row["b"], row["c"], row["d"]
        # sigma_stack may be None (GFS-only fallback carries no cross-model
        # spread) -- FORECAST_STDDEV_F stands in as the *raw* sigma fed into
        # this same row's (c, d) transform, so mu and sigma still come from
        # the identical calibration row (never fitted sigma with an
        # uncalibrated mu).
        sigma_raw = sigma_stack if sigma_stack is not None else FORECAST_STDDEV_F
        mu_cal = a + b * mu_stack
        sigma_cal = c + d * sigma_raw
        if sigma_cal > 0:
            return mu_cal, sigma_cal
        log.warning(
            "[next-day] sigma_cal=%.4f <= 0 for city=%s -- falling back to raw stack + multiplier",
            sigma_cal, city,
        )

    return mu_stack, FORECAST_STDDEV_F * next_day_sigma_multiplier


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
    # Pre-clamp (uncapped) model probability and its derived edges -- issue #551.
    # Always populated by scan_markets(); equals the capped p_yes/ev_* fields when
    # MODEL_PROB_CAP did not fire. Entry gates never read these -- only ranking
    # (behind RANK_ON_RAW_PROB) and logging do.
    p_yes_raw: "float | None" = field(default=None)
    ev_yes_raw: "float | None" = field(default=None)
    ev_no_raw: "float | None" = field(default=None)
    taf_disruption: bool = field(default=False)  # TEMPO/PROB TS/SH/FG overlap
    shadow: bool = field(default=False)           # True when yes_enabled=False for station
    direction: Literal["high", "low"] = field(default="high")  # daily-high or daily-low market
    # True when this candidate came from next-day evaluation (issue #687) --
    # a forecast-only evaluation of a station's next market, always shadow=True
    # regardless of station overrides (no live entries from next-day eval).
    is_next_day: bool = field(default=False)


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
    weather_low: "dict | None" = None,
    prob_low_fn=None,
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
        weather_low: optional dict mapping station → low-side weather state for low markets.
            When None, low-side markets are detected but not scored (skipped silently).
        prob_low_fn: callable(bracket, state_low, mins_left, forecast_stddev_f) → float.
            Required when weather_low is provided. Injected by the caller so this
            module does not directly depend on the low-side model package.

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
        rank_on_raw_prob = bool(_live.get("RANK_ON_RAW_PROB", CONFIG_DEFAULTS["RANK_ON_RAW_PROB"]))
        # DEB_ENABLED resolved once per scan from live config (issue #549) —
        # passed into true_probability_yes so the dashboard toggle is respected
        # without a per-bracket DB read.
        _deb_enabled = bool(_live.get("DEB_ENABLED", CONFIG_DEFAULTS["DEB_ENABLED"]))
        # USE_ENSEMBLE_SIGMA resolved once per scan from live config (issue #448) —
        # same pattern as DEB_ENABLED. Default off: no live behaviour change
        # until a station's ensemble_sigma_f is populated AND this is flipped on.
        _use_ensemble_sigma = bool(_live.get("USE_ENSEMBLE_SIGMA", CONFIG_DEFAULTS["USE_ENSEMBLE_SIGMA"]))
        # Entry-gate thresholds live-read from bot_config (issue #644): the
        # dashboard already exposed these keys, but the gates below consumed
        # the import-time module constants, so edits (e.g. MIN_PRICE_CENTS=75
        # set on 2026-07) were silently ignored until a process restart with
        # matching env vars. Raw rows (not get_live_config) so that a key the
        # DB has never seeded falls back to the module constant, which is
        # env-var aware.
        try:
            _raw_cfg = db.get_all_config()
        except Exception:
            _raw_cfg = {}

        def _cfg(key, default, cast):
            try:
                return cast(_raw_cfg[key]) if key in _raw_cfg else default
            except (ValueError, TypeError):
                return default
        min_edge_cents      = _cfg("MIN_EDGE_CENTS", MIN_EDGE_CENTS, float)
        max_edge_cents      = _cfg("MAX_EDGE_CENTS", MAX_EDGE_CENTS, float)
        min_price_cents     = _cfg("MIN_PRICE_CENTS", MIN_PRICE_CENTS, lambda v: int(float(v)))
        max_conf_yes_for_no = _cfg("MAX_CONFIDENCE_YES_FOR_NO", MAX_CONFIDENCE_YES_FOR_NO, float)
        sigma_climb_fraction = _cfg("ENVELOPE_SIGMA_CLIMB_FRACTION", ENVELOPE_SIGMA_CLIMB_FRACTION, float)
        # Next-day evaluation (issue #687), default off -- resolved once per
        # scan, same pattern as DEB_ENABLED/USE_ENSEMBLE_SIGMA above.
        next_day_evaluation = bool(_live.get("NEXT_DAY_EVALUATION", CONFIG_DEFAULTS["NEXT_DAY_EVALUATION"]))
        next_day_sigma_multiplier = float(_live.get(
            "NEXT_DAY_SIGMA_MULTIPLIER", CONFIG_DEFAULTS["NEXT_DAY_SIGMA_MULTIPLIER"]
        ))
    else:
        shadow_yes_edge_min  = float(CONFIG_DEFAULTS["SHADOW_MIN_EDGE_CENTS_YES"])
        shadow_yes_conf_min  = float(CONFIG_DEFAULTS["SHADOW_MIN_CONFIDENCE_YES"])
        shadow_yes_price_min = int(CONFIG_DEFAULTS["SHADOW_MIN_PRICE_CENTS_YES"])
        rank_on_raw_prob = bool(CONFIG_DEFAULTS["RANK_ON_RAW_PROB"])
        _deb_enabled = None  # no DB: envelope falls back to the DEB_ENABLED env var
        _use_ensemble_sigma = None  # no DB: envelope falls back to the USE_ENSEMBLE_SIGMA env var
        min_edge_cents      = MIN_EDGE_CENTS
        max_edge_cents      = MAX_EDGE_CENTS
        min_price_cents     = MIN_PRICE_CENTS
        max_conf_yes_for_no = MAX_CONFIDENCE_YES_FOR_NO
        sigma_climb_fraction = ENVELOPE_SIGMA_CLIMB_FRACTION
        # No DB: fall back to the env var (same precedence pattern as
        # DEB_ENABLED/USE_ENSEMBLE_SIGMA's env-var fallback, applied here
        # directly since next-day resolution has no downstream env fallback
        # of its own).
        next_day_evaluation = os.getenv(
            "NEXT_DAY_EVALUATION", str(CONFIG_DEFAULTS["NEXT_DAY_EVALUATION"])
        ).strip().lower() == "true"
        next_day_sigma_multiplier = float(os.getenv(
            "NEXT_DAY_SIGMA_MULTIPLIER", str(CONFIG_DEFAULTS["NEXT_DAY_SIGMA_MULTIPLIER"])
        ))

    # Next-day evaluation (issue #687): per-station eligibility + which future
    # date to treat as "next-day" for that station. Computed once per scan,
    # entirely behind next_day_evaluation (default off) -- while off,
    # eligible_next_day_date stays empty and the per-market wrong_date check
    # below never takes the next-day branch, so behaviour is byte-for-byte
    # unchanged from today.
    #
    # Eligibility is per-station: once today's own market for a station is
    # past MIN_MINUTES_TO_SETTLEMENT (or absent from the fetched market set),
    # that station's next market (by closest endDate) becomes evaluable. A
    # station whose today-market is still live is unaffected.
    today_utc = datetime.now(timezone.utc).date()
    eligible_next_day_date: dict = {}
    if next_day_evaluation:
        _today_mins_by_station: dict = {}
        _next_dates_by_station: dict = {}
        for _m in markets:
            _is_temp, _station = is_highest_temp_market(_m)
            if not _is_temp or _station not in weather:
                continue
            _end_str = (
                _m.get("endDate") or _m.get("end_date_iso")
                or _m.get("endDateIso") or _m.get("close_time") or ""
            )
            if not _end_str:
                continue
            try:
                _end_dt = dtparse.parse(str(_end_str))
                if _end_dt.tzinfo is None:
                    _end_dt = _end_dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if _end_dt.date() == today_utc:
                _mins = minutes_to_settlement(_m)
                _today_mins_by_station[_station] = max(
                    _today_mins_by_station.get(_station, -9999.0), _mins
                )
            elif _end_dt.date() > today_utc:
                _next_dates_by_station.setdefault(_station, set()).add(_end_dt.date())

        for _station, _dates in _next_dates_by_station.items():
            _today_mins = _today_mins_by_station.get(_station)
            if _today_mins is not None and _today_mins >= MIN_MINUTES_TO_SETTLEMENT:
                continue  # today's market still live for this station -- not eligible
            eligible_next_day_date[_station] = min(_dates)  # closest future date

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
            is_next_day_eval = False
            if end_str:
                try:
                    end_dt = dtparse.parse(str(end_str))
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    if end_dt.date() != today_utc:
                        # Issue #687: a station past its own MIN_MINUTES_TO_SETTLEMENT
                        # (or with no today-market at all) may evaluate its next
                        # market instead of hard-skipping here -- see the
                        # eligible_next_day_date precompute above. Per-station only;
                        # a station whose today-market is still live never reaches
                        # this branch's eligibility (precompute excludes it).
                        if next_day_evaluation and eligible_next_day_date.get(station) == end_dt.date():
                            is_next_day_eval = True
                        else:
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

            if is_next_day_eval:
                # Issue #687: next-day evaluation is its own explicit
                # probability path -- never true_probability_yes on a doctored
                # state (see envelope.next_day_probability_yes docstring).
                # mins_left here is this market's own lead time (its close is
                # the next-day date, not today's), so it doubles as the lead
                # hours the #665 lead-bin machinery expects.
                emos_mode_used = "next_day"
                _resolved = _resolve_next_day_mu_sigma(
                    station, city, mins_left / 60.0, db, next_day_sigma_multiplier
                )
                if _resolved is None:
                    label = market.get("groupItemTitle") or market.get("question", "")[:40]
                    log.debug("[%s] -- SKIPPED %s: no next-day forecast available",
                              station, "next_day_forecast_unavailable")
                    skip_reason_counts["next_day_forecast_unavailable"] += 1
                    continue
                _next_day_mu, _next_day_sigma = _resolved
                p_yes = next_day_probability_yes(bracket, _next_day_mu, _next_day_sigma)
                # Next-day rows have no today-anchored running high/latest temp
                # to report (that's the whole point of forecast-only
                # evaluation) -- forecast_high reflects the actual mean fed
                # into p_yes rather than today's (irrelevant) forecast_high_f.
                _snap_current_high = None
                _snap_latest_temp = None
                _snap_forecast_high = _next_day_mu
            else:
                # EMOS mode switching: determine mode for this city and optionally
                # apply linear bias correction to forecast_mean and forecast_stddev.
                emos_mode_used = "legacy"
                emos_stddev_override = None
                # Sigma EMOS serving should feed apply_emos (issue #448): the
                # station's ensemble_sigma_f when USE_ENSEMBLE_SIGMA is on and the
                # value is available, else the legacy fixed FORECAST_STDDEV_F.
                # Resolves to FORECAST_STDDEV_F unchanged while the flag is off.
                _sigma_raw = resolve_sigma_raw(state, _use_ensemble_sigma, FORECAST_STDDEV_F)
                if db is not None:
                    mode = get_city_mode(city, db)
                    if mode == "emos_shadow":
                        # Shadow: compute the SERVING construction (#658 — plain
                        # stack mean → apply_emos → + intraday delta) for logging
                        # only; legacy probabilities are served unchanged. Logging
                        # the same values the primary path would serve keeps the
                        # shadow comparison honest. mins_left drives per-lead-bin
                        # sigma coefficient selection when a city has been
                        # retrained at more than one lead bin (issue #665); a
                        # city fit only at the legacy default bin sees no change.
                        _serving = emos_serving_mu(state, city, db, _sigma_raw, minutes_to_settlement=mins_left)
                        if _serving is not None:
                            _mu_final, _sigma_cal = _serving
                            log.debug(
                                "[emos] shadow city=%s legacy_mu=%.2f emos_mu=%.2f"
                                " legacy_sigma=%.2f emos_sigma=%.2f",
                                city,
                                state.corrected_mu_f or state.deb_mu_f or state.forecast_high_f or 0.0,
                                _mu_final,
                                _sigma_raw,
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
                            # #658 layer contract: EMOS consumes the plain
                            # equal-weight stack mean it was TRAINED on — never
                            # corrected_mu_f/deb_mu_f (train/serve parity; the
                            # residual correction drops out of this path because
                            # EMOS's intercept learns the same static bias). The
                            # decayed intraday delta is layered on top inside
                            # emos_serving_mu. mins_left drives per-lead-bin sigma
                            # coefficient selection (issue #665) -- unchanged
                            # behaviour for any city fit only at the legacy
                            # default lead bin.
                            _serving = emos_serving_mu(state, city, db, _sigma_raw, minutes_to_settlement=mins_left)
                            if _serving is None:
                                log.warning(
                                    "[emos] city=%s emos_primary but no stack member"
                                    " forecast on state — falling back to legacy",
                                    city,
                                )
                                emos_mode_used = "legacy"
                            else:
                                _mu_final, _sigma_cal = _serving
                                # Inject calibrated values into a patched state so
                                # true_probability_yes uses the EMOS-corrected mean.
                                # We do this by temporarily wrapping: pass corrected_mu_f
                                # and a stddev override without mutating the shared state.
                                from dataclasses import replace as _dc_replace
                                state = _dc_replace(state, corrected_mu_f=_mu_final)
                                emos_stddev_override = _sigma_cal
                                emos_mode_used = "emos_primary"

                if emos_stddev_override is not None:
                    # emos_stddev_override is already the EMOS-calibrated sigma (derived
                    # from _sigma_raw, which itself may already be ensemble_sigma_f-based
                    # -- see resolve_sigma_raw above). Pass use_ensemble_sigma=False so
                    # true_probability_yes uses this value as-is instead of re-overriding
                    # it with the raw state.ensemble_sigma_f (issue #448).
                    p_yes = true_probability_yes(bracket, state, mins_left, forecast_stddev=emos_stddev_override, deb_enabled=_deb_enabled, sigma_climb_fraction=sigma_climb_fraction, use_ensemble_sigma=False)
                else:
                    p_yes = true_probability_yes(bracket, state, mins_left, deb_enabled=_deb_enabled, sigma_climb_fraction=sigma_climb_fraction, use_ensemble_sigma=_use_ensemble_sigma)
                _snap_current_high = state.current_high_f
                _snap_latest_temp = state.latest_temp_f
                _snap_forecast_high = state.forecast_high_f

            raw_p_yes = p_yes
            # round() avoids IEEE 754 creep: 1.0-0.95 = 0.050000000000000044
            # which would silently fail the p_yes <= MAX_CONFIDENCE_YES_FOR_NO=0.05 gate.
            _cap_lower = round(1.0 - MODEL_PROB_CAP, 10)
            p_yes = min(max(p_yes, _cap_lower), MODEL_PROB_CAP)
            if raw_p_yes != p_yes and db is not None:
                db.log_guardrail_event(
                    ts, station, "cap_applied", raw_p_yes, p_yes, bracket.ticker
                )
            fee = estimate_fee_cents(min(bracket.yes_ask_cents, bracket.no_ask_cents))

            ev_yes = p_yes * 100 - bracket.yes_ask_cents - fee
            ev_no = (1 - p_yes) * 100 - bracket.no_ask_cents - fee
            # Raw (pre-clamp) edges -- logging + optional ranking only (issue #551).
            # Entry gates above/below always use ev_yes/ev_no (capped).
            ev_yes_raw = raw_p_yes * 100 - bracket.yes_ask_cents - fee
            ev_no_raw = (1 - raw_p_yes) * 100 - bracket.no_ask_cents - fee

            snap = {
                "ts": ts, "station": station, "ticker": bracket.ticker,
                "bracket_low": bracket.low_f, "bracket_high": bracket.high_f,
                "yes_ask": bracket.yes_ask_cents, "no_ask": bracket.no_ask_cents,
                "current_high": _snap_current_high, "latest_temp": _snap_latest_temp,
                "forecast_high": _snap_forecast_high, "p_yes": round(p_yes, 4),
                "raw_p_yes": round(raw_p_yes, 4), "capped_p_yes": round(p_yes, 4),
                "ev_yes": round(ev_yes, 2), "ev_no": round(ev_no, 2),
                "ev_yes_raw": round(ev_yes_raw, 2), "ev_no_raw": round(ev_no_raw, 2),
                "minutes_to_settlement": round(mins_left, 1),
                "emos_mode": emos_mode_used,
                "is_next_day": 1 if is_next_day_eval else 0,
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

            shadow_yes = not yes_enabled
            shadow_no = not no_enabled

            # Use looser shadow thresholds on the YES shadow path so the
            # shadow loop can collect data.  The NO branch is untouched.
            _yes_edge = shadow_yes_edge_min if shadow_yes else min_edge_cents
            _yes_conf = shadow_yes_conf_min if shadow_yes else MIN_CONFIDENCE_YES
            _yes_price = shadow_yes_price_min if shadow_yes else min_price_cents

            if ev_yes >= _yes_edge and p_yes >= _yes_conf and bracket.yes_ask_cents >= _yes_price:
                if ev_yes > max_edge_cents:
                    skipped_reason = "max_edge"
                    log.debug("[%s] -- SKIPPED %s: %s edge=%.2f¢ > MAX=%.2f¢",
                              station, skipped_reason, "YES", ev_yes, max_edge_cents)
                    skip_reason_counts[skipped_reason] += 1
                else:
                    candidate = Candidate(
                        station=station, bracket=bracket, side="YES",
                        edge_cents=ev_yes, price_cents=bracket.yes_ask_cents,
                        confidence=p_yes, p_yes=p_yes,
                        ev_yes=ev_yes, ev_no=ev_no,
                        minutes_to_settlement=mins_left, market=market,
                        # Issue #687: next-day candidates are always shadow-only
                        # -- no live entries under any circumstance -- regardless
                        # of the station's own yes_enabled override. The gate
                        # thresholds above (_yes_edge/_yes_conf/_yes_price) still
                        # use the station's real shadow_yes classification
                        # unchanged; only the final routing flag is forced here.
                        shadow=(shadow_yes or is_next_day_eval),
                        p_yes_raw=raw_p_yes, ev_yes_raw=ev_yes_raw, ev_no_raw=ev_no_raw,
                        is_next_day=is_next_day_eval,
                    )
            elif ev_no >= min_edge_cents and p_yes <= max_conf_yes_for_no and bracket.no_ask_cents >= min_price_cents:
                # Next-day: skip the margin gate rather than evaluate it against
                # `state.forecast_high_f`/`current_high_f` -- those are today's
                # observation-anchored values and have no bearing on a next-day
                # bracket (same safety property as the probability path itself).
                margin_gap = no_entry_margin_gap(bracket, state) if not is_next_day_eval else None
                if ev_no > max_edge_cents:
                    skipped_reason = "max_edge"
                    log.debug("[%s] -- SKIPPED %s: %s edge=%.2f¢ > MAX=%.2f¢",
                              station, skipped_reason, "NO", ev_no, max_edge_cents)
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
                        shadow=(shadow_no or is_next_day_eval),
                        p_yes_raw=raw_p_yes, ev_yes_raw=ev_yes_raw, ev_no_raw=ev_no_raw,
                        is_next_day=is_next_day_eval,
                    )
            else:
                # YES gates passed but NO gate failed (or both edges below MIN_EDGE_CENTS).
                # YES branch is now handled above unconditionally, so only NO failures
                # and low-edge cases reach here.
                if ev_no >= min_edge_cents:
                    if p_yes > max_conf_yes_for_no:
                        skipped_reason = "confidence_gate"
                        log.debug("[%s] -- SKIPPED %s: p_yes=%.4f > MAX=%.4f",
                                  station, skipped_reason, p_yes, max_conf_yes_for_no)
                    elif bracket.no_ask_cents < min_price_cents:
                        skipped_reason = "min_edge"
                        log.debug("[%s] -- SKIPPED %s: NO price=%.0f¢ < MIN=%.0f¢",
                                  station, skipped_reason, bracket.no_ask_cents, min_price_cents)
                    else:
                        skipped_reason = "min_edge"
                    skip_reason_counts[skipped_reason] += 1
                else:
                    # Both edges below MIN_EDGE_CENTS
                    skipped_reason = "min_edge"
                    log.debug("[%s] -- SKIPPED %s: max(%.2f¢, %.2f¢) < MIN=%.2f¢",
                              station, skipped_reason, ev_yes, ev_no, min_edge_cents)
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

    # -----------------------------------------------------------------------
    # Low-side scan (Epic C, shadow-only).  Only runs when weather_low is
    # provided — absent that dict no low-side state is available to score.
    # All low-side candidates are emitted with shadow=True regardless of any
    # station_overrides settings (promotion to live is tracked in #458).
    # -----------------------------------------------------------------------
    if weather_low and prob_low_fn is not None:
        for market in markets:
            try:
                is_low, station = is_lowest_temp_market(market)
                if not is_low:
                    continue
                if station not in weather_low:
                    skip_reason_counts["low_no_state"] += 1
                    continue

                mins_left = minutes_to_settlement(market)
                if mins_left < MIN_MINUTES_TO_SETTLEMENT:
                    skip_reason_counts["outside_window"] += 1
                    continue

                end_str = (
                    market.get("endDate") or market.get("end_date_iso")
                    or market.get("endDateIso") or market.get("close_time") or ""
                )
                if end_str:
                    try:
                        end_dt = dtparse.parse(str(end_str))
                        if end_dt.tzinfo is None:
                            end_dt = end_dt.replace(tzinfo=timezone.utc)
                        if end_dt.date() != datetime.now(timezone.utc).date():
                            skip_reason_counts["wrong_date"] += 1
                            continue
                    except Exception:
                        pass

                bracket = parse_bracket_from_market(market)
                if not bracket:
                    skip_reason_counts["bracket_parse_fail"] += 1
                    continue

                if ENABLE_CLOB_ENRICHMENT:
                    _enrich_from_clob(bracket, orderbooks=orderbooks)

                state_low = weather_low[station]
                p_yes = prob_low_fn(bracket, state_low, mins_left, FORECAST_STDDEV_F)
                raw_p_yes_low = p_yes
                # Issue #567 decision: clamp symmetrically to [1-cap, cap], matching the
                # high-side clamp above (~L744), instead of the upper-only clamp PR #564
                # deliberately left in place (that PR's scope excluded any live-gate
                # change; it flagged this asymmetry as a follow-up). No concrete reason
                # was found for the low side needing an unfloored small p, so we align
                # for consistency. This is still shadow-only (low-side scanner currently
                # records zero shadow trades per bug #554) so it has no live impact.
                _cap_lower_low = round(1.0 - MODEL_PROB_CAP, 10)
                p_yes = min(max(p_yes, _cap_lower_low), MODEL_PROB_CAP)

                ev_yes = (p_yes * 100 - bracket.yes_ask_cents) - estimate_fee_cents(bracket.yes_ask_cents)
                ev_no  = ((1 - p_yes) * 100 - bracket.no_ask_cents) - estimate_fee_cents(bracket.no_ask_cents)
                # Raw (pre-clamp) edges -- logging + optional ranking only (issue #551).
                ev_yes_raw = (raw_p_yes_low * 100 - bracket.yes_ask_cents) - estimate_fee_cents(bracket.yes_ask_cents)
                ev_no_raw  = ((1 - raw_p_yes_low) * 100 - bracket.no_ask_cents) - estimate_fee_cents(bracket.no_ask_cents)

                low_candidate = None
                if ev_no >= min_edge_cents and bracket.no_ask_cents >= min_price_cents:
                    if ev_no <= max_edge_cents:
                        low_candidate = Candidate(
                            station=station, bracket=bracket, side="NO",
                            edge_cents=ev_no, price_cents=bracket.no_ask_cents,
                            confidence=1 - p_yes, p_yes=p_yes,
                            ev_yes=ev_yes, ev_no=ev_no,
                            minutes_to_settlement=mins_left, market=market,
                            shadow=True,          # low-side is shadow-only this week
                            direction="low",
                            p_yes_raw=raw_p_yes_low, ev_yes_raw=ev_yes_raw, ev_no_raw=ev_no_raw,
                        )
                    else:
                        skip_reason_counts["max_edge"] += 1
                elif ev_yes >= min_edge_cents and p_yes >= MIN_CONFIDENCE_YES and bracket.yes_ask_cents >= min_price_cents:
                    if ev_yes <= max_edge_cents:
                        low_candidate = Candidate(
                            station=station, bracket=bracket, side="YES",
                            edge_cents=ev_yes, price_cents=bracket.yes_ask_cents,
                            confidence=p_yes, p_yes=p_yes,
                            ev_yes=ev_yes, ev_no=ev_no,
                            minutes_to_settlement=mins_left, market=market,
                            shadow=True,          # low-side is shadow-only this week
                            direction="low",
                            p_yes_raw=raw_p_yes_low, ev_yes_raw=ev_yes_raw, ev_no_raw=ev_no_raw,
                        )
                    else:
                        skip_reason_counts["max_edge"] += 1
                else:
                    skip_reason_counts["min_edge"] += 1

                if low_candidate:
                    candidates.append(low_candidate)
                    label = market.get("groupItemTitle") or f"{bracket.low_f:.0f}-{bracket.high_f:.0f}°F"
                    log.info(
                        "  ** FLAGGED LOW [%s] %s %s @ %sc edge=%.2fc p=%.2f%% (shadow)",
                        station, label, low_candidate.side, low_candidate.price_cents,
                        low_candidate.edge_cents, low_candidate.confidence * 100,
                    )

            except Exception as e:
                mid = (market.get("conditionId") or market.get("id") or "unknown")[:16]
                log.warning("[low-market] error processing %s...: %s, skipping", mid, e, exc_info=True)
                continue

    # Log summary at INFO level
    num_flagged = len(candidates)
    total_markets = len(markets)
    counts_str = ", ".join(f"{count} {reason}" for reason, count in skip_reason_counts.most_common())
    log.info("[scan] %d markets: %d flagged, %s", total_markets, num_flagged, counts_str)

    # RANK_ON_RAW_PROB (issue #551, default off): re-order candidates for execution
    # by the raw-probability-derived edge on their flagged side, instead of scan
    # order. Entry gates above are untouched either way -- this only changes which
    # candidate is preferred when capital/risk slots run out. Bit-identical to
    # today's behaviour while the flag is off (no sort is applied at all).
    if rank_on_raw_prob:
        candidates.sort(
            key=lambda c: c.ev_yes_raw if c.side == "YES" else c.ev_no_raw,
            reverse=True,
        )

    return candidates, snapshots

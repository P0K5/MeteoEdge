"""Intraday real-time bias correction for MeteoEdge.

Computes a corrected forecast mean by blending the DEB consensus forecast with
the observed deviation at the current observation time, scaled by a time-decay
factor.  Correction influence is 1.0 at market open and decays to 0.0 as the
peak window approaches.

The main entry point is ``compute_correction(city, state, db)``.
"""
import json
import logging
import os
from datetime import datetime, timezone

from src.config import STATIONS, get_source_priority
from src.model.deb_hourly_consensus import build_consensus
from src.model.deb_weighting import get_weights
from src.model.decay_functions import get_decay_factor

log = logging.getLogger(__name__)

# Fallback basis used when live DEB weights are unavailable (e.g. the
# get_weights() call fails, or the city has no STATIONS entry to resolve a
# DEB region from). Matches the historical hardcoded basis this module used
# before issue #572, so legacy rows and fallback rows share the same tag.
_FALLBACK_WEIGHTS: dict[str, float] = {"open_meteo": 1.0}

# Cities that use high-freq sources but have no METAR entry in STATIONS.
# Values are (lat, lon) tuples for build_consensus() calls.
_CITY_LAT_LON_OVERRIDES: dict[str, tuple[float, float]] = {
    "Tokyo": (35.5494, 139.7798),  # RJTT (Haneda Airport)
}

# European ICAO prefixes (E* = North/Central Europe, L* = South Europe / MENA
# border). Mirrors src.weather.builder._EU_ICAO_PREFIXES / _deb_station_region
# — duplicated here (rather than imported) because builder.py imports
# compute_correction from this module, so importing builder here would create
# a circular import.
_EU_ICAO_PREFIXES = frozenset(("E", "L"))


def _get_lat_lon(city: str) -> "tuple[float, float] | None":
    """Return (lat, lon) for city, checking overrides first then STATIONS."""
    if city in _CITY_LAT_LON_OVERRIDES:
        return _CITY_LAT_LON_OVERRIDES[city]
    for row in STATIONS:
        if row[3] == city:
            return row[1], row[2]
    return None


def _get_station_region(city: str) -> str:
    """Map *city* to its DEB registry region ("us", "eu", or "global").

    Mirrors ``src.weather.builder._deb_station_region``'s mapping rules:
    - unit="F" -> "us"  (NWS/HRRR/NBM are applicable)
    - unit="C" + European ICAO prefix -> "eu"  (ICON-EU is applicable)
    - unit="C" otherwise -> "global"

    Falls back to "global" when *city* has no STATIONS entry (e.g. cities
    resolved only via ``_CITY_LAT_LON_OVERRIDES``).
    """
    for row in STATIONS:
        if row[3] == city:
            station_code, unit = row[0], row[5]
            if unit == "F":
                return "us"
            if station_code[:1] in _EU_ICAO_PREFIXES:
                return "eu"
            return "global"
    return "global"


def _get_consensus_weights(city: str, db) -> dict[str, float]:
    """Return the DEB weights to use as the intraday consensus basis for *city*.

    Uses the city's live DEB weights (``get_weights``) so the hourly consensus
    curve tracks whatever blend DEB is currently producing for this city,
    rather than a hardcoded open_meteo-only basis (issue #572). Falls back to
    ``_FALLBACK_WEIGHTS`` only when weights are genuinely unavailable (the
    lookup raises, or returns a dict with no "open_meteo" entry at all).

    Note that DEB's own cold-start / disabled equal-weight dict is a valid,
    "available" result — it is passed through as-is, since that reflects
    DEB's actual current basis rather than an error condition.
    """
    station_region = _get_station_region(city)
    try:
        weights = get_weights(db, city, station_region=station_region)
    except Exception as exc:
        log.warning(
            "[intraday] get_weights failed for city=%s (region=%s): %s — "
            "falling back to open_meteo-only basis",
            city, station_region, exc,
        )
        return dict(_FALLBACK_WEIGHTS)

    if not weights or "open_meteo" not in weights:
        return dict(_FALLBACK_WEIGHTS)

    return weights


def _city_stations(city: str) -> list[str]:
    """Return all station codes associated with a city from STATIONS."""
    return [row[0] for row in STATIONS if row[3] == city]


def _get_cadence_for_city(city: str) -> "int | None":
    """Return the minimum cadence_min across all sources for a city."""
    sources = get_source_priority(city)
    if not sources:
        return None
    cadences = [s["cadence_min"] for s in sources if s.get("cadence_min")]
    return min(cadences) if cadences else None


def _interpolate_model_temp(
    consensus: "list[tuple[str, float]]", obs_time: datetime
) -> "float | None":
    """Linearly interpolate model temperature at obs_time from the hourly consensus path.

    consensus: list of (iso_time_str, temp_f) sorted by time ascending.
    obs_time: tz-aware UTC datetime.
    Returns interpolated temp_f, or None if obs_time is out of range.
    """
    if not consensus:
        return None

    # Parse all consensus times to datetimes
    parsed: list[tuple[datetime, float]] = []
    for t_str, t_val in consensus:
        try:
            dt = datetime.fromisoformat(t_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            parsed.append((dt, t_val))
        except ValueError:
            continue

    if not parsed:
        return None

    # Find bracketing points
    before = None
    after = None
    for dt, val in parsed:
        if dt <= obs_time:
            before = (dt, val)
        elif after is None:
            after = (dt, val)
            break

    if before is None and after is not None:
        return after[1]
    if after is None and before is not None:
        return before[1]
    if before is None:
        return None

    # Linear interpolation between before and after
    t0, v0 = before
    t1, v1 = after
    span = (t1 - t0).total_seconds()
    if span <= 0:
        return v0
    elapsed = (obs_time - t0).total_seconds()
    return v0 + (v1 - v0) * (elapsed / span)


def compute_correction(
    city: str,
    state,
    db,
    obs: "dict | None" = None,
) -> "float | None":
    """Compute intraday-corrected forecast mean for city.

    Args:
        city:  City name (e.g. "Tokyo").
        state: WeatherState — must have ``deb_mu_f`` set.
        db:    Database instance for obs lookup and correction storage.
        obs:   Optional pre-fetched observation dict.  When None, the latest
               observation is fetched from ``db`` using ``get_source_priority``.

    Returns:
        corrected_mu_f (float) or None if correction cannot be computed.
    """
    if not os.getenv("INTRADAY_CORRECTION_ENABLED", "true").lower() == "true":
        return None

    if state.deb_mu_f is None:
        return None

    now_utc = datetime.now(timezone.utc)

    # --- Fetch observation ---
    src_cfg: "dict | None" = None
    if obs is None:
        sources = get_source_priority(city)
        for _src_cfg in sources:
            candidate = db.get_latest_observation(_src_cfg["source"], _src_cfg["station"])
            if candidate is not None:
                obs = candidate
                src_cfg = _src_cfg
                cadence_min = _src_cfg.get("cadence_min")
                break
        else:
            return None
    else:
        cadence_min = _get_cadence_for_city(city)

    if obs is None:
        return None

    # --- Staleness check: age > 2× cadence_min → skip ---
    if cadence_min is not None:
        try:
            obs_dt = datetime.fromisoformat(obs["ts"])
            if obs_dt.tzinfo is None:
                obs_dt = obs_dt.replace(tzinfo=timezone.utc)
            else:
                obs_dt = obs_dt.astimezone(timezone.utc)
            age_minutes = (now_utc - obs_dt).total_seconds() / 60.0
            if age_minutes > 2 * cadence_min:
                return None
        except (ValueError, KeyError):
            return None
    else:
        try:
            obs_dt = datetime.fromisoformat(obs["ts"])
            if obs_dt.tzinfo is None:
                obs_dt = obs_dt.replace(tzinfo=timezone.utc)
            else:
                obs_dt = obs_dt.astimezone(timezone.utc)
        except (ValueError, KeyError):
            return None

    obs_temp_f: float = obs["temp_f"]

    # --- Build DEB hourly consensus for model interpolation ---
    lat_lon = _get_lat_lon(city)
    if lat_lon is None:
        return None

    lat, lon = lat_lon
    basis_weights = _get_consensus_weights(city, db)
    consensus = build_consensus(lat, lon, weights=basis_weights)
    if not consensus:
        return None

    model_temp_f = _interpolate_model_temp(consensus, obs_dt)
    if model_temp_f is None:
        return None

    # --- Compute deviation and apply decay ---
    delta_f = obs_temp_f - model_temp_f
    decay_factor = get_decay_factor(city, obs_dt)

    corrected_mu_f = state.deb_mu_f + delta_f * decay_factor

    # --- Persist to DB ---
    date_str = obs_dt.astimezone(timezone.utc).date().isoformat()
    obs_time_str = obs_dt.isoformat()
    db.upsert_intraday_correction(
        city=city,
        station=src_cfg["station"] if src_cfg else "",
        source=src_cfg["source"] if src_cfg else "",
        date=date_str,
        obs_time=obs_time_str,
        obs_temp_f=obs_temp_f,
        model_temp_f=model_temp_f,
        delta_f=delta_f,
        corrected_mu_f=corrected_mu_f,
        decay_factor=decay_factor,
        basis_weights=json.dumps(basis_weights, sort_keys=True),
    )

    return corrected_mu_f

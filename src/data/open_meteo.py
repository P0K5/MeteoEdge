"""Open-Meteo secondary forecast source.

Used as a weighted secondary input (40%) alongside NWS (60%) in the ensemble.
Cached 30 min — Open-Meteo updates hourly and the free tier caps at 10,000 req/day.

GFS model:
The Open-Meteo API also exposes NOAA's Global Forecast System (GFS) as a distinct
model via the `models=gfs_seamless` parameter.  GFS is globally available (unlike
NWS, which is US-only) and gives international stations a genuine second NWP source
for DEB/EMOS ensemble weighting.  It is fetched as a separate call so its forecast
is always logged under model="gfs" in model_forecast_log, independent of the
default best-match open_meteo entry.
"""
from datetime import datetime, timedelta, timezone

import logging

from src.http_client import cached_fetch_json

log = logging.getLogger(__name__)


def _fetch_open_meteo_hourly(lat: float, lon: float) -> dict | None:
    """Fetch raw Open-Meteo hourly payload for the given coordinates.

    Cached 30 min per coordinate pair.
    Returns None if unavailable.
    """
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m,weather_code"
        f"&temperature_unit=fahrenheit&timezone=auto"
    )
    return cached_fetch_json(url, ttl_minutes=30)


def _fetch_open_meteo_gfs_hourly(lat: float, lon: float) -> dict | None:
    """Fetch raw Open-Meteo GFS hourly payload for the given coordinates.

    Uses Open-Meteo's ``models=gfs_seamless`` parameter, which requests NOAA's
    Global Forecast System.  GFS is globally available (unlike NWS), making it
    the preferred second model for international stations.

    Cached 30 min per coordinate pair.
    Returns None if unavailable.
    """
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m"
        f"&models=gfs_seamless"
        f"&temperature_unit=fahrenheit&timezone=auto"
    )
    return cached_fetch_json(url, ttl_minutes=30)


def fetch_secondary_forecast(lat: float, lon: float) -> float | None:
    """Fetch forecast daily high (°F) from Open-Meteo for the given coordinates.

    Cached 30 min per coordinate pair.
    Returns None if unavailable.
    """
    data = _fetch_open_meteo_hourly(lat, lon)
    if not data:
        return None
    try:
        temps = data["hourly"]["temperature_2m"][:24]
        return max(temps) if temps else None
    except Exception as e:
        log.warning("[open-meteo] parse error for (%s,%s): %s", lat, lon, e)
        return None


def fetch_gfs_forecast_high(lat: float, lon: float) -> float | None:
    """Fetch GFS forecast daily high (°F) from Open-Meteo for the given coordinates.

    Requests the Open-Meteo GFS seamless model (``models=gfs_seamless``).  This
    is a globally-available NWP source that works for international stations where
    NWS data is unavailable, giving DEB/EMOS a genuine second model to compare
    against the default open_meteo (best-match) forecast.

    Cached 30 min per coordinate pair.
    Returns None if unavailable.
    """
    data = _fetch_open_meteo_gfs_hourly(lat, lon)
    if not data:
        return None
    try:
        temps = data["hourly"]["temperature_2m"][:24]
        valid = [t for t in temps if t is not None]
        return max(valid) if valid else None
    except Exception as e:
        log.warning("[open-meteo/gfs] parse error for (%s,%s): %s", lat, lon, e)
        return None


def fetch_open_meteo_with_spread(
    lat: float, lon: float, lead_hours: int
) -> "tuple[float, float] | None":
    """Fetch Open-Meteo forecast (mu_f, sigma_f) for the given coordinates.

    Queries multiple constituent models and returns the mean daily-high as mu_f
    and the cross-model standard deviation as sigma_f.  At least two models must
    return valid values; otherwise returns None.

    Models queried: ecmwf_ifs04, gfs_seamless, jma_seamless, best_match.
    The `lead_hours` parameter is accepted for API consistency with other
    with_spread fetchers; the multi-model spread is derived from the current
    forecast horizon and is not directly keyed on lead_hours.

    Args:
        lat:        Latitude.
        lon:        Longitude.
        lead_hours: Lead time (hours) — used to select the forecast window.
                    24 → tomorrow's max; ≤18 → today's remaining window.

    Returns:
        (mu_f, sigma_f) in °F, or None if fewer than two models respond.
    """
    import math as _math

    models = ["ecmwf_ifs04", "gfs_seamless", "jma_seamless", "best_match"]
    # Determine the day offset: lead≥20h → tomorrow, else today
    day_offset = 1 if lead_hours >= 20 else 0
    window_start = day_offset * 24   # index into the 168-hour hourly forecast
    window_end = window_start + 24

    highs: list[float] = []
    for model_name in models:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&hourly=temperature_2m"
            f"&models={model_name}"
            f"&temperature_unit=fahrenheit&timezone=UTC"
            f"&forecast_days=2"
        )
        data = cached_fetch_json(url, ttl_minutes=30)
        if not data:
            continue
        try:
            temps = data["hourly"]["temperature_2m"][window_start:window_end]
            valid = [t for t in temps if t is not None]
            if valid:
                highs.append(max(valid))
        except Exception as e:
            log.debug("[open-meteo/spread] model=%s parse error (%s,%s): %s", model_name, lat, lon, e)

    if len(highs) < 2:
        log.warning(
            "[open-meteo/spread] insufficient model responses (%d/4) for (%s,%s)", len(highs), lat, lon
        )
        return None

    mu_f = sum(highs) / len(highs)
    variance = sum((h - mu_f) ** 2 for h in highs) / (len(highs) - 1)
    sigma_f = _math.sqrt(variance)
    return float(mu_f), float(sigma_f)


def fetch_gfs_with_spread(
    lat: float, lon: float, lead_hours: int
) -> "tuple[float, None] | None":
    """Fetch a genuine single-model GFS forecast (mu_f) via Open-Meteo.

    Unlike fetch_open_meteo_with_spread() (which averages across several
    constituent models: ecmwf_ifs04, gfs_seamless, jma_seamless, best_match),
    this issues its own request scoped to ``models=gfs_seamless`` only, so the
    "gfs" channel in model_forecast_log reflects NOAA's Global Forecast System
    in isolation rather than a duplicate of the open_meteo multi-model blend
    (see issue #548).

    sigma_f is always returned as None: GFS here is a single deterministic NWP
    run, not an ensemble, so there is no cross-member spread to compute. This
    mirrors how the ecmwf/icon/hrrr/nbm deterministic channels persist NULL
    sigma_f in model_forecast_log (see src/scripts/capture_forecasts.py). Do
    not synthesize a placeholder sigma here — the committed per-channel
    sigma-sourcing decision table (derive vs. NULL, with rationale) lives in
    the module docstring of src/scripts/capture_forecasts.py and
    docs/OPERATIONS.md → "Architectural Decisions" (issue #555).

    Args:
        lat:        Latitude.
        lon:        Longitude.
        lead_hours: Lead time (hours) — used to select the forecast window.
                    24 → tomorrow's max; <20 → today's remaining window.

    Returns:
        (mu_f, None) in °F, or None if the GFS model is unavailable.
    """
    # Determine the day offset: lead≥20h → tomorrow, else today
    day_offset = 1 if lead_hours >= 20 else 0
    window_start = day_offset * 24   # index into the 168-hour hourly forecast
    window_end = window_start + 24

    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m"
        f"&models=gfs_seamless"
        f"&temperature_unit=fahrenheit&timezone=UTC"
        f"&forecast_days=2"
    )
    data = cached_fetch_json(url, ttl_minutes=30)
    if not data:
        log.warning("[open-meteo/gfs/spread] no response for (%s,%s)", lat, lon)
        return None

    try:
        temps = data["hourly"]["temperature_2m"][window_start:window_end]
        valid = [t for t in temps if t is not None]
        if not valid:
            log.warning(
                "[open-meteo/gfs/spread] no valid temperatures for (%s,%s)", lat, lon
            )
            return None
        mu_f = max(valid)
    except Exception as e:
        log.debug("[open-meteo/gfs/spread] parse error (%s,%s): %s", lat, lon, e)
        return None

    return float(mu_f), None


def fetch_hourly_temp_now(lat: float, lon: float) -> float | None:
    """Return Open-Meteo hourly temperature (°F) for the nearest past hour, now.

    ``_fetch_open_meteo_hourly()`` requests ``timezone=auto``, so
    ``hourly.time[]`` timestamps are naive and expressed in the station's
    *local* time, not UTC (contrast the explicit ``timezone=UTC`` fetches
    used by ``fetch_open_meteo_with_spread``/``fetch_gfs_with_spread``).  The
    response also carries ``utc_offset_seconds`` for that same local time.
    We use it to convert each naive local timestamp to its true UTC instant
    before comparing against "now" — otherwise, for any station not on UTC,
    this silently selects a temperature several hours off from the real
    current hour (see issue #810).

    Returns None if unavailable.
    """
    data = _fetch_open_meteo_hourly(lat, lon)
    if not data:
        return None
    try:
        times = data["hourly"]["time"]
        temps = data["hourly"]["temperature_2m"]
        utc_offset_seconds = data.get("utc_offset_seconds", 0) or 0
        now_utc = datetime.now(timezone.utc)
        best_temp = None
        for t_str, t_val in zip(times, temps):
            t_dt = datetime.fromisoformat(t_str)
            if t_dt.tzinfo is None:
                # t_dt is a naive *local* timestamp. Stamping it as UTC and
                # then subtracting the station's UTC offset yields the true
                # UTC instant it represents (local - offset = UTC).
                t_dt = t_dt.replace(tzinfo=timezone.utc) - timedelta(seconds=utc_offset_seconds)
            if t_dt <= now_utc and t_val is not None:
                best_temp = float(t_val)
        return best_temp
    except Exception as e:
        log.warning("[open-meteo] hourly parse error for (%s,%s): %s", lat, lon, e)
        return None

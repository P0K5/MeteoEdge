"""DEB hourly consensus temperature path builder."""
import os
from datetime import datetime, timedelta, timezone
from src.data.open_meteo import _fetch_open_meteo_hourly

# Sources that provide hourly temperature resolution. NWS/HRRR/NBM have
# no hourly path today; add keys here when they do.
_HOURLY_CAPABLE_SOURCES: frozenset[str] = frozenset({"open_meteo"})

# Rolling window (hours, each direction) used to select "current" slots from
# the Open-Meteo multi-day hourly payload. Replaces a local-date-string match
# (see issue #1525) — a date-string comparison mixes the station's *local*
# calendar day against "today in UTC", which can silently drop valid hours
# near the UTC-day boundary for stations far from UTC.
_CONSENSUS_WINDOW_HOURS = 36


def build_consensus(
    lat: float, lon: float, weights: dict[str, float]
) -> list[tuple[str, float]] | None:
    """Return DEB-weighted hourly temperature path around now (UTC).

    ``_fetch_open_meteo_hourly()`` requests ``timezone=auto``, so the
    response's ``hourly.time[]`` entries are naive strings in the station's
    *local* time, not UTC (the response also carries ``utc_offset_seconds``
    for that same local time). This function converts each naive-local
    timestamp to its true UTC instant (local - offset = UTC) before
    returning it — mirroring ``fetch_hourly_temp_now()``'s #810/#818 fix —
    so callers (e.g. ``_interpolate_model_temp()`` in
    ``intraday_correction.py``) always receive clean, unambiguous UTC
    instants and need no tz-handling logic of their own (issue #1525).

    Slot selection also switched from a local-date-string match to a rolling
    ``±_CONSENSUS_WINDOW_HOURS`` window around "now" in UTC, for the same
    reason: a date-string match compares a local-labelled string against a
    UTC "today", which can drop valid hours near the UTC-day boundary for
    stations far from UTC.

    Renormalises the DEB weights over the subset of hourly-capable sources
    (see ``_HOURLY_CAPABLE_SOURCES``; currently only Open-Meteo — NWS has no
    hourly resolution) so the returned temperatures are on the correct
    absolute scale regardless of the full weight dict.

    To extend for a future hourly source: add its key to
    ``_HOURLY_CAPABLE_SOURCES`` and add a fetch + blend step here.

    Returns list of (iso_time_str, temp_f) for slots within
    ``_CONSENSUS_WINDOW_HOURS`` of now (UTC), with timestamps already
    converted to true UTC instants, or None.
    """
    data = _fetch_open_meteo_hourly(lat, lon)
    if not data:
        return None
    try:
        times = data["hourly"]["time"]
        temps = data["hourly"]["temperature_2m"]
        utc_offset_seconds = data.get("utc_offset_seconds", 0) or 0
        # Renormalise DEB weights over the hourly-capable subset so the returned
        # temperatures are on the correct absolute scale regardless of the full weight dict.
        hourly_total = sum(weights.get(s, 0.0) for s in _HOURLY_CAPABLE_SOURCES)
        om_scale = (weights.get("open_meteo", 1.0) / hourly_total) if hourly_total > 0.0 else 1.0
        now_utc = datetime.now(timezone.utc)
        window = timedelta(hours=_CONSENSUS_WINDOW_HOURS)
        result = []
        for t_str, t_val in zip(times, temps):
            if t_val is None:
                continue
            t_dt = datetime.fromisoformat(t_str)
            if t_dt.tzinfo is None:
                # t_dt is a naive *local* timestamp. Stamping it as UTC and
                # then subtracting the station's UTC offset yields the true
                # UTC instant it represents (local - offset = UTC).
                t_dt = t_dt.replace(tzinfo=timezone.utc) - timedelta(seconds=utc_offset_seconds)
            else:
                t_dt = t_dt.astimezone(timezone.utc)
            if abs(t_dt - now_utc) > window:
                continue
            result.append((t_dt.isoformat(), float(t_val) * om_scale))
        return result if result else None
    except Exception:
        return None


def compute_deb_mu_f(
    forecast_nws: float | None,
    forecast_open_meteo: float | None,
    weights: dict[str, float],
    forecast_gfs: float | None = None,
    forecast_hrrr: float | None = None,
    forecast_nbm: float | None = None,
    forecast_ecmwf: float | None = None,
    forecast_icon: float | None = None,
    station_region: str = "us",
) -> float | None:
    """Return DEB-weighted forecast high (°F).

    Replaces the static 60/40 ensemble_forecast() blend when DEB_ENABLED=true.
    Supports up to seven models: nws, open_meteo, gfs, hrrr, nbm, ecmwf, icon.

    When a model forecast is None its contribution is dropped and the remaining
    available forecasts are renormalised to sum to 1.0.  This means:
    - International stations without NWS/HRRR/NBM data automatically use a two-model
      (open_meteo + gfs) blend.
    - Any station where only one source is available receives that source at
      full weight rather than returning None.
    - 4-model consensus (nws + open_meteo + hrrr + nbm) is used when HRRR and NBM
      are both available; degrades gracefully when either is missing.
    - ECMWF and ICON are used for international stations; ECMWF data for US stations
      triggers a warning and is excluded.

    Returns None only when all inputs are None.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    # Guard: ECMWF should not be used for US stations.
    if forecast_ecmwf is not None and station_region == "us":
        _log.warning(
            "[deb] ECMWF forecast provided for US station (region=%r) — "
            "ECMWF is a global model but should not be used for US stations; "
            "excluding from ensemble.",
            station_region,
        )
        forecast_ecmwf = None

    # Build the set of available (weight, value) pairs.
    available: list[tuple[float, float]] = []
    pairs = [
        ("nws", forecast_nws),
        ("open_meteo", forecast_open_meteo),
        ("gfs", forecast_gfs),
        ("hrrr", forecast_hrrr),
        ("nbm", forecast_nbm),
        ("ecmwf", forecast_ecmwf),
        ("icon", forecast_icon),
    ]
    for key, value in pairs:
        if value is not None:
            w = weights.get(key, 0.0)
            available.append((w, value))

    if not available:
        return None

    # If weights are all zero (e.g. fallback equal-weight dict doesn't carry gfs),
    # treat as equal weight among available models.
    total_w = sum(w for w, _ in available)
    if total_w == 0.0:
        return sum(v for _, v in available) / len(available)

    # Weighted average, renormalised so available models sum to 1.0.
    return sum(w * v for w, v in available) / total_w

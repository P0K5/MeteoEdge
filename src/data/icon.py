"""DWD ICON-EU hourly 2-m temperature ingestion for European stations.

Fetches hourly 2-m temperature forecasts from the ICON-EU model (Deutscher
Wetterdienst) for forecast hours 1 through 24.  ICON-EU only covers Europe;
stations outside the approximate bounding box (lat 29–72, lon -25 to 45)
return an empty list immediately without any network calls.

All GRIB2 I/O is delegated to :mod:`src.data.grib_cache`; this module never
imports herbie directly except for cycle resolution (where grib_cache's
_resolve_latest_cycle only covers hourly cadences, not 6h-cadence models).

ICON-EU cycles: 00/06/12/18 UTC (4x daily, matching NBM/NBM cadence).
DWD HTTP endpoint: https://opendata.dwd.de/weather/nwp/icon-eu/grib/
Herbie model name: "icon-eu" (herbie >= 2023.3).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.data.hrrr import HourlyTemp

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ICON-EU domain bounds (approximate)
# ---------------------------------------------------------------------------

_EU_LAT_MIN = 29.0
_EU_LAT_MAX = 72.0
_EU_LON_MIN = -25.0
_EU_LON_MAX = 45.0

# Forecast hours to fetch (F01 … F24)
_FORECAST_HOURS = list(range(1, 25))

# Cycle cadence
_ICON_CYCLE_STEP_H = 6
_ICON_MAX_LOOKBACK_CYCLES = 4  # look back up to 24 hours


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_eu_domain(lat: float, lon: float) -> bool:
    """Return True if (lat, lon) falls within the approximate ICON-EU domain."""
    return (
        _EU_LAT_MIN <= lat <= _EU_LAT_MAX
        and _EU_LON_MIN <= lon <= _EU_LON_MAX
    )


def _kelvin_to_fahrenheit(kelvin: float) -> float:
    """Convert temperature from Kelvin to degrees Fahrenheit."""
    return (kelvin - 273.15) * 9.0 / 5.0 + 32.0


def _floor_to_icon_cycle(dt: datetime) -> datetime:
    """Return dt floored to the nearest past ICON-EU cycle (00/06/12/18 UTC)."""
    hour = (dt.hour // _ICON_CYCLE_STEP_H) * _ICON_CYCLE_STEP_H
    return dt.replace(hour=hour, minute=0, second=0, microsecond=0)


def _resolve_icon_cycle(fxx: int = 1) -> Optional[datetime]:
    """Return the most recent available ICON-EU cycle datetime (UTC).

    Steps back in 6-hour increments up to _ICON_MAX_LOOKBACK_CYCLES times,
    checking herbie's IDX availability for each candidate cycle.

    Args:
        fxx: Forecast hour to test availability against (default 1).

    Returns:
        UTC datetime of the latest available ICON-EU cycle, or None if none
        found within the lookback window.
    """
    try:
        from herbie import Herbie  # type: ignore[import]
    except ImportError as exc:
        raise ImportError("herbie-data is required: pip install herbie-data") from exc

    now_utc = datetime.now(timezone.utc)
    candidate = _floor_to_icon_cycle(now_utc)

    for attempt in range(_ICON_MAX_LOOKBACK_CYCLES + 1):
        try:
            H = Herbie(candidate, model="icon-eu", fxx=fxx, verbose=False)
            _ = H.idx  # raises if cycle not yet published
            log.debug("[icon] resolved cycle: %s (attempt %d)", candidate, attempt)
            return candidate
        except Exception:
            log.debug("[icon] cycle %s not available, stepping back 6h", candidate)
            candidate = candidate - timedelta(hours=_ICON_CYCLE_STEP_H)

    log.warning(
        "[icon] no available ICON-EU cycle found in last %dh",
        _ICON_MAX_LOOKBACK_CYCLES * _ICON_CYCLE_STEP_H,
    )
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_icon_hourly(
    lat: float,
    lon: float,
    station: Optional[str] = None,
) -> list[HourlyTemp]:
    """Fetch ICON-EU hourly 2-m temperature forecasts for the next 24 forecast hours.

    Resolves the latest available ICON-EU cycle (00/06/12/18 UTC), then fetches
    t_2m (2-m temperature in Kelvin) at forecast hours F01 through F24.
    Temperatures are converted from Kelvin to °F.

    Results are keyed to the cycle so the on-disk grib_cache acts as a
    per-cycle cache with a configurable TTL (default 6 h, aligns with ICON-EU
    cycle cadence).

    Args:
        lat:     Latitude (WGS-84).
        lon:     Longitude (WGS-84).
        station: Optional station identifier for logging only.

    Returns:
        List of :class:`~src.data.hrrr.HourlyTemp` instances (F01 … F24), or
        an empty list if:
        - The station is outside the ICON-EU domain (lat/lon bounds check).
        - No ICON-EU cycle is currently available.
        - All forecast-hour fetches fail.
    """
    label = station or f"({lat:.4f},{lon:.4f})"

    # EU domain bounds check — ICON-EU does not cover areas outside these bounds.
    if not _is_eu_domain(lat, lon):
        log.debug(
            "[icon] %s is outside ICON-EU domain (lat 29–72, lon -25 to 45) — skipping",
            label,
        )
        return []

    from src.data import grib_cache as _grib_cache

    # Resolve the latest available ICON-EU cycle.
    cycle_dt = _resolve_icon_cycle(fxx=1)
    if cycle_dt is None:
        log.warning("[icon] no available ICON-EU cycle found for %s", label)
        return []

    log.info("[icon] using cycle %s for %s", cycle_dt.strftime("%Y-%m-%dT%HZ"), label)

    ttl_hours = _grib_cache._get_cache_ttl_hours()
    cache_dir = _grib_cache._get_cache_dir()

    results: list[HourlyTemp] = []
    for fxx in _FORECAST_HOURS:
        path = _grib_cache._fetch_grib_slice(
            "icon-eu", "TMP_2m", cycle_dt, fxx, cache_dir, ttl_hours
        )
        if path is None:
            log.debug("[icon] fxx=%02d unavailable for %s", fxx, label)
            continue
        kelvin = _grib_cache._read_grib_nearest(path, lat, lon)
        if kelvin is None:
            log.debug("[icon] fxx=%02d read returned None for %s", fxx, label)
            continue
        temp_f = _kelvin_to_fahrenheit(kelvin)
        valid_time = cycle_dt + timedelta(hours=fxx)
        results.append(HourlyTemp(ts_utc=valid_time, temp_f=temp_f))

    log.info(
        "[icon] %s — cycle %s — retrieved %d/%d forecast hours",
        label,
        cycle_dt.strftime("%Y-%m-%dT%HZ"),
        len(results),
        len(_FORECAST_HOURS),
    )
    return results

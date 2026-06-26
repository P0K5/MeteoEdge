"""HRRR hourly 2-m temperature ingestion for US (CONUS) stations.

Fetches hourly 2-m temperature forecasts from the High-Resolution Rapid Refresh
(HRRR) model for forecast hours 1 through 18.  HRRR only covers CONUS; stations
outside the approximate bounding box (lat 20–55, lon -130 to -60) return an
empty list immediately without any network calls.

All GRIB2 I/O is delegated to :mod:`src.data.grib_cache`; this module never
imports herbie directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CONUS bounding box (approximate)
# ---------------------------------------------------------------------------

_CONUS_LAT_MIN = 20.0
_CONUS_LAT_MAX = 55.0
_CONUS_LON_MIN = -130.0
_CONUS_LON_MAX = -60.0

# Forecast hours to fetch (F01 … F18)
_FORECAST_HOURS = list(range(1, 19))


# ---------------------------------------------------------------------------
# Data type
# ---------------------------------------------------------------------------

@dataclass
class HourlyTemp:
    """Single hourly temperature forecast from HRRR.

    Attributes:
        ts_utc: Valid time of the forecast (UTC, timezone-aware).
        temp_f: 2-m air temperature in degrees Fahrenheit.
    """

    ts_utc: datetime
    temp_f: float


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_hrrr_hourly(
    lat: float,
    lon: float,
    station: Optional[str] = None,
) -> list[HourlyTemp]:
    """Fetch HRRR hourly 2-m temperature forecasts for the next 18 forecast hours.

    Resolves the latest available HRRR cycle, then fetches TMP_2m at forecast
    hours F01 through F18.  Temperatures are converted from Kelvin to °F.
    Results are keyed to the cycle so the on-disk grib_cache acts as a
    per-cycle cache with a configurable TTL (default 6 h, aligns with HRRR
    hourly init cadence).

    Args:
        lat:     Latitude (WGS-84).
        lon:     Longitude (WGS-84).
        station: Optional station identifier for logging only.

    Returns:
        List of :class:`HourlyTemp` instances (F01 … F18), or an empty list if:
        - The station is outside CONUS (lat/lon bounds check).
        - No HRRR cycle is currently available.
        - All forecast-hour fetches fail.
    """
    label = station or f"({lat:.4f},{lon:.4f})"

    # CONUS bounds check — HRRR does not cover areas outside these bounds.
    if not _is_conus(lat, lon):
        log.info("[hrrr] %s is outside CONUS bounds — returning empty list", label)
        return []

    from src.data.grib_cache import fetch_hrrr_field, _resolve_latest_cycle

    # Resolve the latest available cycle (grib_cache handles fallback internally).
    cycle_dt = _resolve_latest_cycle("hrrr")
    if cycle_dt is None:
        log.warning("[hrrr] no available HRRR cycle found for %s", label)
        return []

    log.info("[hrrr] using cycle %s for %s", cycle_dt.strftime("%Y-%m-%dT%HZ"), label)

    results: list[HourlyTemp] = []
    for fxx in _FORECAST_HOURS:
        kelvin = fetch_hrrr_field("TMP_2m", lat, lon, fxx=fxx)
        if kelvin is None:
            log.debug("[hrrr] fxx=%02d unavailable for %s", fxx, label)
            continue
        temp_f = _kelvin_to_fahrenheit(kelvin)
        valid_time = cycle_dt + timedelta(hours=fxx)
        results.append(HourlyTemp(ts_utc=valid_time, temp_f=temp_f))

    log.info(
        "[hrrr] %s — cycle %s — retrieved %d/%d forecast hours",
        label,
        cycle_dt.strftime("%Y-%m-%dT%HZ"),
        len(results),
        len(_FORECAST_HOURS),
    )
    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_conus(lat: float, lon: float) -> bool:
    """Return True if (lat, lon) falls within the approximate CONUS bounding box."""
    return (
        _CONUS_LAT_MIN <= lat <= _CONUS_LAT_MAX
        and _CONUS_LON_MIN <= lon <= _CONUS_LON_MAX
    )


def _kelvin_to_fahrenheit(kelvin: float) -> float:
    """Convert temperature from Kelvin to degrees Fahrenheit."""
    return (kelvin - 273.15) * 9.0 / 5.0 + 32.0

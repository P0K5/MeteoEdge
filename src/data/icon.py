"""DWD ICON-EU hourly 2-m temperature ingestion for European stations.

Fetches hourly 2-m temperature forecasts from the ICON-EU model (Deutscher
Wetterdienst) for forecast hours 1 through 24.  ICON-EU only covers Europe;
stations outside the approximate bounding box (lat 29–72, lon -25 to 45)
return an empty list immediately without any network calls.

All GRIB2 I/O is delegated to :mod:`src.data.grib_cache`; this module
downloads bzip2-compressed GRIB2 files directly from DWD opendata via httpx
and decompresses them before handing off to grib_cache for parsing.

DWD HTTP endpoint:
  https://opendata.dwd.de/weather/nwp/icon-eu/grib/{HH}/{var}/
  where HH = 00/06/12/18, var = t_2m

File naming pattern:
  icon-eu_europe_regular-lat-lon_single-level_YYYYMMDDCC_FFF_T_2M.grib2.bz2

ICON-EU cycles: 00/06/12/18 UTC (4x daily, matching NBM cadence).
"""

from __future__ import annotations

import bz2
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
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

_DWD_BASE = "https://opendata.dwd.de/weather/nwp/icon-eu/grib"


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
    return (kelvin - 273.15) * 9.0 / 5.0 + 32.0


def _floor_to_icon_cycle(dt: datetime) -> datetime:
    """Return dt floored to the nearest past ICON-EU cycle (00/06/12/18 UTC)."""
    hour = (dt.hour // _ICON_CYCLE_STEP_H) * _ICON_CYCLE_STEP_H
    return dt.replace(hour=hour, minute=0, second=0, microsecond=0)


def _dwd_filename(cycle_dt: datetime, fxx: int) -> str:
    """Build the DWD ICON-EU filename for a given cycle and forecast hour."""
    date_str = cycle_dt.strftime("%Y%m%d")
    cc = f"{cycle_dt.hour:02d}"
    fff = f"{fxx:03d}"
    return f"icon-eu_europe_regular-lat-lon_single-level_{date_str}{cc}_{fff}_T_2M.grib2.bz2"


def _dwd_url(cycle_dt: datetime, fxx: int) -> str:
    hh = f"{cycle_dt.hour:02d}"
    filename = _dwd_filename(cycle_dt, fxx)
    return f"{_DWD_BASE}/{hh}/t_2m/{filename}"


def _resolve_icon_cycle() -> Optional[datetime]:
    """Return the most recent available ICON-EU cycle datetime (UTC).

    Steps back in 6-hour increments up to _ICON_MAX_LOOKBACK_CYCLES times,
    issuing an HTTP HEAD request for the F001 file of each candidate cycle
    to confirm the cycle is published on DWD opendata.

    Returns:
        UTC datetime (tz-naive) of the latest available cycle, or None.
    """
    try:
        import httpx  # type: ignore[import]
    except ImportError as exc:
        raise ImportError("httpx is required: pip install httpx") from exc

    now_utc = datetime.now(timezone.utc)
    candidate = _floor_to_icon_cycle(now_utc).replace(tzinfo=None)

    for attempt in range(_ICON_MAX_LOOKBACK_CYCLES + 1):
        url = _dwd_url(candidate, fxx=1)
        try:
            r = httpx.head(url, timeout=10, follow_redirects=True)
            if r.status_code == 200:
                log.debug("[icon] resolved cycle: %s (attempt %d)", candidate, attempt)
                return candidate
            log.debug("[icon] cycle %s not yet published (HTTP %d), stepping back", candidate, r.status_code)
        except Exception as exc:
            log.debug("[icon] cycle %s HEAD failed: %s, stepping back", candidate, exc)
        candidate = candidate - timedelta(hours=_ICON_CYCLE_STEP_H)

    log.warning(
        "[icon] no available ICON-EU cycle found in last %dh",
        _ICON_MAX_LOOKBACK_CYCLES * _ICON_CYCLE_STEP_H,
    )
    return None


def _fetch_icon_grib(
    cycle_dt: datetime,
    fxx: int,
    cache_dir: Path,
    ttl_hours: float,
) -> Optional[Path]:
    """Download and decompress a single ICON-EU GRIB2 slice, with caching.

    Downloads the bzip2-compressed file from DWD opendata, decompresses it,
    and writes the raw GRIB2 to cache_dir.  Returns the path on success.
    """
    from src.data.grib_cache import _cache_key, _cache_path, _is_cache_valid

    key = _cache_key("icon", "ICON_T2M", cycle_dt, fxx)
    path = _cache_path(cache_dir, key)

    if _is_cache_valid(path, ttl_hours):
        log.debug("[icon] cache hit: %s", key)
        return path

    url = _dwd_url(cycle_dt, fxx)
    log.info("[icon] fetching fxx=%03d from %s", fxx, url)

    try:
        import httpx  # type: ignore[import]
    except ImportError as exc:
        raise ImportError("httpx is required: pip install httpx") from exc

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
        raw_bz2 = resp.content
        grib_bytes = bz2.decompress(raw_bz2)
        path.write_bytes(grib_bytes)
        log.info("[icon] cached fxx=%03d → %s (%d bytes)", fxx, path, len(grib_bytes))
        return path
    except Exception as exc:
        log.warning("[icon] fetch failed fxx=%03d: %s", fxx, exc)
        path.unlink(missing_ok=True)
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

    Resolves the latest available ICON-EU cycle (00/06/12/18 UTC) via HTTP HEAD
    against DWD opendata, then downloads bzip2-compressed GRIB2 files for
    forecast hours F01 through F24, decompresses them, and extracts the
    nearest-neighbour temperature value at (lat, lon).

    Args:
        lat:     Latitude (WGS-84).
        lon:     Longitude (WGS-84).
        station: Optional station identifier for logging only.

    Returns:
        List of :class:`~src.data.hrrr.HourlyTemp` instances (F01 … F24), or
        an empty list if the station is outside the ICON-EU domain, no cycle
        is currently available, or all forecast-hour fetches fail.
    """
    label = station or f"({lat:.4f},{lon:.4f})"

    if not _is_eu_domain(lat, lon):
        log.debug(
            "[icon] %s is outside ICON-EU domain (lat 29–72, lon -25 to 45) — skipping",
            label,
        )
        return []

    from src.data.grib_cache import _get_cache_dir, _get_cache_ttl_hours, _read_grib_nearest

    cycle_dt = _resolve_icon_cycle()
    if cycle_dt is None:
        log.warning("[icon] no available ICON-EU cycle found for %s", label)
        return []

    log.info("[icon] using cycle %s for %s", cycle_dt.strftime("%Y-%m-%dT%HZ"), label)

    ttl_hours = _get_cache_ttl_hours()
    cache_dir = _get_cache_dir()

    results: list[HourlyTemp] = []
    for fxx in _FORECAST_HOURS:
        path = _fetch_icon_grib(cycle_dt, fxx, cache_dir, ttl_hours)
        if path is None:
            log.debug("[icon] fxx=%03d unavailable for %s", fxx, label)
            continue
        kelvin = _read_grib_nearest(path, lat, lon)
        if kelvin is None:
            log.debug("[icon] fxx=%03d read returned None for %s", fxx, label)
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

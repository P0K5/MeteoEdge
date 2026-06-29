"""NOAA National Blend of Models (NBM) daily-high temperature ingestion.

Fetches TMAX_2m (or derives max from hourly TMP_2m) at a US station's lat/lon
for a target date.  NBM is CONUS-only (like HRRR).

Design notes
------------
- herbie is accessed indirectly via grib_cache infrastructure where possible.
  For variables not in grib_cache.SUPPORTED_VARS (TMAX), we call herbie
  directly inside this module so grib_cache internals are not polluted with
  NBM-specific search strings.
- Cycle resolution: NBM publishes on 00/06/12/18 UTC boundaries.  We step
  back in 6-hour increments (up to 4 cycles = 24 h) to find the latest
  available cycle.
- Cache TTL: 6 hours (matches NBM cycle cadence).
- CONUS bounds: lat 20–55, lon -130 to -60.  Returns None outside CONUS.
- Temperature conversion: K → °F  via  (K - 273.15) * 9/5 + 32.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CONUS bounds (same as HRRR)
# ---------------------------------------------------------------------------

_CONUS_LAT_MIN = 20.0
_CONUS_LAT_MAX = 55.0
_CONUS_LON_MIN = -130.0
_CONUS_LON_MAX = -60.0

# ---------------------------------------------------------------------------
# NBM cycle step (hours)
# ---------------------------------------------------------------------------

_NBM_CYCLE_STEP_H = 6
_NBM_MAX_LOOKBACK_CYCLES = 4  # look back up to 24 hours

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class NbmForecast:
    """Result of a successful NBM daily-high fetch.

    Attributes:
        forecast_high_f: Forecast daily maximum temperature in °F.
        valid_date:      The calendar date the forecast is valid for.
        cycle_ts:        UTC datetime of the NBM model cycle used.
    """

    forecast_high_f: float
    valid_date: date
    cycle_ts: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _kelvin_to_f(kelvin: float) -> float:
    return (kelvin - 273.15) * 9 / 5 + 32


def _is_conus(lat: float, lon: float) -> bool:
    return (
        _CONUS_LAT_MIN <= lat <= _CONUS_LAT_MAX
        and _CONUS_LON_MIN <= lon <= _CONUS_LON_MAX
    )


def _get_cache_ttl_hours() -> float:
    return float(os.getenv("GRIB_CACHE_TTL_HOURS", "6.0"))


def _get_cache_dir() -> Path:
    return Path(os.getenv("GRIB_CACHE_DIR", ".grib_cache"))


def _cache_path_nbm(cache_dir: Path, cycle_dt: datetime, target_date: date) -> Path:
    """Return the on-disk cache path for a (cycle, target_date) pair."""
    ts = cycle_dt.strftime("%Y%m%dT%HZ")
    date_str = target_date.strftime("%Y%m%d")
    return cache_dir / f"nbm_TMAX_{ts}_{date_str}.txt"


def _is_cache_valid(path: Path, ttl_hours: float) -> bool:
    if not path.exists():
        return False
    age_seconds = time.time() - path.stat().st_mtime
    return age_seconds < ttl_hours * 3600


# ---------------------------------------------------------------------------
# NBM cycle resolution
# ---------------------------------------------------------------------------


def _floor_to_nbm_cycle(dt: datetime) -> datetime:
    """Return dt floored to the nearest past NBM cycle (00/06/12/18 UTC)."""
    hour = (dt.hour // _NBM_CYCLE_STEP_H) * _NBM_CYCLE_STEP_H
    return dt.replace(hour=hour, minute=0, second=0, microsecond=0)


def _resolve_nbm_cycle(fxx: int = 0) -> Optional[datetime]:
    """Return the most recent available NBM cycle datetime (UTC).

    Steps back in 6-hour increments up to _NBM_MAX_LOOKBACK_CYCLES times,
    checking herbie's IDX availability for each candidate cycle.

    Args:
        fxx: Forecast hour to test availability against (default 0).

    Returns:
        UTC datetime of the latest available NBM cycle, or None if none found.
    """
    try:
        from herbie import Herbie  # type: ignore[import]
    except ImportError as exc:
        raise ImportError("herbie-data is required: pip install herbie-data") from exc

    now_utc = datetime.now(timezone.utc)
    candidate = _floor_to_nbm_cycle(now_utc)

    for attempt in range(_NBM_MAX_LOOKBACK_CYCLES + 1):
        try:
            H = Herbie(candidate.replace(tzinfo=None), model="nbm", fxx=fxx, verbose=False)
            if H.grib is not None:
                log.debug("[nbm] resolved cycle: %s (attempt %d)", candidate, attempt)
                return candidate.replace(tzinfo=None)
            log.debug("[nbm] cycle %s grib=None (not yet published), stepping back 6h", candidate)
        except Exception:
            log.debug("[nbm] cycle %s not available, stepping back 6h", candidate)
        candidate = candidate - timedelta(hours=_NBM_CYCLE_STEP_H)

    log.warning("[nbm] no available NBM cycle found in last %dh", _NBM_MAX_LOOKBACK_CYCLES * _NBM_CYCLE_STEP_H)
    return None


# ---------------------------------------------------------------------------
# Core fetch — TMAX_2m via herbie with TMP_2m hourly fallback
# ---------------------------------------------------------------------------


def _fxx_range_for_date(cycle_dt: datetime, target_date: date) -> list[int]:
    """Return the forecast-hour offsets (fxx) that cover target_date.

    NBM is initialised at cycle_dt; we want hourly fxx values whose valid
    time falls within target_date (UTC midnight to midnight).
    """
    target_start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
    target_end = target_start + timedelta(days=1)

    fxx_values = []
    # NBM forecasts go out to ~264 h; scan up to 120 h to cover tomorrow
    for fxx in range(0, 121):
        valid_dt = cycle_dt + timedelta(hours=fxx)
        if target_start <= valid_dt < target_end:
            fxx_values.append(fxx)
    return fxx_values


def _fetch_tmax_herbie(cycle_dt: datetime, target_date: date, lat: float, lon: float) -> Optional[float]:
    """Try fetching TMAX_2m directly from NBM via herbie.

    Returns temperature in Kelvin, or None if unavailable.
    """
    try:
        from herbie import Herbie  # type: ignore[import]
        import numpy as np  # type: ignore[import]
    except ImportError:
        return None

    # TMAX is typically at fxx=24 relative to the cycle for "next-day" max.
    # We probe the fxx values that cover target_date, preferring lower fxx.
    for fxx in _fxx_range_for_date(cycle_dt, target_date):
        try:
            H = Herbie(cycle_dt.replace(tzinfo=None), model="nbm", fxx=fxx, verbose=False)
            ds = H.xarray(":TMAX:2 m above ground:", remove_grib=False)
            if ds is None:
                continue
            data_vars = list(ds.data_vars)
            if not data_vars:
                continue
            da = ds[data_vars[0]]
            lats = ds.coords["latitude"].values
            lons = ds.coords["longitude"].values
            lon_q = lon % 360
            dist = np.sqrt((lats - lat) ** 2 + (lons - lon_q) ** 2)
            idx = np.unravel_index(np.argmin(dist), dist.shape)
            val = float(da.values[idx])
            log.info("[nbm] TMAX_2m at (%.4f,%.4f) fxx=%d = %.2f K", lat, lon, fxx, val)
            return val
        except Exception as exc:
            log.debug("[nbm] TMAX fxx=%d failed: %s", fxx, exc)
            continue
    return None


def _fetch_tmp_hourly_max(cycle_dt: datetime, target_date: date, lat: float, lon: float) -> Optional[float]:
    """Derive daily max from hourly TMP_2m values covering target_date.

    Falls back to grib_cache._fetch_grib_slice + _read_grib_nearest for each
    fxx in the target-date window, taking the maximum.

    Returns temperature in Kelvin, or None if no hourly values could be read.
    """
    from src.data.grib_cache import _fetch_grib_slice, _read_grib_nearest

    ttl_hours = _get_cache_ttl_hours()
    cache_dir = _get_cache_dir()
    fxx_values = _fxx_range_for_date(cycle_dt, target_date)

    if not fxx_values:
        log.warning("[nbm] no fxx values cover target_date=%s for cycle=%s", target_date, cycle_dt)
        return None

    temps: list[float] = []
    for fxx in fxx_values:
        try:
            # grib_cache._fetch_grib_slice only supports SUPPORTED_VARS (TMP/DPT),
            # so we use it for TMP_2m here.
            path = _fetch_grib_slice("nbm", "TMP_2m", cycle_dt, fxx, cache_dir, ttl_hours)
            if path is None:
                continue
            val = _read_grib_nearest(path, lat, lon)
            if val is not None:
                temps.append(val)
        except Exception as exc:
            log.debug("[nbm] TMP_2m fxx=%d read failed: %s", fxx, exc)

    if not temps:
        return None
    return max(temps)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_nbm_daily_high(
    lat: float,
    lon: float,
    station: Optional[str] = None,
    target_date: Optional[date] = None,
) -> Optional[NbmForecast]:
    """Fetch NBM forecast daily-high temperature for a US location.

    Attempts to fetch TMAX_2m directly; falls back to computing the max of
    hourly TMP_2m values over the target date window if TMAX is unavailable.

    Args:
        lat:         Latitude (WGS-84).
        lon:         Longitude (WGS-84).
        station:     Optional station identifier for logging.
        target_date: Calendar date to forecast (defaults to tomorrow UTC).

    Returns:
        NbmForecast dataclass, or None if outside CONUS or data unavailable.
    """
    station_label = station or f"({lat:.4f},{lon:.4f})"

    if not _is_conus(lat, lon):
        log.info("[nbm] station %s outside CONUS bounds — skipping", station_label)
        return None

    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)

    if target_date is None:
        target_date = date.today() + timedelta(days=1)

    ttl_hours = _get_cache_ttl_hours()
    cache_dir = _get_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Resolve the latest available NBM cycle
    # Choose a representative fxx to test availability: first fxx covering target_date,
    # or 24 as a reasonable default.
    cycle_dt = _resolve_nbm_cycle(fxx=24)
    if cycle_dt is None:
        log.warning("[nbm] no NBM cycle available for station %s", station_label)
        return None

    # Check on-disk cache (keyed per cycle + target_date)
    cache_file = _cache_path_nbm(cache_dir, cycle_dt, target_date)
    if _is_cache_valid(cache_file, ttl_hours):
        try:
            raw = float(cache_file.read_text().strip())
            log.info("[nbm] cache hit for %s cycle=%s target=%s → %.1f°F",
                     station_label, cycle_dt, target_date, raw)
            return NbmForecast(
                forecast_high_f=raw,
                valid_date=target_date,
                cycle_ts=cycle_dt,
            )
        except Exception:
            pass  # fall through to fresh fetch

    # Attempt 1: TMAX_2m directly
    kelvin: Optional[float] = _fetch_tmax_herbie(cycle_dt, target_date, lat, lon)

    # Attempt 2: max of hourly TMP_2m
    if kelvin is None:
        log.info("[nbm] TMAX unavailable for %s, falling back to hourly TMP_2m max", station_label)
        kelvin = _fetch_tmp_hourly_max(cycle_dt, target_date, lat, lon)

    if kelvin is None:
        log.warning("[nbm] no temperature data for station %s cycle=%s target=%s",
                    station_label, cycle_dt, target_date)
        return None

    forecast_high_f = _kelvin_to_f(kelvin)
    log.info("[nbm] station=%s cycle=%s target=%s → %.1f°F (%.2f K)",
             station_label, cycle_dt, target_date, forecast_high_f, kelvin)

    # Write to cache
    try:
        cache_file.write_text(f"{forecast_high_f:.4f}")
    except Exception as exc:
        log.debug("[nbm] cache write failed: %s", exc)

    return NbmForecast(
        forecast_high_f=forecast_high_f,
        valid_date=target_date,
        cycle_ts=cycle_dt,
    )

"""ECMWF Open Data ingestion — global 2-m temperature forecasts.

Fetches 2-m temperature (2t, Kelvin) from ECMWF HRES IFS model via the
ECMWF Open Data AWS bucket (s3://ecmwf-forecasts/).  Herbie supports this
via model="ifs".

Design notes
------------
- ECMWF HRES publishes on 00Z and 12Z UTC cycles (twice daily).
- `_resolve_ecmwf_cycle()` steps back in 12-hour increments (up to 2 cycles
  = 24 h) to find the latest available cycle.
- Covers global domain — no CONUS guard needed.
- Variable: ``2t`` (2-m temperature, Kelvin).  Conversion: ``(K - 273.15) * 9/5 + 32``.
- Attribution: ECMWF Open Data, CC-BY-4.0 — logged at INFO on every fetch.
- Herbie is accessed indirectly via :mod:`src.data.grib_cache` where possible;
  cycle resolution uses herbie directly (same pattern as :mod:`src.data.nbm`).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from src.data.hrrr import HourlyTemp

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ECMWF_ATTRIBUTION = "ECMWF Open Data, CC-BY-4.0"
_ECMWF_CYCLE_STEP_H = 12  # HRES publishes at 00Z and 12Z
_ECMWF_MAX_LOOKBACK_CYCLES = 2  # look back up to 24 hours (2 cycles)
_ECMWF_FORECAST_HOURS = list(range(1, 25))  # F01 … F24

# ECMWF 2-m temperature search string for herbie / IDX sidecar
_ECMWF_2T_MATCHER = ":2t:"  # matches "2 metre temperature" in ECMWF GRIB index


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class EcmwfForecast:
    """Result of a successful ECMWF daily-high fetch.

    Attributes:
        forecast_high_f: Forecast daily maximum 2-m temperature in °F.
        valid_date:      The calendar date the forecast is valid for.
        cycle_ts:        UTC datetime of the ECMWF model cycle used.
        attribution:     Data attribution string (CC-BY-4.0).
    """

    forecast_high_f: float
    valid_date: date
    cycle_ts: datetime
    attribution: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _kelvin_to_f(kelvin: float) -> float:
    """Convert Kelvin to degrees Fahrenheit."""
    return (kelvin - 273.15) * 9.0 / 5.0 + 32.0


def _get_cache_ttl_hours() -> float:
    return float(os.getenv("GRIB_CACHE_TTL_HOURS", "6.0"))


def _get_cache_dir() -> Path:
    return Path(os.getenv("GRIB_CACHE_DIR", ".grib_cache"))


def _floor_to_ecmwf_cycle(dt: datetime) -> datetime:
    """Return dt floored to the nearest past ECMWF HRES cycle (00Z or 12Z)."""
    hour = (dt.hour // _ECMWF_CYCLE_STEP_H) * _ECMWF_CYCLE_STEP_H
    return dt.replace(hour=hour, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# ECMWF cycle resolution
# ---------------------------------------------------------------------------


def _resolve_ecmwf_cycle(fxx: int = 1) -> Optional[datetime]:
    """Return the most recent available ECMWF HRES cycle datetime (UTC).

    Steps back in 12-hour increments up to _ECMWF_MAX_LOOKBACK_CYCLES times,
    checking herbie IDX availability for each candidate.

    Args:
        fxx: Forecast hour to test availability against (default 1).

    Returns:
        UTC datetime of the latest available ECMWF cycle, or None if not found.
    """
    try:
        from herbie import Herbie  # type: ignore[import]
    except ImportError as exc:
        raise ImportError("herbie-data is required: pip install herbie-data") from exc

    now_utc = datetime.now(timezone.utc)
    candidate = _floor_to_ecmwf_cycle(now_utc)

    for attempt in range(_ECMWF_MAX_LOOKBACK_CYCLES + 1):
        try:
            H = Herbie(candidate, model="ifs", fxx=fxx, verbose=False)
            _ = H.idx  # raises if cycle not yet published
            log.debug("[ecmwf] resolved cycle: %s (attempt %d)", candidate, attempt)
            return candidate
        except Exception:
            log.debug("[ecmwf] cycle %s not available, stepping back 12h", candidate)
            candidate = candidate - timedelta(hours=_ECMWF_CYCLE_STEP_H)

    log.warning(
        "[ecmwf] no available ECMWF cycle found in last %dh",
        _ECMWF_MAX_LOOKBACK_CYCLES * _ECMWF_CYCLE_STEP_H,
    )
    return None


# ---------------------------------------------------------------------------
# Core fetch — 2t via herbie xarray at a point
# ---------------------------------------------------------------------------


def _fetch_ecmwf_2t(cycle_dt: datetime, fxx: int, lat: float, lon: float) -> Optional[float]:
    """Fetch ECMWF 2-m temperature at a single grid point for one forecast hour.

    Uses herbie xarray() with a search string to fetch only the 2t field.
    Returns temperature in Kelvin, or None on failure.

    Args:
        cycle_dt: ECMWF cycle datetime (UTC).
        fxx:      Forecast hour offset.
        lat:      Latitude (WGS-84).
        lon:      Longitude (WGS-84).

    Returns:
        Scalar temperature in Kelvin, or None if unavailable.
    """
    try:
        from herbie import Herbie  # type: ignore[import]
        import numpy as np  # type: ignore[import]
    except ImportError:
        return None

    try:
        H = Herbie(cycle_dt, model="ifs", fxx=fxx, verbose=False)
        ds = H.xarray(_ECMWF_2T_MATCHER, remove_grib=False)
        if ds is None:
            return None
        data_vars = list(ds.data_vars)
        if not data_vars:
            return None
        da = ds[data_vars[0]]

        if "latitude" in ds.coords and "longitude" in ds.coords:
            lats = ds.coords["latitude"].values
            lons = ds.coords["longitude"].values
            lon_q = lon % 360
            dist = np.sqrt((lats - lat) ** 2 + (lons - lon_q) ** 2)
            idx = np.unravel_index(np.argmin(dist), dist.shape)
            val = float(da.values[idx])
        else:
            val = float(da.values.flat[0])

        log.debug("[ecmwf] 2t at (%.4f,%.4f) fxx=%d = %.2f K", lat, lon, fxx, val)
        return val
    except Exception as exc:
        log.debug("[ecmwf] 2t fxx=%d failed: %s", fxx, exc)
        return None


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def _cache_path_ecmwf_daily(cache_dir: Path, cycle_dt: datetime, target_date: date) -> Path:
    ts = cycle_dt.strftime("%Y%m%dT%HZ")
    date_str = target_date.strftime("%Y%m%d")
    return cache_dir / f"ecmwf_2t_daily_{ts}_{date_str}.txt"


def _is_cache_valid(path: Path, ttl_hours: float) -> bool:
    if not path.exists():
        return False
    age_seconds = time.time() - path.stat().st_mtime
    return age_seconds < ttl_hours * 3600


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_ecmwf_hourly(
    lat: float,
    lon: float,
    station: Optional[str] = None,
) -> list[HourlyTemp]:
    """Fetch ECMWF HRES hourly 2-m temperature forecasts for the next 24 hours.

    Resolves the latest available ECMWF cycle (00Z or 12Z), then fetches 2t
    at forecast hours F01 through F24.  Temperatures are converted from Kelvin
    to °F.

    ECMWF Open Data is global; no geographic restriction is applied.

    Args:
        lat:     Latitude (WGS-84).
        lon:     Longitude (WGS-84).
        station: Optional station identifier for logging only.

    Returns:
        List of :class:`~src.data.hrrr.HourlyTemp` instances (F01 … F24), or
        an empty list if no ECMWF cycle is currently available or all fetches fail.
    """
    label = station or f"({lat:.4f},{lon:.4f})"
    log.info("[ecmwf] data from %s", _ECMWF_ATTRIBUTION)

    cycle_dt = _resolve_ecmwf_cycle(fxx=1)
    if cycle_dt is None:
        log.warning("[ecmwf] no available ECMWF cycle found for %s", label)
        return []

    log.info("[ecmwf] using cycle %s for %s", cycle_dt.strftime("%Y-%m-%dT%HZ"), label)

    results: list[HourlyTemp] = []
    for fxx in _ECMWF_FORECAST_HOURS:
        kelvin = _fetch_ecmwf_2t(cycle_dt, fxx, lat, lon)
        if kelvin is None:
            log.debug("[ecmwf] fxx=%02d unavailable for %s", fxx, label)
            continue
        temp_f = _kelvin_to_f(kelvin)
        valid_time = cycle_dt + timedelta(hours=fxx)
        results.append(HourlyTemp(ts_utc=valid_time, temp_f=temp_f))

    log.info(
        "[ecmwf] %s — cycle %s — retrieved %d/%d forecast hours",
        label,
        cycle_dt.strftime("%Y-%m-%dT%HZ"),
        len(results),
        len(_ECMWF_FORECAST_HOURS),
    )
    return results


def fetch_ecmwf_daily_high(
    lat: float,
    lon: float,
    station: Optional[str] = None,
    target_date: Optional[date] = None,
) -> Optional[EcmwfForecast]:
    """Fetch ECMWF HRES forecast daily-high 2-m temperature for any global location.

    Resolves the latest available ECMWF cycle (00Z or 12Z), fetches 2t at the
    24 forecast hours that cover target_date, and returns the maximum as the
    forecast daily high.

    Results are cached on disk per (cycle, target_date) pair using the
    GRIB_CACHE_TTL_HOURS TTL (default 6 h, aligned with ECMWF's 12-h cadence
    with overlap).

    Args:
        lat:         Latitude (WGS-84).
        lon:         Longitude (WGS-84).
        station:     Optional station identifier for logging.
        target_date: Calendar date for which to retrieve the daily high
                     (defaults to tomorrow UTC).

    Returns:
        :class:`EcmwfForecast` dataclass, or None if data is unavailable.
    """
    station_label = station or f"({lat:.4f},{lon:.4f})"
    log.info("[ecmwf] data from %s", _ECMWF_ATTRIBUTION)

    if target_date is None:
        target_date = (datetime.now(timezone.utc) + timedelta(days=1)).date()

    ttl_hours = _get_cache_ttl_hours()
    cache_dir = _get_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    cycle_dt = _resolve_ecmwf_cycle(fxx=1)
    if cycle_dt is None:
        log.warning("[ecmwf] no ECMWF cycle available for station %s", station_label)
        return None

    # Check on-disk cache (keyed per cycle + target_date)
    cache_file = _cache_path_ecmwf_daily(cache_dir, cycle_dt, target_date)
    if _is_cache_valid(cache_file, ttl_hours):
        try:
            raw = float(cache_file.read_text().strip())
            log.info(
                "[ecmwf] cache hit for %s cycle=%s target=%s → %.1f°F",
                station_label,
                cycle_dt,
                target_date,
                raw,
            )
            return EcmwfForecast(
                forecast_high_f=raw,
                valid_date=target_date,
                cycle_ts=cycle_dt,
                attribution=_ECMWF_ATTRIBUTION,
            )
        except Exception:
            pass  # fall through to fresh fetch

    # Determine which forecast hours cover target_date
    target_start = datetime(
        target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc
    )
    target_end = target_start + timedelta(days=1)

    temps: list[float] = []
    for fxx in range(1, 121):  # scan up to 120 h ahead
        valid_dt = cycle_dt + timedelta(hours=fxx)
        if valid_dt < target_start:
            continue
        if valid_dt >= target_end:
            break
        kelvin = _fetch_ecmwf_2t(cycle_dt, fxx, lat, lon)
        if kelvin is not None:
            temps.append(kelvin)

    if not temps:
        log.warning(
            "[ecmwf] no temperature data for station %s cycle=%s target=%s",
            station_label,
            cycle_dt,
            target_date,
        )
        return None

    forecast_high_f = _kelvin_to_f(max(temps))
    log.info(
        "[ecmwf] station=%s cycle=%s target=%s → %.1f°F",
        station_label,
        cycle_dt,
        target_date,
        forecast_high_f,
    )

    # Write to cache
    try:
        cache_file.write_text(f"{forecast_high_f:.4f}")
    except Exception as exc:
        log.debug("[ecmwf] cache write failed: %s", exc)

    return EcmwfForecast(
        forecast_high_f=forecast_high_f,
        valid_date=target_date,
        cycle_ts=cycle_dt,
        attribution=_ECMWF_ATTRIBUTION,
    )

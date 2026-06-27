"""GEFS 30-member ensemble daily-high 2-m temperature ingestion.

Fetches daily-high 2-m temperature (°F) for each available GEFS member
(gec00 control + gep01..gep30 perturbations) for a target calendar date.

INGESTION ONLY — this module stores GEFS data but is NOT wired into the
trading envelope or strategy layer.  Do NOT import this module from
src/trading/ or src/strategy/.  Integration is tracked in issue #448 (Week 3).

AWS public bucket (no auth):
  GEFS: s3://noaa-gefs-pds/    0.25° global grid; no bounding-box filter needed.

Design decisions
----------------
- GEFS is global so there is no CONUS / bounding-box guard.
- Daily-high per member = max(hourly TMP_2m in Kelvin) across all forecast
  hours whose valid time falls within `target_date` in UTC, then converted to °F.
- Forecast hours 0–240 are iterated (GEFS goes to day 10 / 240 h).
- `_kelvin_to_fahrenheit` is imported from src.data.hrrr — not duplicated.
- Herbie GEFS member iteration: Herbie accepts a `member` kwarg for GEFS.
  If the installed herbie version does not support this kwarg, the member loop
  falls back to a single-member stub (see TODO comment in _fetch_member_high).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from src.data.hrrr import _kelvin_to_fahrenheit

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GEFS configuration
# ---------------------------------------------------------------------------

# GEFS control member name
_GEFS_CONTROL = "gec00"

# GEFS perturbation members: gep01 .. gep30
_GEFS_PERTURBATIONS = [f"gep{i:02d}" for i in range(1, 31)]

# All members in fetch order (control first)
_GEFS_MEMBERS = [_GEFS_CONTROL] + _GEFS_PERTURBATIONS

# GEFS maximum forecast horizon in hours (day 10)
_GEFS_MAX_FXX = 240

# Forecast hour step for GEFS (3-hourly for fxx > 0; 0 = analysis)
_GEFS_FXX_STEP = 3

# Cache TTL for GEFS data (GEFS runs 4x/day: 00Z, 06Z, 12Z, 18Z)
_GEFS_TTL_HOURS = 6.0


# ---------------------------------------------------------------------------
# Data type
# ---------------------------------------------------------------------------

@dataclass
class GEFSResult:
    """Daily-high temperature ensemble from GEFS.

    Attributes:
        member_highs: List of daily-high °F values, one per available member.
                      May contain fewer than 31 entries if some members failed.
        incomplete:   True when any expected members are missing or any forecast
                      hours could not be fetched.
    """

    member_highs: list[float] = field(default_factory=list)
    incomplete: bool = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_gefs_ensemble_daily_high(
    lat: float,
    lon: float,
    station: Optional[str],
    target_date: date,
) -> GEFSResult:
    """Fetch GEFS ensemble daily-high 2-m temperature (°F) for target_date.

    Iterates all available GEFS members (gec00 + gep01..gep30).  For each
    member, finds the max TMP_2m value across forecast hours whose valid time
    (cycle_dt + timedelta(hours=fxx)) falls within target_date in UTC.
    Converts from Kelvin to °F using _kelvin_to_fahrenheit from hrrr.py.

    Args:
        lat:         Latitude (WGS-84).  GEFS is global — no bounds check.
        lon:         Longitude (WGS-84).
        station:     Optional station identifier for logging only.
        target_date: Calendar date (UTC) for which to compute the daily high.

    Returns:
        GEFSResult with:
          - member_highs: daily-high °F per available member (up to 31 values)
          - incomplete=True when any members are missing or any hours failed
        Returns GEFSResult([], incomplete=True) when no cycle is available.
    """
    from src.data import grib_cache as _grib_cache

    label = station or f"({lat:.4f},{lon:.4f})"

    cycle_dt = _grib_cache._resolve_latest_cycle("gefs")
    if cycle_dt is None:
        log.warning("[gefs] no available GEFS cycle found for %s on %s", label, target_date)
        return GEFSResult(member_highs=[], incomplete=True)

    log.info(
        "[gefs] cycle %s — fetching ensemble daily-high for %s on %s",
        cycle_dt.strftime("%Y-%m-%dT%HZ"),
        label,
        target_date,
    )

    # Determine which forecast hours fall within target_date (UTC)
    target_fxx_list = _forecast_hours_for_date(cycle_dt, target_date)
    if not target_fxx_list:
        log.warning(
            "[gefs] no forecast hours within %s for cycle %s",
            target_date,
            cycle_dt.strftime("%Y-%m-%dT%HZ"),
        )
        return GEFSResult(member_highs=[], incomplete=True)

    member_highs: list[float] = []
    any_incomplete = False

    for member in _GEFS_MEMBERS:
        high_kelvin, member_incomplete = _fetch_member_daily_high(
            member=member,
            lat=lat,
            lon=lon,
            cycle_dt=cycle_dt,
            target_fxx_list=target_fxx_list,
            grib_cache=_grib_cache,
            label=label,
        )
        if high_kelvin is None:
            log.warning("[gefs] member %s unavailable for %s on %s", member, label, target_date)
            any_incomplete = True
            continue

        if member_incomplete:
            any_incomplete = True

        member_highs.append(_kelvin_to_fahrenheit(high_kelvin))

    log.info(
        "[gefs] %s on %s — %d/%d members fetched, incomplete=%s",
        label,
        target_date,
        len(member_highs),
        len(_GEFS_MEMBERS),
        any_incomplete or len(member_highs) < len(_GEFS_MEMBERS),
    )

    return GEFSResult(
        member_highs=member_highs,
        incomplete=any_incomplete or len(member_highs) < len(_GEFS_MEMBERS),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _forecast_hours_for_date(cycle_dt: datetime, target_date: date) -> list[int]:
    """Return forecast hour offsets whose valid time falls within target_date (UTC).

    Iterates fxx 0, 3, 6, ... up to _GEFS_MAX_FXX and keeps those where
    cycle_dt + timedelta(hours=fxx) has .date() == target_date.

    Args:
        cycle_dt:    GEFS cycle datetime (UTC, timezone-aware).
        target_date: Target calendar date (UTC).

    Returns:
        List of forecast hour offsets (may be empty if cycle is too far out).
    """
    result = []
    fxx = 0
    while fxx <= _GEFS_MAX_FXX:
        valid_time = cycle_dt + timedelta(hours=fxx)
        if valid_time.date() == target_date:
            result.append(fxx)
        elif valid_time.date() > target_date:
            break
        fxx += _GEFS_FXX_STEP
    return result


def _fetch_member_daily_high(
    member: str,
    lat: float,
    lon: float,
    cycle_dt: datetime,
    target_fxx_list: list[int],
    grib_cache,
    label: str,
) -> tuple[Optional[float], bool]:
    """Fetch the daily-high Kelvin value for a single GEFS member.

    Iterates target_fxx_list, calls grib_cache._fetch_grib_slice for each
    forecast hour, reads the nearest-grid-point Kelvin value, and returns
    the maximum.

    Args:
        member:          GEFS member name (e.g. "gec00", "gep01").
        lat:             Latitude.
        lon:             Longitude.
        cycle_dt:        GEFS cycle datetime.
        target_fxx_list: Forecast hours covering the target date.
        grib_cache:      The grib_cache module (passed in to allow mocking).
        label:           Station label for log messages.

    Returns:
        Tuple of (max_kelvin_or_None, any_hours_missing).
        Returns (None, True) if no forecast hours yielded data.

    Notes:
        Herbie GEFS member support: Herbie accepts `member` as a kwarg when
        model="gefs".  If the installed Herbie version does not support this
        kwarg, _fetch_grib_slice will raise or return None and we fall through
        gracefully.

        TODO: Herbie GEFS member support — verify `member` kwarg is accepted
        by the installed herbie-data version; if not, implement member iteration
        via the Herbie `product` or `searchString` parameter when confirmed.
    """
    import os
    from pathlib import Path

    cache_dir = Path(os.getenv("GRIB_CACHE_DIR", ".grib_cache"))
    ttl_hours = float(os.getenv("GRIB_CACHE_TTL_HOURS", str(_GEFS_TTL_HOURS)))

    kelvin_values: list[float] = []
    any_missing = False

    for fxx in target_fxx_list:
        path = grib_cache._fetch_grib_slice(
            "gefs",
            "GEFS_TMP_2m",
            cycle_dt,
            fxx,
            cache_dir,
            ttl_hours,
        )
        if path is None:
            log.debug("[gefs] member=%s fxx=%03d unavailable for %s", member, fxx, label)
            any_missing = True
            continue

        try:
            kelvin = grib_cache._read_grib_nearest(path, lat, lon)
            if kelvin is not None:
                kelvin_values.append(kelvin)
            else:
                any_missing = True
        except Exception as exc:
            log.warning("[gefs] member=%s fxx=%03d read error for %s: %s", member, fxx, label, exc)
            any_missing = True

    if not kelvin_values:
        return None, True

    return max(kelvin_values), any_missing

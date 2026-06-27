"""GEFS (Global Ensemble Forecast System) 2-m temperature ingestion.

Fetches 2-m temperature forecasts from all 31 GEFS ensemble members
(gec00 — control, gep01 … gep30 — perturbed) for the forecast hours
defined by :data:`_FORECAST_HOURS`.

All GRIB2 I/O is delegated to :mod:`src.data.grib_cache`; this module
never imports herbie directly.

GEFS members
------------
- ``gec00``: control member (unperturbed)
- ``gep01`` … ``gep30``: 30 perturbed members

Each member is fetched and cached independently so that parallel runs or
subsequent calls do not cross-contaminate cached GRIB slices.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GEFS member identifiers
# ---------------------------------------------------------------------------

#: Control member + 30 perturbed members (31 total).
GEFS_MEMBERS: list[str] = ["gec00"] + [f"gep{i:02d}" for i in range(1, 31)]

# Forecast hours to fetch (F01 … F10 by default; extend as needed).
_FORECAST_HOURS: list[int] = list(range(1, 11))

_MODEL = "gefs"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class GEFSMemberForecast:
    """Single hourly temperature forecast from one GEFS ensemble member.

    Attributes:
        member:  GEFS member identifier (e.g. ``"gec00"``, ``"gep01"``).
        ts_utc:  Valid time of the forecast (UTC, timezone-aware).
        temp_k:  2-m air temperature in Kelvin.
    """

    member: str
    ts_utc: datetime
    temp_k: float


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_gefs_ensemble(
    lat: float,
    lon: float,
    station: Optional[str] = None,
    fxx: int = 6,
    db=None,
) -> list[GEFSMemberForecast]:
    """Fetch GEFS TMP_2m for all 31 ensemble members at a single forecast hour.

    Resolves the latest available GEFS cycle then iterates over all 31
    members, fetching a GRIB slice for each one.  The ``member`` kwarg is
    forwarded through :func:`src.data.grib_cache._fetch_grib_slice` so that:

    1. Herbie fetches the correct member-specific GRIB file from NOAA S3.
    2. Each member is stored under a unique cache key, preventing any
       member from returning another member's cached data.

    Args:
        lat:     Latitude (WGS-84).
        lon:     Longitude (WGS-84).
        station: Optional station identifier for logging only.
        fxx:     Forecast hour offset (default 6).
        db:      Optional DB handle for live config reads.

    Returns:
        List of :class:`GEFSMemberForecast` instances — one per successfully
        fetched member.  Returns an empty list if no GEFS cycle is available
        or all member fetches fail.
    """
    label = station or f"({lat:.4f},{lon:.4f})"

    from src.data import grib_cache as _grib_cache

    ttl_hours = _grib_cache._get_cache_ttl_hours(db)
    cache_dir = _grib_cache._get_cache_dir(db)

    # Opportunistic eviction
    _grib_cache._evict_expired(cache_dir, ttl_hours)

    cycle_dt = _grib_cache._resolve_latest_cycle(_MODEL, fxx=fxx)
    if cycle_dt is None:
        log.warning("[gefs] no available GEFS cycle found for %s", label)
        return []

    log.info("[gefs] cycle %s — fetching %d members for %s fxx=%d",
             cycle_dt.strftime("%Y-%m-%dT%HZ"), len(GEFS_MEMBERS), label, fxx)

    results: list[GEFSMemberForecast] = []
    for member in GEFS_MEMBERS:
        path = _grib_cache._fetch_grib_slice(
            model=_MODEL,
            var="TMP_2m",
            cycle_dt=cycle_dt,
            fxx=fxx,
            cache_dir=cache_dir,
            ttl_hours=ttl_hours,
            member=member,
        )
        if path is None:
            log.debug("[gefs] member=%s fxx=%02d unavailable for %s", member, fxx, label)
            continue

        try:
            temp_k = _grib_cache._read_grib_nearest(path, lat, lon)
        except Exception as exc:
            log.warning("[gefs] cfgrib read failed for member=%s: %s", member, exc)
            continue

        if temp_k is None:
            continue

        valid_time = cycle_dt + timedelta(hours=fxx)
        results.append(GEFSMemberForecast(member=member, ts_utc=valid_time, temp_k=temp_k))

    log.info("[gefs] %s — retrieved %d/%d members", label, len(results), len(GEFS_MEMBERS))
    return results

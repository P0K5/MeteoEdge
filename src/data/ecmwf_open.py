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

ECMWF ENS (issue #824)
-----------------------
ECMWF Open Data also publishes a 51-member ensemble ("enfo" product, 1
control + 50 perturbed) on the same 00Z/12Z cadence and buckets as HRES
("oper" product). ``fetch_ecmwf_daily_high()``/``fetch_ecmwf_hourly()``
above read "oper" only (forecast_high_f). ``fetch_ecmwf_ensemble_spread()``
reads "enfo" separately to derive ``sigma_f`` — see that function's
docstring and ``src/scripts/capture_forecasts.py``'s per-channel sigma_f
sourcing table for the full rationale.
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
_ECMWF_FORECAST_HOURS = list(range(0, 25, 3))  # 3-hourly: F00, F03, F06, …, F24

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


def _resolve_ecmwf_cycle(fxx: int = 3) -> Optional[datetime]:
    """Return the most recent available ECMWF HRES cycle datetime (UTC).

    Steps back in 12-hour increments up to _ECMWF_MAX_LOOKBACK_CYCLES times,
    checking herbie IDX availability for each candidate.

    Args:
        fxx: Forecast hour to test availability against (default 3).

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
            H = Herbie(candidate.replace(tzinfo=None), model="ifs", fxx=fxx, verbose=False)
            if H.grib is not None:
                log.debug("[ecmwf] resolved cycle: %s (attempt %d)", candidate, attempt)
                return candidate.replace(tzinfo=None)
            log.debug("[ecmwf] cycle %s grib=None (not yet published), stepping back 12h", candidate)
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
        H = Herbie(cycle_dt.replace(tzinfo=None), model="ifs", fxx=fxx, verbose=False)
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
            # Normalise longitude query to match the DataArray's convention:
            # ECMWF Open Data uses –180…+180; HRRR/GEFS use 0…360.
            lon_q = lon % 360 if lons.min() >= 0 else lon
            if lats.ndim == 1:
                val = float(da.sel({"latitude": lat, "longitude": lon_q}, method="nearest").values)
            else:
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
# ECMWF ENS (ensemble) — sigma_f source (issue #824)
# ---------------------------------------------------------------------------
#
# ECMWF Open Data publishes TWO products on the same 00Z/12Z cadence and the
# same AWS/GCS/Azure buckets used above for HRES:
#   - "oper": the deterministic high-resolution run (what _fetch_ecmwf_2t and
#     fetch_ecmwf_daily_high read for forecast_high_f).
#   - "enfo": the 51-member ensemble (1 control "cf" + 50 perturbed "pf"),
#     confirmed available via herbie's bundled ECMWF template
#     (Herbie(..., model="ifs", product="enfo")) -- see herbie/models/ecmwf.py
#     PRODUCTS mapping. This contradicts the pre-#824 assumption recorded in
#     this module's docstring/#555 decision table ("open-data endpoint does
#     not expose the ECMWF ensemble/EPS spread"): the ENS *is* exposed, just
#     under a different `product=` than the deterministic run.
#
# Unlike GEFS (one GRIB file per member on NOAA S3), ENS bundles every
# member's message for a given step into the SAME file, so all ~51 member
# values for one step are fetched with a single herbie byte-range download
# (searchString=":2t:") rather than 51 separate ones.


def _fetch_ecmwf_ens_members_2t(
    cycle_dt: datetime, fxx: int, lat: float, lon: float
) -> Optional[list[float]]:
    """Fetch ECMWF ENS 2-m temperature for every member at a single step.

    Args:
        cycle_dt: ECMWF cycle datetime (UTC). ENS runs on the same 00Z/12Z
                   cadence as HRES, so a cycle resolved via
                   _resolve_ecmwf_cycle() is reused for both products.
        fxx:       Forecast hour offset.
        lat:       Latitude (WGS-84).
        lon:       Longitude (WGS-84).

    Returns:
        List of per-member temperatures in Kelvin (up to 51: control + 50
        perturbed), or None if herbie/cfgrib is unavailable, no ENS cycle is
        published, or the fetch/parse otherwise fails.
    """
    try:
        from herbie import Herbie  # type: ignore[import]
        import numpy as np  # type: ignore[import]
    except ImportError:
        return None

    try:
        H = Herbie(
            cycle_dt.replace(tzinfo=None),
            model="ifs",
            product="enfo",
            fxx=fxx,
            verbose=False,
        )
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
            lon_q = lon % 360 if lons.min() >= 0 else lon
            if lats.ndim == 1:
                point = da.sel({"latitude": lat, "longitude": lon_q}, method="nearest")
            else:
                dist = np.sqrt((lats - lat) ** 2 + (lons - lon_q) ** 2)
                idx = np.unravel_index(np.argmin(dist), dist.shape)
                point = da.isel({da.dims[-2]: idx[0], da.dims[-1]: idx[1]})
        else:
            point = da

        values = np.atleast_1d(np.asarray(point.values, dtype=float)).ravel().tolist()
        if not values:
            return None
        log.debug(
            "[ecmwf-ens] %d member(s) at (%.4f,%.4f) fxx=%d", len(values), lat, lon, fxx,
        )
        return values
    except Exception as exc:
        log.debug("[ecmwf-ens] members fxx=%d failed: %s", fxx, exc)
        return None


def fetch_ecmwf_ensemble_spread(
    lat: float,
    lon: float,
    station: Optional[str] = None,
    target_date: Optional[date] = None,
) -> Optional[float]:
    """Fetch the raw (unfloored) ECMWF ENS member-spread for sigma_f (issue #824).

    Companion to fetch_ecmwf_daily_high(): that function reads the "oper"
    (HRES deterministic) product for forecast_high_f; this reads the separate
    "enfo" (ENS ensemble) product to derive a genuine dispersion signal for
    sigma_f. Per the #555 capture/consumption split, the return value is the
    UNFLOORED sample stdev via src.model.ensemble_sigma.raw_member_sigma() --
    never a fabricated placeholder, and SIGMA_FLOOR_F is never applied here
    (that only happens at consumption time, in compute_ensemble_sigma()).

    Picks a single representative 3-hourly step within target_date's UTC
    calendar-day window (the step nearest the window's midpoint) rather than
    reconstructing a per-member daily max across every step -- mirroring
    fetch_gefs_ensemble()'s fixed-fxx simplification for the gefs channel: one
    ensemble snapshot per capture, not a full per-member daily-max scan.

    Args:
        lat:         Latitude (WGS-84).
        lon:         Longitude (WGS-84).
        station:     Optional station identifier for logging only.
        target_date: Calendar date (UTC) to source the ensemble snapshot
                     from (defaults to tomorrow UTC, matching
                     fetch_ecmwf_daily_high). Accepts an ISO date string.

    Returns:
        Raw sample stdev in °F across ENS members, or None when no ENS cycle
        is available, no step falls within the target_date window, fewer
        than 2 members are returned, or the fetch otherwise fails.
    """
    from src.model.ensemble_sigma import raw_member_sigma

    label = station or f"({lat:.4f},{lon:.4f})"

    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)
    if target_date is None:
        target_date = (datetime.now(timezone.utc) + timedelta(days=1)).date()

    cycle_dt = _resolve_ecmwf_cycle(fxx=3)
    if cycle_dt is None:
        log.debug("[ecmwf-ens] no ECMWF cycle available for %s", label)
        return None

    target_start = datetime(target_date.year, target_date.month, target_date.day)
    target_end = target_start + timedelta(days=1)
    window_mid = target_start + timedelta(hours=12)

    candidates = [
        fxx
        for fxx in range(0, 91, 3)
        if target_start <= (cycle_dt + timedelta(hours=fxx)).replace(tzinfo=None) < target_end
    ]
    if not candidates:
        log.debug(
            "[ecmwf-ens] no ENS step falls within target_date=%s window for %s",
            target_date, label,
        )
        return None

    fxx = min(
        candidates,
        key=lambda f: abs((cycle_dt + timedelta(hours=f)).replace(tzinfo=None) - window_mid),
    )

    members_k = _fetch_ecmwf_ens_members_2t(cycle_dt, fxx, lat, lon)
    if not members_k:
        log.debug("[ecmwf-ens] no ENS members returned for %s fxx=%d", label, fxx)
        return None

    members_f = [_kelvin_to_f(k) for k in members_k]
    sigma_f = raw_member_sigma(members_f)
    log.info(
        "[ecmwf-ens] %s cycle=%s fxx=%d target=%s -> sigma=%s (%d members)",
        label,
        cycle_dt.strftime("%Y-%m-%dT%HZ"),
        fxx,
        target_date,
        "None" if sigma_f is None else f"{sigma_f:.2f}F",
        len(members_f),
    )
    return sigma_f


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def _cache_path_ecmwf_daily(
    cache_dir: Path, cycle_dt: datetime, target_date: date, lat: float, lon: float
) -> Path:
    ts = cycle_dt.strftime("%Y%m%dT%HZ")
    date_str = target_date.strftime("%Y%m%d")
    return cache_dir / f"ecmwf_2t_daily_{ts}_{date_str}_{lat:.4f}_{lon:.4f}.txt"


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

    cycle_dt = _resolve_ecmwf_cycle(fxx=3)
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

    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)

    if target_date is None:
        target_date = (datetime.now(timezone.utc) + timedelta(days=1)).date()

    ttl_hours = _get_cache_ttl_hours()
    cache_dir = _get_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    cycle_dt = _resolve_ecmwf_cycle(fxx=3)
    if cycle_dt is None:
        log.warning("[ecmwf] no ECMWF cycle available for station %s", station_label)
        return None

    # Check on-disk cache (keyed per cycle + target_date + lat/lon)
    cache_file = _cache_path_ecmwf_daily(cache_dir, cycle_dt, target_date, lat, lon)
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
    target_start = datetime(target_date.year, target_date.month, target_date.day)
    target_end = target_start + timedelta(days=1)

    temps: list[float] = []
    for fxx in range(0, 91, 3):  # HRES Open Data publishes 3-hourly steps up to ~90h
        valid_dt = (cycle_dt + timedelta(hours=fxx)).replace(tzinfo=None)
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

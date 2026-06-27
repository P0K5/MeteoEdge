"""GRIB2 / HRRR ingestion layer using herbie for model-run resolution and byte-range
AWS S3 fetches.

Design decisions
----------------
herbie (MIT, NOAA-archive-aware) is used instead of a custom cfgrib+s3fs wrapper
because:
1. herbie already implements model-run latency detection and automatic fallback to
   the previous cycle — reimplementing that logic correctly is non-trivial.
2. herbie generates the IDX (index) sidecar URL and parses it to identify exact
   byte ranges per variable, so we never download multi-hundred-MB GRIB files.
3. MIT license is compatible with this project's commercial use.

Only TMP_2m and DPT_2m fields are fetched.  HRRR is US-only; use Denver (39.73,
-104.99) for smoke tests.

AWS public buckets (no auth):
  HRRR: s3://noaa-hrrr-bdp-pds/
  NBM:  s3://noaa-nbm-grib2-pds/

Config params (all wired through CONFIG_DEFAULTS + get_live_config):
  GRIB_CACHE_TTL_HOURS  — on-disk cache TTL in hours (default 6)
  GRIB_CACHE_DIR        — directory for cached GRIB slices (default .grib_cache)
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config helpers — live-read pattern (same as rest of codebase)
# ---------------------------------------------------------------------------

def _get_cache_ttl_hours(db=None) -> float:
    """Return GRIB cache TTL in hours. Reads live config when db is provided."""
    if db is not None:
        try:
            from src.config import get_live_config
            cfg = get_live_config(db)
            return float(cfg.get("GRIB_CACHE_TTL_HOURS", 6.0))
        except Exception:
            pass
    return float(os.getenv("GRIB_CACHE_TTL_HOURS", "6.0"))


def _get_cache_dir(db=None) -> Path:
    """Return GRIB on-disk cache directory. Reads live config when db is provided."""
    if db is not None:
        try:
            from src.config import get_live_config
            cfg = get_live_config(db)
            raw = cfg.get("GRIB_CACHE_DIR", ".grib_cache")
            return Path(str(raw))
        except Exception:
            pass
    return Path(os.getenv("GRIB_CACHE_DIR", ".grib_cache"))


# ---------------------------------------------------------------------------
# Variable matchers — only TMP_2m and DPT_2m
# ---------------------------------------------------------------------------

SUPPORTED_VARS = {
    "TMP_2m": ":TMP:2 m above ground:",
    "DPT_2m": ":DPT:2 m above ground:",
    "ICON_T2M": ":t_2m:",  # DWD ICON-EU 2-m temperature (Kelvin)
    "GEFS_TMP_2m": ":TMP:2 m above ground:",  # GEFS 2-m temperature (Kelvin)
}

# ---------------------------------------------------------------------------
# Cache key & file helpers
# ---------------------------------------------------------------------------

def _cache_key(model: str, var: str, cycle_dt, fxx: int) -> str:
    """Build a unique string key for a cached GRIB slice."""
    ts = cycle_dt.strftime("%Y%m%dT%HZ")
    return f"{model}_{var}_{ts}_f{fxx:03d}"


def _cache_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"{key}.grib2"


def _is_cache_valid(path: Path, ttl_hours: float) -> bool:
    """Return True if *path* exists and is younger than ttl_hours."""
    if not path.exists():
        return False
    age_seconds = time.time() - path.stat().st_mtime
    return age_seconds < ttl_hours * 3600


def _evict_expired(cache_dir: Path, ttl_hours: float) -> None:
    """Delete all .grib2 files in cache_dir that have exceeded the TTL."""
    if not cache_dir.exists():
        return
    now = time.time()
    cutoff = ttl_hours * 3600
    for f in cache_dir.glob("*.grib2"):
        try:
            if now - f.stat().st_mtime > cutoff:
                f.unlink(missing_ok=True)
                log.debug("[grib_cache] evicted %s", f.name)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Core fetch — herbie byte-range download
# ---------------------------------------------------------------------------

def _fetch_grib_slice(
    model: str,
    var: str,
    cycle_dt,
    fxx: int,
    cache_dir: Path,
    ttl_hours: float,
) -> Optional[Path]:
    """Download (or return cached) a single GRIB2 message for *var*.

    Uses herbie's built-in byte-range (IDX sidecar) logic — never downloads
    the full GRIB file.

    Args:
        model:     herbie model name (e.g. "hrrr").
        var:       Variable key from SUPPORTED_VARS (e.g. "TMP_2m").
        cycle_dt:  datetime of the model cycle (UTC).
        fxx:       Forecast hour offset (0 = analysis).
        cache_dir: Path to the on-disk cache directory.
        ttl_hours: Cache TTL in hours.

    Returns:
        Path to the cached .grib2 slice, or None on failure.
    """
    if var not in SUPPORTED_VARS:
        raise ValueError(f"Unsupported variable '{var}'. Choose from: {list(SUPPORTED_VARS)}")

    cache_dir.mkdir(parents=True, exist_ok=True)
    key = _cache_key(model, var, cycle_dt, fxx)
    path = _cache_path(cache_dir, key)

    if _is_cache_valid(path, ttl_hours):
        log.debug("[grib_cache] cache hit: %s", key)
        return path

    try:
        from herbie import Herbie  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "herbie-data is required: pip install herbie-data"
        ) from exc

    matcher = SUPPORTED_VARS[var]
    log.info("[grib_cache] fetching %s %s cycle=%s fxx=%d", model, var, cycle_dt, fxx)
    try:
        H = Herbie(
            cycle_dt,
            model=model,
            fxx=fxx,
            save_dir=str(cache_dir),
            overwrite=False,
            verbose=False,
        )
        # download() with searchString fetches only matching byte ranges via
        # the IDX sidecar — never the full GRIB2 file.
        downloaded = H.download(matcher, save_dir=str(cache_dir), overwrite=False)
        # herbie returns the path it wrote; rename to our deterministic key path
        if downloaded and Path(downloaded).exists() and Path(downloaded) != path:
            Path(downloaded).rename(path)
        elif not path.exists():
            log.warning("[grib_cache] herbie did not produce expected file for %s", key)
            return None
        log.info("[grib_cache] cached %s → %s", key, path)
        return path
    except Exception as exc:
        log.warning("[grib_cache] fetch failed for %s: %s", key, exc)
        return None


# ---------------------------------------------------------------------------
# Model cycle resolution — latest available with fallback
# ---------------------------------------------------------------------------

def _resolve_latest_cycle(model: str, fxx: int = 0):
    """Return the most recent available model cycle datetime (UTC).

    Tries the current UTC hour, then steps back in 1-hour increments up to
    MAX_LOOKBACK_HOURS to find a cycle that herbie can resolve (i.e. whose
    IDX file is already published on S3).

    Args:
        model: herbie model name (e.g. "hrrr").
        fxx:   Forecast hour (default 0 = analysis).

    Returns:
        datetime of the latest available cycle, or None if none found.
    """
    from datetime import datetime, timezone, timedelta

    MAX_LOOKBACK_HOURS = 6

    try:
        from herbie import Herbie  # type: ignore[import]
    except ImportError as exc:
        raise ImportError("herbie-data is required: pip install herbie-data") from exc

    now_utc = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for hours_back in range(MAX_LOOKBACK_HOURS + 1):
        candidate = now_utc - timedelta(hours=hours_back)
        try:
            H = Herbie(candidate, model=model, fxx=fxx, verbose=False)
            # Accessing .idx triggers an availability check; if it raises, cycle
            # is not yet published.
            _ = H.idx
            log.debug("[grib_cache] resolved cycle: %s", candidate)
            return candidate
        except Exception:
            log.debug("[grib_cache] cycle %s not yet available, stepping back", candidate)
            continue

    log.warning("[grib_cache] no available cycle found for %s in last %dh", model, MAX_LOOKBACK_HOURS)
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_hrrr_field(
    var: str,
    lat: float,
    lon: float,
    fxx: int = 0,
    db=None,
) -> Optional[float]:
    """Fetch a single scalar HRRR field value at (lat, lon).

    Resolves the latest available HRRR cycle, fetches only the matching GRIB2
    message (byte-range via IDX sidecar), caches it on disk, and returns the
    value at the nearest grid point.

    Args:
        var:  Variable key — "TMP_2m" or "DPT_2m".
        lat:  Latitude (WGS-84).
        lon:  Longitude (WGS-84).
        fxx:  Forecast hour offset (default 0 = analysis/F00).
        db:   Optional DB handle for live config reads.

    Returns:
        Scalar field value at the nearest HRRR grid point (Kelvin for temperature),
        or None if unavailable.
    """
    ttl_hours = _get_cache_ttl_hours(db)
    cache_dir = _get_cache_dir(db)

    # Opportunistic eviction on every fetch call (cheap glob scan)
    _evict_expired(cache_dir, ttl_hours)

    cycle_dt = _resolve_latest_cycle("hrrr", fxx=fxx)
    if cycle_dt is None:
        log.warning("[grib_cache] could not resolve HRRR cycle")
        return None

    path = _fetch_grib_slice("hrrr", var, cycle_dt, fxx, cache_dir, ttl_hours)
    if path is None:
        return None

    try:
        value = _read_grib_nearest(path, lat, lon)
        if value is None:
            return None
        log.info("[grib_cache] %s at (%.4f, %.4f) = %.2f", var, lat, lon, value)
        return value
    except Exception as exc:
        log.warning("[grib_cache] cfgrib read failed for %s: %s", path, exc)
        return None


def _read_grib_nearest(path: Path, lat: float, lon: float) -> Optional[float]:
    """Open *path* with cfgrib and return the value at the nearest grid point.

    Extracted into its own function so tests can mock it without patching the
    cfgrib import machinery.

    Args:
        path: Path to a GRIB2 slice file.
        lat:  Target latitude.
        lon:  Target longitude.

    Returns:
        Scalar value (Kelvin for temperature fields), or None if unreadable.
    """
    import cfgrib  # type: ignore[import]
    import numpy as np  # type: ignore[import]

    # cfgrib >=0.9.10 exposes open_datasets; older versions use open_file.
    if hasattr(cfgrib, "open_datasets"):
        ds_list = cfgrib.open_datasets(str(path))
    else:
        ds_list = [cfgrib.open_file(str(path))]

    if not ds_list:
        log.warning("[grib_cache] cfgrib returned no datasets for %s", path)
        return None
    ds = ds_list[0]

    data_vars = list(ds.data_vars)
    if not data_vars:
        log.warning("[grib_cache] no data variables in dataset for %s", path)
        return None

    da = ds[data_vars[0]]

    if "latitude" in ds.coords and "longitude" in ds.coords:
        lats = ds.coords["latitude"].values
        lons = ds.coords["longitude"].values
        lon_query = lon % 360
        dist = np.sqrt((lats - lat) ** 2 + (lons - lon_query) ** 2)
        idx = np.unravel_index(np.argmin(dist), dist.shape)
        return float(da.values[idx])
    else:
        log.warning("[grib_cache] lat/lon coords not found, returning first grid point")
        return float(da.values.flat[0])

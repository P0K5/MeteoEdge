"""Cron capture worker — log model forecasts at fixed lead-time bins.

This script is the SOLE writer to ``model_forecast_log`` after the #422 schema
migration.  It must be invoked by an external scheduler (systemd timer or cron)
at each of the target UTC hours; it must NOT be called from the 5-minute scan
loop in src/scripts/run.py.

Deployment (systemd timer example):
------------------------------------
Create ``/etc/systemd/system/meteoedge-capture.service``::

    [Unit]
    Description=MeteoEdge forecast capture

    [Service]
    Type=oneshot
    WorkingDirectory=/opt/meteoedge
    ExecStart=/opt/meteoedge/.venv/bin/python -m src.scripts.capture_forecasts
    EnvironmentFile=/opt/meteoedge/.env

Create ``/etc/systemd/system/meteoedge-capture.timer``::

    [Unit]
    Description=MeteoEdge forecast capture timer

    [Timer]
    # Fire at each target UTC hour: 06, 12, 18, 21.
    # 12Z is shared: captures BOTH tomorrow@24h lead and today@12h lead.
    OnCalendar=*-*-* 06:02 UTC
    OnCalendar=*-*-* 12:02 UTC
    OnCalendar=*-*-* 18:02 UTC
    OnCalendar=*-*-* 21:02 UTC
    Persistent=true

    [Install]
    WantedBy=timers.target

Enable with::

    systemctl enable --now meteoedge-capture.timer

Independence guarantee:
-----------------------
A scanner crash does NOT affect forecast captures because this script runs as a
separate process triggered by the timer.  Captures at scheduled times are
guaranteed as long as the host is running.

Lead-time bins:
---------------
- 12Z: log forecast for TOMORROW → lead_hours ≈ 24 (next day's typical high)
- 06Z: log forecast for TODAY    → lead_hours ≈ 18
- 12Z: log forecast for TODAY    → lead_hours ≈ 12  (same invocation as 12Z tomorrow)
- 18Z: log forecast for TODAY    → lead_hours ≈ 6
- 21Z: log forecast for TODAY    → lead_hours ≈ 3

sigma_f sourcing policy, by channel (issue #555, ecmwf row updated by #824):
-----------------------------------------------------------------------
EMOS trains ``σ_calibrated = c + d·σ_ensemble``, so ``sigma_f`` must either
carry a genuine dispersion signal or be an honestly-NULL "we don't have
one" — never a silently-invented number. Per-channel decision:

    Channel      | Decision | Rationale
    -------------|----------|-----------------------------------------------
    nws          | Derive   | Climatological σ keyed on lead_hours (see
                 |          | src/data/nws.py:_nws_sigma_for_lead) — a
                 |          | documented static per-lead dispersion table,
                 |          | not a raw ensemble, but a real signal.
    open_meteo   | Derive   | Cross-model stdev across 4 constituent NWP
                 |          | runs (ecmwf_ifs04/gfs_seamless/jma_seamless/
                 |          | best_match) — genuine multi-model spread.
    gfs          | NULL     | Single deterministic run (#548); no ensemble
                 |          | or multi-model spread exists to derive from.
                 |          | (#824 investigation: the 1170/3546 historical
                 |          | rows with non-NULL sigma_f predate PR #563
                 |          | (merged 2026-07-02), when fetch_gfs_with_spread
                 |          | was literally `return
                 |          | fetch_open_meteo_with_spread(...)` -- i.e. gfs
                 |          | duplicated the open_meteo multi-model spread.
                 |          | Expected/historical, not a live bug; every row
                 |          | since #563 is correctly NULL.)
    gefs         | Derive   | Raw (UNFLOORED) stdev of the ~30 GEFS members
                 |          | — the one true ensemble-member spread we
                 |          | capture. SIGMA_FLOOR_F is applied only at
                 |          | consumption (src/model/ensemble_sigma.py:
                 |          | compute_ensemble_sigma), never here.
    hrrr         | NULL     | Single deterministic run, hourly grid only;
                 |          | no member spread. A lagged-run spread (diff
                 |          | between consecutive HRRR cycles) could be
                 |          | derived but requires additional cycle-history
                 |          | plumbing not yet built — left NULL rather
                 |          | than inventing a static number. Revisit if a
                 |          | future issue adds lagged-run ingestion.
    nbm          | NULL     | NBM itself blends models internally but the
                 |          | open endpoint used here exposes only the
                 |          | point forecast, not its internal spread —
                 |          | left NULL rather than a fabricated constant.
                 |          | (#824 investigation: NOAA does publish an NBM
                 |          | "qmd" percentile file — 10/25/50/75/90th pct —
                 |          | a genuine spread proxy without needing a full
                 |          | ensemble, but it is a distinct GRIB
                 |          | product/variable not yet in grib_cache's
                 |          | SUPPORTED_VARS; scope as its own follow-up
                 |          | issue rather than folded into this one.)
    ecmwf        | Derive   | (#824) forecast_high_f still comes from the
                 |          | "oper" (HRES) deterministic product, but
                 |          | sigma_f is now the raw (UNFLOORED) stdev of
                 |          | the separate "enfo" ENS product's ~51 members
                 |          | (see fetch_ecmwf_ensemble_spread() in
                 |          | src/data/ecmwf_open.py). The pre-#824
                 |          | assumption that Open Data doesn't expose the
                 |          | ECMWF ensemble was wrong — it's exposed under
                 |          | a different ``product=`` than the
                 |          | deterministic run. NULL when the ENS cycle/
                 |          | fetch is unavailable (never fabricated).
    icon         | NULL     | Single deterministic run (ICON-EU). DWD does
                 |          | publish an ICON-EPS ensemble (icon-eu-eps,
                 |          | ~40 members) at a different opendata path,
                 |          | but it is not yet wired here — new endpoint,
                 |          | new member-loop plumbing (#824 follow-up:
                 |          | scope as its own issue rather than folded
                 |          | into this one).

EMOS trains per-channel (see ``forecast_source``/``regime`` filtering in
src/model/emos_calibration.py:fetch_training_data), so a NULL sigma_f for
a given channel just means that channel's EMOS fit falls back to
``FORECAST_STDDEV_F`` rather than being blocked — it does not stop other
channels from training on their own real sigma. See also
docs/OPERATIONS.md → "Architectural Decisions" for the committed decision
table and scripts/check_emos_data_quality.py for floor-saturation / NULL
reporting per channel.

Usage:
    python -m src.scripts.capture_forecasts [--dry-run]

    --dry-run  Fetch forecasts but do not write to the database.
"""
from __future__ import annotations

import argparse
import logging
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta, timezone

from src.config import STATIONS, STATION_TZ
from src.data.db import Database
from src.data.gefs import fetch_gefs_ensemble
from src.data.nws import fetch_nws_with_spread, fetch_nws_forecast_high
from src.data.open_meteo import (
    fetch_open_meteo_with_spread,
    fetch_gfs_with_spread,
    fetch_secondary_forecast,
)
from src.logging_config import setup_logging
from src.model.ensemble_sigma import raw_member_sigma

log = logging.getLogger(__name__)

# Mapping: UTC hour of invocation → list of (target_day_offset, lead_hours)
# target_day_offset: 0 = today, 1 = tomorrow
# lead_hours: the lead-time bin label stored in model_forecast_log
_CAPTURE_SCHEDULE: dict[int, list[tuple[int, int]]] = {
    6:  [(0, 18)],          # 06Z → today at lead ≈ 18h
    12: [(1, 24), (0, 12)], # 12Z → tomorrow@24h AND today@12h
    18: [(0, 6)],           # 18Z → today at lead ≈ 6h
    21: [(0, 3)],           # 21Z → today at lead ≈ 3h
}

# Explicit per-fetch timeout (issue #717). Prime suspect for the 2026-07-07
# silent death: a hung herbie GRIB download blocked the capture loop
# indefinitely (log frozen mid-stream, no traceback). 90s comfortably covers
# a slow-but-healthy GRIB byte-range fetch or HTTP call while still letting
# the loop move on quickly from a genuinely hung download. This is the
# in-process defense; deploy/systemd/meteoedge-capture-forecasts.service's
# TimeoutStartSec is the process-level backstop.
CAPTURE_FETCH_TIMEOUT_SECONDS = float(os.getenv("CAPTURE_FETCH_TIMEOUT_SECONDS", "90"))


# Issue #895: the GEFS cache warm-up gets its own, larger budget. It is doing
# 31 member GRIB downloads in one call -- structurally ~31x the work of every
# other per-station fetch -- so holding it to the same per-fetch timeout is what
# produced the bug this exists to fix.
CAPTURE_GEFS_WARMUP_TIMEOUT_SECONDS = float(
    os.getenv("CAPTURE_GEFS_WARMUP_TIMEOUT_SECONDS", "600")
)


def _warm_gefs_cache(dry_run: bool = False) -> None:
    """Pre-download the GEFS member GRIB slices once per run (issue #895).

    **The bug this fixes.** ``fetch_gefs_ensemble`` downloads 31 member GRIB
    files, cached by ``(model, var, cycle, fxx, member)`` -- *not* by station.
    So on a fresh GEFS cycle the FIRST station in ``STATIONS`` pays for all 31
    downloads while stations 2..30 read from the warm cache. ``STATIONS[0]`` is
    KORD, which therefore blew the 90s per-fetch timeout on nearly every run:
    98 failures against 8-10 for every other station, 100% of them
    ``exceeded 90s timeout``, clustered at capture-run start times.

    The downstream cost was not a missing row. With no ``gefs`` row, EMOS
    training for KORD fell through to the next model with a non-NULL sigma_f --
    ``nws``, whose sigma is a per-lead climatological CONSTANT. With sigma
    constant, ``c`` and ``d`` in ``sigma_cal = c + d*sigma_raw`` collapse onto a
    flat ridge and ``d`` is unidentifiable (see issue #893). KORD -- the
    most-traded station and the one docs/REMEDIATION_PLAN.md's diagnosis is
    built on -- had degenerate sigma calibration because it sorts first in a
    Python list.

    Warming the shared cache once, before the loop, means no station pays the
    cold start. Deliberately NOT fixed by reordering ``STATIONS``: that only
    moves the penalty to whichever station lands first, and silently changes
    which station ends up degenerate.

    Failure here is non-fatal and unlogged beyond a warning -- every station
    still attempts its own fetch, so a failed warm-up restores exactly the
    previous behaviour rather than skipping GEFS.
    """
    if dry_run:
        log.info("[capture] gefs warm-up skipped (dry run)")
        return
    # Any station's coordinates warm the same per-cycle member cache; the
    # lat/lon only selects the grid point read out of the already-downloaded
    # slice. STATIONS[0] is the one that would otherwise pay this cost.
    _, lat, lon, *_rest = STATIONS[0]
    started = time.monotonic()
    try:
        members = _call_with_timeout(
            fetch_gefs_ensemble, lat, lon,
            station="__warmup__", _label="gefs-warmup",
            _timeout=CAPTURE_GEFS_WARMUP_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        log.warning(
            "[capture] gefs cache warm-up failed after %.0fs: %s -- "
            "per-station fetches will fall back to cold-start behaviour",
            time.monotonic() - started, exc,
        )
        return
    log.info(
        "[capture] gefs cache warmed: %d members in %.0fs",
        len(members or []), time.monotonic() - started,
    )


def _call_with_timeout(func, *args, _label: str = "", _timeout: "float | None" = None,
                       **kwargs):
    """Call *func(*args, **kwargs)* with a hard wall-clock timeout.

    Runs func in a single-use worker thread and waits up to *_timeout*
    seconds, defaulting to CAPTURE_FETCH_TIMEOUT_SECONDS. Raises TimeoutError
    if it does not complete in time -- callers must catch this (along with any
    exception func itself might raise) and continue with the next
    station/model rather than letting it propagate (issue #717).

    *_timeout* exists for the GEFS warm-up (issue #895), which does 31 member
    GRIB downloads in one call and legitimately needs a larger budget than a
    single-endpoint per-station fetch.

    The worker thread is NOT forcibly killed on timeout (Python has no safe
    way to do that); it is abandoned to finish or error out on its own in the
    background while this function returns immediately, which is exactly what
    keeps the capture loop from stalling the way it did on 2026-07-07.
    """
    timeout = CAPTURE_FETCH_TIMEOUT_SECONDS if _timeout is None else _timeout
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"capture-{_label or 'fetch'}")
    future = pool.submit(func, *args, **kwargs)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError:
        raise TimeoutError(
            f"{_label or getattr(func, '__name__', 'fetch')} "
            f"exceeded {timeout:.0f}s timeout"
        )
    finally:
        pool.shutdown(wait=False)


def _target_date(day_offset: int) -> str:
    """Return the ISO date string for today+day_offset in UTC."""
    return (datetime.now(timezone.utc).date() + timedelta(days=day_offset)).isoformat()


def run_captures(db, *, dry_run: bool = False, force: bool = False) -> None:
    """Run all captures appropriate for the current UTC hour.

    For each station and each (day_offset, lead_hours) pair scheduled at the
    current UTC hour, fetches NWS / Open-Meteo / GFS forecasts (with spread
    where available) and writes them to model_forecast_log via
    db.upsert_forecast_log_v2().

    Args:
        db:      Database instance (or None in dry-run mode).
        dry_run: If True, fetch but do not write to the database.
        force:   If True, bypass the scheduled-hour gate and run captures
                 immediately (for manual smoke tests).
    """
    utc_hour = datetime.now(timezone.utc).hour
    captures = _CAPTURE_SCHEDULE.get(utc_hour, [])

    if not captures:
        if force:
            # Use 6Z captures as a representative set when forcing outside schedule
            captures = _CAPTURE_SCHEDULE[6]
            log.info(
                "[capture] --now/--force: UTC hour %02dZ is not a scheduled time; "
                "running 06Z capture set as smoke test",
                utc_hour,
            )
        else:
            log.info(
                "[capture] UTC hour %02dZ is not a scheduled capture time. "
                "Scheduled hours: %s",
                utc_hour, sorted(_CAPTURE_SCHEDULE.keys()),
            )
            return

    log.info(
        "[capture] UTC hour %02dZ → %d capture(s): %s",
        utc_hour, len(captures), captures,
    )

    issued_at = datetime.now(timezone.utc).isoformat()

    _warm_gefs_cache(dry_run=dry_run)

    for station, lat, lon, city, *_ in STATIONS:
        for day_offset, lead_hours in captures:
            target_date = _target_date(day_offset)
            log.info(
                "[capture] station=%s target_date=%s lead_hours=%d",
                station, target_date, lead_hours,
            )

            try:
                _capture_station(
                    db=db,
                    station=station,
                    lat=lat,
                    lon=lon,
                    target_date=target_date,
                    lead_hours=lead_hours,
                    issued_at=issued_at,
                    dry_run=dry_run,
                )
            except Exception as exc:
                # Defense in depth (issue #717): _capture_station already
                # isolates each channel's fetch with its own timeout + try/
                # except, but this outer guard guarantees that even an
                # unexpected failure for one station/lead-time combination
                # can never kill the rest of the capture run.
                log.error(
                    "[capture] station=%s target_date=%s lead_hours=%d capture failed "
                    "unexpectedly -- continuing with next station/model: %s",
                    station, target_date, lead_hours, exc, exc_info=True,
                )


def _capture_station(
    *,
    db,
    station: str,
    lat: float,
    lon: float,
    target_date: str,
    lead_hours: int,
    issued_at: str,
    dry_run: bool,
) -> None:
    """Fetch and log forecasts for a single station at a given lead-time."""
    # --- NWS ---
    try:
        nws_result = _call_with_timeout(fetch_nws_with_spread, lat, lon, lead_hours, _label="nws")
    except Exception as exc:
        log.warning("[capture] %s nws fetch failed/timed out (lead=%dh): %s", station, lead_hours, exc)
        nws_result = None
    if nws_result is not None:
        nws_mu, nws_sigma = nws_result
        log.info(
            "[capture] %s nws mu=%.1fF sigma=%.2fF lead=%dh date=%s",
            station, nws_mu, nws_sigma, lead_hours, target_date,
        )
        if not dry_run and db is not None:
            db.upsert_forecast_log_v2(
                station=station,
                model="nws",
                date=target_date,
                forecast_high_f=nws_mu,
                lead_hours=lead_hours,
                issued_at=issued_at,
                sigma_f=nws_sigma,
            )
    else:
        log.debug("[capture] %s nws unavailable (lead=%dh)", station, lead_hours)

    # --- Open-Meteo (best_match / multi-model) ---
    try:
        om_result = _call_with_timeout(fetch_open_meteo_with_spread, lat, lon, lead_hours, _label="open_meteo")
    except Exception as exc:
        log.warning("[capture] %s open_meteo fetch failed/timed out (lead=%dh): %s", station, lead_hours, exc)
        om_result = None
    if om_result is not None:
        om_mu, om_sigma = om_result
        log.info(
            "[capture] %s open_meteo mu=%.1fF sigma=%.2fF lead=%dh date=%s",
            station, om_mu, om_sigma, lead_hours, target_date,
        )
        if not dry_run and db is not None:
            db.upsert_forecast_log_v2(
                station=station,
                model="open_meteo",
                date=target_date,
                forecast_high_f=om_mu,
                lead_hours=lead_hours,
                issued_at=issued_at,
                sigma_f=om_sigma,
            )
    else:
        # Fallback: try single-model fetch
        try:
            om_mu = _call_with_timeout(fetch_secondary_forecast, lat, lon, _label="open_meteo_fallback")
        except Exception as exc:
            log.warning(
                "[capture] %s open_meteo fallback fetch failed/timed out (lead=%dh): %s",
                station, lead_hours, exc,
            )
            om_mu = None
        if om_mu is not None:
            log.info(
                "[capture] %s open_meteo mu=%.1fF sigma=None (single-model fallback) lead=%dh date=%s",
                station, om_mu, lead_hours, target_date,
            )
            if not dry_run and db is not None:
                db.upsert_forecast_log_v2(
                    station=station,
                    model="open_meteo",
                    date=target_date,
                    forecast_high_f=om_mu,
                    lead_hours=lead_hours,
                    issued_at=issued_at,
                    sigma_f=None,
                )
        else:
            log.debug("[capture] %s open_meteo unavailable (lead=%dh)", station, lead_hours)

    # --- GFS ---
    try:
        gfs_result = _call_with_timeout(fetch_gfs_with_spread, lat, lon, lead_hours, _label="gfs")
    except Exception as exc:
        log.warning("[capture] %s gfs fetch failed/timed out (lead=%dh): %s", station, lead_hours, exc)
        gfs_result = None
    if gfs_result is not None:
        gfs_mu, gfs_sigma = gfs_result
        # gfs_sigma is None since #548 (single deterministic model, no member
        # spread) — format defensively so a None never crashes the %.2f log.
        log.info(
            "[capture] %s gfs mu=%.1fF sigma=%s lead=%dh date=%s",
            station, gfs_mu,
            "None" if gfs_sigma is None else f"{gfs_sigma:.2f}F",
            lead_hours, target_date,
        )
        if not dry_run and db is not None:
            db.upsert_forecast_log_v2(
                station=station,
                model="gfs",
                date=target_date,
                forecast_high_f=gfs_mu,
                lead_hours=lead_hours,
                issued_at=issued_at,
                sigma_f=gfs_sigma,
            )
    else:
        log.debug("[capture] %s gfs unavailable (lead=%dh)", station, lead_hours)

    # --- GEFS ensemble ---
    try:
        gefs_raw = _call_with_timeout(fetch_gefs_ensemble, lat, lon, station=station, _label="gefs")
    except Exception as exc:
        log.warning("[capture] %s gefs unavailable (lead=%dh): %s", station, lead_hours, exc)
        gefs_raw = []

    if gefs_raw:
        # Convert GEFSMemberForecast.temp_k (Kelvin) → °F for downstream consumers
        gefs_members = [(m.temp_k - 273.15) * 9.0 / 5.0 + 32.0 for m in gefs_raw]
        gefs_mu = statistics.mean(gefs_members)
        # #555: log the RAW (unfloored) member-spread sigma — SIGMA_FLOOR_F must
        # only be applied at consumption time (see src/model/ensemble_sigma.py),
        # never baked into what's persisted here. raw_member_sigma() returns
        # None when it cannot be computed (e.g. <2 members); format defensively
        # so a None never crashes the %.2f log, mirroring the gfs channel above.
        gefs_sigma = raw_member_sigma(gefs_members)
        log.info(
            "[capture] %s gefs mu=%.1fF sigma=%s (raw) members=%d lead=%dh date=%s",
            station, gefs_mu,
            "None" if gefs_sigma is None else f"{gefs_sigma:.2f}F",
            len(gefs_members), lead_hours, target_date,
        )
        if not dry_run and db is not None:
            db.upsert_forecast_log_v2(
                station=station,
                model="gefs",
                date=target_date,
                forecast_high_f=gefs_mu,
                lead_hours=lead_hours,
                issued_at=issued_at,
                sigma_f=gefs_sigma,
            )
    else:
        log.warning("[capture] %s gefs unavailable (lead=%dh)", station, lead_hours)

    # --- HRRR ---
    try:
        from src.data.hrrr import fetch_hrrr_hourly
        hrrr_rows = _call_with_timeout(fetch_hrrr_hourly, lat, lon, station=station, _label="hrrr")
        if hrrr_rows:
            hrrr_mu = statistics.mean(r.temp_f for r in hrrr_rows)
            log.info("[capture] %s hrrr mu=%.1fF lead=%dh date=%s", station, hrrr_mu, lead_hours, target_date)
            if not dry_run and db is not None:
                db.upsert_forecast_log_v2(
                    station=station, model="hrrr", date=target_date,
                    forecast_high_f=hrrr_mu, lead_hours=lead_hours,
                    issued_at=issued_at, sigma_f=None,
                )
        else:
            log.debug("[capture] %s hrrr unavailable or out-of-domain (lead=%dh)", station, lead_hours)
    except Exception as exc:
        log.warning("[capture] %s hrrr ingestion failed: %s", station, exc)

    # --- NBM ---
    try:
        from src.data.nbm import fetch_nbm_daily_high
        nbm_result = _call_with_timeout(
            fetch_nbm_daily_high, lat, lon, station=station, target_date=target_date, _label="nbm",
        )
        if nbm_result is not None:
            log.info("[capture] %s nbm mu=%.1fF lead=%dh date=%s", station, nbm_result.forecast_high_f, lead_hours, target_date)
            if not dry_run and db is not None:
                db.upsert_forecast_log_v2(
                    station=station, model="nbm", date=target_date,
                    forecast_high_f=nbm_result.forecast_high_f, lead_hours=lead_hours,
                    issued_at=issued_at, sigma_f=None,
                )
        else:
            log.debug("[capture] %s nbm unavailable or out-of-domain (lead=%dh)", station, lead_hours)
    except Exception as exc:
        log.warning("[capture] %s nbm ingestion failed: %s", station, exc)

    # --- ECMWF ---
    try:
        from src.data.ecmwf_open import fetch_ecmwf_daily_high, fetch_ecmwf_ensemble_spread
        ecmwf_result = _call_with_timeout(
            fetch_ecmwf_daily_high, lat, lon, station=station, target_date=target_date, _label="ecmwf",
        )
        if ecmwf_result is not None:
            # #824: derive sigma_f from the separate ECMWF ENS ("enfo") product
            # -- a genuine raw (unfloored) member-spread signal, independent of
            # the HRES ("oper") deterministic run used for forecast_high_f
            # above. An ENS fetch failure/timeout must never block the HRES
            # row from being written; it only demotes sigma_f to NULL.
            try:
                ecmwf_sigma = _call_with_timeout(
                    fetch_ecmwf_ensemble_spread, lat, lon, station=station,
                    target_date=target_date, _label="ecmwf_ens",
                )
            except Exception as exc:
                log.warning(
                    "[capture] %s ecmwf ensemble spread fetch failed/timed out (lead=%dh): %s",
                    station, lead_hours, exc,
                )
                ecmwf_sigma = None
            log.info(
                "[capture] %s ecmwf mu=%.1fF sigma=%s lead=%dh date=%s",
                station, ecmwf_result.forecast_high_f,
                "None" if ecmwf_sigma is None else f"{ecmwf_sigma:.2f}F",
                lead_hours, target_date,
            )
            if not dry_run and db is not None:
                db.upsert_forecast_log_v2(
                    station=station, model="ecmwf", date=target_date,
                    forecast_high_f=ecmwf_result.forecast_high_f, lead_hours=lead_hours,
                    issued_at=issued_at, sigma_f=ecmwf_sigma,
                )
        else:
            log.debug("[capture] %s ecmwf unavailable (lead=%dh)", station, lead_hours)
    except Exception as exc:
        log.warning("[capture] %s ecmwf ingestion failed: %s", station, exc)

    # --- ICON ---
    try:
        from src.data.icon import fetch_icon_hourly
        icon_rows = _call_with_timeout(fetch_icon_hourly, lat, lon, station=station, _label="icon")
        if icon_rows:
            icon_mu = statistics.mean(r.temp_f for r in icon_rows)
            log.info("[capture] %s icon mu=%.1fF lead=%dh date=%s", station, icon_mu, lead_hours, target_date)
            if not dry_run and db is not None:
                db.upsert_forecast_log_v2(
                    station=station, model="icon", date=target_date,
                    forecast_high_f=icon_mu, lead_hours=lead_hours,
                    issued_at=issued_at, sigma_f=None,
                )
        else:
            log.debug("[capture] %s icon unavailable or out-of-domain (lead=%dh)", station, lead_hours)
    except Exception as exc:
        log.warning("[capture] %s icon ingestion failed: %s", station, exc)


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Capture forecast data at fixed lead-time bins for EMOS training."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Fetch forecasts but do not write to the database.",
    )
    parser.add_argument(
        "--now", "--force",
        dest="force",
        action="store_true",
        help="Run captures immediately regardless of UTC hour (for manual smoke tests).",
    )
    args = parser.parse_args()

    if args.dry_run:
        log.info("[capture] DRY RUN — no database writes will occur")
        run_captures(db=None, dry_run=True, force=args.force)
    else:
        db = Database()
        run_captures(db=db, dry_run=False, force=args.force)

    log.info("[capture] Done.")


if __name__ == "__main__":
    main()

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

Usage:
    python -m src.scripts.capture_forecasts [--dry-run]

    --dry-run  Fetch forecasts but do not write to the database.
"""
from __future__ import annotations

import argparse
import logging
import statistics
from datetime import datetime, timedelta, timezone

from src.config import STATIONS, STATION_TZ
from src.data.db import Database
from src.data.gefs import fetch_gefs_ensemble
from src.data.nws import fetch_nws_with_spread, fetch_nws_forecast_high
from src.data.open_meteo import (
    fetch_open_meteo_with_spread,
    fetch_gfs_with_spread,
    fetch_secondary_forecast,
    fetch_gfs_forecast_high,
)
from src.logging_config import setup_logging
from src.model.ensemble_sigma import compute_ensemble_sigma

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


def _target_date(day_offset: int) -> str:
    """Return the ISO date string for today+day_offset in UTC."""
    return (datetime.now(timezone.utc).date() + timedelta(days=day_offset)).isoformat()


def run_captures(db, *, dry_run: bool = False) -> None:
    """Run all captures appropriate for the current UTC hour.

    For each station and each (day_offset, lead_hours) pair scheduled at the
    current UTC hour, fetches NWS / Open-Meteo / GFS forecasts (with spread
    where available) and writes them to model_forecast_log via
    db.upsert_forecast_log_v2().

    Args:
        db:      Database instance (or None in dry-run mode).
        dry_run: If True, fetch but do not write to the database.
    """
    utc_hour = datetime.now(timezone.utc).hour
    captures = _CAPTURE_SCHEDULE.get(utc_hour, [])

    if not captures:
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

    for station, lat, lon, city, *_ in STATIONS:
        for day_offset, lead_hours in captures:
            target_date = _target_date(day_offset)
            log.info(
                "[capture] station=%s target_date=%s lead_hours=%d",
                station, target_date, lead_hours,
            )

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
    nws_result = fetch_nws_with_spread(lat, lon, lead_hours)
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
    om_result = fetch_open_meteo_with_spread(lat, lon, lead_hours)
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
        om_mu = fetch_secondary_forecast(lat, lon)
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
    gfs_result = fetch_gfs_with_spread(lat, lon, lead_hours)
    if gfs_result is not None:
        gfs_mu, gfs_sigma = gfs_result
        log.info(
            "[capture] %s gfs mu=%.1fF sigma=%.2fF lead=%dh date=%s",
            station, gfs_mu, gfs_sigma, lead_hours, target_date,
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
        # Fallback: try single-model GFS fetch
        gfs_mu = fetch_gfs_forecast_high(lat, lon)
        if gfs_mu is not None:
            log.info(
                "[capture] %s gfs mu=%.1fF sigma=None (single-model fallback) lead=%dh date=%s",
                station, gfs_mu, lead_hours, target_date,
            )
            if not dry_run and db is not None:
                db.upsert_forecast_log_v2(
                    station=station,
                    model="gfs",
                    date=target_date,
                    forecast_high_f=gfs_mu,
                    lead_hours=lead_hours,
                    issued_at=issued_at,
                    sigma_f=None,
                )
        else:
            log.debug("[capture] %s gfs unavailable (lead=%dh)", station, lead_hours)

    # --- GEFS ensemble ---
    try:
        gefs_raw = fetch_gefs_ensemble(lat, lon, station=station)
    except Exception as exc:
        log.warning("[capture] %s gefs unavailable (lead=%dh): %s", station, lead_hours, exc)
        gefs_raw = []

    if gefs_raw:
        # Convert GEFSMemberForecast.temp_k (Kelvin) → °F for downstream consumers
        gefs_members = [(m.temp_k - 273.15) * 9.0 / 5.0 + 32.0 for m in gefs_raw]
        gefs_mu = statistics.mean(gefs_members)
        gefs_sigma = compute_ensemble_sigma(gefs_members, station, history_db=db)
        log.info(
            "[capture] %s gefs mu=%.1fF sigma=%.2fF members=%d lead=%dh date=%s",
            station, gefs_mu, gefs_sigma, len(gefs_members), lead_hours, target_date,
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
        hrrr_rows = fetch_hrrr_hourly(lat, lon, station=station)
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
        nbm_result = fetch_nbm_daily_high(lat, lon, station=station, target_date=target_date)
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
        from src.data.ecmwf_open import fetch_ecmwf_daily_high
        ecmwf_result = fetch_ecmwf_daily_high(lat, lon, station=station, target_date=target_date)
        if ecmwf_result is not None:
            log.info("[capture] %s ecmwf mu=%.1fF lead=%dh date=%s", station, ecmwf_result.forecast_high_f, lead_hours, target_date)
            if not dry_run and db is not None:
                db.upsert_forecast_log_v2(
                    station=station, model="ecmwf", date=target_date,
                    forecast_high_f=ecmwf_result.forecast_high_f, lead_hours=lead_hours,
                    issued_at=issued_at, sigma_f=None,
                )
        else:
            log.debug("[capture] %s ecmwf unavailable (lead=%dh)", station, lead_hours)
    except Exception as exc:
        log.warning("[capture] %s ecmwf ingestion failed: %s", station, exc)

    # --- ICON ---
    try:
        from src.data.icon import fetch_icon_hourly
        icon_rows = fetch_icon_hourly(lat, lon, station=station)
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
    args = parser.parse_args()

    if args.dry_run:
        log.info("[capture] DRY RUN — no database writes will occur")
        run_captures(db=None, dry_run=True)
    else:
        db = Database()
        run_captures(db=db, dry_run=False)

    log.info("[capture] Done.")


if __name__ == "__main__":
    main()

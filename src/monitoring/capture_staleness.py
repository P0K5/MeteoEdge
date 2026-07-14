"""Forecast-capture staleness watchdog (issue #717).

Watches MAX(model_forecast_log.logged_at) and raises a visible ERROR-level
alert when the forecast-capture job (src/scripts/capture_forecasts.py, run by
the separate meteoedge-capture-forecasts.service/.timer) has gone silently
stale. This is exactly the failure mode found in the 2026-07-13 production
health audit: on 2026-07-07 13:46 UTC a hung herbie GRIB download left the
capture oneshot unit stuck in `activating` state (systemd timers skip firing
while a oneshot is still active), so the job stayed dead for 6+ days with
zero errors logged anywhere and nobody noticed.

This monitor is READ-ONLY with respect to the capture job: it only observes
model_forecast_log via the existing Database access layer (src/data/db.py)
and never touches the capture process itself. The process-level backstop for
a hung run is `TimeoutStartSec` on
deploy/systemd/meteoedge-capture-forecasts.service.

Usage -- wired into the main poll loop (src/scripts/run.py) so alerts land in
logs/bot.log, the only log file continuously written by a long-running
process. The capture job itself runs as a separate oneshot systemd unit with
its own log file and cannot be relied on to log its own death:

    from src.monitoring.capture_staleness import check_capture_staleness
    check_capture_staleness(db)

Also exposed read-only via GET /api/forecast-capture-health for the dashboard
health tile (src/dashboard/api.py), via get_capture_health().
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from src.config import CONFIG_DEFAULTS

log = logging.getLogger(__name__)

_CONFIG_KEY = "FORECAST_CAPTURE_STALENESS_THRESHOLD_HOURS"

# De-duplication: don't re-log the same ERROR every poll tick (every
# POLL_INTERVAL_SECONDS, default 5 min) while the condition persists -- once
# per hour is enough to stay visible in bot.log without spamming it during a
# multi-day outage like the one that prompted this issue.
_ALERT_DEDUP_HOURS = 1.0

# Module-level state: last time an ALERT was logged (None = not currently alerting).
_last_alert_time: "datetime | None" = None


def _threshold_hours(db) -> float:
    """Read the staleness threshold from DB config, falling back to the hardcoded default."""
    default = float(CONFIG_DEFAULTS[_CONFIG_KEY])
    if db is None:
        return default
    try:
        raw = db.get_config(_CONFIG_KEY)
        return float(raw) if raw is not None else default
    except (ValueError, TypeError):
        return default


def get_capture_health(db, *, now: "datetime | None" = None) -> dict:
    """Return the current forecast-capture staleness state, read-only.

    Args:
        db:  Database instance (see src/data/db.py). May be None.
        now: Override "current time" for testing; defaults to UTC now.

    Returns:
        dict with:
            last_logged_at:  ISO timestamp of the most recent
                              model_forecast_log row, or None if the table is
                              empty/missing/unreadable.
            age_hours:        Hours since last_logged_at, or None when
                               last_logged_at is None or unparseable.
            threshold_hours:  The active staleness threshold (hours).
            stale:            True if age_hours exceeds threshold_hours, or if
                               no capture has ever run / the timestamp could
                               not be parsed.
    """
    now = now or datetime.now(timezone.utc)
    threshold_hours = _threshold_hours(db)

    last_logged_at = None
    if db is not None:
        try:
            last_logged_at = db.get_last_forecast_capture_ts()
        except Exception as exc:
            log.warning("[capture-staleness] DB read failed: %s", exc)

    if last_logged_at is None:
        return {
            "last_logged_at": None,
            "age_hours": None,
            "threshold_hours": threshold_hours,
            "stale": True,
        }

    try:
        if not isinstance(last_logged_at, str):
            raise TypeError(f"expected str, got {type(last_logged_at).__name__}")
        last_dt = datetime.fromisoformat(last_logged_at.replace("Z", "+00:00"))
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError, TypeError):
        log.warning("[capture-staleness] unparseable logged_at value: %r", last_logged_at)
        return {
            "last_logged_at": last_logged_at,
            "age_hours": None,
            "threshold_hours": threshold_hours,
            "stale": True,
        }

    age_hours = (now - last_dt).total_seconds() / 3600.0
    return {
        "last_logged_at": last_logged_at,
        "age_hours": age_hours,
        "threshold_hours": threshold_hours,
        "stale": age_hours > threshold_hours,
    }


def check_capture_staleness(db, *, now: "datetime | None" = None) -> dict:
    """Evaluate forecast-capture staleness and log an ERROR (deduped) when stale.

    Intended to be called once per poll tick from src/scripts/run.py so alerts
    surface in logs/bot.log. Returns the same dict as get_capture_health() so
    callers (e.g. the dashboard endpoint) can reuse a single read.
    """
    global _last_alert_time
    now = now or datetime.now(timezone.utc)
    health = get_capture_health(db, now=now)

    if not health["stale"]:
        if _last_alert_time is not None:
            log.info(
                "[capture-staleness] forecast capture recovered (age=%.1fh, threshold=%.1fh)",
                health["age_hours"], health["threshold_hours"],
            )
        _last_alert_time = None
        return health

    if _last_alert_time is not None and (now - _last_alert_time) < timedelta(hours=_ALERT_DEDUP_HOURS):
        return health

    if health["age_hours"] is None:
        log.error(
            "[capture-staleness] ALERT: no readable model_forecast_log rows -- "
            "forecast capture may have never run, or logged_at is unparseable. "
            "Check meteoedge-capture-forecasts.service/.timer."
        )
    else:
        log.error(
            "[capture-staleness] ALERT: forecast capture stale -- last write %.1fh ago "
            "(threshold=%.1fh, last_logged_at=%s). Check meteoedge-capture-forecasts.service/.timer "
            "(issue #717 -- prior incident: hung herbie GRIB download left the oneshot unit stuck "
            "in 'activating' state, silently blocking all future scheduled runs).",
            health["age_hours"], health["threshold_hours"], health["last_logged_at"],
        )
    _last_alert_time = now
    return health

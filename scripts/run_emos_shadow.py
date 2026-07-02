#!/usr/bin/env python3
"""Daily EMOS shadow calibration runner.

Iterates all cities in STATIONS, fits emos_shadow coefficients for cities
with enough training data, skips gracefully when data is insufficient.

For every city that fits, a CRPS score row is appended to ``emos_crps_log``
(via ``db.log_crps``). This per-day record is what the promotion guard in
``emos_mode.get_city_mode`` counts against ``EMOS_MIN_SAMPLES`` before a city
is allowed to serve ``emos_primary`` — without it, promotion stays blocked
forever because the sample count never leaves zero.

    python scripts/run_emos_shadow.py [--db-path PATH]
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def _enabled() -> bool:
    """Return False when EMOS_SHADOW_ENABLED is explicitly disabled."""
    if os.environ.get("EMOS_SHADOW_ENABLED", "true").lower() == "false":
        log.debug("[emos_shadow] EMOS_SHADOW_ENABLED=false — skipping")
        return False
    return True


def _run_calibration(db, stack: str = "baseline") -> None:
    """Fit emos_shadow coefficients for every city and log a daily CRPS row.

    For each city with sufficient training data:
      1. Fit (a, b, c, d) by minimising mean CRPS.
      2. Persist coefficients (model_mode='emos_shadow', ready_for_promotion=0).
      3. Append one CRPS row to emos_crps_log for today's date — deduplicated so
         a same-day re-run (e.g. after a process restart) cannot double-count.
    """
    from src.config import FORECAST_STACK_MODELS, STATIONS, station_city
    from src.model.crps_score import crps_gaussian
    from src.model.emos_calibration import (
        fetch_training_data,
        fit_emos,
        save_coefficients,
        InsufficientDataError,
    )

    regime = FORECAST_STACK_MODELS.get(stack, FORECAST_STACK_MODELS["baseline"])
    log.info("[emos_shadow] FORECAST_STACK=%s, regime=%s", stack, sorted(regime))

    today = datetime.now(timezone.utc).date().isoformat()
    fitted = 0
    skipped = 0

    for station_cfg in STATIONS:
        city = station_city(station_cfg)
        try:
            training_data = fetch_training_data(
                city, db, regime=regime, forecast_source=stack,
            )
            a, b, c, d = fit_emos(training_data)
            crps_scores = [
                crps_gaussian(a + b * mu, c + d * sigma, y)
                for mu, sigma, y in training_data
            ]
            mean_crps = sum(crps_scores) / len(crps_scores) if crps_scores else 0.0
            save_coefficients(
                city,
                a,
                b,
                c,
                d,
                mean_crps,
                db,
                forecast_source=stack,
                sample_count=len(training_data),
            )

            # One CRPS sample per city per calendar day. The promotion guard
            # counts these rows, so logging exactly once a day gives the
            # operator an honest "days of shadow evidence" measure.
            if db.emos_crps_logged_for_date(city, today, "emos_shadow"):
                log.debug(
                    "[emos_shadow] city=%s: CRPS already logged for %s — skipping",
                    city, today,
                )
            else:
                db.log_crps(city, today, mean_crps, model_mode="emos_shadow")
            log.info(
                "[emos_shadow] city=%s: fit complete, coefficients saved (crps=%.4f)",
                city, mean_crps,
            )
            fitted += 1
        except InsufficientDataError as e:
            log.debug("[emos_shadow] city=%s: insufficient data — %s", city, e)
            skipped += 1
        except Exception as e:
            log.warning("[emos_shadow] city=%s: fit failed — %s", city, e)
            skipped += 1

    log.info("[emos_shadow] done: %d fitted, %d skipped", fitted, skipped)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=os.environ.get("DB_PATH", "meteoedge.db"))
    parser.add_argument(
        "--forecast-stack",
        default=None,
        help="Forecast stack identifier (default: read from DB, fallback to 'baseline')",
    )
    args = parser.parse_args()

    if not _enabled():
        return

    from src.data.db import Database

    db = Database(args.db_path)
    stack = args.forecast_stack or db.get_config("FORECAST_STACK") or "baseline"
    _run_calibration(db, stack=stack)


def main_with_db(db, stack: str | None = None) -> None:
    """Entry point for callers that already have a Database instance."""
    if not _enabled():
        return
    resolved_stack = stack or db.get_config("FORECAST_STACK") or "baseline"
    _run_calibration(db, stack=resolved_stack)


if __name__ == "__main__":
    main()

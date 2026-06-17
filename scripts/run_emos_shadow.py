#!/usr/bin/env python3
"""Daily EMOS shadow calibration runner.

Iterates all cities in STATIONS, fits emos_shadow coefficients for cities
with enough training data, skips gracefully when data is insufficient.

    python scripts/run_emos_shadow.py [--db-path PATH]
"""
import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> None:
    if os.environ.get("EMOS_SHADOW_ENABLED", "true").lower() == "false":
        log.debug("[emos_shadow] EMOS_SHADOW_ENABLED=false — skipping")
        return

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=os.environ.get("DB_PATH", "meteoedge.db"))
    args = parser.parse_args()

    from src.data.db import Database
    from src.config import STATIONS
    from src.model.emos_calibration import (
        fetch_training_data,
        fit_emos,
        save_coefficients,
        InsufficientDataError,
    )

    db = Database(args.db_path)
    fitted = 0
    skipped = 0

    for station_cfg in STATIONS:
        city = station_cfg[3] if isinstance(station_cfg, (list, tuple)) else station_cfg
        try:
            training_data = fetch_training_data(city, db)
            a, b, c, d = fit_emos(training_data)
            # Compute mean CRPS on training data for logging
            from src.model.crps_score import crps_gaussian
            crps_scores = [
                crps_gaussian(a + b * mu, c + d * sigma, y)
                for mu, sigma, y in training_data
            ]
            mean_crps = sum(crps_scores) / len(crps_scores) if crps_scores else 0.0
            save_coefficients(city, a, b, c, d, mean_crps, db)
            log.info("[emos_shadow] city=%s: fit complete, coefficients saved", city)
            fitted += 1
        except InsufficientDataError as e:
            log.debug("[emos_shadow] city=%s: insufficient data — %s", city, e)
            skipped += 1
        except Exception as e:
            log.warning("[emos_shadow] city=%s: fit failed — %s", city, e)
            skipped += 1

    log.info("[emos_shadow] done: %d fitted, %d skipped", fitted, skipped)


def main_with_db(db) -> None:
    """Entry point for callers that already have a Database instance."""
    if os.environ.get("EMOS_SHADOW_ENABLED", "true").lower() == "false":
        log.debug("[emos_shadow] EMOS_SHADOW_ENABLED=false — skipping")
        return

    from src.config import STATIONS
    from src.model.emos_calibration import (
        fetch_training_data,
        fit_emos,
        save_coefficients,
        InsufficientDataError,
    )

    fitted = 0
    skipped = 0

    for station_cfg in STATIONS:
        city = station_cfg[3] if isinstance(station_cfg, (list, tuple)) else station_cfg
        try:
            training_data = fetch_training_data(city, db)
            a, b, c, d = fit_emos(training_data)
            from src.model.crps_score import crps_gaussian
            crps_scores = [
                crps_gaussian(a + b * mu, c + d * sigma, y)
                for mu, sigma, y in training_data
            ]
            mean_crps = sum(crps_scores) / len(crps_scores) if crps_scores else 0.0
            save_coefficients(city, a, b, c, d, mean_crps, db)
            log.info("[emos_shadow] city=%s: fit complete, coefficients saved", city)
            fitted += 1
        except InsufficientDataError as e:
            log.debug("[emos_shadow] city=%s: insufficient data — %s", city, e)
            skipped += 1
        except Exception as e:
            log.warning("[emos_shadow] city=%s: fit failed — %s", city, e)
            skipped += 1

    log.info("[emos_shadow] done: %d fitted, %d skipped", fitted, skipped)


if __name__ == "__main__":
    main()

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
        fetch_training_data_pooled,
        fit_emos,
        pooling_group,
        save_coefficients,
        InsufficientDataError,
    )

    regime = FORECAST_STACK_MODELS.get(stack, FORECAST_STACK_MODELS["baseline"])
    log.info("[emos_shadow] FORECAST_STACK=%s, regime=%s", stack, sorted(regime))

    today = datetime.now(timezone.utc).date().isoformat()
    fitted = 0
    pooled_fitted = 0
    skipped = 0

    def _persist(city, a, b, c, d, training_data, sample_count, provenance):
        """Save coefficients + one CRPS row per city per calendar day.

        The CRPS row is what the promotion guard counts, so logging exactly
        once a day gives the operator an honest "days of shadow evidence"
        measure. CRPS is always scored on the CITY's own triples, even when
        the coefficients came from a pooled fit — that is the evidence that
        matters for promoting THIS city.
        """
        crps_scores = [
            crps_gaussian(a + b * mu, c + d * sigma, y)
            for mu, sigma, y in training_data
        ]
        mean_crps = sum(crps_scores) / len(crps_scores) if crps_scores else 0.0
        save_coefficients(
            city, a, b, c, d, mean_crps, db,
            forecast_source=stack, sample_count=sample_count,
        )
        if db.emos_crps_logged_for_date(city, today, "emos_shadow"):
            log.debug(
                "[emos_shadow] city=%s: CRPS already logged for %s — skipping",
                city, today,
            )
        else:
            db.log_crps(city, today, mean_crps, model_mode="emos_shadow")
        log.info(
            "[emos_shadow] city=%s: %s fit, coefficients saved (crps=%.4f, n_city=%d)",
            city, provenance, mean_crps, len(training_data),
        )

    # ---- Pass 1: per-city fits (min_samples=60, unchanged behavior) ----
    insufficient: list[str] = []
    for station_cfg in STATIONS:
        city = station_city(station_cfg)
        try:
            training_data = fetch_training_data(
                city, db, regime=regime, forecast_source=stack,
            )
            a, b, c, d = fit_emos(training_data)
            _persist(city, a, b, c, d, training_data, len(training_data), "per-city")
            fitted += 1
        except InsufficientDataError as e:
            log.debug("[emos_shadow] city=%s: insufficient data — %s", city, e)
            insufficient.append(city)
        except Exception as e:
            log.warning("[emos_shadow] city=%s: fit failed — %s", city, e)
            skipped += 1

    # ---- Pass 2: pooled fallback (issue #659) ----
    # Cities below the per-city bar receive coefficients from a shared fit
    # over their pooling group (see emos_calibration.pooling_group). The
    # pooled fit uses ALL group members' data (including cities that fit
    # individually — more data, better fit) but only cities WITHOUT an
    # individual fit receive the pooled coefficients: a per-city fit is more
    # specific and always takes precedence. A city must contribute at least
    # POOLED_MIN_CITY_SAMPLES of its own triples to receive pooled
    # coefficients — a fit can't be shadow-scored against a city with no
    # local evidence.
    POOLED_MIN_CITY_SAMPLES = 5
    groups: dict[str, list[str]] = {}
    for city in insufficient:
        g = pooling_group(city)
        if g is None:
            skipped += 1
            continue
        groups.setdefault(g, []).append(city)

    all_cities_by_group: dict[str, list[str]] = {}
    for station_cfg in STATIONS:
        city = station_city(station_cfg)
        g = pooling_group(city)
        if g is not None:
            all_cities_by_group.setdefault(g, []).append(city)

    for g, needy_cities in sorted(groups.items()):
        try:
            pooled, per_city = fetch_training_data_pooled(
                all_cities_by_group.get(g, []), db,
                regime=regime, forecast_source=stack,
            )
            a, b, c, d = fit_emos(pooled)
            log.info(
                "[emos_shadow] group=%s: pooled fit over %d triples from %d cities",
                g, len(pooled), len(per_city),
            )
        except InsufficientDataError as e:
            log.info("[emos_shadow] group=%s: pooled data still insufficient — %s", g, e)
            skipped += len(needy_cities)
            continue
        except Exception as e:
            log.warning("[emos_shadow] group=%s: pooled fit failed — %s", g, e)
            skipped += len(needy_cities)
            continue

        for city in needy_cities:
            n_city = per_city.get(city, 0)
            if n_city < POOLED_MIN_CITY_SAMPLES:
                log.debug(
                    "[emos_shadow] city=%s: only %d own triples (<%d) — no pooled"
                    " coefficients", city, n_city, POOLED_MIN_CITY_SAMPLES,
                )
                skipped += 1
                continue
            try:
                city_triples = fetch_training_data(
                    city, db, min_samples=1, regime=regime, forecast_source=stack,
                )
                _persist(city, a, b, c, d, city_triples, len(pooled),
                         f"pooled({g})")
                pooled_fitted += 1
            except Exception as e:
                log.warning("[emos_shadow] city=%s: pooled persist failed — %s", city, e)
                skipped += 1

    log.info(
        "[emos_shadow] done: %d per-city fits, %d pooled fits, %d skipped",
        fitted, pooled_fitted, skipped,
    )


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

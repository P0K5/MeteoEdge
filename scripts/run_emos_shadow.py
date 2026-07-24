#!/usr/bin/env python3
"""Daily EMOS shadow calibration runner.

Iterates all cities in STATIONS, fits emos_shadow coefficients for cities
with enough training data, skips gracefully when data is insufficient.

For every city that fits, a CRPS score row is appended to ``emos_crps_log``
(via ``db.log_crps``) with ``model_mode="emos_shadow"`` and
``forecast_source=stack``. This per-day record is what the promotion guard in
``emos_mode.get_city_mode`` counts against ``EMOS_MIN_SAMPLES_PROMOTION``
(scoped to the active forecast_source only, issue #759) before a city is
allowed to serve ``emos_primary`` — without it, promotion stays blocked
forever because the sample count never leaves zero. Scoping the row and its
per-day dedup guard by ``forecast_source`` means two stacks calibrated on the
same calendar day each log their own row instead of the second stack's run
silently skipping because the first already claimed that day's slot.

A second row is appended alongside it with ``model_mode="legacy"``: the SAME
training triples scored with their raw, uncorrected ``(mu, sigma)`` — i.e.
the plain equal-weight construction the legacy path actually serves, with no
``a + b*mu`` / ``c + d*sigma`` EMOS transform applied. This gives a direct
EMOS-vs-legacy CRPS comparison per city per day (issue #667), rather than
relying on sample count + a holdout threshold alone as promotion evidence.

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
    from src.config import CONFIG_DEFAULTS, FORECAST_STACK_MODELS, STATIONS, get_live_config, station_city
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

    # Issue #799: resolve sigma_source from the SAME USE_ENSEMBLE_SIGMA flag
    # that gates live serving (resolve_sigma_raw / Database._active_sigma_source)
    # and thread it explicitly through every fetch_training_data/save_coefficients
    # call below. fetch_training_data's own default ("ensemble") must never be
    # relied on here -- it is independent of the live flag, so a bare call would
    # silently fit against one sigma track while save_coefficients (sigma_source
    # resolved via the DB layer) persists under whatever the flag says, landing
    # a mismatched fit under the wrong key -- exactly the #658-style train/serve
    # skew this issue closes.
    use_ensemble_sigma = bool(get_live_config(db).get(
        "USE_ENSEMBLE_SIGMA", CONFIG_DEFAULTS["USE_ENSEMBLE_SIGMA"]
    ))
    sigma_source = "ensemble" if use_ensemble_sigma else "fixed"
    log.info(
        "[emos_shadow] USE_ENSEMBLE_SIGMA=%s -> sigma_source=%s",
        use_ensemble_sigma, sigma_source,
    )

    today = datetime.now(timezone.utc).date().isoformat()
    fitted = 0
    pooled_fitted = 0
    skipped = 0

    def _persist(city, a, b, c, d, training_data, sample_count, provenance):
        """Save coefficients + one EMOS CRPS row + one legacy CRPS row per city per day.

        The EMOS row is what the promotion guard counts, so logging exactly
        once a day gives the operator an honest "days of shadow evidence"
        measure. CRPS is always scored on the CITY's own triples, even when
        the coefficients came from a pooled fit — that is the evidence that
        matters for promoting THIS city.

        The legacy row scores the SAME triples' raw (uncorrected) mu/sigma —
        i.e. what the legacy path actually serves today — so promotion
        evidence is an EMOS-vs-legacy comparison, not an EMOS-alone score
        (issue #667).
        """
        crps_scores = [
            crps_gaussian(a + b * mu, c + d * sigma, y)
            for mu, sigma, y in training_data
        ]
        mean_crps = sum(crps_scores) / len(crps_scores) if crps_scores else 0.0

        legacy_crps_scores = [
            crps_gaussian(mu, sigma, y)
            for mu, sigma, y in training_data
        ]
        legacy_mean_crps = (
            sum(legacy_crps_scores) / len(legacy_crps_scores) if legacy_crps_scores else 0.0
        )

        save_coefficients(
            city, a, b, c, d, mean_crps, db,
            forecast_source=stack, sample_count=sample_count,
            sigma_source=sigma_source,
        )
        if db.emos_crps_logged_for_date(city, today, "emos_shadow", forecast_source=stack):
            log.debug(
                "[emos_shadow] city=%s: CRPS already logged for %s (stack=%s) — skipping",
                city, today, stack,
            )
        else:
            db.log_crps(city, today, mean_crps, model_mode="emos_shadow", forecast_source=stack)

        if db.emos_crps_logged_for_date(city, today, "legacy", forecast_source=stack):
            log.debug(
                "[emos_shadow] city=%s: legacy CRPS already logged for %s (stack=%s) — skipping",
                city, today, stack,
            )
        else:
            db.log_crps(city, today, legacy_mean_crps, model_mode="legacy", forecast_source=stack)

        log.info(
            "[emos_shadow] city=%s: %s fit, coefficients saved "
            "(crps=%.4f, legacy_crps=%.4f, n_city=%d)",
            city, provenance, mean_crps, legacy_mean_crps, len(training_data),
        )

    # ---- Pass 1: per-city fits (min_samples=60, unchanged behavior) ----
    insufficient: list[str] = []
    for station_cfg in STATIONS:
        city = station_city(station_cfg)
        try:
            training_data = fetch_training_data(
                city, db, regime=regime, forecast_source=stack, sigma_source=sigma_source,
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
                regime=regime, forecast_source=stack, sigma_source=sigma_source,
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
                    sigma_source=sigma_source,
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

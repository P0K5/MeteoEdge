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

Coefficients are produced by a partial-pooling shrinkage blend (issue #798)
rather than the prior hard min_samples=60 per-city cutover (issue #659): a
city's own fit and its cross-station pooling group's fit are combined,
weighted by how many of the city's own triples are available
(``emos_calibration.shrinkage_weight`` / ``blend_coefficients``). A city with
0 own triples gets the pure pooled fit; one at/above 60 gets its pure
per-city fit (identical to the pre-#798 behaviour); everywhere in between
gets a smooth mix instead of the old all-or-nothing switch.

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

    For each city with training data:
      1. Fit (a, b, c, d) by minimising mean CRPS — blended with the city's
         cross-station pooling group fit per its own sample count (issue #798;
         see the module docstring and emos_calibration.shrinkage_weight).
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
        blend_coefficients,
        shrinkage_weight,
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

    # Minimum own triples a city must contribute before it receives ANY
    # coefficients (pure per-city, or blended) — a fit can't be shadow-scored
    # against a city with (almost) no local evidence. Unchanged from #659.
    POOLED_MIN_CITY_SAMPLES = 5

    # Sample count at which a city's own fit gets full weight and the pooled
    # group contributes nothing — matches the legacy min_samples=60 cutover
    # point, so cities that already cleared that bar are unaffected (#798).
    FULL_WEIGHT_SAMPLES = 60

    all_cities_by_group: dict[str, list[str]] = {}
    for station_cfg in STATIONS:
        city = station_city(station_cfg)
        g = pooling_group(city)
        if g is not None:
            all_cities_by_group.setdefault(g, []).append(city)

    # Lazily computed & cached per group: fit_emos() over ALL group members'
    # triples (issue #659 — pooling uses every city's data, "more data,
    # better fit", regardless of which cities end up needing the blend).
    # None means the group's pooled data was attempted and found insufficient
    # (or the fit failed), so we don't re-fetch/re-fit it per city.
    group_pooled_fit: "dict[str, tuple[float, float, float, float] | None]" = {}

    def _pooled_fit_for_group(g):
        if g in group_pooled_fit:
            return group_pooled_fit[g]
        try:
            pooled_triples, per_city_counts = fetch_training_data_pooled(
                all_cities_by_group.get(g, []), db,
                regime=regime, forecast_source=stack, sigma_source=sigma_source,
            )
            fit = fit_emos(pooled_triples)
            log.info(
                "[emos_shadow] group=%s: pooled fit over %d triples from %d cities",
                g, len(pooled_triples), len(per_city_counts),
            )
        except InsufficientDataError as e:
            log.info("[emos_shadow] group=%s: pooled data still insufficient — %s", g, e)
            fit = None
        except Exception as e:
            log.warning("[emos_shadow] group=%s: pooled fit failed — %s", g, e)
            fit = None
        group_pooled_fit[g] = fit
        return fit

    # ---- Single blended pass (issue #798) ----
    # Every city's own training data determines both whether it gets a fit
    # at all and, if below FULL_WEIGHT_SAMPLES, how much weight its own fit
    # carries against its pooling group's fit.
    for station_cfg in STATIONS:
        city = station_city(station_cfg)
        try:
            training_data = fetch_training_data(
                city, db, min_samples=1, regime=regime, forecast_source=stack,
                sigma_source=sigma_source,
            )
        except InsufficientDataError as e:
            log.debug("[emos_shadow] city=%s: no training data — %s", city, e)
            training_data = []
        except Exception as e:
            log.warning("[emos_shadow] city=%s: fetch failed — %s", city, e)
            skipped += 1
            continue

        n_city = len(training_data)

        if n_city == 0:
            skipped += 1
            continue

        if n_city >= FULL_WEIGHT_SAMPLES:
            # Full weight -> identical to the pre-#798 per-city-only fit;
            # no need to touch the pooling group at all.
            try:
                a, b, c, d = fit_emos(training_data)
                _persist(city, a, b, c, d, training_data, n_city, "per-city")
                fitted += 1
            except Exception as e:
                log.warning("[emos_shadow] city=%s: fit failed — %s", city, e)
                skipped += 1
            continue

        if n_city < POOLED_MIN_CITY_SAMPLES:
            log.debug(
                "[emos_shadow] city=%s: only %d own triples (<%d) — no coefficients",
                city, n_city, POOLED_MIN_CITY_SAMPLES,
            )
            skipped += 1
            continue

        g = pooling_group(city)
        if g is None:
            skipped += 1
            continue

        pooled_fit = _pooled_fit_for_group(g)
        if pooled_fit is None:
            skipped += 1
            continue

        try:
            city_fit = fit_emos(training_data)
            weight = shrinkage_weight(n_city, FULL_WEIGHT_SAMPLES)
            a, b, c, d = blend_coefficients(city_fit, pooled_fit, weight)
            _persist(
                city, a, b, c, d, training_data, n_city,
                f"blended(w={weight:.2f}, group={g})",
            )
            pooled_fitted += 1
        except Exception as e:
            log.warning("[emos_shadow] city=%s: blended fit failed — %s", city, e)
            skipped += 1

    log.info(
        "[emos_shadow] done: %d per-city fits, %d blended fits, %d skipped",
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

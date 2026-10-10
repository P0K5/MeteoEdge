"""Offline EMOS retraining script.

CRITICAL: Run on a LOCAL copy of the production SQLite DB only.
         DO NOT run this on the VPS.

Usage:
    python scripts/auto_retrain_probability_calibration.py --db local_snapshot.db
    python scripts/auto_retrain_probability_calibration.py --db local.db --city Tokyo
    python scripts/auto_retrain_probability_calibration.py --db local.db --dry-run

auto_retrain_report.json schema (one entry per city):
    {
        "Tokyo": {
            "crps_train": 0.071,          # mean EMOS-corrected CRPS on training set
            "crps_holdout": 0.073,        # mean EMOS-corrected CRPS on held-out 20%
            "crps_holdout_legacy": 0.091, # same holdout, uncorrected (mu, sigma) -- what legacy serves
            "samples": 120,               # total training triples used
            "ready_for_promotion": 1,     # 1 iff samples>=min AND crps_holdout beats
                                           # crps_holdout_legacy by > --promote-min-relative-improvement
            "trained_at": "2026-06-12T18:00:00+00:00"
        }
    }
"""
import argparse
import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

import logging

from src.config import CONFIG_DEFAULTS, FORECAST_STACK_MODELS, STATIONS, get_live_config
from src.data.db import Database
from src.model.crps_score import mean_crps
from src.model.emos_calibration import (
    InsufficientDataError,
    fetch_training_data,
    fit_emos,
    save_coefficients,
)


def _check_not_on_vps() -> None:
    """Guard: abort immediately if running on the VPS.

    Checks two signals:
    - Presence of /etc/systemd/system/meteoedge.service
    - Hostname containing 'meteoedge'
    """
    hostname = socket.gethostname().lower()
    systemd_unit = Path("/etc/systemd/system/meteoedge.service")
    if systemd_unit.exists() or "meteoedge" in hostname:
        raise SystemExit(
            "ERROR: Do not run retraining on the VPS. Copy the DB locally first.\n"
            "  scp user@vps:/path/to/meteoedge.db ./local_snapshot.db\n"
            "  python scripts/auto_retrain_probability_calibration.py --db local_snapshot.db"
        )


def validate_report(report: dict) -> None:
    """Raise ValueError if any city entry is missing required fields."""
    required = {"crps_train", "crps_holdout", "samples", "ready_for_promotion", "trained_at"}
    for city, entry in report.items():
        missing = required - entry.keys()
        if missing:
            raise ValueError(f"Report entry for {city!r} missing fields: {missing}")


def get_all_cities() -> list:
    """Return list of city names from the STATIONS config."""
    return [cfg[3] for cfg in STATIONS]


def _ready_for_promotion(
    crps_holdout: float,
    crps_holdout_legacy: float,
    samples: int,
    min_samples: int,
    min_relative_improvement: float,
) -> int:
    """Return 1 iff EMOS clears the sample floor and beats its own legacy
    (uncorrected mu/sigma) holdout CRPS by at least *min_relative_improvement*.

    A fixed absolute CRPS threshold doesn't generalise: observed holdout CRPS
    for real stations ranges roughly 0.7-2.4 depending on each city's own
    temperature spread, so a single absolute cutoff is either unreachable for
    every city or meaningless for picking out real skill. Comparing against
    the SAME holdout triples' legacy score is scale-free and mirrors the
    emos_shadow/legacy CRPS pair run_emos_shadow.py already logs daily for
    exactly this comparison (issue #667).
    """
    if samples < min_samples:
        return 0
    if crps_holdout_legacy <= 0:
        return 0
    improvement = 1.0 - (crps_holdout / crps_holdout_legacy)
    return 1 if improvement > min_relative_improvement else 0


log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> None:
    """Entry point for the offline EMOS retraining script."""
    _check_not_on_vps()  # First thing — abort if running on VPS

    parser = argparse.ArgumentParser(description="Offline EMOS retraining")
    parser.add_argument("--db", required=True, help="Path to local SQLite DB snapshot")
    parser.add_argument("--city", default=None, help="Retrain one city (default: all)")
    parser.add_argument("--min-samples", type=int, default=60)
    parser.add_argument(
        "--promote-min-relative-improvement",
        type=float,
        default=0.10,
        help=(
            "Minimum fractional improvement of EMOS holdout CRPS over its own "
            "legacy (uncorrected) holdout CRPS required for ready_for_promotion=1 "
            "(default 0.10 = EMOS must beat legacy by >10%% on held-out data)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute but do not write to DB or JSON",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Print current DB state and exit",
    )
    parser.add_argument(
        "--forecast-stack",
        default=None,
        help="Forecast stack identifier (default: read from DB, fallback to 'baseline')",
    )
    parser.add_argument(
        "--sigma-source",
        default=None,
        choices=["fixed", "ensemble"],
        help=(
            "EMOS sigma track (default: derived from the DB's USE_ENSEMBLE_SIGMA "
            "bot_config value, issue #799 -- 'ensemble' if true, else 'fixed')."
        ),
    )
    args = parser.parse_args()

    db = Database(args.db)

    # Resolve active forecast stack and model regime
    stack = getattr(args, "forecast_stack", None) or db.get_config("FORECAST_STACK") or "baseline"
    regime = FORECAST_STACK_MODELS.get(stack, FORECAST_STACK_MODELS["baseline"])
    log.info("[retrain] FORECAST_STACK=%s, regime=%s", stack, sorted(regime))

    # Issue #799: resolve sigma_source from the SAME USE_ENSEMBLE_SIGMA flag
    # that gates live serving, and thread it explicitly through EVERY
    # fetch_training_data/save_coefficients call below -- never rely on
    # fetch_training_data's own default ("ensemble"), which is independent of
    # the live flag and would silently fit against one sigma track while the
    # save resolves a different one, landing a mismatched fit under the wrong
    # key (the #658-style train/serve skew this issue closes). An explicit
    # --sigma-source overrides the DB-derived value for one-off comparisons.
    sigma_source = getattr(args, "sigma_source", None)
    if sigma_source is None:
        use_ensemble_sigma = bool(get_live_config(db).get(
            "USE_ENSEMBLE_SIGMA", CONFIG_DEFAULTS["USE_ENSEMBLE_SIGMA"]
        ))
        sigma_source = "ensemble" if use_ensemble_sigma else "fixed"
    log.info("[retrain] sigma_source=%s", sigma_source)

    # Handle --report-only: query DB and exit
    if args.report_only:
        print("\n=== Current EMOS Calibration State ===")
        print(f"{'City':<15} {'Model Mode':<15} {'CRPS Score':>11} {'Promote':>8} {'Trained At'}")
        query = "SELECT city, model_mode, crps_score, ready_for_promotion, trained_at FROM emos_calibration"
        try:
            rows = db._conn.execute(query).fetchall()
            for row in rows:
                city, model_mode, crps_score, promote, trained_at = row
                crps_str = f"{crps_score:.4f}" if crps_score is not None else "N/A"
                print(f"{city:<15} {model_mode:<15} {crps_str:>11} {promote:>8} {trained_at}")
        except Exception as e:
            print(f"Error querying database: {e}")
        return

    cities = [args.city] if args.city else get_all_cities()

    report = {}
    for city in cities:
        print(f"Processing {city}...")
        try:
            data = fetch_training_data(
                city, db, min_samples=args.min_samples,
                regime=regime, forecast_source=stack, sigma_source=sigma_source,
            )
        except InsufficientDataError as e:
            print(f"  {city}: SKIP — {e}")
            continue

        # 80/20 train/holdout split (chronological — no shuffle)
        split = int(len(data) * 0.8)
        train, holdout = data[:split], data[split:]

        a, b, c, d = fit_emos(train)
        crps_train = mean_crps(
            [(a + b * mu, max(c + d * sigma, 1e-6), y) for mu, sigma, y in train]
        )
        crps_holdout = mean_crps(
            [(a + b * mu, max(c + d * sigma, 1e-6), y) for mu, sigma, y in holdout]
        )
        # Same holdout triples, uncorrected (mu, sigma) -- what legacy actually
        # serves -- so readiness compares EMOS against ITS OWN holdout baseline
        # rather than an arbitrary absolute CRPS number (issue #667 precedent).
        crps_holdout_legacy = mean_crps(
            [(mu, max(sigma, 1e-6), y) for mu, sigma, y in holdout]
        )

        # Default to 0 if somehow empty
        crps_train = crps_train if crps_train is not None else float("inf")
        crps_holdout = crps_holdout if crps_holdout is not None else float("inf")
        crps_holdout_legacy = crps_holdout_legacy if crps_holdout_legacy is not None else 0.0

        ready = _ready_for_promotion(
            crps_holdout, crps_holdout_legacy, len(data),
            args.min_samples, args.promote_min_relative_improvement,
        )
        trained_at = datetime.now(timezone.utc).isoformat()

        report[city] = {
            "crps_train": round(crps_train, 6),
            "crps_holdout": round(crps_holdout, 6),
            "crps_holdout_legacy": round(crps_holdout_legacy, 6),
            "samples": len(data),
            "ready_for_promotion": ready,
            "trained_at": trained_at,
        }

        if not args.dry_run:
            save_coefficients(
                city,
                a,
                b,
                c,
                d,
                crps_holdout,
                db,
                forecast_source=stack,
                sample_count=len(data),
                sigma_source=sigma_source,
            )
            if ready:
                db.upsert_emos_coefficients(
                    city=city,
                    model_mode="emos_shadow",
                    a=a,
                    b=b,
                    c=c,
                    d=d,
                    crps_score=crps_holdout,
                    trained_at=trained_at,
                    ready_for_promotion=1,
                    forecast_source=stack,
                    sigma_source=sigma_source,
                )

        print(
            f"  {city}: CRPS train={crps_train:.4f} holdout={crps_holdout:.4f} "
            f"(legacy={crps_holdout_legacy:.4f}) samples={len(data)} ready={ready}"
        )

    if not args.dry_run and report:
        # Validate report schema before writing
        validate_report(report)
        # Atomic write: write to .tmp then rename to avoid partial reads
        report_path = Path("auto_retrain_report.json")
        tmp = report_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, indent=2))
        tmp.rename(report_path)
        print(f"\nReport written to {report_path}")

    # Summary table
    print("\n=== Retraining Summary ===")
    print(
        f"{'City':<15} {'Samples':>8} {'CRPS Train':>11} {'CRPS Hold':>10} "
        f"{'CRPS Legacy':>11} {'Promote':>8}"
    )
    for city, r in report.items():
        print(
            f"{city:<15} {r['samples']:>8} {r['crps_train']:>11.4f} "
            f"{r['crps_holdout']:>10.4f} {r['crps_holdout_legacy']:>11.4f} "
            f"{r['ready_for_promotion']:>8}"
        )


if __name__ == "__main__":
    main()

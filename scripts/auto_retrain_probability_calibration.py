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
            "crps_train": 0.071,        # mean CRPS on training set
            "crps_holdout": 0.073,      # mean CRPS on held-out 20%
            "samples": 120,             # total training triples used
            "ready_for_promotion": 1,   # 1 if criteria met, 0 otherwise
            "trained_at": "2026-06-12T18:00:00+00:00"
        }
    }
"""
import argparse
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

from src.config import STATIONS
from src.data.db import Database
from src.model.crps_score import mean_crps


class InsufficientDataError(Exception):
    """Raised when a city has fewer training samples than the minimum required."""


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


def get_all_cities() -> list:
    """Return list of city names from the STATIONS config."""
    return [cfg[3] for cfg in STATIONS]


def fetch_training_data(
    city: str,
    db: Database,
    min_samples: int = 60,
) -> list:
    """Fetch (mu, sigma, y) training triples for a city from the DB.

    Pulls from the emos_training_data table (or equivalent source).
    Each row must have: forecast_mu, forecast_sigma, observed_temp.

    Args:
        city: City name (as in STATIONS config).
        db: Database instance pointing to a local SQLite snapshot.
        min_samples: Minimum number of triples required; raises InsufficientDataError if fewer.

    Returns:
        List of (mu, sigma, y) tuples ordered by timestamp ascending.

    Raises:
        InsufficientDataError: If fewer than min_samples rows are found.
    """
    cur = db._conn.execute(
        """
        SELECT forecast_mu, forecast_sigma, observed_temp
        FROM emos_training_data
        WHERE city = ?
        ORDER BY ts ASC
        """,
        (city,),
    )
    rows = cur.fetchall()
    triples = [(float(r[0]), float(r[1]), float(r[2])) for r in rows]
    if len(triples) < min_samples:
        raise InsufficientDataError(
            f"{city}: only {len(triples)} samples (need {min_samples})"
        )
    return triples


def fit_emos(data: list) -> tuple:
    """Fit EMOS linear regression coefficients from (mu, sigma, y) triples.

    EMOS model: calibrated_mu = a + b*mu, calibrated_sigma = c + d*sigma
    Coefficients are fitted by minimising mean CRPS via gradient-free
    Nelder-Mead optimisation.

    Args:
        data: List of (mu, sigma, y) tuples — raw NWP forecasts and observations.

    Returns:
        Tuple (a, b, c, d) of fitted EMOS coefficients.
    """
    from scipy.optimize import minimize  # optional dependency — only needed here

    def _objective(params):
        a, b, c, d = params
        calibrated = [(a + b * mu, max(c + d * sigma, 1e-6), y) for mu, sigma, y in data]
        score = mean_crps(calibrated)
        return score if score is not None else float("inf")

    # Initialise at identity: no-op transformation
    x0 = [0.0, 1.0, 0.0, 1.0]
    bounds = [
        (None, None),  # a — intercept for mu (unrestricted)
        (None, None),  # b — slope for mu (unrestricted)
        (0.0, None),   # c — intercept for sigma (must keep sigma > 0)
        (0.0, None),   # d — slope for sigma (must keep sigma > 0)
    ]
    result = minimize(_objective, x0, method="Nelder-Mead", options={"maxiter": 5000, "xatol": 1e-6, "fatol": 1e-8})
    a, b, c, d = result.x
    return float(a), float(b), float(c), float(d)


def save_coefficients(
    city: str,
    a: float,
    b: float,
    c: float,
    d: float,
    crps_score: float,
    db: Database,
) -> None:
    """Persist EMOS coefficients to the DB in shadow mode (not yet promoted).

    Args:
        city: City name.
        a, b, c, d: EMOS coefficients.
        crps_score: Holdout CRPS used as the quality metric.
        db: Database instance.
    """
    db.upsert_emos_coefficients(
        city=city,
        model_mode="emos_shadow",
        a=a,
        b=b,
        c=c,
        d=d,
        crps_score=crps_score,
        trained_at=datetime.now(timezone.utc).isoformat(),
        ready_for_promotion=0,
    )


def main() -> None:
    """Entry point for the offline EMOS retraining script."""
    _check_not_on_vps()  # First thing — abort if running on VPS

    parser = argparse.ArgumentParser(description="Offline EMOS retraining")
    parser.add_argument("--db", required=True, help="Path to local SQLite DB snapshot")
    parser.add_argument("--city", default=None, help="Retrain one city (default: all)")
    parser.add_argument("--min-samples", type=int, default=60)
    parser.add_argument(
        "--promote-threshold",
        type=float,
        default=0.08,
        help="Holdout CRPS threshold for ready_for_promotion=1",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute but do not write to DB or JSON",
    )
    args = parser.parse_args()

    db = Database(args.db)
    cities = [args.city] if args.city else get_all_cities()

    report = {}
    for city in cities:
        print(f"Processing {city}...")
        try:
            data = fetch_training_data(city, db, min_samples=args.min_samples)
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

        # Default to 0 if somehow empty
        crps_train = crps_train if crps_train is not None else float("inf")
        crps_holdout = crps_holdout if crps_holdout is not None else float("inf")

        ready = (
            1
            if (crps_holdout < args.promote_threshold and len(data) >= args.min_samples)
            else 0
        )
        trained_at = datetime.now(timezone.utc).isoformat()

        report[city] = {
            "crps_train": round(crps_train, 6),
            "crps_holdout": round(crps_holdout, 6),
            "samples": len(data),
            "ready_for_promotion": ready,
            "trained_at": trained_at,
        }

        if not args.dry_run:
            save_coefficients(city, a, b, c, d, crps_holdout, db)
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
                )

        print(
            f"  {city}: CRPS train={crps_train:.4f} holdout={crps_holdout:.4f} "
            f"samples={len(data)} ready={ready}"
        )

    if not args.dry_run and report:
        # Atomic write: write to .tmp then rename to avoid partial reads
        report_path = Path("auto_retrain_report.json")
        tmp = report_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, indent=2))
        tmp.rename(report_path)
        print(f"\nReport written to {report_path}")

    # Summary table
    print("\n=== Retraining Summary ===")
    print(
        f"{'City':<15} {'Samples':>8} {'CRPS Train':>11} {'CRPS Hold':>10} {'Promote':>8}"
    )
    for city, r in report.items():
        print(
            f"{city:<15} {r['samples']:>8} {r['crps_train']:>11.4f} "
            f"{r['crps_holdout']:>10.4f} {r['ready_for_promotion']:>8}"
        )


if __name__ == "__main__":
    main()

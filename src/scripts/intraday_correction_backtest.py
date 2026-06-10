"""Backtest script for intraday bias correction.

Compares corrected forecast RMSE vs uncorrected baseline RMSE using historical
``intraday_corrections`` and ``settlements`` data stored in the MeteoEdge DB.

Usage:
    python -m src.scripts.intraday_correction_backtest --city Chicago
    python -m src.scripts.intraday_correction_backtest --city Seoul --days 14
"""
import argparse
import logging
import math
import os
import sys
from datetime import date, timedelta

log = logging.getLogger(__name__)

from src.config import STATIONS
from src.data.db import Database
from src.logging_config import setup_logging

_DEFAULT_DB_PATH = os.getenv("DB_PATH", "data/meteoedge.db")


# ---------------------------------------------------------------------------
# Pure helpers (extracted for testability)
# ---------------------------------------------------------------------------

def compute_rmse(errors: list) -> float:
    """Return RMSE for a list of absolute errors (already non-negative).

    Args:
        errors: List of per-observation absolute error values (floats).

    Returns:
        RMSE value.  Raises ``ValueError`` if *errors* is empty.
    """
    if not errors:
        raise ValueError("Cannot compute RMSE from empty error list")
    return math.sqrt(sum(e * e for e in errors) / len(errors))


def _station_for_city(city: str) -> "str | None":
    """Return the METAR station code for *city* from STATIONS config, or None."""
    for row in STATIONS:
        if row[3] == city:
            return row[0]
    return None


def _build_daily_stats(
    corrections: list,
    actual_high_f: float,
) -> "dict | None":
    """Compute per-day corrected and baseline errors from correction rows.

    Uses the **last** correction row of the day as the final forecast (closest
    to settlement).  Baseline is reconstructed as:
        baseline_forecast = corrected_mu_f - delta_f * decay_factor

    Args:
        corrections: List of intraday_correction dicts for a single date,
                     ordered by obs_time ascending (as returned by DB).
        actual_high_f: The settlement actual high temperature in °F.

    Returns:
        Dict with keys ``corrected_error``, ``baseline_error``, or None if
        *corrections* is empty.
    """
    if not corrections:
        return None

    last = corrections[-1]
    corrected_mu_f = last["corrected_mu_f"]
    delta_f = last["delta_f"]
    decay_factor = last["decay_factor"]

    baseline_forecast = corrected_mu_f - delta_f * decay_factor
    corrected_error = abs(actual_high_f - corrected_mu_f)
    baseline_error = abs(actual_high_f - baseline_forecast)

    return {
        "corrected_error": corrected_error,
        "baseline_error": baseline_error,
        "corrected_mu_f": corrected_mu_f,
        "baseline_forecast": baseline_forecast,
        "actual_high_f": actual_high_f,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Backtest intraday bias correction — compare corrected vs baseline RMSE"
    )
    parser.add_argument("--city", required=True, help="City name (e.g. Chicago, Seoul)")
    parser.add_argument(
        "--days", type=int, default=7,
        help="Number of past days to evaluate (default: 7)"
    )
    args = parser.parse_args()

    city: str = args.city
    days: int = args.days

    # --- Find METAR station for this city ---
    station = _station_for_city(city)
    if station is None:
        log.error("Unknown city: %s. Available cities: %s", city, [r[3] for r in STATIONS])
        sys.exit(1)

    db = Database(_DEFAULT_DB_PATH)

    today = date.today()
    since_date = today - timedelta(days=days)
    since_iso = since_date.isoformat()

    # --- Fetch settlements for the station since N days ago ---
    settlements = db.get_settlements(station, since_iso)

    # Index settlements by date string (YYYY-MM-DD)
    settlement_by_date: dict = {}
    for s in settlements:
        s_date = s["ts"][:10]
        # Keep latest settlement per date in case of duplicates
        settlement_by_date[s_date] = s

    # --- Gather per-day stats ---
    daily_results: list = []

    for i in range(days):
        target_date = (today - timedelta(days=days - i)).isoformat()
        corrections = db.get_intraday_corrections(city, target_date)
        if not corrections:
            continue

        settlement = settlement_by_date.get(target_date)
        if settlement is None:
            continue

        actual_high_f = settlement["actual_high_f"]
        stats = _build_daily_stats(corrections, actual_high_f)
        if stats is None:
            continue

        daily_results.append({"date": target_date, **stats})

    db.close()

    # --- No data path ---
    if not daily_results:
        log.info("No intraday_corrections data found for %s in the last %s days.", city, days)
        sys.exit(0)

    # --- Compute overall RMSE ---
    baseline_errors = [r["baseline_error"] for r in daily_results]
    corrected_errors = [r["corrected_error"] for r in daily_results]

    overall_baseline_rmse = compute_rmse(baseline_errors)
    overall_corrected_rmse = compute_rmse(corrected_errors)
    improvement = overall_baseline_rmse - overall_corrected_rmse
    improvement_pct = (improvement / overall_baseline_rmse * 100) if overall_baseline_rmse else 0.0

    # --- Print report ---
    log.info("Intraday Correction Backtest -- %s -- last %s days", city, days)
    log.info("=" * 44)
    log.info("%-14s %13s %14s %8s", "Date", "Baseline Err", "Corrected Err", "Delta")

    for r in daily_results:
        delta = r["corrected_error"] - r["baseline_error"]
        marker = " ok" if delta < 0 else (" WORSE" if delta > 0 else "")
        log.info(
            "%-14s %13.2f %14.2f %8.2f%s",
            r['date'], r['baseline_error'], r['corrected_error'], delta, marker,
        )

    log.info("-" * 44)
    log.info("Overall baseline RMSE:  %.2f F", overall_baseline_rmse)
    log.info("Overall corrected RMSE: %.2f F", overall_corrected_rmse)
    log.info("Improvement:            %.2f F (%.1f%%)", improvement, improvement_pct)
    log.info("Dates with data: %s/%s", len(daily_results), days)


if __name__ == "__main__":
    main()

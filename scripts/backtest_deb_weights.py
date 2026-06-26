"""Backtest script: per-model DEB weight contribution over 30 days.

Reports per-model weight evolution and contribution to consensus forecast
for a given station over a 30-day window.

Usage:
    python scripts/backtest_deb_weights.py --station KORD --city "Chicago" [--db path/to/db]

Output:
    - Day-by-day table of per-model weights
    - Per-model average weight and RMSE over the window
    - Summary of which models contributed to each day's consensus
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import date, timedelta
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.model.deb_weighting import (
    _REGISTRY,
    _cadence_decay_rate,
    _decay_weight,
    _model_names_for_region,
    _MIN_SAMPLES,
    _equal_weights_for,
    BASE_DECAY_RATE,
    GROUP_WEIGHT_CAP,
    _apply_group_cap,
    _models_for_region,
)


def _compute_weights_with_history(
    db,
    station: str,
    city: str,
    window_days: int = 30,
    station_region: str = "us",
) -> list[dict]:
    """Return per-day weight snapshots for the trailing *window_days* window.

    For each day D in the window, computes DEB weights using only data
    available on day D (i.e. all log rows with date < D).  Returns a list
    of dicts sorted by date ascending, each containing:
      - "date": ISO date string
      - "weights": dict[model -> weight]
      - "rmse": dict[model -> float | None]
      - "n_samples": dict[model -> int]
    """
    applicable = _models_for_region(station_region)
    applicable_names = [m.name for m in applicable]

    today = date.today()
    start_date = today - timedelta(days=window_days)
    since_str = start_date.isoformat()

    # Pull all data once
    if hasattr(db, "get_forecast_log_by_lead"):
        log_rows = db.get_forecast_log_by_lead(station, since_str, lead_hours=24)
    else:
        log_rows = db.get_forecast_log(station, since_str)
    settlement_rows = db.get_settlements(station, since_str + "T00:00:00")

    actuals: dict[str, float] = {r["ts"][:10]: r["actual_high_f"] for r in settlement_rows}

    # Build error history: model -> [(date_str, days_ago, abs_error)]
    all_errors: dict[str, list[tuple[str, int, float]]] = {m: [] for m in applicable_names}
    for row in log_rows:
        m = row["model"]
        if m not in all_errors:
            continue
        d = row["date"]
        if d not in actuals:
            continue
        days_ago = (today - date.fromisoformat(d)).days
        err = abs(row["forecast_high_f"] - actuals[d])
        all_errors[m].append((d, days_ago, err))

    snapshots = []
    for day_offset in range(window_days, 0, -1):
        snapshot_date = today - timedelta(days=day_offset)
        snapshot_str = snapshot_date.isoformat()

        # Only use errors from before this snapshot date
        errors: dict[str, list[tuple[int, float]]] = {m: [] for m in applicable_names}
        for m in applicable_names:
            for (d_str, days_ago, err) in all_errors[m]:
                if d_str < snapshot_str:
                    # Recompute days_ago relative to snapshot_date
                    d_obj = date.fromisoformat(d_str)
                    relative_days = (snapshot_date - d_obj).days
                    errors[m].append((relative_days, err))

        cold_start = [m for m in applicable_names if len(errors[m]) < _MIN_SAMPLES]
        calibrated = [m for m in applicable_names if len(errors[m]) >= _MIN_SAMPLES]

        n_models = len(applicable_names)
        n_samples = {m: len(errors[m]) for m in applicable_names}
        rmse_out: dict[str, float | None] = {m: None for m in applicable_names}

        if len(cold_start) == n_models:
            eq = _equal_weights_for(station_region)
            snapshots.append({
                "date": snapshot_str,
                "weights": eq,
                "rmse": rmse_out,
                "n_samples": n_samples,
                "mode": "equal",
            })
            continue

        rmse: dict[str, float] = {}
        for m in calibrated:
            entry = _REGISTRY[m]
            decay_rate = _cadence_decay_rate(entry)
            total_w = sum(_decay_weight(k, decay_rate) for k, _ in errors[m])
            if total_w == 0:
                continue
            wmse = sum(_decay_weight(k, decay_rate) * e**2 for k, e in errors[m]) / total_w
            rmse[m] = math.sqrt(wmse)
            rmse_out[m] = rmse[m]

        raw_calibrated = {m: 1.0 / rmse[m] for m in calibrated if m in rmse}
        total_calibrated = sum(raw_calibrated.values())

        cold_start_reserved = sum(
            _REGISTRY[m].cold_start_fraction / n_models for m in cold_start
        )
        calibrated_budget = 1.0 - cold_start_reserved

        weights: dict[str, float] = {}
        if total_calibrated > 0:
            for m in calibrated:
                if m in raw_calibrated:
                    weights[m] = raw_calibrated[m] / total_calibrated * calibrated_budget
                else:
                    weights[m] = 0.0
        for m in cold_start:
            weights[m] = _REGISTRY[m].cold_start_fraction / n_models

        weights = _apply_group_cap(weights, applicable)

        snapshots.append({
            "date": snapshot_str,
            "weights": weights,
            "rmse": rmse_out,
            "n_samples": n_samples,
            "mode": "deb",
        })

    return snapshots


def run_backtest(station: str, city: str, db_path: str | None, window_days: int = 30) -> None:
    """Print per-model weight evolution and contribution summary."""
    if db_path:
        from src.data.db import Database
        db = Database(db_path)
    else:
        print("ERROR: --db is required to run the backtest against real data.")
        print("Usage: python scripts/backtest_deb_weights.py --station KORD --city Chicago --db path/to/meteoedge.db")
        sys.exit(1)

    print("=" * 80)
    print(f"DEB WEIGHT BACKTEST: {city} ({station})  |  window={window_days}d")
    print("=" * 80)

    snapshots = _compute_weights_with_history(db, station, city, window_days)

    models = list(_model_names_for_region("us"))

    # Header
    col_w = 9
    header = f"{'Date':12}" + "".join(f"{m:>{col_w}}" for m in models) + f"  {'Mode':8}"
    print("\nPer-day weight evolution:")
    print(header)
    print("-" * len(header))

    weight_history: dict[str, list[float]] = {m: [] for m in models}
    for snap in snapshots:
        row = f"{snap['date']:12}"
        for m in models:
            w = snap["weights"].get(m, 0.0)
            weight_history[m].append(w)
            row += f"{w:>{col_w}.4f}"
        row += f"  {snap['mode']:8}"
        print(row)

    # Per-model summary
    print("\nPer-model summary over window:")
    print(f"  {'Model':12} {'Avg Weight':>12} {'Final Weight':>13} {'Final RMSE':>11} {'Final N':>8}")
    print("  " + "-" * 60)
    for m in models:
        history = weight_history[m]
        avg_w = sum(history) / len(history) if history else 0.0
        final_w = snapshots[-1]["weights"].get(m, 0.0) if snapshots else 0.0
        final_rmse = snapshots[-1]["rmse"].get(m) if snapshots else None
        final_n = snapshots[-1]["n_samples"].get(m, 0) if snapshots else 0
        rmse_str = f"{final_rmse:.3f}°F" if final_rmse is not None else "cold-start"
        print(f"  {m:12} {avg_w:>12.4f} {final_w:>13.4f} {rmse_str:>11} {final_n:>8}")

    # Contribution table: days where each model contributed (weight > 0)
    print("\nModel contribution (days with weight > 0.01):")
    for m in models:
        contributing_days = sum(1 for s in snapshots if s["weights"].get(m, 0.0) > 0.01)
        print(f"  {m:12}: {contributing_days}/{len(snapshots)} days")

    print("\nDone.")


def main() -> None:
    parser = argparse.ArgumentParser(description="DEB weight backtest over 30 days")
    parser.add_argument("--station", required=True, help="Station ICAO code (e.g. KORD)")
    parser.add_argument("--city", required=True, help="City name (e.g. Chicago)")
    parser.add_argument("--db", default=None, help="Path to SQLite database file")
    parser.add_argument("--window", type=int, default=30, help="Lookback window in days (default 30)")
    args = parser.parse_args()

    run_backtest(station=args.station, city=args.city, db_path=args.db, window_days=args.window)


if __name__ == "__main__":
    main()

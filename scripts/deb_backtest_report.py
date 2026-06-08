#!/usr/bin/env python3
"""DEB backtest report: compare DEB-weighted vs equal-weight forecast RMSE.

Run from repo root to compare forecast accuracy over the last 60 settled days:
    python scripts/deb_backtest_report.py

Computes per-city RMSE for both equal-weight and DEB-weighted forecast approaches,
then prints a comparison table showing the improvement percentage.
"""
import os
import sqlite3
import sys
from math import sqrt
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))


def get_db_connection(db_path: str) -> sqlite3.Connection:
    """Open database connection or exit with code 1 on failure."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        print(f"[error] Database connection failed: {e}", file=sys.stderr)
        sys.exit(1)


def get_latest_model_weights(conn: sqlite3.Connection) -> dict:
    """Fetch latest model weights by (city, model) as of today.

    Returns dict mapping (city, model) -> weight.
    """
    cur = conn.execute("""
        SELECT city, model, weight FROM model_weights
        WHERE (city, model, date) IN (
            SELECT city, model, MAX(date) FROM model_weights
            GROUP BY city, model
        )
    """)
    weights = {}
    for row in cur.fetchall():
        weights[(row["city"], row["model"])] = row["weight"]
    return weights


def compute_rmse(residuals: list[float]) -> float:
    """Compute RMSE from list of residuals."""
    if not residuals:
        return 0.0
    return sqrt(sum(r * r for r in residuals) / len(residuals))


def main() -> None:
    db_path = os.getenv("DB_PATH", "data/meteoedge.db")
    conn = get_db_connection(db_path)

    try:
        # Fetch latest model weights for DEB calculation
        weights = get_latest_model_weights(conn)

        # Query: join model_forecast_log with settlements for last 60 days
        # Group by station and date, compute both forecast approaches
        cur = conn.execute("""
            SELECT mfl.station, mfl.model, mfl.date, mfl.forecast_high_f, s.actual_high_f
            FROM model_forecast_log mfl
            JOIN settlements s ON mfl.station = s.station AND mfl.date = DATE(s.ts)
            WHERE mfl.date >= DATE('now', '-60 days')
            ORDER BY mfl.station, mfl.date, mfl.model
        """)

        # Group data by (station, date)
        data_by_station_date = {}
        for row in cur.fetchall():
            station = row["station"]
            model = row["model"]
            date = row["date"]
            forecast = row["forecast_high_f"]
            actual = row["actual_high_f"]

            key = (station, date)
            if key not in data_by_station_date:
                data_by_station_date[key] = {"forecasts": {}, "actual": actual}
            data_by_station_date[key]["forecasts"][model] = forecast

        # Compute per-city metrics
        results = {}
        for (station, date), forecast_data in data_by_station_date.items():
            if station not in results:
                results[station] = {
                    "equal_residuals": [],
                    "deb_residuals": [],
                    "n_days": set(),
                }

            results[station]["n_days"].add(date)
            actual = forecast_data["actual"]
            forecasts = forecast_data["forecasts"]

            if not forecasts:
                continue

            # Equal-weight: mean of all forecast_high_f values
            equal_forecast = sum(forecasts.values()) / len(forecasts)
            equal_residual = equal_forecast - actual
            results[station]["equal_residuals"].append(equal_residual)

            # DEB-weighted: use weights from model_weights table
            deb_forecast = 0.0
            total_weight = 0.0
            for model, forecast in forecasts.items():
                weight = weights.get((station, model), 0.0)
                if weight > 0:
                    deb_forecast += weight * forecast
                    total_weight += weight

            if total_weight > 0:
                deb_forecast /= total_weight
                deb_residual = deb_forecast - actual
                results[station]["deb_residuals"].append(deb_residual)

        # Print table header
        header = "city".ljust(20) + "deb_rmse_f".ljust(15) + "equal_rmse_f".ljust(15) + "n_days".ljust(10) + "improvement_%"
        print(header)
        print("-" * len(header))

        # Print results for each station
        for station in sorted(results.keys()):
            data = results[station]
            n_days = len(data["n_days"])

            if n_days < 5:
                print(f"{station.ljust(20)}insufficient data".ljust(40))
                continue

            equal_rmse = compute_rmse(data["equal_residuals"])
            deb_rmse = compute_rmse(data["deb_residuals"])

            # Compute improvement percentage
            if equal_rmse > 0:
                improvement = (equal_rmse - deb_rmse) / equal_rmse * 100
            else:
                improvement = 0.0

            row = (
                station.ljust(20) +
                f"{deb_rmse:.4f}".ljust(15) +
                f"{equal_rmse:.4f}".ljust(15) +
                str(n_days).ljust(10) +
                f"{improvement:.2f}%"
            )
            print(row)

    finally:
        conn.close()


if __name__ == "__main__":
    main()

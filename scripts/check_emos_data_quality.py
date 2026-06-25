#!/usr/bin/env python3
"""Check EMOS data quality: forecast log row counts per city/lead/source.

Queries model_forecast_log grouped by (station, lead_hours, model) and reports
per-city, per-lead-hour, per-source row counts. Flags cities with < 60 rows at
lead_hours=24 as "at risk" (below MIN_SAMPLES for EMOS calibration).

Usage:
    python scripts/check_emos_data_quality.py [--db /path/to/db]

Options:
    --db PATH    Override database path (default: data/meteoedge.db)

Output:
    - Table of row counts per (city, lead_hours, model)
    - Per-city summary at lead_hours=24
    - Flag cities at risk (< 60 rows)
    - Estimated readiness date: reset_date + (60 - current_rows) days
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

# Allow running from repo root or scripts/ directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.data.db import Database
from src.config import STATIONS

MIN_SAMPLES = 60
LEAD_HOURS_TARGET = 24


def get_city_for_station(station: str) -> str:
    """Map METAR station code to city name."""
    for metar_code, _lat, _lon, city, *_ in STATIONS:
        if metar_code == station:
            return city
    return station


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check EMOS data quality in model_forecast_log"
    )
    parser.add_argument(
        "--db",
        type=str,
        default=os.getenv("DB_PATH", "data/meteoedge.db"),
        help="Path to SQLite database",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Error: database not found at {db_path}")
        sys.exit(1)

    db = Database(str(db_path))

    # Get reset timestamp from bot_config
    reset_ts_str = db.get_config("model_forecast_log_reset_at")
    if reset_ts_str:
        try:
            reset_ts = datetime.fromisoformat(reset_ts_str)
        except (ValueError, TypeError):
            reset_ts = None
    else:
        reset_ts = None

    print("=" * 80)
    print("EMOS Data Quality Report")
    print("=" * 80)

    if reset_ts:
        print(f"Reset timestamp: {reset_ts_str}")
        print(f"Approx. days since reset: {(datetime.now(timezone.utc) - reset_ts).days}")
    else:
        print("Reset timestamp: NOT SET")
    print()

    # Query model_forecast_log grouped by (station, lead_hours, model)
    cursor = db._conn.execute(
        """
        SELECT
            station,
            lead_hours,
            model,
            COUNT(*) as row_count
        FROM model_forecast_log
        GROUP BY station, lead_hours, model
        ORDER BY station, lead_hours DESC, model
        """
    )
    rows = cursor.fetchall()

    if not rows:
        print("No rows in model_forecast_log yet (or table is empty).")
        print("Waiting for new capture pipeline to populate data...")
        db.close()
        return

    print("Data by (Station, Lead Hours, Model):")
    print("-" * 80)
    print(f"{'Station':<15} {'Lead Hours':>12} {'Model':<20} {'Row Count':>10}")
    print("-" * 80)

    # Group by station and lead_hours for summary
    summary_by_city_and_lead = defaultdict(lambda: defaultdict(int))

    for station, lead_hours, model, count in rows:
        city = get_city_for_station(station)
        print(f"{station:<15} {lead_hours or 'NULL':>12} {model:<20} {count:>10}")

        if lead_hours == LEAD_HOURS_TARGET:
            summary_by_city_and_lead[city][lead_hours] += count

    print()
    print("=" * 80)
    print(f"Summary at {LEAD_HOURS_TARGET}-hour lead time:")
    print("-" * 80)
    print(f"{'City':<20} {'Row Count':>12} {'Status':>20}")
    print("-" * 80)

    at_risk_cities = []
    for city in sorted(summary_by_city_and_lead.keys()):
        count = summary_by_city_and_lead[city].get(LEAD_HOURS_TARGET, 0)
        if count < MIN_SAMPLES:
            status = f"AT RISK ({count}/{MIN_SAMPLES})"
            at_risk_cities.append((city, count))
        else:
            status = f"OK ({count}/{MIN_SAMPLES})"
        print(f"{city:<20} {count:>12} {status:>20}")

    print()
    if at_risk_cities:
        print("=" * 80)
        print("READINESS PROJECTION:")
        print("-" * 80)
        if reset_ts:
            for city, count in at_risk_cities:
                rows_needed = MIN_SAMPLES - count
                # Assume 1 row per day at lead_hours=24 (conservative estimate)
                estimated_days = rows_needed
                readiness_date = reset_ts + timedelta(days=estimated_days)
                print(
                    f"{city:<20} {count:>3}/{MIN_SAMPLES} rows "
                    f"→ ready ~{readiness_date.strftime('%Y-%m-%d')} "
                    f"(+{estimated_days} days)"
                )
        else:
            print("Cannot estimate readiness without reset timestamp in bot_config.")
            print(
                "Run: python scripts/check_emos_data_quality.py"
                " (after reset timestamp is set)"
            )
    else:
        print("All monitored cities have >= MIN_SAMPLES rows at 24h lead.")
        print("EMOS calibration should be ready.")

    print()
    db.close()


if __name__ == "__main__":
    main()

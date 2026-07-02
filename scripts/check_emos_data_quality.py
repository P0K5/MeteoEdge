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
    - Sigma quality per model: % of rows at exactly SIGMA_FLOOR_F (floor
      saturation) and % of rows with NULL sigma_f (see issue #555)
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
from src.model.ensemble_sigma import SIGMA_FLOOR_F

MIN_SAMPLES = 60
LEAD_HOURS_TARGET = 24


def get_city_for_station(station: str) -> str:
    """Map METAR station code to city name."""
    for metar_code, _lat, _lon, city, *_ in STATIONS:
        if metar_code == station:
            return city
    return station


def print_sigma_quality_report(db: Database) -> None:
    """Print per-model sigma_f quality: floor-saturation and NULL rates.

    #555: a floored or NULL sigma_f starves EMOS's spread coefficient ``d``
    of signal. This flags, per model:
      - % of rows with sigma_f exactly == SIGMA_FLOOR_F (floor-saturated —
        only meaningful for channels that attempt to derive a sigma, e.g.
        gefs; a high rate there means the real spread is being clamped away)
      - % of rows with sigma_f IS NULL (channel has no sigma source at all)
    """
    cursor = db._conn.execute(
        """
        SELECT
            model,
            COUNT(*) as total,
            SUM(CASE WHEN sigma_f IS NULL THEN 1 ELSE 0 END) as null_count,
            SUM(CASE WHEN sigma_f = ? THEN 1 ELSE 0 END) as floor_count
        FROM model_forecast_log
        GROUP BY model
        ORDER BY model
        """,
        (SIGMA_FLOOR_F,),
    )
    rows = cursor.fetchall()

    print("=" * 80)
    print(f"Sigma Quality by Model (SIGMA_FLOOR_F = {SIGMA_FLOOR_F:.2f}F):")
    print("-" * 80)
    print(f"{'Model':<15} {'Rows':>8} {'NULL sigma_f':>16} {'At floor':>16}")
    print("-" * 80)

    if not rows:
        print("No rows in model_forecast_log yet.")
        print()
        return

    for model, total, null_count, floor_count in rows:
        null_pct = f"{null_count}/{total} ({100.0 * null_count / total:.0f}%)"
        floor_pct = f"{floor_count}/{total} ({100.0 * floor_count / total:.0f}%)"
        print(f"{model:<15} {total:>8} {null_pct:>16} {floor_pct:>16}")

    print()


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

    # Query settled days per station (forecast date with a matching METAR observation)
    settled_cursor = db._conn.execute(
        """
        SELECT mfl.station, COUNT(DISTINCT mfl.date) as settled_days
        FROM model_forecast_log mfl
        WHERE EXISTS (
            SELECT 1 FROM observations o
            WHERE o.station = mfl.station
              AND o.source = 'metar'
              AND DATE(o.ts) = mfl.date
        )
        GROUP BY mfl.station
        """
    )
    settled_by_station: dict[str, int] = {r[0]: r[1] for r in settled_cursor.fetchall()}

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
    print(f"{'City':<20} {'Forecast':>10} {'Settled':>10} {'Status':>20}")
    print("-" * 80)

    # Build city→station map for settled days lookup
    city_to_station: dict[str, str] = {
        city: metar for metar, _lat, _lon, city, *_ in STATIONS
    }

    at_risk_cities = []
    for city in sorted(summary_by_city_and_lead.keys()):
        forecast_count = summary_by_city_and_lead[city].get(LEAD_HOURS_TARGET, 0)
        station = city_to_station.get(city, "")
        settled_count = settled_by_station.get(station, 0)
        binding = min(forecast_count, settled_count) if settled_count > 0 else forecast_count
        if binding < MIN_SAMPLES:
            status = f"AT RISK ({binding}/{MIN_SAMPLES})"
            at_risk_cities.append((city, forecast_count, settled_count))
        else:
            status = f"OK ({binding}/{MIN_SAMPLES})"
        print(f"{city:<20} {forecast_count:>10} {settled_count:>10} {status:>20}")

    print()
    if at_risk_cities:
        print("=" * 80)
        print("READINESS PROJECTION:")
        print("-" * 80)
        if reset_ts:
            for city, forecast_count, settled_count in at_risk_cities:
                # Binding constraint: both forecast rows AND settled days must reach 60
                days_needed = max(MIN_SAMPLES - forecast_count, MIN_SAMPLES - settled_count)
                readiness_date = reset_ts + timedelta(days=days_needed)
                print(
                    f"{city:<20} forecast={forecast_count:>3} settled={settled_count:>3} "
                    f"→ ready ~{readiness_date.strftime('%Y-%m-%d')} "
                    f"(+{days_needed} days, bottleneck={'settled' if settled_count < forecast_count else 'forecast'})"
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
    print_sigma_quality_report(db)
    db.close()


if __name__ == "__main__":
    main()

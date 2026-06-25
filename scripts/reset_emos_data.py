#!/usr/bin/env python3
"""Perform EMOS data reset: verify legacy archive and tag reset timestamp.

USAGE:
    python scripts/reset_emos_data.py [--db /path/to/db]

This script:
1. Verifies that model_forecast_log_legacy_v1 exists and logs row count
2. Inserts model_forecast_log_reset_at timestamp into bot_config
3. Confirms the reset was recorded

Do NOT run this more than once; the reset timestamp is permanent.
"""
import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.data.db import Database


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Perform EMOS data reset: archive old rows and tag reset timestamp"
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
    print("=" * 80)
    print("EMOS Data Reset")
    print("=" * 80)
    print()

    # Step 1: Check for legacy table
    cursor = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='model_forecast_log_legacy_v1'"
    )
    legacy_exists = cursor.fetchone() is not None

    if legacy_exists:
        cursor = db._conn.execute("SELECT COUNT(*) FROM model_forecast_log_legacy_v1")
        legacy_count = cursor.fetchone()[0]
        print(f"✓ Legacy table found: model_forecast_log_legacy_v1")
        print(f"  Archived rows: {legacy_count}")
    else:
        print("⚠ Legacy table NOT found: model_forecast_log_legacy_v1")
        print("  (This is OK if migration #422 has not been deployed yet)")
        print("  (Or if this DB is fresh with no pre-migration data)")

    print()

    # Step 2: Check if reset timestamp already exists
    existing_reset = db.get_config("model_forecast_log_reset_at")
    if existing_reset:
        print(f"⚠ Reset timestamp already set: {existing_reset}")
        print("  Skipping reset (idempotent)")
        db.close()
        return

    # Step 3: Insert reset timestamp
    reset_timestamp = datetime.now(timezone.utc).isoformat()
    print(f"Setting reset timestamp: {reset_timestamp}")
    db.set_config("model_forecast_log_reset_at", reset_timestamp)

    # Step 4: Verify
    stored = db.get_config("model_forecast_log_reset_at")
    if stored == reset_timestamp:
        print(f"✓ Reset timestamp confirmed in bot_config")
    else:
        print(f"✗ ERROR: Reset timestamp not stored correctly")
        db.close()
        sys.exit(1)

    print()
    print("=" * 80)
    print("Reset complete. Run 'python scripts/check_emos_data_quality.py' to monitor progress.")
    print("=" * 80)

    db.close()


if __name__ == "__main__":
    main()

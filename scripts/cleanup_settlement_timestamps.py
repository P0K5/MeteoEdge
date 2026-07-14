#!/usr/bin/env python3
"""Fix settlements table rows with double timezone suffix (issue #719).

Removes trailing 'Z' from timestamps that have both +00:00 and Z,
converting '2026-07-12T12:01:01.762227+00:00Z' to '2026-07-12T12:01:01.762227+00:00'.

Idempotent — only affects rows with the malformed suffix:
    python scripts/cleanup_settlement_timestamps.py

Affected rows were created before the fix in src/data/settlements.py (line 33).
"""
import sqlite3
import sys
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.data.db import Database


def main() -> None:
    db = Database()
    conn = db.db

    # Find all settlement rows with the double timezone suffix (+00:00Z)
    sql_count = "SELECT COUNT(*) FROM settlements WHERE ts LIKE '%+00:00Z'"
    cur = conn.execute(sql_count)
    count_to_fix = cur.fetchone()[0]

    if count_to_fix == 0:
        print("[cleanup_settlement_timestamps] No rows with double timezone suffix found")
        return

    print(f"[cleanup_settlement_timestamps] Found {count_to_fix} row(s) with +00:00Z suffix")

    # Fix the timestamps by removing the trailing Z
    sql_update = "UPDATE settlements SET ts = substr(ts, 1, length(ts)-1) WHERE ts LIKE '%+00:00Z'"
    conn.execute(sql_update)
    conn.commit()

    # Verify the fix by checking if any rows remain with the bad suffix
    cur = conn.execute(sql_count)
    remaining = cur.fetchone()[0]

    if remaining == 0:
        print(f"[cleanup_settlement_timestamps] Successfully fixed {count_to_fix} row(s)")
        # Verify date() function works now
        sql_verify = "SELECT COUNT(*) FROM settlements WHERE date(ts) IS NULL"
        cur = conn.execute(sql_verify)
        null_dates = cur.fetchone()[0]
        if null_dates == 0:
            print("[cleanup_settlement_timestamps] Verified: SQLite date() works on all settlements")
        else:
            print(f"[cleanup_settlement_timestamps] WARNING: {null_dates} row(s) still have NULL date()")
    else:
        print(f"[cleanup_settlement_timestamps] ERROR: {remaining} row(s) still have +00:00Z suffix after fix")
        sys.exit(1)


if __name__ == "__main__":
    main()

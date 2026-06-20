"""One-shot deduplication of shadow trades in data/meteoedge.db.

Usage:
    python -m src.scripts.dedupe_shadow_trades            # live run
    python -m src.scripts.dedupe_shadow_trades --dry-run  # preview only

Background:
    Shadow-mode trades rows were written on every poll (~5 min) that a bracket
    was flagged, with no dedup logic.  This inflated the shadow table (547 raw
    rows vs 77 unique = 7.10x inflation) and corrupted win-rate statistics.

    For each (station, bracket_low, bracket_high, side, day) group where
    mode='shadow' and COUNT(*) > 1: keep the row with the earliest ts, warn if
    rows disagree on pnl, and delete all other rows.

    Live (mode='live') rows are NEVER touched.

Steps:
    1. Refuse to run if backup already exists (prevents accidental double-run).
    2. Create backup: data/meteoedge.db.pre_366J.bak
    3. Dedup shadow rows per the logic above.
    4. Print before/after row counts and inflation factor.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path

_DB_PATH = Path("data/meteoedge.db")
_BAK_PATH = Path("data/meteoedge.db.pre_366J.bak")


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _count_shadow_rows(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) FROM trades WHERE mode='shadow'")
    return cur.fetchone()[0]


def _count_live_rows(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) FROM trades WHERE mode='live'")
    return cur.fetchone()[0]


def _find_groups_and_ids(conn: sqlite3.Connection) -> tuple[list[dict], int, int]:
    """Return (groups_info, total_to_delete, pnl_warning_count).

    groups_info is a list of dicts with keys: group metadata and delete_ids list.
    """
    cur = conn.execute(
        """
        SELECT station, bracket_low, bracket_high, side, substr(ts,1,10) AS day,
               COUNT(*) AS cnt
        FROM trades
        WHERE mode='shadow'
        GROUP BY station, bracket_low, bracket_high, side, substr(ts,1,10)
        HAVING COUNT(*) > 1
        """
    )
    raw_groups = [dict(r) for r in cur.fetchall()]

    groups_info = []
    total_to_delete = 0
    pnl_warnings = 0

    for g in raw_groups:
        rows_cur = conn.execute(
            """
            SELECT id, ts, pnl FROM trades
            WHERE mode='shadow'
              AND station=?
              AND bracket_low=?
              AND bracket_high=?
              AND side=?
              AND substr(ts,1,10)=?
            ORDER BY ts ASC
            """,
            (g["station"], g["bracket_low"], g["bracket_high"], g["side"], g["day"]),
        )
        rows = [dict(r) for r in rows_cur.fetchall()]
        keep_id = rows[0]["id"]
        keep_ts = rows[0]["ts"]
        delete_ids = [r["id"] for r in rows[1:]]

        pnl_values = {r["pnl"] for r in rows if r["pnl"] is not None}
        has_pnl_disagreement = len(pnl_values) > 1
        if has_pnl_disagreement:
            pnl_warnings += 1

        total_to_delete += len(delete_ids)
        groups_info.append({
            "station": g["station"],
            "bracket_low": g["bracket_low"],
            "bracket_high": g["bracket_high"],
            "side": g["side"],
            "day": g["day"],
            "keep_id": keep_id,
            "keep_ts": keep_ts,
            "delete_ids": delete_ids,
            "pnl_values": pnl_values,
            "has_pnl_disagreement": has_pnl_disagreement,
        })

    return groups_info, total_to_delete, pnl_warnings


def dedupe(dry_run: bool = False) -> None:
    if not _DB_PATH.exists():
        raise SystemExit(f"[dedupe] DB not found: {_DB_PATH}")

    if _BAK_PATH.exists():
        raise SystemExit(
            f"[dedupe] Backup already exists: {_BAK_PATH}\n"
            "  Refusing to run to prevent accidental double-dedup.\n"
            "  If you have already run this script and want to run again,\n"
            "  remove or rename the backup file first."
        )

    # Phase 1: read-only analysis
    conn = _connect(_DB_PATH)
    before_shadow = _count_shadow_rows(conn)
    before_live = _count_live_rows(conn)
    print(f"[dedupe] Before: {before_shadow} shadow rows, {before_live} live rows")

    groups_info, total_to_delete, pnl_warnings = _find_groups_and_ids(conn)
    conn.close()

    print(f"[dedupe] Found {len(groups_info)} duplicate group(s) to process")

    for g in groups_info:
        msg = (
            f"  {'[dry-run] would' if dry_run else ''} keep id={g['keep_id']} ({g['keep_ts']}), "
            f"delete ids={g['delete_ids']} "
            f"[{g['station']} {g['bracket_low']}-{g['bracket_high']} {g['side']} {g['day']}]"
        )
        print(msg)
        if g["has_pnl_disagreement"]:
            print(
                f"  [warn] pnl disagreement in above group: "
                f"pnl values={g['pnl_values']} -- keeping earliest ts row"
            )

    if dry_run:
        print(
            f"\n[dry-run] Would delete {total_to_delete} rows "
            f"(from {before_shadow} shadow rows down to ~{before_shadow - total_to_delete})\n"
            f"[dry-run] Live rows would be unchanged: {before_live}"
        )
        if pnl_warnings:
            print(f"[dry-run] WARNING: {pnl_warnings} group(s) have pnl disagreement")
        print("[dry-run] No changes made.")
        return

    # Phase 2: backup then apply deletions
    print(f"[dedupe] Creating backup: {_BAK_PATH}")
    shutil.copy2(str(_DB_PATH), str(_BAK_PATH))

    conn = _connect(_DB_PATH)
    for g in groups_info:
        if not g["delete_ids"]:
            continue
        placeholders = ",".join("?" * len(g["delete_ids"]))
        conn.execute(
            f"DELETE FROM trades WHERE id IN ({placeholders})",
            g["delete_ids"],
        )
    conn.commit()

    after_shadow = _count_shadow_rows(conn)
    after_live = _count_live_rows(conn)
    conn.close()

    deleted = before_shadow - after_shadow
    inflation = round(before_shadow / after_shadow, 2) if after_shadow > 0 else float("inf")
    print(
        f"[dedupe] After:  {after_shadow} shadow rows, {after_live} live rows\n"
        f"[dedupe] Deleted {deleted} duplicate shadow rows "
        f"(was {before_shadow}, now {after_shadow}, inflation factor was {inflation}x)\n"
        f"[dedupe] Live rows unchanged: {before_live} -> {after_live}"
    )
    if pnl_warnings:
        print(f"[dedupe] WARNING: {pnl_warnings} group(s) had pnl disagreement — review logs above")
    if before_live != after_live:
        raise SystemExit(
            f"[dedupe] FATAL: live row count changed ({before_live} -> {after_live})! "
            "Investigate immediately."
        )
    print("[dedupe] Done.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deduplicate shadow trades in data/meteoedge.db"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview what would change without modifying the DB",
    )
    args = parser.parse_args()
    dedupe(dry_run=args.dry_run)


if __name__ == "__main__":
    main()

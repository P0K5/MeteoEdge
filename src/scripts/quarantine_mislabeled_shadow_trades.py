"""One-shot quarantine of the 5 shadow trades mislabeled by issue #610.

Background:
    The shadow upsert in src/scripts/run.py never passed ``direction=cand.direction``,
    so every low-side shadow candidate was stored with the upsert default
    ``direction='high'``. ``settle_shadow_trades()`` then resolved every shadow
    row's bracket against the daily HIGH with no direction check, so these
    mislabeled low brackets would settle as near-guaranteed fake NO wins,
    poisoning the Wilson promotion-bar statistics (see issue #610).

    Five known rows landed mislabeled on 2026-07-03:

        id   | station | side | bracket (F)
        -----|---------|------|-------------
        1162 | LFPB    | NO   | 59.0-60.8
        1170 | LFPB    | NO   | 57.2-59.0
        1175 | KMIA    | NO   | 80.0-81.0
        1176 | LFPB    | YES  | 59.0-60.8
        1201 | KMIA    | NO   | 78.0-79.0

    This script corrects their `direction` to 'low' and marks them settled
    with pnl=NULL and a close_reason note, so they are excluded from all
    shadow statistics (compute_promotion_bar filters to direction='high';
    settle_shadow_trades skips direction='low' rows; both already exclude
    settled-with-NULL-pnl rows from win/loss counts).

    This script does NOT implement daily-LOW truth settlement -- that is
    Epic C scope (#458/#452). It only removes the 5 known-bad rows from the
    shadow record.

Safety:
    - Each row is matched against its EXPECTED (station, side, bracket_low,
      bracket_high, mode='shadow') before being touched. If a row's id
      exists but does not match the expected values, it is skipped with a
      loud warning and the script exits non-zero (protects against operating
      on the wrong DB / a row that has already changed for other reasons).
    - Idempotent: rows already quarantined (close_reason == _CLOSE_REASON)
      are detected and skipped on a second run -- safe to run twice.
    - Missing rows (e.g. already cleaned up) are reported and skipped, not
      treated as fatal.

Usage (run against the production DB by the operator; not run in CI):
    python -m src.scripts.quarantine_mislabeled_shadow_trades            # live run
    python -m src.scripts.quarantine_mislabeled_shadow_trades --dry-run  # preview only
    python -m src.scripts.quarantine_mislabeled_shadow_trades --db-path /path/to/meteoedge.db
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_DB_PATH = Path("data/meteoedge.db")

_CLOSE_REASON = "quarantined_mislabeled_direction_610"

# (trade_id, expected_station, expected_side, expected_bracket_low, expected_bracket_high)
_ROWS_TO_QUARANTINE = [
    (1162, "LFPB", "NO", 59.0, 60.8),
    (1170, "LFPB", "NO", 57.2, 59.0),
    (1175, "KMIA", "NO", 80.0, 81.0),
    (1176, "LFPB", "YES", 59.0, 60.8),
    (1201, "KMIA", "NO", 78.0, 79.0),
]


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def quarantine(db_path: Path, dry_run: bool = False) -> int:
    """Quarantine the 5 known mislabeled rows. Returns a process exit code."""
    if not db_path.exists():
        print(f"[quarantine] DB not found: {db_path}")
        return 1

    conn = _connect(db_path)
    now_iso = datetime.now(timezone.utc).isoformat()

    n_fixed = 0
    n_already_done = 0
    n_missing = 0
    n_mismatched = 0

    for trade_id, exp_station, exp_side, exp_low, exp_high in _ROWS_TO_QUARANTINE:
        row = conn.execute(
            "SELECT id, station, side, bracket_low, bracket_high, mode, "
            "direction, settled_at, pnl, close_reason FROM trades WHERE id=?",
            (trade_id,),
        ).fetchone()

        if row is None:
            print(f"[quarantine] id={trade_id}: NOT FOUND -- skipping")
            n_missing += 1
            continue

        if row["close_reason"] == _CLOSE_REASON and row["direction"] == "low":
            print(f"[quarantine] id={trade_id}: already quarantined -- skipping")
            n_already_done += 1
            continue

        matches = (
            row["mode"] == "shadow"
            and row["station"] == exp_station
            and row["side"] == exp_side
            and abs(float(row["bracket_low"]) - exp_low) < 1e-6
            and abs(float(row["bracket_high"]) - exp_high) < 1e-6
        )
        if not matches:
            print(
                f"[quarantine] id={trade_id}: MISMATCH -- expected "
                f"mode=shadow station={exp_station} side={exp_side} "
                f"bracket=({exp_low}, {exp_high}); found mode={row['mode']} "
                f"station={row['station']} side={row['side']} "
                f"bracket=({row['bracket_low']}, {row['bracket_high']}). "
                "Refusing to touch this row."
            )
            n_mismatched += 1
            continue

        action = "[dry-run] would set" if dry_run else "setting"
        print(
            f"[quarantine] id={trade_id} ({exp_station} {exp_side} "
            f"{exp_low}-{exp_high}): {action} direction='low', "
            f"settled_at={now_iso}, pnl=NULL, close_reason='{_CLOSE_REASON}'"
        )
        if not dry_run:
            conn.execute(
                "UPDATE trades SET direction='low', settled_at=?, pnl=NULL, "
                "close_reason=? WHERE id=?",
                (now_iso, _CLOSE_REASON, trade_id),
            )
        n_fixed += 1

    if not dry_run:
        conn.commit()
    conn.close()

    print(
        f"\n[quarantine] Summary: {n_fixed} {'would be ' if dry_run else ''}quarantined, "
        f"{n_already_done} already done, {n_missing} not found, {n_mismatched} mismatched"
    )
    if n_mismatched:
        print("[quarantine] FAILED: mismatched row(s) found -- investigate before re-running")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Quarantine the 5 shadow trades mislabeled by issue #610 "
            "(direction dropped at insert, settled against the wrong truth)."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview what would change without modifying the DB",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=_DEFAULT_DB_PATH,
        help=f"Path to the SQLite DB (default: {_DEFAULT_DB_PATH})",
    )
    args = parser.parse_args()
    raise SystemExit(quarantine(args.db_path, dry_run=args.dry_run))


if __name__ == "__main__":
    main()

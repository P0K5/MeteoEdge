"""One-off backfill: populate trades.size_eur on historical live rows (issue #746).

Until #746, ``size_eur`` was written only on the sell path, so ``filled`` and
``timeout`` live rows held to settlement had it NULL. The writer now sets it at
placement time; this migration fills the historical rows.

Rule: for ``mode='live'`` rows where ``size_eur IS NULL`` and ``capital_before > 0``,
set ``size_eur = capital_before`` (capital_before was already the stake at
placement). Inherently idempotent -- once set, ``size_eur IS NOT NULL`` so the
row is never re-touched. Read-only preview via ``--dry-run``.

Usage (operator, against the production DB; not run in CI):
    python -m src.scripts.backfill_live_size_eur_746 --dry-run
    python -m src.scripts.backfill_live_size_eur_746
    python -m src.scripts.backfill_live_size_eur_746 --db-path /path/to/meteoedge.db
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

_DEFAULT_DB_PATH = Path(os.getenv("DB_PATH", "data/meteoedge.db"))

_SELECT = (
    "SELECT id, capital_before FROM trades "
    "WHERE mode='live' AND size_eur IS NULL AND capital_before IS NOT NULL "
    "AND capital_before > 0"
)


def backfill(db_path: Path, dry_run: bool = False) -> int:
    if not db_path.exists():
        print(f"[backfill-746] DB not found: {db_path}")
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(_SELECT).fetchall()

    n = 0
    for r in rows:
        action = "[dry-run] would set" if dry_run else "setting"
        print(f"[backfill-746] id={r['id']}: {action} size_eur={r['capital_before']}")
        if not dry_run:
            conn.execute(
                "UPDATE trades SET size_eur=? WHERE id=?",
                (r["capital_before"], r["id"]),
            )
        n += 1

    if not dry_run:
        conn.commit()
    conn.close()
    verb = "would backfill" if dry_run else "backfilled"
    print(f"\n[backfill-746] Summary: {verb} size_eur on {n} live row(s).")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill trades.size_eur = capital_before on historical live rows (#746)."
    )
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Preview without modifying the DB")
    parser.add_argument("--db-path", type=Path, default=_DEFAULT_DB_PATH,
                        help=f"Path to the SQLite DB (default: {_DEFAULT_DB_PATH})")
    args = parser.parse_args()
    raise SystemExit(backfill(args.db_path, dry_run=args.dry_run))


if __name__ == "__main__":
    main()

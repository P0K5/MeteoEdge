"""Purge old rows from candidates and guardrail_events tables to control DB size.

Idempotent daily job that deletes rows older than the configured retention
windows from the live trading database (data/meteoedge.db).

Usage::

    python -m src.scripts.purge_retention
    python -m src.scripts.purge_retention --dry-run
    python -m src.scripts.purge_retention --candidates-days 90 --guardrail-days 60
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_DB_PATH = os.getenv("DB_PATH", "data/meteoedge.db")
_DEFAULT_CANDIDATES_DAYS = int(os.getenv("CANDIDATES_RETAIN_DAYS", "90"))
_DEFAULT_GUARDRAIL_DAYS = int(os.getenv("GUARDRAIL_RETAIN_DAYS", "60"))


def _get_cutoff_ts(days: int) -> str:
    """Return ISO 8601 timestamp string for *days* ago (UTC).

    Example: if today is 2026-07-11 13:00 UTC and days=90,
    returns "2026-04-12T13:00:00+00:00" or similar (the exact format
    depends on how timestamps are stored, but we use a safe string comparison).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.isoformat()


def purge_candidates(
    conn: sqlite3.Connection,
    days: int,
    dry_run: bool = False,
) -> int:
    """Delete candidates rows older than *days* days. Returns count deleted.

    Args:
        conn: SQLite connection to meteoedge.db.
        days: Retention window in days.
        dry_run: If True, count rows but do not delete.

    Returns:
        Number of rows deleted (or that would be deleted if dry_run=True).
    """
    cutoff_ts = _get_cutoff_ts(days)
    sql = "SELECT COUNT(*) FROM candidates WHERE ts < ?"
    cur = conn.execute(sql, (cutoff_ts,))
    count_to_delete = cur.fetchone()[0]

    if count_to_delete > 0 and not dry_run:
        sql_delete = "DELETE FROM candidates WHERE ts < ?"
        conn.execute(sql_delete, (cutoff_ts,))
        conn.commit()
        log.info(f"Deleted {count_to_delete} candidates rows older than {cutoff_ts}")
    elif dry_run and count_to_delete > 0:
        log.info(f"[DRY RUN] Would delete {count_to_delete} candidates rows older than {cutoff_ts}")

    return count_to_delete


def purge_guardrail_events(
    conn: sqlite3.Connection,
    days: int,
    dry_run: bool = False,
) -> int:
    """Delete guardrail_events rows older than *days* days. Returns count deleted.

    Args:
        conn: SQLite connection to meteoedge.db.
        days: Retention window in days.
        dry_run: If True, count rows but do not delete.

    Returns:
        Number of rows deleted (or that would be deleted if dry_run=True).
    """
    cutoff_ts = _get_cutoff_ts(days)
    sql = "SELECT COUNT(*) FROM guardrail_events WHERE ts < ?"
    cur = conn.execute(sql, (cutoff_ts,))
    count_to_delete = cur.fetchone()[0]

    if count_to_delete > 0 and not dry_run:
        sql_delete = "DELETE FROM guardrail_events WHERE ts < ?"
        conn.execute(sql_delete, (cutoff_ts,))
        conn.commit()
        log.info(f"Deleted {count_to_delete} guardrail_events rows older than {cutoff_ts}")
    elif dry_run and count_to_delete > 0:
        log.info(f"[DRY RUN] Would delete {count_to_delete} guardrail_events rows older than {cutoff_ts}")

    return count_to_delete


def run(
    db_path: str | Path | None = None,
    candidates_days: int | None = None,
    guardrail_days: int | None = None,
    dry_run: bool = False,
) -> None:
    """Execute purge for both tables.

    Args:
        db_path: Path to meteoedge.db. Defaults to DB_PATH env var.
        candidates_days: Retention window for candidates. Defaults to CANDIDATES_RETAIN_DAYS env var or 90.
        guardrail_days: Retention window for guardrail_events. Defaults to GUARDRAIL_RETAIN_DAYS env var or 60.
        dry_run: If True, count rows but do not delete.
    """
    db_path = db_path or _DEFAULT_DB_PATH
    candidates_days = candidates_days if candidates_days is not None else _DEFAULT_CANDIDATES_DAYS
    guardrail_days = guardrail_days if guardrail_days is not None else _DEFAULT_GUARDRAIL_DAYS

    db_path = Path(db_path).resolve()
    if not db_path.exists():
        log.error(f"Database not found: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    try:
        cand_deleted = purge_candidates(conn, candidates_days, dry_run)
        guard_deleted = purge_guardrail_events(conn, guardrail_days, dry_run)
    finally:
        conn.close()

    dry_tag = " [DRY RUN]" if dry_run else ""
    print(
        f"[purge_retention]{dry_tag} "
        f"candidates: retention={candidates_days}d, deleted={cand_deleted} | "
        f"guardrail_events: retention={guardrail_days}d, deleted={guard_deleted}"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Purge old rows from candidates and guardrail_events tables (idempotent)."
    )
    parser.add_argument(
        "--db-path",
        metavar="PATH",
        default=_DEFAULT_DB_PATH,
        help=f"Path to meteoedge.db (default: {_DEFAULT_DB_PATH}).",
    )
    parser.add_argument(
        "--candidates-days",
        type=int,
        default=_DEFAULT_CANDIDATES_DAYS,
        metavar="N",
        help=f"Retention window for candidates in days (default: {_DEFAULT_CANDIDATES_DAYS}).",
    )
    parser.add_argument(
        "--guardrail-days",
        type=int,
        default=_DEFAULT_GUARDRAIL_DAYS,
        metavar="N",
        help=f"Retention window for guardrail_events in days (default: {_DEFAULT_GUARDRAIL_DAYS}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count rows only; do not delete.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    run(
        db_path=args.db_path,
        candidates_days=args.candidates_days,
        guardrail_days=args.guardrail_days,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":  # pragma: no cover
    main()

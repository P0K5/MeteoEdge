"""Purge old rows from candidates, guardrail_events, and copy_signals tables
to control DB size.

Idempotent daily job that deletes rows older than the configured retention
windows from the live trading database (data/meteoedge.db).

Usage::

    python -m src.scripts.purge_retention
    python -m src.scripts.purge_retention --dry-run
    python -m src.scripts.purge_retention --candidates-days 90 --guardrail-days 60 --copy-signals-days 30
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.data.db import connect_with_busy_timeout

log = logging.getLogger(__name__)

_DEFAULT_DB_PATH = os.getenv("DB_PATH", "data/meteoedge.db")
_DEFAULT_CANDIDATES_DAYS = int(os.getenv("CANDIDATES_RETAIN_DAYS", "90"))
_DEFAULT_GUARDRAIL_DAYS = int(os.getenv("GUARDRAIL_RETAIN_DAYS", "60"))
# Issue #1339: copy_signals is append-only and has no other retention bound
# (unlike candidates/guardrail_events, which were already covered here) --
# growing ~120 rows/hour, it was the root cause of the Activity Feed
# endpoint's unbounded full-table query. 30 days matches this job's
# existing guardrail_events default.
_DEFAULT_COPY_SIGNALS_DAYS = int(os.getenv("COPY_SIGNALS_RETAIN_DAYS", "30"))

# Issue #1238: meteoedge-purge-retention.timer runs daily at 01:00 UTC
# (02:00 local during DST) -- squarely inside the 01:00-03:00 local window
# where the live bot and the MSS collector cluster "database is locked"
# errors. This job's own connection used to be a bare sqlite3.connect() with
# no busy_timeout at all (the default 5s), and -- more importantly, since
# this job is the one HOLDING the write lock, not waiting on one -- each
# DELETE ran as a single transaction over however many rows matched the
# retention cutoff. `candidates` is the highest-write-volume table in the
# schema (one row per bracket per station per poll), so after a long
# retention window its matching-row count isn't bounded by anything this
# script controls. Chunking bounds how long any ONE transaction holds the
# write lock to roughly one chunk's delete time, independent of how many
# rows total are past the cutoff -- the property needed here, since the
# actual row count on the host isn't something this PR can measure without
# host access.
_DELETE_CHUNK_SIZE = 2000


def _get_cutoff_ts(days: int) -> str:
    """Return ISO 8601 timestamp string for *days* ago (UTC).

    Example: if today is 2026-07-11 13:00 UTC and days=90,
    returns "2026-04-12T13:00:00+00:00" or similar (the exact format
    depends on how timestamps are stored, but we use a safe string comparison).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.isoformat()


def _purge_table_chunked(
    conn: sqlite3.Connection,
    table: str,
    days: int,
    dry_run: bool = False,
    ts_column: str = "ts",
) -> int:
    """Delete *table* rows older than *days* days, in bounded-size chunks.

    Each chunk is its own transaction (commit after every chunk) so the
    write lock is held for roughly one chunk's delete time rather than for
    however long the full matching set takes -- see `_DELETE_CHUNK_SIZE`'s
    docstring comment (issue #1238). Purging is idempotent either way: an
    interrupted run simply leaves the remaining old rows for the next one.

    Args:
        conn: SQLite connection to meteoedge.db.
        table: Table name (``"candidates"``, ``"guardrail_events"``, or
            ``"copy_signals"``) -- trusted, not user input; never built
            from external data.
        days: Retention window in days.
        dry_run: If True, count rows but do not delete.
        ts_column: Name of the table's timestamp column to filter on.
            Defaults to ``"ts"`` (candidates/guardrail_events); issue #1339
            added ``copy_signals``, which has no ``ts`` column -- its
            timestamp column is ``detected_at`` -- so this is now a
            parameter rather than hardcoded. Trusted, not user input, same
            as *table*.

    Returns:
        Number of rows deleted (or that would be deleted if dry_run=True).
    """
    cutoff_ts = _get_cutoff_ts(days)
    count_sql = f"SELECT COUNT(*) FROM {table} WHERE {ts_column} < ?"  # noqa: S608 - table/column are trusted
    count_to_delete = conn.execute(count_sql, (cutoff_ts,)).fetchone()[0]

    if count_to_delete == 0:
        return 0

    if dry_run:
        log.info(f"[DRY RUN] Would delete {count_to_delete} {table} rows older than {cutoff_ts}")
        return count_to_delete

    delete_sql = (
        f"DELETE FROM {table} WHERE id IN "  # noqa: S608 - table/column are trusted
        f"(SELECT id FROM {table} WHERE {ts_column} < ? LIMIT ?)"
    )
    deleted_total = 0
    while True:
        cur = conn.execute(delete_sql, (cutoff_ts, _DELETE_CHUNK_SIZE))
        conn.commit()
        deleted_total += cur.rowcount
        if cur.rowcount < _DELETE_CHUNK_SIZE:
            break

    log.info(f"Deleted {deleted_total} {table} rows older than {cutoff_ts}")
    return deleted_total


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
    return _purge_table_chunked(conn, "candidates", days, dry_run)


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
    return _purge_table_chunked(conn, "guardrail_events", days, dry_run)


def purge_copy_signals(
    conn: sqlite3.Connection,
    days: int,
    dry_run: bool = False,
) -> int:
    """Delete copy_signals rows older than *days* days. Returns count deleted.

    Issue #1339: copy_signals is append-only, with no prior retention
    policy, growing ~120 rows/hour -- same chunked-delete shape as
    purge_candidates/purge_guardrail_events, but cutting on ``detected_at``
    (this table's own timestamp column; it has no ``ts`` column).

    Args:
        conn: SQLite connection to meteoedge.db.
        days: Retention window in days.
        dry_run: If True, count rows but do not delete.

    Returns:
        Number of rows deleted (or that would be deleted if dry_run=True).
    """
    return _purge_table_chunked(conn, "copy_signals", days, dry_run, ts_column="detected_at")


def run(
    db_path: str | Path | None = None,
    candidates_days: int | None = None,
    guardrail_days: int | None = None,
    copy_signals_days: int | None = None,
    dry_run: bool = False,
) -> None:
    """Execute purge for all three tables.

    Args:
        db_path: Path to meteoedge.db. Defaults to DB_PATH env var.
        candidates_days: Retention window for candidates. Defaults to CANDIDATES_RETAIN_DAYS env var or 90.
        guardrail_days: Retention window for guardrail_events. Defaults to GUARDRAIL_RETAIN_DAYS env var or 60.
        copy_signals_days: Retention window for copy_signals (issue #1339). Defaults to COPY_SIGNALS_RETAIN_DAYS env var or 30.
        dry_run: If True, count rows but do not delete.
    """
    db_path = db_path or _DEFAULT_DB_PATH
    candidates_days = candidates_days if candidates_days is not None else _DEFAULT_CANDIDATES_DAYS
    guardrail_days = guardrail_days if guardrail_days is not None else _DEFAULT_GUARDRAIL_DAYS
    copy_signals_days = copy_signals_days if copy_signals_days is not None else _DEFAULT_COPY_SIGNALS_DAYS

    db_path = Path(db_path).resolve()
    if not db_path.exists():
        log.error(f"Database not found: {db_path}")
        sys.exit(1)

    conn = connect_with_busy_timeout(str(db_path))  # issue #1238
    try:
        cand_deleted = purge_candidates(conn, candidates_days, dry_run)
        guard_deleted = purge_guardrail_events(conn, guardrail_days, dry_run)
        signals_deleted = purge_copy_signals(conn, copy_signals_days, dry_run)
    finally:
        conn.close()

    dry_tag = " [DRY RUN]" if dry_run else ""
    log.info(
        f"[purge_retention]{dry_tag} "
        f"candidates: retention={candidates_days}d, deleted={cand_deleted} | "
        f"guardrail_events: retention={guardrail_days}d, deleted={guard_deleted} | "
        f"copy_signals: retention={copy_signals_days}d, deleted={signals_deleted}"
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
        "--copy-signals-days",
        type=int,
        default=_DEFAULT_COPY_SIGNALS_DAYS,
        metavar="N",
        help=f"Retention window for copy_signals in days (default: {_DEFAULT_COPY_SIGNALS_DAYS}).",
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
        copy_signals_days=args.copy_signals_days,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":  # pragma: no cover
    main()

"""Archival ETL: ingest snapshot JSONL files into data/analytics.db.

Reads all rotated JSONL files for snapshots and position_snapshots,
applies high-water-mark (HWM) filtering for incremental runs, and
inserts new records via ArchiveDatabase (INSERT OR IGNORE — idempotent).

Usage::

    python -m src.scripts.archive_snapshots
    python -m src.scripts.archive_snapshots --dry-run
    python -m src.scripts.archive_snapshots --from-date 2026-06-01 --to-date 2026-06-15
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from src.config import SNAPSHOTS_JSONL, POSITION_SNAPSHOTS_JSONL
from src.data.archive_db import ArchiveDatabase
from src.utils.log_rotation import iter_rotated_jsonl

log = logging.getLogger(__name__)

_BATCH_SIZE = 1000

# Required fields per table — records missing any of these are malformed.
_SNAPSHOT_REQUIRED = frozenset({"ts", "station", "ticker"})
_POSITION_REQUIRED = frozenset({"ts", "no_token_id"})


def _process_table(
    *,
    db: ArchiveDatabase,
    base_path: Path,
    table: str,
    required_fields: frozenset,
    insert_fn,
    hwm: str | None,
    from_date: str | None,
    to_date: str | None,
    dry_run: bool,
) -> tuple[int, int, int, int]:
    """ETL loop for a single archive table.

    Returns (scanned, inserted, skipped_existing, malformed).
    """
    scanned = 0
    inserted = 0
    skipped_existing = 0
    malformed = 0

    batch: list[dict] = []

    def _flush() -> None:
        nonlocal inserted, skipped_existing
        if not batch:
            return
        if not dry_run:
            n = insert_fn(batch)
            inserted += n
            skipped_existing += len(batch) - n
        batch.clear()

    raw_iter = iter_rotated_jsonl(base_path)

    # Wrap in HWM / date filter; intercept malformed records before the filter
    # so we can count them separately.
    for line_record in raw_iter:
        # Validate required fields
        if not required_fields.issubset(line_record.keys()):
            malformed += 1
            continue

        ts = line_record.get("ts")
        if not isinstance(ts, str) or not ts:
            malformed += 1
            continue

        scanned += 1

        # Apply HWM + date-window filter (skip if out of range)
        if hwm is not None and ts <= hwm:
            skipped_existing += 1
            continue
        if from_date is not None and ts < from_date:
            continue
        if to_date is not None and ts > to_date:
            continue

        batch.append(line_record)
        if len(batch) >= _BATCH_SIZE:
            _flush()

    _flush()

    return scanned, inserted, skipped_existing, malformed


def run(
    db_path: str | Path | None = None,
    dry_run: bool = False,
    from_date: str | None = None,
    to_date: str | None = None,
) -> None:
    """Execute the archival ETL for both snapshot tables.

    Args:
        db_path: Path to analytics.db.  Defaults to ARCHIVE_DB_PATH env var
                 (handled by ArchiveDatabase.__init__).
        dry_run: If True, count records but do not write to the database.
        from_date: ISO 8601 date string (YYYY-MM-DD); filter lower bound on ts.
        to_date: ISO 8601 date string (YYYY-MM-DD); filter upper bound on ts.
    """
    kwargs = {} if db_path is None else {"path": db_path}
    with ArchiveDatabase(**kwargs) as db:
        # ---- snapshot_archive ------------------------------------------------
        snap_hwm = db.get_max_archived_ts("snapshot_archive")
        # When --from-date is set, ignore the HWM so the caller can re-ingest
        # a historical window without needing to wipe the DB.
        effective_snap_hwm = None if from_date else snap_hwm

        snap_scanned, snap_inserted, snap_skipped, snap_malformed = _process_table(
            db=db,
            base_path=SNAPSHOTS_JSONL,
            table="snapshot_archive",
            required_fields=_SNAPSHOT_REQUIRED,
            insert_fn=db.insert_snapshots,
            hwm=effective_snap_hwm,
            from_date=from_date,
            to_date=to_date,
            dry_run=dry_run,
        )
        snap_hwm_after = db.get_max_archived_ts("snapshot_archive") if not dry_run else snap_hwm

        # ---- position_snapshot_archive ----------------------------------------
        pos_hwm = db.get_max_archived_ts("position_snapshot_archive")
        effective_pos_hwm = None if from_date else pos_hwm

        pos_scanned, pos_inserted, pos_skipped, pos_malformed = _process_table(
            db=db,
            base_path=POSITION_SNAPSHOTS_JSONL,
            table="position_snapshot_archive",
            required_fields=_POSITION_REQUIRED,
            insert_fn=db.insert_position_snapshots,
            hwm=effective_pos_hwm,
            from_date=from_date,
            to_date=to_date,
            dry_run=dry_run,
        )
        pos_hwm_after = db.get_max_archived_ts("position_snapshot_archive") if not dry_run else pos_hwm

    dry_tag = " [DRY RUN]" if dry_run else ""
    print(
        f"[archive]{dry_tag} "
        f"snapshots: scanned {snap_scanned}, inserted {snap_inserted}, "
        f"skipped-existing {snap_skipped}, malformed {snap_malformed} "
        f"| new HWM: {snap_hwm_after}"
    )
    print(
        f"[archive]{dry_tag} "
        f"position_snapshots: scanned {pos_scanned}, inserted {pos_inserted}, "
        f"skipped-existing {pos_skipped}, malformed {pos_malformed} "
        f"| new HWM: {pos_hwm_after}"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Ingest snapshot JSONL files into data/analytics.db (incremental, idempotent)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count records only; do not write to the database.",
    )
    parser.add_argument(
        "--from-date",
        metavar="YYYY-MM-DD",
        help="Only ingest records with ts >= FROM_DATE (also disables HWM filter).",
    )
    parser.add_argument(
        "--to-date",
        metavar="YYYY-MM-DD",
        help="Only ingest records with ts <= TO_DATE.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    run(
        dry_run=args.dry_run,
        from_date=args.from_date,
        to_date=args.to_date,
    )


if __name__ == "__main__":  # pragma: no cover
    main()

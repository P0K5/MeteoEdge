"""Date-based JSONL/CSV log rotation utilities.

Each log file type rotates daily by appending the UTC date to the stem:
    snapshots.2026-06-16.jsonl
    candidates.2026-06-16.csv
    live_trades.2026-06-16.jsonl
    position_snapshots.2026-06-16.jsonl

A symlink (or plain text index on systems that don't support symlinks) named
after the original filename always points to today's dated file so that
existing readers need only resolve the symlink to open the current file.

Old dated files are gzip-compressed after LOG_ROTATION_COMPRESS_AFTER_DAYS days
(default 1 day, i.e. yesterday's files are compressed the next day) and deleted
after LOG_ROTATION_RETAIN_DAYS days (default 30).

Usage — writers::

    from src.utils.log_rotation import rotated_path
    with open(rotated_path(SNAPSHOTS_JSONL), "a") as f:
        f.write(json.dumps(row) + "\n")

Usage — readers::

    from src.utils.log_rotation import resolve_current, iter_rotated_jsonl
    # Single-file reader (current day only):
    path = resolve_current(SNAPSHOTS_JSONL)
    # Multi-file reader across all retained dates:
    for record in iter_rotated_jsonl(SNAPSHOTS_JSONL):
        process(record)
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (overridable via env vars)
# ---------------------------------------------------------------------------

# Number of days before a dated file is gzip-compressed (0 = compress immediately after rotation)
LOG_ROTATION_COMPRESS_AFTER_DAYS: int = int(os.getenv("LOG_ROTATION_COMPRESS_AFTER_DAYS", "1"))

# Number of days to retain dated files before deletion
LOG_ROTATION_RETAIN_DAYS: int = int(os.getenv("LOG_ROTATION_RETAIN_DAYS", "30"))


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _dated_path(base: Path, for_date: date | None = None) -> Path:
    """Return the dated variant of *base* for a given date (default: today UTC).

    Examples::
        _dated_path(Path("logs/snapshots.jsonl"))
        → Path("logs/snapshots.2026-06-16.jsonl")

        _dated_path(Path("logs/candidates.csv"))
        → Path("logs/candidates.2026-06-16.csv")
    """
    d = for_date or _today_utc()
    stem = base.stem          # e.g. "snapshots"
    suffix = base.suffix      # e.g. ".jsonl"
    return base.parent / f"{stem}.{d.isoformat()}{suffix}"


def rotated_path(base: Path, for_date: date | None = None) -> Path:
    """Return the path to write for *base* today, and keep the symlink current.

    Creates the parent directory and the dated file if necessary.  Updates
    the symlink so that ``base`` always resolves to today's dated file.
    """
    base.parent.mkdir(parents=True, exist_ok=True)
    dated = _dated_path(base, for_date)

    # Create dated file if missing (so symlink target always exists)
    if not dated.exists():
        dated.touch()

    # Maintain a symlink from the bare name → today's dated file
    _update_symlink(base, dated)

    return dated


def resolve_current(base: Path) -> Path:
    """Return the path of the current (today's) log file for *base*.

    If the symlink / dated file already exists, returns it.  Otherwise falls
    back to *base* itself so legacy callers still work during a first run.
    """
    dated = _dated_path(base)
    if dated.exists():
        return dated
    if base.exists():
        return base
    return dated  # writer will create it


def _update_symlink(base: Path, target: Path) -> None:
    """Atomically update the symlink at *base* to point to *target*.

    Uses a relative target path so the symlink works regardless of the
    working directory.  Falls back silently if symlinks are not supported
    (e.g. certain Windows or restricted environments).
    """
    try:
        rel_target = Path(target.name)  # same directory, so just the filename
        # Remove stale symlink or plain file only if it differs
        if base.is_symlink():
            if os.readlink(base) == str(rel_target):
                return  # already correct
            base.unlink()
        elif base.exists() and not base.is_symlink():
            # Plain file from before rotation was introduced — leave it alone;
            # don't clobber existing data.  Symlink will be created once the
            # old file is gone.
            return

        base.symlink_to(rel_target)
    except (OSError, NotImplementedError):
        pass  # symlinks not supported — readers fall back to resolve_current()


# ---------------------------------------------------------------------------
# Housekeeping: compression + deletion of old dated files
# ---------------------------------------------------------------------------

def housekeep(base: Path) -> None:
    """Compress and/or delete old dated files for *base*.

    Called by writers on first open each day.  Should be cheap (no-op if
    nothing has aged out).
    """
    today = _today_utc()
    stem = base.stem
    suffix = base.suffix
    directory = base.parent

    if not directory.exists():
        return

    for path in directory.glob(f"{stem}.????-??-??{suffix}"):
        # Extract date from filename
        date_part = path.stem[len(stem) + 1:]  # e.g. "2026-06-01"
        try:
            file_date = date.fromisoformat(date_part)
        except ValueError:
            continue

        age_days = (today - file_date).days
        if age_days <= 0:
            continue  # today's file — skip

        if age_days > LOG_ROTATION_RETAIN_DAYS:
            # Delete (also remove .gz if present)
            _safe_remove(path)
            _safe_remove(Path(str(path) + ".gz"))
            log.info("[log_rotation] deleted aged-out file: %s (age=%d days)", path.name, age_days)

        elif age_days >= LOG_ROTATION_COMPRESS_AFTER_DAYS:
            gz_path = Path(str(path) + ".gz")
            if not gz_path.exists() and path.exists():
                _compress(path, gz_path)
                _safe_remove(path)
                log.info("[log_rotation] compressed: %s → %s", path.name, gz_path.name)


def _compress(src: Path, dst: Path) -> None:
    with open(src, "rb") as f_in, gzip.open(dst, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)


def _safe_remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("[log_rotation] could not remove %s: %s", path, e)


# ---------------------------------------------------------------------------
# Multi-file reader helpers
# ---------------------------------------------------------------------------

def iter_rotated_jsonl(base: Path, include_compressed: bool = True) -> Iterator[dict]:
    """Yield every JSON record across all retained dated JSONL files for *base*.

    Files are yielded in chronological order (oldest first).  Compressed
    .gz variants are decompressed on-the-fly when *include_compressed* is True.

    Falls back to reading *base* directly if no dated files exist (graceful
    degradation for environments that haven't rotated yet).
    """
    stem = base.stem
    suffix = base.suffix
    directory = base.parent

    dated_files: list[tuple[date, Path, bool]] = []  # (date, path, is_gz)

    if directory.exists():
        for path in directory.glob(f"{stem}.????-??-??{suffix}"):
            date_part = path.stem[len(stem) + 1:]
            try:
                file_date = date.fromisoformat(date_part)
                dated_files.append((file_date, path, False))
            except ValueError:
                pass

        if include_compressed:
            for path in directory.glob(f"{stem}.????-??-??{suffix}.gz"):
                # stem of "snapshots.2026-06-01.jsonl.gz" → "snapshots.2026-06-01.jsonl"
                inner_stem = path.name[: -len(suffix + ".gz")]  # "snapshots.2026-06-01"
                date_part = inner_stem[len(stem) + 1:]
                try:
                    file_date = date.fromisoformat(date_part)
                    dated_files.append((file_date, path, True))
                except ValueError:
                    pass

    if not dated_files:
        # Legacy fallback: bare filename
        if base.exists():
            yield from _read_jsonl_file(base, is_gz=False)
        return

    for _, path, is_gz in sorted(dated_files, key=lambda t: t[0]):
        yield from _read_jsonl_file(path, is_gz=is_gz)


def _read_jsonl_file(path: Path, is_gz: bool) -> Iterator[dict]:
    try:
        opener = gzip.open(path, "rt", encoding="utf-8") if is_gz else open(path, "r", encoding="utf-8")
        with opener as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
    except OSError as e:
        log.warning("[log_rotation] could not read %s: %s", path, e)

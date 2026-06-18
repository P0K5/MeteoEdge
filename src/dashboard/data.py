"""Shared data-access layer for dashboard. All JSONL reads go through mtime-keyed cache. Thread-safe."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from src.config import LIVE_TRADES_JSONL, SNAPSHOTS_JSONL, POSITION_SNAPSHOTS_JSONL
from src.utils.log_rotation import iter_rotated_jsonl, rotated_sources

# ---------------------------------------------------------------------------
# mtime-keyed JSONL cache — thread-safe
# ---------------------------------------------------------------------------

_cache: dict[str, dict[str, Any]] = {}
_cache_lock = threading.Lock()


def read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file and return a list of dicts.

    Re-parses only when the file mtime or size has changed since the last
    read.  Returns [] when the file is missing or corrupt.
    """
    if not path.exists():
        return []

    try:
        stat = path.stat()
        mtime = stat.st_mtime
        size = stat.st_size
    except OSError:
        return []

    cache_key = str(path)
    with _cache_lock:
        entry = _cache.get(cache_key)
        if entry is not None and entry["mtime"] == mtime and entry["size"] == size:
            return entry["data"]

    # Parse outside the lock so a slow disk read doesn't block other threads.
    records: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        return []

    with _cache_lock:
        _cache[cache_key] = {"mtime": mtime, "size": size, "data": records}

    return records


def _read_rotated_cached(base: Path) -> list[dict]:
    """Read every rotated source (legacy + dated) for *base* into a list.

    Cache key = (max mtime, total size) across every source file, so the
    cache invalidates whenever any rotation slot changes.
    """
    sources = rotated_sources(base)
    if not sources:
        return []

    max_mtime = 0.0
    total_size = 0
    for p in sources:
        try:
            st = os.stat(p)
        except OSError:
            continue
        if st.st_mtime > max_mtime:
            max_mtime = st.st_mtime
        total_size += st.st_size

    cache_key = f"rotated::{base}"
    with _cache_lock:
        entry = _cache.get(cache_key)
        if entry is not None and entry["mtime"] == max_mtime and entry["size"] == total_size:
            return entry["data"]

    records = list(iter_rotated_jsonl(base))

    with _cache_lock:
        _cache[cache_key] = {"mtime": max_mtime, "size": total_size, "data": records}
    return records


def load_live_trades() -> list[dict]:
    """Return all records from live_trades, spanning every rotated source."""
    return _read_rotated_cached(LIVE_TRADES_JSONL)


def load_snapshots() -> list[dict]:
    """Return all records from snapshots, spanning every rotated source."""
    return _read_rotated_cached(SNAPSHOTS_JSONL)


def load_position_snapshots() -> list[dict]:
    """Return all records from position_snapshots, spanning every rotated source."""
    return _read_rotated_cached(POSITION_SNAPSHOTS_JSONL)


def get_db():
    """Return the module-level Database singleton from src.dashboard.api.

    Deferred import avoids a circular dependency — api.py imports from data.py
    and data.py only needs _db at call time.
    """
    from src.dashboard import api as _api
    return _api._db

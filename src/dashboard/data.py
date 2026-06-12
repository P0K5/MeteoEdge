"""Shared data-access layer for dashboard. All JSONL reads go through mtime-keyed cache. Thread-safe."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from src.config import LIVE_TRADES_JSONL, SNAPSHOTS_JSONL, POSITION_SNAPSHOTS_JSONL

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


def load_live_trades() -> list[dict]:
    """Return all records from the live trades JSONL file."""
    return read_jsonl(LIVE_TRADES_JSONL)


def load_snapshots() -> list[dict]:
    """Return all records from the snapshots JSONL file."""
    return read_jsonl(SNAPSHOTS_JSONL)


def load_position_snapshots() -> list[dict]:
    """Return all records from the position snapshots JSONL file."""
    return read_jsonl(POSITION_SNAPSHOTS_JSONL)


def get_db():
    """Return the module-level Database singleton from src.dashboard.api.

    Deferred import avoids a circular dependency — api.py imports from data.py
    and data.py only needs _db at call time.
    """
    from src.dashboard import api as _api
    return _api._db

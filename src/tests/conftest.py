"""Shared pytest configuration for the MeteoEdge test suite.

Isolates the SQLite database **per test process** so the suite can run in
parallel (`pytest -n auto`, pytest-xdist) without workers racing on the database.

Why this is needed
------------------
`src/data/db.py` binds the default DB path at import time::

    _DEFAULT_PATH = os.getenv("DB_PATH", "data/meteoedge.db")

and `src/dashboard/api.py` opens it eagerly at import time::

    _db = Database()

Every test module that imports `src.dashboard.api` (11 of them) therefore opens
the real, shared ``data/meteoedge.db`` WAL file the moment it is collected. Under
xdist each worker is a separate process that repeats this open and runs the DDL +
migrations (writes) against the *same* file, racing on the WAL lock::

    sqlite3.OperationalError: database is locked

Giving each process its own DB file removes the race and also stops tests from
touching the developer's real local database.

Why ``pytest_configure`` and not module-level code
--------------------------------------------------
xdist spawns workers that **inherit the controller's environment**. If we set
``DB_PATH`` once at conftest import, the controller's value is inherited by every
worker and they all share a single file — defeating the isolation. ``pytest_configure``
runs in the controller *and* in each worker (each with its own ``workerid``), and
runs before test modules are collected/imported, so it is the correct place to
assign a per-process path.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest


def pytest_configure(config):
    """Point this process (controller or xdist worker) at its own SQLite DB."""
    # ``workerinput`` only exists in xdist worker processes; the controller and a
    # plain sequential run fall back to "main". The pid keeps it unique even if two
    # processes ever report the same worker id.
    worker = getattr(config, "workerinput", {}).get("workerid", "main")
    db_dir = Path(tempfile.gettempdir()) / "meteoedge-tests"
    db_dir.mkdir(parents=True, exist_ok=True)
    os.environ["DB_PATH"] = str(db_dir / f"test-{worker}-{os.getpid()}.db")


@pytest.fixture(autouse=True)
def _restore_api_db():
    """Snapshot and restore ``src.dashboard.api._db`` around every test.

    The dashboard endpoints read a module-level ``_db`` global. Several tests
    intentionally reassign it (``set_db(...)``, ``_db = None``, ``_db = MagicMock()``)
    to exercise the 503/no-DB paths, and not all of them restore it. A leaked value
    would poison later tests on the same process — e.g. a left-over ``_db = None``
    makes an unrelated endpoint test raise ``AttributeError`` instead of querying the
    DB. This is order-dependent and was exposed once `pytest-xdist` started packing
    multiple test files onto one worker. Restoring here makes the global hermetic per
    test regardless of what each test does to it.
    """
    mod = sys.modules.get("src.dashboard.api")
    if mod is None:
        # API module not imported by this test — nothing to protect.
        yield
        return
    saved = mod._db
    try:
        yield
    finally:
        mod._db = saved

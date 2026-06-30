"""conftest.py for the integration test suite.

Sets DB_PATH to an isolated temp file before any test modules are imported,
mirroring the pattern used in src/tests/conftest.py.  This is important
because src.dashboard.api opens _db = Database() at import time.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def pytest_configure(config):
    """Give this test process its own SQLite file so it never touches the dev DB."""
    worker = getattr(config, "workerinput", {}).get("workerid", "main")
    db_dir = Path(tempfile.gettempdir()) / "meteoedge-integration-tests"
    db_dir.mkdir(parents=True, exist_ok=True)
    os.environ["DB_PATH"] = str(db_dir / f"test-integration-{worker}-{os.getpid()}.db")

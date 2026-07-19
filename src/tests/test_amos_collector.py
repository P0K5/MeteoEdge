"""Unit tests for src/data/collectors/amos.py.

Issue #740: AmosCollector is retired. KMA ASOS was unobtainable by design
(archive-only, issue #694) and the Open-Meteo fallback it used to poll never
produced official readings -- every source='amos' row since 2026-06-08 was
modelled data (is_official=0). METAR (RKSI/RKPK) is now the sole Korea
observation truth feed (see config/source_priority.yaml).

These tests verify the collector is now a true no-op: no HTTP calls, no DB
writes, no exceptions -- kept only so the existing import/wiring in
src/scripts/run.py continues to work.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import src.data.collectors.amos as amos_module
from src.data.collectors.amos import AmosCollector
from src.data.db import Database


def _db() -> Database:
    """Fresh in-memory Database with cadence_min/is_official columns applied.

    These columns are added by the #106 schema migration. We apply them here
    so tests pass before that PR is merged (idempotent — ALTER TABLE is a no-op
    if columns already exist after #106 merges).
    """
    db = Database(":memory:")
    for col, defn in [("cadence_min", "INTEGER"), ("is_official", "INTEGER DEFAULT 1")]:
        try:
            db._conn.execute(f"ALTER TABLE observations ADD COLUMN {col} {defn}")
            db._conn.commit()
        except Exception:
            pass  # column already exists
    return db


class TestAmosCollectorRetired:
    """Issue #740: poll() and run_loop() are no-ops."""

    def test_poll_returns_false_for_both_stations(self):
        db = _db()
        collector = AmosCollector(db)
        results = collector.poll()
        assert results == {"Seoul": False, "Busan": False}

    def test_poll_writes_no_source_amos_observation_rows(self):
        """No new source='amos' rows are written by poll() -- the whole point
        of retiring this collector (issue #740/#741)."""
        db = _db()
        collector = AmosCollector(db)

        collector.poll()

        assert db.get_observations("Seoul", since="2000-01-01") == []
        assert db.get_observations("Busan", since="2000-01-01") == []

    def test_poll_does_not_raise_without_kma_env(self, monkeypatch):
        """No KMA_API_KEY handling remains -- poll() must not depend on it."""
        monkeypatch.delenv("KMA_API_KEY", raising=False)
        db = _db()
        collector = AmosCollector(db)
        # Must not raise.
        collector.poll()

    def test_run_loop_logs_retirement_and_returns(self, caplog):
        """run_loop() must log once and return immediately (no infinite loop,
        no HTTP calls) so the collector thread exits cleanly."""
        db = _db()
        collector = AmosCollector(db)

        with caplog.at_level(logging.INFO, logger="src.data.collectors.amos"):
            collector.run_loop()  # must return, not block

        assert any("retired" in r.message for r in caplog.records)

    def test_collector_accepts_any_db_including_mock(self):
        """Constructor must not touch db beyond storing it (no-op body)."""
        mock_db = MagicMock()
        collector = AmosCollector(mock_db)
        results = collector.poll()
        assert results == {"Seoul": False, "Busan": False}
        mock_db.insert_observation.assert_not_called()


class TestDeadKmaCodeRemoved:
    """Issue #740: the KMA ASOS path, KMA_API_KEY handling, and the
    Open-Meteo-fallback-writes-observations path must be gone, not just
    unreachable."""

    def test_no_fetch_kma_method(self):
        assert not hasattr(AmosCollector, "_fetch_kma")

    def test_no_fetch_open_meteo_method(self):
        assert not hasattr(AmosCollector, "_fetch_open_meteo")

    def test_no_check_staleness_method(self):
        assert not hasattr(AmosCollector, "_check_staleness")

    def test_no_kma_endpoint_constant(self):
        assert not hasattr(amos_module, "_KMA_ENDPOINT")
        assert not hasattr(amos_module, "_KMA_STATIONS")

    def test_no_open_meteo_coords_constant(self):
        assert not hasattr(amos_module, "_OPEN_METEO_COORDS")

    def test_no_fetch_import(self):
        """The module must not import fetch() at all -- it makes no HTTP calls."""
        assert not hasattr(amos_module, "fetch")

    def test_constructor_reads_no_kma_api_key_env(self, monkeypatch):
        """The constructor must not read KMA_API_KEY -- confirm no attribute
        is derived from it."""
        monkeypatch.setenv("KMA_API_KEY", "should-be-ignored")
        db = _db()
        collector = AmosCollector(db)
        assert not hasattr(collector, "_api_key")

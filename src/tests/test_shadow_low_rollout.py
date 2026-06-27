"""Tests for Epic-C shadow-low rollout (issue #457).

Covers:
- station_overrides.low_no_enabled column (DB layer)
- seed_station_overrides seeds Epic-C cities with low_no_enabled=0
- shadow_low_report script produces correct log entries
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# DB layer: low_no_enabled column
# ---------------------------------------------------------------------------

class TestLowNoEnabledColumn:
    def _make_db(self):
        from src.data.db import Database
        tmp = tempfile.mktemp(suffix=".db")
        return Database(path=tmp), tmp

    def test_get_station_override_includes_low_no_enabled(self):
        db, tmp = self._make_db()
        try:
            db.set_station_override("EGLC", yes_enabled=False, no_enabled=False, low_no_enabled=False)
            row = db.get_station_override("EGLC")
            assert row is not None
            assert "low_no_enabled" in row
            assert row["low_no_enabled"] is False
        finally:
            os.unlink(tmp)

    def test_default_low_no_enabled_is_false(self):
        db, tmp = self._make_db()
        try:
            # set_station_override without low_no_enabled → default False
            db.set_station_override("LFPB", yes_enabled=True, no_enabled=True)
            row = db.get_station_override("LFPB")
            assert row["low_no_enabled"] is False
        finally:
            os.unlink(tmp)

    def test_set_low_no_enabled_true(self):
        db, tmp = self._make_db()
        try:
            db.set_station_override("RJTT", yes_enabled=False, no_enabled=False, low_no_enabled=True)
            row = db.get_station_override("RJTT")
            assert row["low_no_enabled"] is True
        finally:
            os.unlink(tmp)

    def test_get_all_station_overrides_includes_low_no_enabled(self):
        db, tmp = self._make_db()
        try:
            db.set_station_override("KMIA", yes_enabled=True, no_enabled=True, low_no_enabled=False)
            all_rows = db.get_all_station_overrides()
            assert "KMIA" in all_rows
            assert "low_no_enabled" in all_rows["KMIA"]
        finally:
            os.unlink(tmp)

    def test_upsert_updates_low_no_enabled(self):
        db, tmp = self._make_db()
        try:
            db.set_station_override("ZSPD", yes_enabled=False, no_enabled=False, low_no_enabled=False)
            db.set_station_override("ZSPD", yes_enabled=False, no_enabled=False, low_no_enabled=True)
            row = db.get_station_override("ZSPD")
            assert row["low_no_enabled"] is True
        finally:
            os.unlink(tmp)

    def test_migration_adds_low_no_enabled_to_old_db(self):
        """DB without low_no_enabled column is migrated on open."""
        import sqlite3
        tmp = tempfile.mktemp(suffix=".db")
        try:
            conn = sqlite3.connect(tmp)
            conn.execute("""
                CREATE TABLE station_overrides (
                    station TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    yes_enabled INTEGER NOT NULL DEFAULT 1,
                    no_enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute(
                "INSERT INTO station_overrides VALUES (?, ?, ?, ?, ?)",
                ("RKSI", 1, 0, 0, "2025-01-01T00:00:00Z"),
            )
            conn.commit()
            conn.close()

            from src.data.db import Database
            db = Database(path=tmp)
            row = db.get_station_override("RKSI")
            assert row is not None
            assert "low_no_enabled" in row
            assert row["low_no_enabled"] is False
        finally:
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# seed_station_overrides — Epic-C cities
# ---------------------------------------------------------------------------

class TestSeedStationOverridesEpicC:
    def _make_db(self):
        from src.data.db import Database
        tmp = tempfile.mktemp(suffix=".db")
        return Database(path=tmp), tmp

    def test_epic_c_cities_seeded_on_first_run(self):
        db, tmp = self._make_db()
        try:
            from src.config import seed_station_overrides
            seed_station_overrides(db)
            for station in ("EGLC", "LFPB", "RJTT", "ZSPD", "KMIA"):
                row = db.get_station_override(station)
                assert row is not None, f"{station} not seeded"
                assert row["low_no_enabled"] is False, f"{station} low_no_enabled should be 0"
        finally:
            os.unlink(tmp)

    def test_seed_does_not_overwrite_existing_row(self):
        db, tmp = self._make_db()
        try:
            # Pre-set EGLC with yes_enabled=True (simulates existing high-side live config)
            db.set_station_override("EGLC", yes_enabled=True, no_enabled=True, low_no_enabled=False)

            from src.config import seed_station_overrides
            seed_station_overrides(db)

            row = db.get_station_override("EGLC")
            # Seed should not overwrite existing row — yes_enabled stays True
            assert row["yes_enabled"] is True
        finally:
            os.unlink(tmp)

    def test_rksi_seeded_with_both_sides_shadow(self):
        db, tmp = self._make_db()
        try:
            from src.config import seed_station_overrides
            seed_station_overrides(db)
            row = db.get_station_override("RKSI")
            assert row is not None
            assert row["yes_enabled"] is False
            assert row["no_enabled"] is False
        finally:
            os.unlink(tmp)

    def test_seed_idempotent(self):
        """Multiple calls to seed_station_overrides produce the same result."""
        db, tmp = self._make_db()
        try:
            from src.config import seed_station_overrides
            seed_station_overrides(db)
            seed_station_overrides(db)
            for station in ("EGLC", "LFPB", "RJTT", "ZSPD", "KMIA", "RKSI"):
                row = db.get_station_override(station)
                assert row is not None
        finally:
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# shadow_low_report — report generation
# ---------------------------------------------------------------------------

class TestShadowLowReport:
    def _make_mock_db(self, rows: list[dict]):
        db = MagicMock()
        # Build a fake cursor
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE candidates ("
            "station TEXT, ticker TEXT, side TEXT, predicted_price INT, "
            "predicted_edge REAL, confidence REAL, ts TEXT, direction TEXT)"
        )
        for r in rows:
            conn.execute(
                "INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?)",
                (r["station"], r["ticker"], r["side"],
                 r["predicted_price"], r["predicted_edge"],
                 r["confidence"], r["ts"], r.get("direction", "low")),
            )
        conn.commit()
        db._conn = conn
        return db

    def test_report_creates_log_file(self, tmp_path):
        from scripts.shadow_low_report import _run
        db = self._make_mock_db([
            {"station": "EGLC", "ticker": "T1", "side": "NO",
             "predicted_price": 55, "predicted_edge": 8.0, "confidence": 0.7,
             "ts": "2026-07-05T10:00:00Z"},
        ])
        _run(db, "2026-07-05", tmp_path)
        log_file = tmp_path / "shadow_low_EGLC.log"
        assert log_file.exists()

    def test_report_counts_no_and_yes(self, tmp_path):
        from scripts.shadow_low_report import _run
        db = self._make_mock_db([
            {"station": "LFPB", "ticker": "T1", "side": "NO",
             "predicted_price": 50, "predicted_edge": 7.0, "confidence": 0.65,
             "ts": "2026-07-05T11:00:00Z"},
            {"station": "LFPB", "ticker": "T2", "side": "NO",
             "predicted_price": 48, "predicted_edge": 9.0, "confidence": 0.72,
             "ts": "2026-07-05T12:00:00Z"},
        ])
        _run(db, "2026-07-05", tmp_path)
        log_file = tmp_path / "shadow_low_LFPB.log"
        content = log_file.read_text()
        assert "total=2" in content
        assert "no=2" in content
        assert "yes=0" in content

    def test_report_empty_station_still_logged(self, tmp_path):
        """Stations with no candidates still get a log entry (zeros)."""
        from scripts.shadow_low_report import _run
        db = self._make_mock_db([])  # no candidates
        _run(db, "2026-07-06", tmp_path)
        for station in ("EGLC", "LFPB", "RJTT", "RKSI", "ZSPD", "KMIA"):
            log_file = tmp_path / f"shadow_low_{station}.log"
            assert log_file.exists(), f"Missing log for {station}"
            content = log_file.read_text()
            assert "total=0" in content

    def test_report_appends_on_multiple_days(self, tmp_path):
        from scripts.shadow_low_report import _run
        db = self._make_mock_db([
            {"station": "RJTT", "ticker": "T1", "side": "NO",
             "predicted_price": 52, "predicted_edge": 6.0, "confidence": 0.6,
             "ts": "2026-07-05T09:00:00Z"},
        ])
        _run(db, "2026-07-05", tmp_path)
        _run(db, "2026-07-06", tmp_path)
        log_file = tmp_path / "shadow_low_RJTT.log"
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) == 2
        assert "2026-07-05" in lines[0]
        assert "2026-07-06" in lines[1]

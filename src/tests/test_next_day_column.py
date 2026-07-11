"""Tests for the is_next_day column on candidates/snapshot_archive (issue #687).

Covers:
- Fresh DB/analytics.db has is_next_day, defaulting to 0
- Migration adds the column idempotently to a pre-existing DB missing it
- insert_candidate() accepts and persists is_next_day, defaulting to 0
- ArchiveDatabase.insert_snapshots() carries is_next_day through, defaulting
  to 0 for legacy rows written before the column existed (no key in the dict)
"""
import tempfile
from pathlib import Path

from src.data.db import Database
from src.data.archive_db import ArchiveDatabase


def _fresh_db() -> Database:
    return Database(":memory:")


def _col_names(db: Database, table: str) -> set[str]:
    rows = db._conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


class TestIsNextDayFreshInstall:
    def test_candidates_has_is_next_day(self):
        db = _fresh_db()
        assert "is_next_day" in _col_names(db, "candidates")


class TestIsNextDayMigration:
    def _legacy_db(self) -> Database:
        """Simulate a pre-is_next_day DB by dropping the column after init."""
        db = _fresh_db()
        db._conn.execute("PRAGMA foreign_keys=OFF")
        info = db._conn.execute("PRAGMA table_info(candidates)").fetchall()
        cols_without = [r[1] for r in info if r[1] != "is_next_day"]
        cols_def = ", ".join(cols_without)
        db._conn.executescript(f"""
            CREATE TABLE candidates_bak AS SELECT {cols_def} FROM candidates;
            DROP TABLE candidates;
            ALTER TABLE candidates_bak RENAME TO candidates;
        """)
        db._conn.execute("PRAGMA foreign_keys=ON")
        db._conn.commit()
        return db

    def test_migration_adds_is_next_day(self):
        db = self._legacy_db()
        assert "is_next_day" not in _col_names(db, "candidates")
        db._migrate()
        assert "is_next_day" in _col_names(db, "candidates")

    def test_migration_is_idempotent(self):
        db = _fresh_db()
        db._migrate()
        db._migrate()
        assert "is_next_day" in _col_names(db, "candidates")


class TestIsNextDayReadWrite:
    def test_insert_candidate_defaults_to_zero(self):
        db = _fresh_db()
        db.insert_candidate(
            ts="2026-07-11T10:00:00Z", station="KORD",
            ticker="TEST-1", bracket_low=81.0, bracket_high=83.0,
            side="NO", predicted_price=5, predicted_edge=15.0,
            market_price=79, confidence=0.95, minutes_to_settlement=120.0,
        )
        row = db._conn.execute(
            "SELECT is_next_day FROM candidates WHERE ticker='TEST-1'"
        ).fetchone()
        assert row[0] == 0

    def test_insert_candidate_with_is_next_day_true(self):
        db = _fresh_db()
        db.insert_candidate(
            ts="2026-07-11T10:00:00Z", station="KORD",
            ticker="TEST-2", bracket_low=81.0, bracket_high=83.0,
            side="YES", predicted_price=70, predicted_edge=15.0,
            market_price=60, confidence=0.60, minutes_to_settlement=1200.0,
            is_next_day=1,
        )
        row = db._conn.execute(
            "SELECT is_next_day FROM candidates WHERE ticker='TEST-2'"
        ).fetchone()
        assert row[0] == 1


class TestArchiveSnapshotIsNextDay:
    def _fresh_archive(self, tmp_path) -> ArchiveDatabase:
        return ArchiveDatabase(path=str(tmp_path / "analytics.db"))

    def test_fresh_install_has_column(self, tmp_path):
        with self._fresh_archive(tmp_path) as db:
            rows = db._conn.execute("PRAGMA table_info(snapshot_archive)").fetchall()
            assert "is_next_day" in {r[1] for r in rows}

    def test_migration_adds_column_to_legacy_db(self, tmp_path):
        db_path = tmp_path / "legacy_analytics.db"
        with ArchiveDatabase(path=str(db_path)) as db:
            info = db._conn.execute("PRAGMA table_info(snapshot_archive)").fetchall()
            cols_without = [r[1] for r in info if r[1] != "is_next_day"]
            cols_def = ", ".join(cols_without)
            db._conn.executescript(f"""
                CREATE TABLE snapshot_archive_bak AS SELECT {cols_def} FROM snapshot_archive;
                DROP TABLE snapshot_archive;
                ALTER TABLE snapshot_archive_bak RENAME TO snapshot_archive;
            """)
            db._conn.commit()
            rows = db._conn.execute("PRAGMA table_info(snapshot_archive)").fetchall()
            assert "is_next_day" not in {r[1] for r in rows}

        # Re-opening runs _migrate() again -- column should be added idempotently.
        with ArchiveDatabase(path=str(db_path)) as db:
            rows = db._conn.execute("PRAGMA table_info(snapshot_archive)").fetchall()
            assert "is_next_day" in {r[1] for r in rows}

    def test_insert_snapshots_carries_is_next_day_through(self, tmp_path):
        with self._fresh_archive(tmp_path) as db:
            db.insert_snapshots([{
                "ts": "2026-07-11T12:00:00Z", "station": "KMIA", "ticker": "T1",
                "is_next_day": 1,
            }])
            row = db._conn.execute(
                "SELECT is_next_day FROM snapshot_archive WHERE ticker='T1'"
            ).fetchone()
            assert row[0] == 1

    def test_insert_snapshots_defaults_missing_key_to_zero(self, tmp_path):
        """A legacy JSONL line written before is_next_day existed has no such
        key -- must default to 0, not fail the NOT NULL constraint."""
        with self._fresh_archive(tmp_path) as db:
            db.insert_snapshots([{
                "ts": "2026-07-11T12:00:00Z", "station": "KMIA", "ticker": "T2",
            }])
            row = db._conn.execute(
                "SELECT is_next_day FROM snapshot_archive WHERE ticker='T2'"
            ).fetchone()
            assert row[0] == 0

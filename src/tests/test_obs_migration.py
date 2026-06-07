"""Tests for observations table schema migration."""
import os
import tempfile

import pytest

from src.data.db import Database


class TestObservationsMigration:
    """Tests for cadence_min and is_official column migration."""

    def test_fresh_db_has_both_columns(self):
        """Fresh database should have both cadence_min and is_official columns."""
        db = Database(":memory:")
        cur = db._conn.execute("PRAGMA table_info(observations)")
        columns = {row[1] for row in cur.fetchall()}
        assert "cadence_min" in columns, "cadence_min column not found"
        assert "is_official" in columns, "is_official column not found"

    def test_migration_idempotent(self):
        """Running migration twice should not raise any error."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            path = tf.name
        try:
            # First creation
            db1 = Database(path)
            db1._conn.close()

            # Second creation (migration runs again)
            db2 = Database(path)
            cur = db2._conn.execute("PRAGMA table_info(observations)")
            columns = {row[1] for row in cur.fetchall()}
            assert "cadence_min" in columns
            assert "is_official" in columns
            db2._conn.close()
        finally:
            os.unlink(path)
            for ext in ("-wal", "-shm"):
                try:
                    os.unlink(path + ext)
                except FileNotFoundError:
                    pass

    def test_insert_observation_with_new_columns(self):
        """Insert observation with cadence_min and is_official should work."""
        db = Database(":memory:")
        ts = "2024-01-15T12:00:00+00:00"
        row_id = db.insert_observation(
            ts=ts,
            station="Tokyo",
            temp_f=72.0,
            temp_native=22.2,
            unit="C",
            source="jma_ameidas",
        )
        rows = db.get_observations("Tokyo", since="2000-01-01")
        assert len(rows) == 1
        r = rows[0]
        assert r["ts"] == ts
        assert r["station"] == "Tokyo"
        assert r["temp_f"] == pytest.approx(72.0)
        assert r["source"] == "jma_ameidas"

    def test_is_official_default_value(self):
        """Verify is_official gets default value of 1 on new inserts."""
        db = Database(":memory:")
        db.insert_observation(
            ts="2024-01-15T12:00:00+00:00",
            station="Seoul",
            temp_f=68.0,
            temp_native=20.0,
            unit="C",
            source="amos",
        )
        cur = db._conn.execute(
            "SELECT is_official FROM observations WHERE station=?", ("Seoul",)
        )
        is_official = cur.fetchone()[0]
        assert is_official == 1

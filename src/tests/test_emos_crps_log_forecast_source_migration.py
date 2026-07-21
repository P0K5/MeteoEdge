"""Tests for the forecast_source column on emos_crps_log (issue #759).

Covers:
- Fresh DB has forecast_source on emos_crps_log, default 'baseline'
- Migration adds the column to a pre-existing DB missing it, and backfills
  every existing row to 'baseline'
- The migration is idempotent (safe to run twice / against an already
  up-to-date DB)
"""
from src.data.db import Database


def _fresh_db() -> Database:
    return Database(":memory:")


def _col_names(db: Database, table: str) -> set[str]:
    rows = db._conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


class TestForecastSourceFreshInstall:
    def test_emos_crps_log_has_forecast_source(self):
        db = _fresh_db()
        assert "forecast_source" in _col_names(db, "emos_crps_log")

    def test_new_row_defaults_to_baseline(self):
        db = _fresh_db()
        db.log_crps("Chicago", "2026-06-01", 1.5)
        row = db._conn.execute(
            "SELECT forecast_source FROM emos_crps_log WHERE city='Chicago'"
        ).fetchone()
        assert row[0] == "baseline"


class TestForecastSourceMigration:
    def _legacy_db(self) -> Database:
        """Simulate a pre-#759 DB by dropping forecast_source after seeding rows."""
        db = _fresh_db()
        # Seed rows through the normal (post-migration) API first, so the
        # rebuild below has data to carry across the "legacy schema" cut.
        db.log_crps("Chicago", "2026-06-01", 1.1, forecast_source="baseline")
        db.log_crps("Chicago", "2026-06-02", 1.2, forecast_source="baseline")
        db.log_crps("Miami", "2026-06-01", 2.0, forecast_source="baseline")

        db._conn.execute("PRAGMA foreign_keys=OFF")
        info = db._conn.execute("PRAGMA table_info(emos_crps_log)").fetchall()
        cols_without = [r[1] for r in info if r[1] != "forecast_source"]
        cols_def = ", ".join(cols_without)
        db._conn.executescript(f"""
            CREATE TABLE emos_crps_log_bak AS SELECT {cols_def} FROM emos_crps_log;
            DROP TABLE emos_crps_log;
            ALTER TABLE emos_crps_log_bak RENAME TO emos_crps_log;
        """)
        db._conn.execute("PRAGMA foreign_keys=ON")
        db._conn.commit()
        return db

    def test_migration_adds_forecast_source_column(self):
        db = self._legacy_db()
        assert "forecast_source" not in _col_names(db, "emos_crps_log")
        db._migrate()
        assert "forecast_source" in _col_names(db, "emos_crps_log")

    def test_migration_backfills_legacy_rows_to_baseline(self):
        db = self._legacy_db()
        db._migrate()
        rows = db._conn.execute(
            "SELECT city, forecast_source FROM emos_crps_log ORDER BY city"
        ).fetchall()
        assert len(rows) == 3
        assert all(r[1] == "baseline" for r in rows)

    def test_migration_preserves_baseline_promotion_count(self):
        """The already-accumulated baseline evidence keeps counting for the
        baseline stack after migration -- no promotion samples are lost."""
        db = self._legacy_db()
        db._migrate()
        assert db.get_emos_crps_count("Chicago", forecast_source="baseline") == 2

    def test_migration_is_idempotent(self):
        db = _fresh_db()
        db._migrate()
        db._migrate()
        assert "forecast_source" in _col_names(db, "emos_crps_log")

    def test_migration_idempotent_on_legacy_db_run_twice(self):
        db = self._legacy_db()
        db._migrate()
        db._migrate()
        rows = db._conn.execute(
            "SELECT city, forecast_source FROM emos_crps_log ORDER BY city"
        ).fetchall()
        assert len(rows) == 3
        assert all(r[1] == "baseline" for r in rows)

"""Tests for the sigma_source column on emos_crps_log (issue #851).

Mirrors test_emos_crps_log_forecast_source_migration.py's coverage of the
analogous forecast_source column (issue #759), for the sigma_source axis:

- Fresh DB has sigma_source on emos_crps_log, default 'fixed' at the schema
  level (matching every pre-#851/#799 row's actual lineage).
- log_crps()/get_emos_crps_count()/emos_crps_logged_for_date() all resolve an
  unset sigma_source to the active USE_ENSEMBLE_SIGMA-derived track (see
  Database._active_sigma_source), so CRPS evidence from a different
  sigma_source lineage never pools into the promotion count -- the exact
  #658-style train/serve-evidence skew #851 exists to close.
- Migration adds the column to a pre-existing DB missing it, and backfills
  every existing row to 'fixed'.
- The migration is idempotent.
"""
from src.data.db import Database


def _fresh_db() -> Database:
    return Database(":memory:")


def _col_names(db: Database, table: str) -> set[str]:
    rows = db._conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


class TestSigmaSourceFreshInstall:
    def test_emos_crps_log_has_sigma_source(self):
        db = _fresh_db()
        assert "sigma_source" in _col_names(db, "emos_crps_log")

    def test_new_row_defaults_to_active_sigma_source(self):
        db = _fresh_db()
        db.set_config("USE_ENSEMBLE_SIGMA", "false")
        db.log_crps("Chicago", "2026-06-01", 1.5)
        row = db._conn.execute(
            "SELECT sigma_source FROM emos_crps_log WHERE city='Chicago'"
        ).fetchone()
        assert row[0] == "fixed"

        db2 = _fresh_db()
        db2.set_config("USE_ENSEMBLE_SIGMA", "true")
        db2.log_crps("Chicago", "2026-06-01", 1.5)
        row2 = db2._conn.execute(
            "SELECT sigma_source FROM emos_crps_log WHERE city='Chicago'"
        ).fetchone()
        assert row2[0] == "ensemble"


class TestCrpsScopedBySigmaSource:
    def test_counts_advance_independently_per_sigma_source(self):
        """Two sigma tracks' CRPS evidence for the same city must not pool
        into one count -- otherwise the promotion guard can't evaluate a
        newly-retrained sigma lineage independently of the previous
        lineage's already-accumulated evidence (issue #851).
        """
        db = _fresh_db()
        db.log_crps("Chicago", "2026-06-01", 1.1, sigma_source="fixed")
        db.log_crps("Chicago", "2026-06-02", 1.2, sigma_source="fixed")
        db.log_crps("Chicago", "2026-06-01", 2.1, sigma_source="ensemble")

        assert db.get_emos_crps_count("Chicago", sigma_source="fixed") == 2
        assert db.get_emos_crps_count("Chicago", sigma_source="ensemble") == 1

    def test_default_sigma_source_resolves_to_active_track(self):
        """log_crps/get_emos_crps_count with sigma_source unset both resolve
        to the active USE_ENSEMBLE_SIGMA-derived track (same resolver as the
        emos_calibration reads/writes, issue #799) -- so the promotion guard
        call site (db.get_emos_crps_count(city), no sigma_source passed)
        automatically tracks whichever sigma lineage is currently active.
        """
        db = _fresh_db()
        db.set_config("USE_ENSEMBLE_SIGMA", "true")
        db.log_crps("Chicago", "2026-06-01", 1.1)  # sigma_source unset

        assert db.get_emos_crps_count("Chicago", sigma_source="ensemble") == 1
        assert db.get_emos_crps_count("Chicago", sigma_source="fixed") == 0
        assert db.get_emos_crps_count("Chicago") == 1  # default also resolves to 'ensemble'

    def test_dedup_guard_scoped_per_sigma_source(self):
        """emos_crps_logged_for_date's per-day dedup guard is keyed on
        sigma_source too -- a sigma_source switch on the same calendar day
        must NOT be treated as already logged just because the previous
        track claimed that (city, date, model_mode, forecast_source) slot.
        """
        db = _fresh_db()
        assert db.emos_crps_logged_for_date(
            "Chicago", "2026-06-01", sigma_source="fixed"
        ) is False

        db.log_crps("Chicago", "2026-06-01", 1.1, sigma_source="fixed")

        assert db.emos_crps_logged_for_date(
            "Chicago", "2026-06-01", sigma_source="fixed"
        ) is True
        assert db.emos_crps_logged_for_date(
            "Chicago", "2026-06-01", sigma_source="ensemble"
        ) is False

    def test_shadow_city_status_averages_scoped_by_active_sigma_source(self):
        """get_emos_shadow_city_status's mean_crps/legacy_mean_crps must not
        blend CRPS scored under a different sigma_source into the average
        for the currently active lineage (issue #851)."""
        db = _fresh_db()
        db.set_config("USE_ENSEMBLE_SIGMA", "false")
        db.log_crps("Chicago", "2026-06-01", 1.0, model_mode="emos_shadow", sigma_source="fixed")
        db.log_crps("Chicago", "2026-06-02", 3.0, model_mode="emos_shadow", sigma_source="ensemble")

        status = db.get_emos_shadow_city_status("Chicago")
        assert status["mean_crps"] == 1.0  # only the active 'fixed' row counts

        db.set_config("USE_ENSEMBLE_SIGMA", "true")
        status2 = db.get_emos_shadow_city_status("Chicago")
        assert status2["mean_crps"] == 3.0  # only the active 'ensemble' row counts


class TestSigmaSourceMigration:
    def _legacy_db(self) -> Database:
        """Simulate a pre-#851 DB by dropping sigma_source after seeding rows."""
        db = _fresh_db()
        # Seed rows through the normal (post-migration) API first, so the
        # rebuild below has data to carry across the "legacy schema" cut.
        db.log_crps("Chicago", "2026-06-01", 1.1, sigma_source="fixed")
        db.log_crps("Chicago", "2026-06-02", 1.2, sigma_source="fixed")
        db.log_crps("Miami", "2026-06-01", 2.0, sigma_source="fixed")

        db._conn.execute("PRAGMA foreign_keys=OFF")
        info = db._conn.execute("PRAGMA table_info(emos_crps_log)").fetchall()
        cols_without = [r[1] for r in info if r[1] != "sigma_source"]
        cols_def = ", ".join(cols_without)
        db._conn.executescript(f"""
            CREATE TABLE emos_crps_log_bak AS SELECT {cols_def} FROM emos_crps_log;
            DROP TABLE emos_crps_log;
            ALTER TABLE emos_crps_log_bak RENAME TO emos_crps_log;
        """)
        db._conn.execute("PRAGMA foreign_keys=ON")
        db._conn.commit()
        return db

    def test_migration_adds_sigma_source_column(self):
        db = self._legacy_db()
        assert "sigma_source" not in _col_names(db, "emos_crps_log")
        db._migrate()
        assert "sigma_source" in _col_names(db, "emos_crps_log")

    def test_migration_backfills_legacy_rows_to_fixed(self):
        db = self._legacy_db()
        db._migrate()
        rows = db._conn.execute(
            "SELECT city, sigma_source FROM emos_crps_log ORDER BY city"
        ).fetchall()
        assert len(rows) == 3
        assert all(r[1] == "fixed" for r in rows)

    def test_migration_preserves_fixed_promotion_count(self):
        """The already-accumulated fixed-sigma evidence keeps counting for
        the fixed track after migration -- no promotion samples are lost."""
        db = self._legacy_db()
        db._migrate()
        assert db.get_emos_crps_count("Chicago", sigma_source="fixed") == 2

    def test_migration_is_idempotent(self):
        db = _fresh_db()
        db._migrate()
        db._migrate()
        assert "sigma_source" in _col_names(db, "emos_crps_log")

    def test_migration_idempotent_on_legacy_db_run_twice(self):
        db = self._legacy_db()
        db._migrate()
        db._migrate()
        rows = db._conn.execute(
            "SELECT city, sigma_source FROM emos_crps_log ORDER BY city"
        ).fetchall()
        assert len(rows) == 3
        assert all(r[1] == "fixed" for r in rows)

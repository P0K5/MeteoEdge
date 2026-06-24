"""Tests for model_forecast_log schema migration (#422 / #423).

Coverage:
1. Schema migration idempotency on fresh in-memory DB.
2. Migration on a DB with legacy rows: renames table, preserves rows.
3. EMOS reader (fetch_training_data) filters by lead_hours correctly.
4. EMOS fit on synthetic data with variable sigma produces (c,d) ≠ (FORECAST_STDDEV_F, 1).
"""
from __future__ import annotations

import math
import os
import random
import sqlite3
import tempfile

import pytest

from src.data.db import Database
from src.model.emos_calibration import (
    InsufficientDataError,
    fetch_training_data,
    fit_emos,
)
from src.config import FORECAST_STDDEV_F


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_db() -> Database:
    return Database(":memory:")


def _insert_obs(db: Database, station: str, date_str: str, temp_f: float) -> None:
    db.insert_observation(
        ts=f"{date_str}T18:00:00+00:00",
        station=station,
        temp_f=temp_f,
        temp_native=temp_f,
        unit="F",
        source="metar",
    )


# ---------------------------------------------------------------------------
# 1. Schema migration idempotency on fresh DB
# ---------------------------------------------------------------------------

class TestSchemaMigrationIdempotency:
    """model_forecast_log new columns exist on fresh DB and survive re-open."""

    def test_fresh_db_has_new_columns(self):
        db = _fresh_db()
        cur = db._conn.execute("PRAGMA table_info(model_forecast_log)")
        cols = {row[1] for row in cur.fetchall()}
        assert "lead_hours" in cols, "lead_hours column missing from fresh DB"
        assert "issued_at" in cols, "issued_at column missing from fresh DB"
        assert "sigma_f" in cols, "sigma_f column missing from fresh DB"

    def test_unique_index_includes_lead_hours(self):
        db = _fresh_db()
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='model_forecast_log'"
        )
        index_names = {row[0] for row in cur.fetchall()}
        assert "idx_mfl_station_model_date_lead" in index_names, (
            "New unique index idx_mfl_station_model_date_lead not found"
        )

    def test_reopening_db_is_idempotent(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            path = tf.name
        try:
            db1 = Database(path)
            db1._conn.close()
            # Second open runs _migrate() again — must not raise
            db2 = Database(path)
            cur = db2._conn.execute("PRAGMA table_info(model_forecast_log)")
            cols = {row[1] for row in cur.fetchall()}
            assert "lead_hours" in cols
            assert "sigma_f" in cols
            db2._conn.close()
        finally:
            os.unlink(path)
            for ext in ("-wal", "-shm"):
                try:
                    os.unlink(path + ext)
                except FileNotFoundError:
                    pass

    def test_upsert_v2_and_read_back(self):
        db = _fresh_db()
        db.upsert_forecast_log_v2(
            station="KORD",
            model="nws",
            date="2026-01-01",
            forecast_high_f=45.0,
            lead_hours=24,
            issued_at="2026-01-01T12:00:00+00:00",
            sigma_f=3.0,
        )
        rows = db.get_forecast_log_by_lead("KORD", "2026-01-01", lead_hours=24)
        assert len(rows) == 1
        r = rows[0]
        assert r["station"] == "KORD"
        assert r["model"] == "nws"
        assert r["lead_hours"] == 24
        assert math.isclose(r["forecast_high_f"], 45.0)
        assert math.isclose(r["sigma_f"], 3.0)

    def test_different_lead_hours_stored_separately(self):
        """Two captures for same station/model/date at different lead times are separate rows."""
        db = _fresh_db()
        db.upsert_forecast_log_v2(
            station="KORD", model="nws", date="2026-01-01",
            forecast_high_f=45.0, lead_hours=24,
        )
        db.upsert_forecast_log_v2(
            station="KORD", model="nws", date="2026-01-01",
            forecast_high_f=47.0, lead_hours=12,
        )
        rows_24 = db.get_forecast_log_by_lead("KORD", "2026-01-01", 24)
        rows_12 = db.get_forecast_log_by_lead("KORD", "2026-01-01", 12)
        assert len(rows_24) == 1 and math.isclose(rows_24[0]["forecast_high_f"], 45.0)
        assert len(rows_12) == 1 and math.isclose(rows_12[0]["forecast_high_f"], 47.0)


# ---------------------------------------------------------------------------
# 2. Migration on DB with legacy rows (simulate pre-#422 state)
# ---------------------------------------------------------------------------

class TestMigrationWithLegacyRows:
    """Migration renames old table to legacy_v1 and preserves its rows."""

    def _make_pre_migration_db(self, path: str, n_rows: int = 670) -> None:
        """Create a SQLite file that looks like a pre-#422 database."""
        conn = sqlite3.connect(path)
        # Create old-schema table WITHOUT lead_hours/issued_at/sigma_f
        conn.execute(
            """
            CREATE TABLE model_forecast_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                station         TEXT NOT NULL,
                model           TEXT NOT NULL,
                date            TEXT NOT NULL,
                forecast_high_f REAL NOT NULL,
                logged_at       TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX idx_mfl_station_model_date "
            "ON model_forecast_log(station, model, date)"
        )
        # Insert n_rows legacy rows
        for i in range(n_rows):
            date_str = f"2025-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}"
            conn.execute(
                "INSERT INTO model_forecast_log(station,model,date,forecast_high_f,logged_at) "
                "VALUES(?,?,?,?,?)",
                ("KORD", "nws", f"2025-01-{i + 1:04d}", 40.0 + (i % 10), "2025-01-01T23:55:00"),
            )
        conn.commit()
        conn.close()

    def test_migration_creates_legacy_table(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            path = tf.name
        try:
            self._make_pre_migration_db(path, n_rows=10)
            # Opening Database triggers _migrate() → _migrate_forecast_log()
            db = Database(path)
            # Legacy table must exist
            legacy = db._conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='model_forecast_log_legacy_v1'"
            ).fetchone()
            assert legacy is not None, "model_forecast_log_legacy_v1 not created"
            db._conn.close()
        finally:
            os.unlink(path)
            for ext in ("-wal", "-shm"):
                try:
                    os.unlink(path + ext)
                except FileNotFoundError:
                    pass

    def test_migration_preserves_legacy_row_count(self):
        """Legacy table retains all 670 original rows after migration."""
        n_rows = 670
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            path = tf.name
        try:
            self._make_pre_migration_db(path, n_rows=n_rows)
            db = Database(path)
            count = db._conn.execute(
                "SELECT COUNT(*) FROM model_forecast_log_legacy_v1"
            ).fetchone()[0]
            assert count == n_rows, (
                f"Expected {n_rows} legacy rows, got {count}"
            )
            db._conn.close()
        finally:
            os.unlink(path)
            for ext in ("-wal", "-shm"):
                try:
                    os.unlink(path + ext)
                except FileNotFoundError:
                    pass

    def test_migration_new_table_starts_empty(self):
        """After migration, model_forecast_log (new) is empty — no rows migrated forward."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            path = tf.name
        try:
            self._make_pre_migration_db(path, n_rows=5)
            db = Database(path)
            count = db._conn.execute(
                "SELECT COUNT(*) FROM model_forecast_log"
            ).fetchone()[0]
            assert count == 0, (
                f"New model_forecast_log should be empty after migration, got {count} rows"
            )
            db._conn.close()
        finally:
            os.unlink(path)
            for ext in ("-wal", "-shm"):
                try:
                    os.unlink(path + ext)
                except FileNotFoundError:
                    pass

    def test_migration_idempotent_when_legacy_already_exists(self):
        """Re-opening a fully-migrated DB does not raise or corrupt data."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            path = tf.name
        try:
            self._make_pre_migration_db(path, n_rows=5)
            db1 = Database(path)
            db1._conn.close()
            # Second open — legacy table already exists, must be no-op
            db2 = Database(path)
            cur = db2._conn.execute("PRAGMA table_info(model_forecast_log)")
            cols = {row[1] for row in cur.fetchall()}
            assert "lead_hours" in cols
            assert "sigma_f" in cols
            count = db2._conn.execute(
                "SELECT COUNT(*) FROM model_forecast_log_legacy_v1"
            ).fetchone()[0]
            assert count == 5
            db2._conn.close()
        finally:
            os.unlink(path)
            for ext in ("-wal", "-shm"):
                try:
                    os.unlink(path + ext)
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------------------
# 3. EMOS reader filters by lead_hours correctly
# ---------------------------------------------------------------------------

class TestEMOSReaderLeadHoursFilter:
    """fetch_training_data returns triples only for the requested lead_hours bin."""

    def _populate_db(self, db: Database, station: str, n: int, lead_hours: int) -> None:
        for i in range(n):
            date_str = f"2025-01-{i + 1:02d}"
            db.upsert_forecast_log_v2(
                station=station,
                model="nws",
                date=date_str,
                forecast_high_f=40.0 + i,
                lead_hours=lead_hours,
                sigma_f=2.0,
            )
            _insert_obs(db, station, date_str, 38.0 + i)

    def test_filters_to_correct_lead(self):
        """Only rows at lead_hours=24 are returned when lead_hours=24 is requested."""
        db = _fresh_db()
        self._populate_db(db, "KORD", n=10, lead_hours=24)
        self._populate_db(db, "KORD", n=5, lead_hours=12)

        result_24 = fetch_training_data("Chicago", db, min_samples=10, lead_hours=24)
        assert len(result_24) == 10

    def test_different_lead_bins_dont_cross_contaminate(self):
        """lead_hours=12 rows are not included when lead_hours=24 is requested."""
        db = _fresh_db()
        # Only add lead=12 rows
        self._populate_db(db, "KORD", n=15, lead_hours=12)

        # Requesting lead=24 should find nothing → InsufficientDataError
        with pytest.raises(InsufficientDataError):
            fetch_training_data("Chicago", db, min_samples=1, lead_hours=24)

    def test_sigma_f_read_from_db_when_present(self):
        """When sigma_f is in the DB, fetch_training_data returns it (not the constant)."""
        db = _fresh_db()
        custom_sigma = 4.5
        for i in range(10):
            date_str = f"2025-01-{i + 1:02d}"
            db.upsert_forecast_log_v2(
                station="KORD",
                model="nws",
                date=date_str,
                forecast_high_f=50.0 + i,
                lead_hours=24,
                sigma_f=custom_sigma,
            )
            _insert_obs(db, "KORD", date_str, 48.0 + i)

        result = fetch_training_data("Chicago", db, min_samples=10, lead_hours=24)
        sigmas = [sigma for _mu, sigma, _y in result]
        assert all(math.isclose(s, custom_sigma) for s in sigmas), (
            f"Expected all sigma={custom_sigma}, got {sigmas}"
        )

    def test_sigma_f_fallback_when_null(self):
        """When sigma_f is NULL in the DB, FORECAST_STDDEV_F is used as fallback."""
        db = _fresh_db()
        for i in range(10):
            date_str = f"2025-01-{i + 1:02d}"
            db.upsert_forecast_log_v2(
                station="KORD",
                model="nws",
                date=date_str,
                forecast_high_f=50.0 + i,
                lead_hours=24,
                sigma_f=None,  # explicitly NULL
            )
            _insert_obs(db, "KORD", date_str, 48.0 + i)

        result = fetch_training_data("Chicago", db, min_samples=10, lead_hours=24)
        sigmas = [sigma for _mu, sigma, _y in result]
        expected = float(FORECAST_STDDEV_F)
        assert all(math.isclose(s, expected) for s in sigmas), (
            f"Expected fallback sigma={expected}, got {sigmas}"
        )


# ---------------------------------------------------------------------------
# 4. EMOS fit on variable-sigma data produces (c,d) ≠ (FORECAST_STDDEV_F, 1)
# ---------------------------------------------------------------------------

class TestEMOSFitWithVariableSigma:
    """EMOS fit on data with varying sigma_raw recovers non-trivial (c, d)."""

    def _generate_variable_sigma_triples(
        self,
        n: int,
        seed: int = 99,
    ) -> list[tuple[float, float, float]]:
        """Generate (mu_raw, sigma_raw, y) with sigma varying per sample.

        sigma_raw ~ U(1.0, 5.0) per sample, so (c, d) cannot trivially be
        (FORECAST_STDDEV_F, 1) — there is genuine heteroscedasticity.
        True params: a=1, b=0.95, c=0.5, d=0.8.
        """
        rng = random.Random(seed)
        triples = []
        for _ in range(n):
            mu_raw = rng.uniform(70.0, 95.0)
            sigma_raw = rng.uniform(1.0, 5.0)
            sigma_true = 0.5 + 0.8 * sigma_raw
            noise = rng.gauss(0.0, sigma_true)
            y = 1.0 + 0.95 * mu_raw + noise
            triples.append((mu_raw, sigma_raw, y))
        return triples

    def test_fit_produces_nontrivial_cd_with_variable_sigma(self):
        """With variable sigma_raw, fitted (c,d) departs from (FORECAST_STDDEV_F, 1)."""
        data = self._generate_variable_sigma_triples(n=200)
        _a, _b, c_fit, d_fit = fit_emos(data)

        constant_sigma = float(FORECAST_STDDEV_F)

        # (c_fit, d_fit) should NOT be (FORECAST_STDDEV_F, 1.0) when sigma varies
        # Tolerance: at least one of c or d must differ by >0.5 from the
        # degenerate (constant-sigma) solution.
        c_differs = abs(c_fit - constant_sigma) > 0.5
        d_differs = abs(d_fit - 1.0) > 0.5

        assert c_differs or d_differs, (
            f"Expected (c,d) to depart from constant-sigma solution "
            f"({constant_sigma}, 1.0) but got c={c_fit:.4f}, d={d_fit:.4f}"
        )

    def test_fit_sigma_positivity_with_variable_sigma(self):
        """c_fit + d_fit * sigma_raw > 0 for all samples even with variable spread."""
        data = self._generate_variable_sigma_triples(n=200)
        _a, _b, c_fit, d_fit = fit_emos(data)

        for mu_raw, sigma_raw, _y in data:
            cal_sigma = c_fit + d_fit * sigma_raw
            assert cal_sigma > 0, (
                f"calibrated sigma <= 0: c={c_fit:.4f}, d={d_fit:.4f}, "
                f"sigma_raw={sigma_raw:.4f} → {cal_sigma:.4f}"
            )

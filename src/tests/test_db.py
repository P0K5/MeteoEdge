"""Unit tests for src/data/db.py.

All tests use an in-memory SQLite database (':memory:') to avoid file I/O,
except test_wal_mode which requires a real file (WAL is a no-op for :memory:).
"""
import os
import tempfile

import pytest

from src.data.db import Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db() -> Database:
    """Return a fresh in-memory Database instance."""
    return Database(":memory:")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestSchemaCreated:
    """All 7 tables must exist after Database() initialisation."""

    def test_schema_created(self):
        db = _db()
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in cur.fetchall()}
        expected = {
            "candidates",
            "observations",
            "open_positions",
            "risk_state",
            "settlements",
            "trades",
        }
        # sqlite_sequence is created implicitly by AUTOINCREMENT — ignore it
        assert expected.issubset(tables), f"Missing tables: {expected - tables}"


# ---------------------------------------------------------------------------
# WAL mode (requires a real file — :memory: always returns 'memory')
# ---------------------------------------------------------------------------

class TestWalMode:
    """PRAGMA journal_mode must return 'wal' when using a file-backed database."""

    def test_wal_mode(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            path = tf.name
        db = None
        try:
            db = Database(path)
            cur = db._conn.execute("PRAGMA journal_mode")
            mode = cur.fetchone()[0]
            assert mode == "wal", f"Expected 'wal', got '{mode}'"
        finally:
            if db is not None:
                db.close()
            os.unlink(path)
            # Remove WAL side-car files if present
            for ext in ("-wal", "-shm"):
                try:
                    os.unlink(path + ext)
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------

class TestInsertObservation:
    """insert_observation / get_observations round-trip."""

    def test_insert_observation(self):
        db = _db()
        ts = "2024-01-15T12:00:00+00:00"
        row_id = db.insert_observation(
            ts=ts,
            station="KORD",
            temp_f=32.0,
            temp_native=0.0,
            unit="C",
            source="metar",
            current_high=35.0,
            raw_json='{"raw": "KORD 011200Z"}',
        )
        assert isinstance(row_id, int)
        assert row_id >= 1

        rows = db.get_observations("KORD", since="2000-01-01")
        assert len(rows) == 1
        r = rows[0]
        assert r["ts"] == ts
        assert r["station"] == "KORD"
        assert r["temp_f"] == pytest.approx(32.0)
        assert r["temp_native"] == pytest.approx(0.0)
        assert r["unit"] == "C"
        assert r["current_high"] == pytest.approx(35.0)
        assert r["source"] == "metar"
        assert r["raw_json"] == '{"raw": "KORD 011200Z"}'

    def test_get_observations_since_filter(self):
        db = _db()
        db.insert_observation(
            ts="2024-01-01T00:00:00+00:00",
            station="KORD",
            temp_f=30.0,
            temp_native=30.0,
            unit="F",
            source="metar",
        )
        db.insert_observation(
            ts="2024-06-01T00:00:00+00:00",
            station="KORD",
            temp_f=75.0,
            temp_native=75.0,
            unit="F",
            source="metar",
        )
        rows = db.get_observations("KORD", since="2024-03-01T00:00:00+00:00")
        assert len(rows) == 1
        assert rows[0]["temp_f"] == pytest.approx(75.0)

    def test_get_observations_optional_fields_none(self):
        db = _db()
        db.insert_observation(
            ts="2024-01-15T12:00:00+00:00",
            station="KJFK",
            temp_f=50.0,
            temp_native=50.0,
            unit="F",
            source="nws",
        )
        rows = db.get_observations("KJFK", since="2000-01-01")
        assert rows[0]["current_high"] is None
        assert rows[0]["raw_json"] is None


# ---------------------------------------------------------------------------
# Open / close positions
# ---------------------------------------------------------------------------

class TestOpenClosePosition:
    """open_position / close_position atomicity and correctness."""

    def _insert_trade(self, db: Database) -> int:
        return db.insert_trade(
            ts="2024-01-15T12:00:00+00:00",
            station="KORD",
            ticker="KORD-2024-01-15-HIGH-32-36",
            bracket_low=32.0,
            bracket_high=36.0,
            side="YES",
            predicted_price=60,
            actual_price=58,
            predicted_edge=0.12,
            mode="paper",
            capital_before=1000.0,
        )

    def test_open_close_position(self):
        db = _db()
        trade_id = self._insert_trade(db)

        db.open_position(
            trade_id=trade_id,
            station="KORD",
            ticker="KORD-2024-01-15-HIGH-32-36",
            token_id="tok123",
            side="YES",
            order_id="ord-abc",
            entry_price=58,
            shares=10.0,
            entry_ts="2024-01-15T12:01:00+00:00",
        )

        positions = db.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["order_id"] == "ord-abc"
        assert positions[0]["trade_id"] == trade_id

        db.close_position("ord-abc")
        assert db.get_open_positions() == []

    def test_open_position_unique_order_id(self):
        """Inserting duplicate order_id must raise an IntegrityError."""
        db = _db()
        trade_id = self._insert_trade(db)
        kwargs = dict(
            trade_id=trade_id,
            station="KORD",
            ticker="KORD-2024-01-15-HIGH-32-36",
            token_id="tok456",
            side="YES",
            order_id="ord-dup",
            entry_price=55,
            shares=5.0,
            entry_ts="2024-01-15T12:02:00+00:00",
        )
        db.open_position(**kwargs)
        with pytest.raises(Exception):
            db.open_position(**kwargs)

    def test_close_nonexistent_position_is_noop(self):
        db = _db()
        # Should not raise
        db.close_position("does-not-exist")
        assert db.get_open_positions() == []


# ---------------------------------------------------------------------------
# Daily PnL accumulation
# ---------------------------------------------------------------------------

class TestDailyPnlAccumulation:
    """upsert_daily_risk accumulates deltas correctly."""

    def test_daily_pnl_accumulation(self):
        db = _db()
        db.upsert_daily_risk("2024-01-15", pnl_delta=10.0, open_positions=1)
        db.upsert_daily_risk("2024-01-15", pnl_delta=-15.0, open_positions=0)
        assert db.get_daily_pnl("2024-01-15") == pytest.approx(-5.0)

    def test_get_daily_pnl_missing_date(self):
        db = _db()
        assert db.get_daily_pnl("1900-01-01") == 0.0

    def test_daily_pnl_different_dates_independent(self):
        db = _db()
        db.upsert_daily_risk("2024-01-15", pnl_delta=20.0, open_positions=2)
        db.upsert_daily_risk("2024-01-16", pnl_delta=-5.0, open_positions=1)
        assert db.get_daily_pnl("2024-01-15") == pytest.approx(20.0)
        assert db.get_daily_pnl("2024-01-16") == pytest.approx(-5.0)

    def test_open_positions_replaced_not_accumulated(self):
        """open_positions column is replaced (not summed) on upsert."""
        db = _db()
        db.upsert_daily_risk("2024-01-15", pnl_delta=0.0, open_positions=5)
        db.upsert_daily_risk("2024-01-15", pnl_delta=0.0, open_positions=3)
        cur = db._conn.execute(
            "SELECT open_positions FROM risk_state WHERE trade_date=?", ("2024-01-15",)
        )
        assert cur.fetchone()[0] == 3


# ---------------------------------------------------------------------------
# Settlement upsert
# ---------------------------------------------------------------------------

class TestInsertSettlementUpsert:
    """insert_settlement must upsert (unique on ticker) and not duplicate rows."""

    def test_insert_settlement_upsert(self):
        db = _db()
        common = dict(
            ts="2024-01-15T20:00:00+00:00",
            station="KORD",
            ticker="KORD-2024-01-15-HIGH-32-36",
            bracket_low=32.0,
            bracket_high=36.0,
            resolved_yes=1,
        )
        db.insert_settlement(**common, actual_high_f=34.5)
        # Re-insert same ticker with different actual_high_f
        db.insert_settlement(**common, actual_high_f=99.0)

        rows = db.get_settlements("KORD", since="2000-01-01")
        assert len(rows) == 1, "Expected exactly 1 row after upsert"
        assert rows[0]["actual_high_f"] == pytest.approx(99.0)

    def test_insert_settlement_defaults(self):
        db = _db()
        db.insert_settlement(
            ts="2024-01-15T20:00:00+00:00",
            station="KJFK",
            ticker="KJFK-2024-01-15-HIGH-40-44",
            bracket_low=40.0,
            bracket_high=44.0,
            actual_high_f=42.0,
            resolved_yes=1,
        )
        rows = db.get_settlements("KJFK", since="2000-01-01")
        assert rows[0]["source"] == "polymarket"
        assert rows[0]["market_final_price"] is None


# ---------------------------------------------------------------------------
# Connection close and context manager
# ---------------------------------------------------------------------------

class TestDatabaseClose:
    """Database.close() and context manager support."""

    def test_close_closes_connection(self):
        """Calling close() should close the underlying connection."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            path = tf.name
        try:
            db = Database(path)
            db.close()
            # After closing, attempting to use the connection should raise
            with pytest.raises(Exception):  # sqlite3.ProgrammingError
                db._conn.execute("SELECT 1")
        finally:
            os.unlink(path)
            for ext in ("-wal", "-shm"):
                try:
                    os.unlink(path + ext)
                except FileNotFoundError:
                    pass

    def test_context_manager_closes_on_exit(self):
        """Using Database in a with statement should close the connection on exit."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            path = tf.name
        try:
            with Database(path) as db:
                # Connection should be open inside the context
                cur = db._conn.execute("SELECT 1")
                assert cur.fetchone()[0] == 1
            # After exiting the context, connection should be closed
            with pytest.raises(Exception):  # sqlite3.ProgrammingError
                db._conn.execute("SELECT 1")
        finally:
            os.unlink(path)
            for ext in ("-wal", "-shm"):
                try:
                    os.unlink(path + ext)
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------------------
# Model weights and forecast log
# ---------------------------------------------------------------------------

class TestModelWeightsTables:
    """upsert_model_weight / get_model_weights and upsert_forecast_log / get_forecast_log."""

    def test_upsert_and_get_model_weights(self):
        """Insert two weight rows for the same city (different models), retrieve and verify."""
        db = _db()
        db.upsert_model_weight(city="KORD", model="model_a", date="2024-01-15", weight=0.5, rmse=2.3)
        db.upsert_model_weight(city="KORD", model="model_b", date="2024-01-15", weight=0.7, rmse=1.8)

        rows = db.get_model_weights("KORD")
        assert len(rows) == 2
        weights_dict = {row["model"]: row for row in rows}
        assert weights_dict["model_a"]["weight"] == pytest.approx(0.5)
        assert weights_dict["model_a"]["rmse"] == pytest.approx(2.3)
        assert weights_dict["model_b"]["weight"] == pytest.approx(0.7)
        assert weights_dict["model_b"]["rmse"] == pytest.approx(1.8)

    def test_model_weight_upsert_replaces(self):
        """Insert same (city, model, date) twice with different rmse, verify only one row with latest value."""
        db = _db()
        db.upsert_model_weight(city="KORD", model="model_a", date="2024-01-15", weight=0.5, rmse=2.3)
        db.upsert_model_weight(city="KORD", model="model_a", date="2024-01-15", weight=0.5, rmse=1.9)

        rows = db.get_model_weights("KORD")
        assert len(rows) == 1, "Expected exactly 1 row after upsert"
        assert rows[0]["rmse"] == pytest.approx(1.9)

    def test_upsert_and_get_forecast_log(self):
        """Insert two forecast log rows for same station (different models), retrieve and verify."""
        db = _db()
        db.upsert_forecast_log(station="KORD", model="model_a", date="2024-01-15", forecast_high_f=32.5)
        db.upsert_forecast_log(station="KORD", model="model_b", date="2024-01-16", forecast_high_f=35.0)

        rows = db.get_forecast_log("KORD", since_date="2024-01-01")
        assert len(rows) == 2
        assert rows[0]["model"] == "model_a"
        assert rows[0]["forecast_high_f"] == pytest.approx(32.5)
        assert rows[1]["model"] == "model_b"
        assert rows[1]["forecast_high_f"] == pytest.approx(35.0)
        assert rows[0]["logged_at"] is not None
        assert rows[1]["logged_at"] is not None

    def test_forecast_log_upsert_replaces(self):
        """Insert same (station, model, date) twice with different forecast_high_f, verify idempotent."""
        db = _db()
        db.upsert_forecast_log(station="KORD", model="model_a", date="2024-01-15", forecast_high_f=32.5)
        db.upsert_forecast_log(station="KORD", model="model_a", date="2024-01-15", forecast_high_f=34.0)

        rows = db.get_forecast_log("KORD", since_date="2024-01-01")
        assert len(rows) == 1, "Expected exactly 1 row after upsert"
        assert rows[0]["forecast_high_f"] == pytest.approx(34.0)


# ---------------------------------------------------------------------------
# Intraday corrections
# ---------------------------------------------------------------------------

class TestIntradayCorrectionTable:
    """upsert_intraday_correction / get_intraday_corrections round-trip."""

    def test_table_exists(self):
        db = _db()
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='intraday_corrections'"
        )
        assert cur.fetchone() is not None

    def test_upsert_and_get(self):
        db = _db()
        db.upsert_intraday_correction(
            city="Tokyo",
            date="2024-01-15",
            obs_time="2024-01-15T09:00:00+00:00",
            obs_temp_f=68.0,
            model_temp_f=65.0,
            delta_f=2.4,
            corrected_mu_f=82.4,
            decay_factor=0.8,
        )
        rows = db.get_intraday_corrections("Tokyo", "2024-01-15")
        assert len(rows) == 1
        r = rows[0]
        assert r["city"] == "Tokyo"
        assert r["obs_temp_f"] == pytest.approx(68.0)
        assert r["model_temp_f"] == pytest.approx(65.0)
        assert r["delta_f"] == pytest.approx(2.4)
        assert r["corrected_mu_f"] == pytest.approx(82.4)
        assert r["decay_factor"] == pytest.approx(0.8)

    def test_upsert_replaces(self):
        db = _db()
        common = dict(
            city="Tokyo",
            date="2024-01-15",
            obs_time="2024-01-15T09:00:00+00:00",
            obs_temp_f=68.0,
            model_temp_f=65.0,
            delta_f=2.4,
            corrected_mu_f=82.4,
            decay_factor=0.8,
        )
        db.upsert_intraday_correction(**common)
        db.upsert_intraday_correction(**{**common, "corrected_mu_f": 99.0})
        rows = db.get_intraday_corrections("Tokyo", "2024-01-15")
        assert len(rows) == 1, "Expected exactly 1 row after upsert"
        assert rows[0]["corrected_mu_f"] == pytest.approx(99.0)


# ---------------------------------------------------------------------------
# Intraday corrections — station/source schema and migration
# ---------------------------------------------------------------------------

class TestIntradayCorrectionsMigration:
    """Tests for the station/source migration of intraday_corrections."""

    def test_fresh_schema_has_station_and_source_columns(self):
        """New DB must have station and source columns in intraday_corrections."""
        db = _db()
        cur = db._conn.execute("PRAGMA table_info(intraday_corrections)")
        col_names = {row[1] for row in cur.fetchall()}
        assert "station" in col_names, "station column missing from intraday_corrections"
        assert "source" in col_names, "source column missing from intraday_corrections"

    def test_fresh_schema_pk_includes_station_and_source(self):
        """PK columns in fresh DB must include city, station, source, date, obs_time."""
        db = _db()
        cur = db._conn.execute("PRAGMA table_info(intraday_corrections)")
        pk_cols = {row[1] for row in cur.fetchall() if row[5] > 0}  # row[5] is pk position
        assert "city" in pk_cols
        assert "station" in pk_cols
        assert "source" in pk_cols
        assert "date" in pk_cols
        assert "obs_time" in pk_cols

    def test_upsert_with_station_source_round_trip(self):
        """upsert_intraday_correction with station+source, get_intraday_corrections preserves them."""
        db = _db()
        db.upsert_intraday_correction(
            city="Busan",
            station="Busan",
            source="amos",
            date="2024-06-01",
            obs_time="2024-06-01T08:00:00+00:00",
            obs_temp_f=70.0,
            model_temp_f=68.0,
            delta_f=2.0,
            corrected_mu_f=82.0,
            decay_factor=0.9,
        )
        rows = db.get_intraday_corrections("Busan", "2024-06-01")
        assert len(rows) == 1
        r = rows[0]
        assert r["station"] == "Busan"
        assert r["source"] == "amos"
        assert r["delta_f"] == pytest.approx(2.0)

    def test_pk_uniqueness_different_station_source(self):
        """Two rows with same (city, date, obs_time) but different (station, source) coexist."""
        db = _db()
        base = dict(
            city="Busan", date="2024-06-01",
            obs_time="2024-06-01T08:00:00+00:00",
            obs_temp_f=70.0, model_temp_f=68.0,
            delta_f=2.0, corrected_mu_f=82.0, decay_factor=0.9,
        )
        db.upsert_intraday_correction(**base, station="Busan", source="amos")
        db.upsert_intraday_correction(**base, station="RKPK", source="metar")
        rows = db.get_intraday_corrections("Busan", "2024-06-01")
        assert len(rows) == 2, "Expected two distinct rows for different (station, source)"

    def test_upsert_replaces_on_same_pk(self):
        """Upserting same (city, station, source, date, obs_time) replaces the row."""
        db = _db()
        base = dict(
            city="Busan", station="Busan", source="amos",
            date="2024-06-01", obs_time="2024-06-01T08:00:00+00:00",
            obs_temp_f=70.0, model_temp_f=68.0, corrected_mu_f=82.0, decay_factor=0.9,
        )
        db.upsert_intraday_correction(**base, delta_f=2.0)
        db.upsert_intraday_correction(**base, delta_f=3.5)
        rows = db.get_intraday_corrections("Busan", "2024-06-01")
        assert len(rows) == 1
        assert rows[0]["delta_f"] == pytest.approx(3.5)

    def test_get_intraday_corrections_for_pair(self):
        """get_intraday_corrections_for_pair filters by (city, station, source)."""
        db = _db()
        base = dict(
            date="2024-06-01", obs_time="2024-06-01T08:00:00+00:00",
            obs_temp_f=70.0, model_temp_f=68.0, delta_f=2.0,
            corrected_mu_f=82.0, decay_factor=0.9,
        )
        db.upsert_intraday_correction(city="Busan", station="Busan", source="amos", **base)
        db.upsert_intraday_correction(city="Busan", station="RKPK", source="metar", **base)

        rows = db.get_intraday_corrections_for_pair("Busan", "Busan", "amos", "2024-01-01")
        assert len(rows) == 1
        assert rows[0]["source"] == "amos"

    def test_get_trailing_deltas_with_station_source_filter(self):
        """get_trailing_deltas with station+source returns only matching rows."""
        db = _db()
        from datetime import date
        today = date.today().isoformat()
        base = dict(
            date=today, obs_time=f"{today}T08:00:00+00:00",
            obs_temp_f=70.0, model_temp_f=68.0,
            corrected_mu_f=82.0, decay_factor=0.9,
        )
        db.upsert_intraday_correction(
            city="Busan", station="Busan", source="amos", delta_f=2.0, **base
        )
        db.upsert_intraday_correction(
            city="Busan", station="RKPK", source="metar", delta_f=5.0, **base
        )

        amos_deltas = db.get_trailing_deltas("Busan", 30, station="Busan", source="amos")
        assert amos_deltas == [pytest.approx(2.0)]

        metar_deltas = db.get_trailing_deltas("Busan", 30, station="RKPK", source="metar")
        assert metar_deltas == [pytest.approx(5.0)]

        all_deltas = db.get_trailing_deltas("Busan", 30)
        assert len(all_deltas) == 2

    def test_migration_old_schema_adds_station_source(self):
        """Migration converts old (city, date, obs_time) PK to new 5-column PK."""
        import sqlite3
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            path = tf.name

        try:
            # Build an old-schema DB directly (no station/source columns)
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE intraday_corrections (
                    city           TEXT NOT NULL,
                    date           TEXT NOT NULL,
                    obs_time       TEXT NOT NULL,
                    obs_temp_f     REAL NOT NULL,
                    model_temp_f   REAL NOT NULL,
                    delta_f        REAL NOT NULL,
                    corrected_mu_f REAL NOT NULL,
                    decay_factor   REAL NOT NULL,
                    PRIMARY KEY (city, date, obs_time)
                )
            """)
            conn.execute("""
                INSERT INTO intraday_corrections
                    (city, date, obs_time, obs_temp_f, model_temp_f, delta_f, corrected_mu_f, decay_factor)
                VALUES ('Busan', '2024-06-01', '2024-06-01T08:00:00+00:00',
                        70.0, 68.0, 2.0, 82.0, 0.9)
            """)
            conn.commit()
            conn.close()

            # Now open with Database() which should run the migration
            db = Database(path)

            try:
                # Check new columns exist
                cur = db._conn.execute("PRAGMA table_info(intraday_corrections)")
                col_names = {row[1] for row in cur.fetchall()}
                assert "station" in col_names
                assert "source" in col_names

                # Check data was preserved with empty station/source
                rows = db.get_intraday_corrections("Busan", "2024-06-01")
                assert len(rows) == 1
                assert rows[0]["station"] == ""
                assert rows[0]["source"] == ""
                assert rows[0]["delta_f"] == pytest.approx(2.0)
            finally:
                db.close()
        finally:
            for fname in [path] + [path + ext for ext in ("-wal", "-shm")]:
                try:
                    os.unlink(fname)
                except (FileNotFoundError, PermissionError):
                    pass


# ---------------------------------------------------------------------------
# EMOS calibration
# ---------------------------------------------------------------------------

class TestEmosCalibrationTable:
    """upsert_emos_coefficients / get_emos_coefficients round-trip and idempotency."""

    def test_table_exists(self):
        """emos_calibration table must exist after Database() initialization."""
        db = _db()
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='emos_calibration'"
        )
        assert cur.fetchone() is not None

    def test_migration_idempotency_create_twice(self):
        """Creating Database twice (or calling _migrate twice) must not raise."""
        db1 = _db()
        # Calling _migrate again should be idempotent
        db1._migrate()
        # Should not raise

    def test_upsert_and_get_round_trip(self):
        """upsert_emos_coefficients and get_emos_coefficients preserve all fields."""
        db = _db()
        db.upsert_emos_coefficients(
            city="Chicago",
            model_mode="nws",
            a=1.5,
            b=0.8,
            c=0.2,
            d=0.05,
            crps_score=0.12,
            trained_at="2024-01-15T10:00:00+00:00",
            ready_for_promotion=1,
        )
        result = db.get_emos_coefficients("Chicago", "nws")
        assert result is not None
        assert result["a"] == pytest.approx(1.5)
        assert result["b"] == pytest.approx(0.8)
        assert result["c"] == pytest.approx(0.2)
        assert result["d"] == pytest.approx(0.05)
        assert result["crps_score"] == pytest.approx(0.12)
        assert result["trained_at"] == "2024-01-15T10:00:00+00:00"
        assert result["ready_for_promotion"] == 1

    def test_upsert_with_optional_none(self):
        """upsert_emos_coefficients with None optional fields must work."""
        db = _db()
        db.upsert_emos_coefficients(
            city="Seoul",
            model_mode="open_meteo",
            a=1.2,
            b=0.7,
            c=0.15,
            d=0.03,
            crps_score=None,
            trained_at=None,
            ready_for_promotion=0,
        )
        result = db.get_emos_coefficients("Seoul", "open_meteo")
        assert result is not None
        assert result["a"] == pytest.approx(1.2)
        assert result["crps_score"] is None
        assert result["trained_at"] is None
        assert result["ready_for_promotion"] == 0

    def test_upsert_overwrites_not_duplicates(self):
        """Second upsert for same (city, model_mode) must overwrite, not duplicate."""
        db = _db()
        db.upsert_emos_coefficients(
            city="Chicago",
            model_mode="nws",
            a=1.0,
            b=0.5,
            c=0.1,
            d=0.01,
            crps_score=0.15,
        )
        db.upsert_emos_coefficients(
            city="Chicago",
            model_mode="nws",
            a=1.5,
            b=0.8,
            c=0.2,
            d=0.05,
            crps_score=0.12,
        )
        # Verify only one row exists
        cur = db._conn.execute(
            "SELECT COUNT(*) FROM emos_calibration WHERE city=? AND model_mode=?",
            ("Chicago", "nws"),
        )
        count = cur.fetchone()[0]
        assert count == 1

        # Verify values are the new ones
        result = db.get_emos_coefficients("Chicago", "nws")
        assert result["a"] == pytest.approx(1.5)
        assert result["crps_score"] == pytest.approx(0.12)

    def test_get_nonexistent_returns_none(self):
        """get_emos_coefficients for non-existent (city, model_mode) must return None."""
        db = _db()
        result = db.get_emos_coefficients("NonexistentCity", "nws")
        assert result is None

    def test_multiple_cities_and_modes(self):
        """Multiple (city, model_mode) pairs must be stored independently."""
        db = _db()
        db.upsert_emos_coefficients(
            city="Chicago", model_mode="nws", a=1.0, b=0.5, c=0.1, d=0.01
        )
        db.upsert_emos_coefficients(
            city="Chicago", model_mode="open_meteo", a=1.2, b=0.6, c=0.12, d=0.02
        )
        db.upsert_emos_coefficients(
            city="Seoul", model_mode="nws", a=1.1, b=0.55, c=0.11, d=0.011
        )

        # Each should be retrievable independently
        chicago_nws = db.get_emos_coefficients("Chicago", "nws")
        chicago_om = db.get_emos_coefficients("Chicago", "open_meteo")
        seoul_nws = db.get_emos_coefficients("Seoul", "nws")

        assert chicago_nws["a"] == pytest.approx(1.0)
        assert chicago_om["a"] == pytest.approx(1.2)
        assert seoul_nws["a"] == pytest.approx(1.1)

        # Total count must be 3
        cur = db._conn.execute("SELECT COUNT(*) FROM emos_calibration")
        assert cur.fetchone()[0] == 3


# ---------------------------------------------------------------------------
# Win-rate unification tests (issue #371)
# ---------------------------------------------------------------------------

from src.data.db import compute_win_rate


class TestComputeWinRate:
    """Test the canonical compute_win_rate helper function."""

    def test_basic_win_rate_calculation(self):
        """compute_win_rate should return wins / filled."""
        assert compute_win_rate(4, 2) == pytest.approx(0.5)
        assert compute_win_rate(3, 2) == pytest.approx(2/3)
        assert compute_win_rate(5, 5) == pytest.approx(1.0)
        assert compute_win_rate(5, 0) == pytest.approx(0.0)

    def test_filled_zero_returns_none(self):
        """compute_win_rate should return None when filled == 0."""
        assert compute_win_rate(0, 0) is None
        assert compute_win_rate(0, 1) is None

    def test_wins_greater_than_filled_is_invalid(self):
        """Wins should not exceed filled, but the function doesn't validate."""
        result = compute_win_rate(2, 3)
        assert result == pytest.approx(1.5)


class TestStationsTradeStatsUnification:
    """Test that get_stations_trade_stats uses the canonical win-rate definition."""

    def _insert_test_trade(self, db: Database, station: str, outcome: str, pnl: "float | None", ts: str = "2024-01-01T10:00:00+00:00") -> int:
        """Helper to insert a trade with minimal required fields."""
        return db.insert_trade(
            ts=ts,
            station=station,
            ticker="TEST-TICK",
            bracket_low=10.0,
            bracket_high=20.0,
            side="YES",
            predicted_price=50,
            actual_price=51,
            predicted_edge=0.12,
            mode="paper",
            capital_before=1000.0,
            outcome=outcome,
            pnl=pnl,
        )

    def test_fixture_five_trades(self):
        """Test with a fixture: 2 won, 1 lost, 1 timeout, 1 pnl=null filled."""
        db = _db()

        # Insert 5 trades for station "KORD":
        # 1. Filled with pnl=+10 (won)
        self._insert_test_trade(db, "KORD", "filled", 10.0, ts="2024-01-01T10:00:00+00:00")
        # 2. Filled with pnl=+5 (won)
        self._insert_test_trade(db, "KORD", "filled", 5.0, ts="2024-01-01T11:00:00+00:00")
        # 3. Filled with pnl=-8 (lost)
        self._insert_test_trade(db, "KORD", "filled", -8.0, ts="2024-01-01T12:00:00+00:00")
        # 4. Timeout (not filled)
        self._insert_test_trade(db, "KORD", "timeout", None, ts="2024-01-01T13:00:00+00:00")
        # 5. Filled with pnl=NULL (partial fill or pending settlement)
        self._insert_test_trade(db, "KORD", "filled", None, ts="2024-01-01T14:00:00+00:00")

        # Call get_stations_trade_stats
        stats = db.get_stations_trade_stats()

        # Expected:
        # trade_count: 5, filled_count: 3 (outcome IN ('filled','sold') AND pnl IS NOT NULL)
        # wins: 2 (pnl > 0), win_rate: 2/3 ≈ 0.6667
        # total_pnl: 10 + 5 - 8 + 0 + 0 = 7.0
        assert "KORD" in stats
        kord_stats = stats["KORD"]
        assert kord_stats["trade_count"] == 5
        assert kord_stats["filled_count"] == 3
        assert kord_stats["win_rate"] == pytest.approx(round(2/3, 4))
        assert kord_stats["total_pnl"] == pytest.approx(7.0)
        assert kord_stats["last_trade_ts"] == "2024-01-01T14:00:00+00:00"

    def test_excludes_pnl_null_from_filled_count(self):
        """Trades with pnl=NULL should not be counted in filled_count."""
        db = _db()
        for i in range(3):
            self._insert_test_trade(db, "TEST", "filled", float(i), ts=f"2024-01-01T{10+i}:00:00+00:00")
        for i in range(2):
            self._insert_test_trade(db, "TEST", "filled", None, ts=f"2024-01-01T{13+i}:00:00+00:00")
        stats = db.get_stations_trade_stats()
        assert stats["TEST"]["filled_count"] == 3
        assert stats["TEST"]["trade_count"] == 5

    def test_excludes_non_filled_outcome(self):
        """Trades with outcome != 'filled'/'sold' should not be counted."""
        db = _db()
        self._insert_test_trade(db, "TEST", "filled", 5.0, ts="2024-01-01T10:00:00+00:00")
        self._insert_test_trade(db, "TEST", "sold", 3.0, ts="2024-01-01T11:00:00+00:00")
        self._insert_test_trade(db, "TEST", "timeout", None, ts="2024-01-01T12:00:00+00:00")
        self._insert_test_trade(db, "TEST", "timeout", None, ts="2024-01-01T13:00:00+00:00")
        stats = db.get_stations_trade_stats()
        assert stats["TEST"]["filled_count"] == 2
        assert stats["TEST"]["trade_count"] == 4
        assert stats["TEST"]["win_rate"] == pytest.approx(1.0)

    def test_last_trade_ts_excludes_timeout(self):
        """last_trade_ts should only include filled/sold, not timeouts."""
        db = _db()
        self._insert_test_trade(db, "TEST", "filled", 5.0, ts="2024-01-01T10:00:00+00:00")
        self._insert_test_trade(db, "TEST", "timeout", None, ts="2024-01-01T11:00:00+00:00")
        stats = db.get_stations_trade_stats()
        assert stats["TEST"]["last_trade_ts"] == "2024-01-01T10:00:00+00:00"

    def test_no_trades_returns_empty_dict(self):
        """get_stations_trade_stats on empty trades table should return empty dict."""
        db = _db()
        stats = db.get_stations_trade_stats()
        assert stats == {}

    def test_multiple_stations_independent(self):
        """Multiple stations should have independent stats."""
        db = _db()
        self._insert_test_trade(db, "KORD", "filled", 10.0, ts="2024-01-01T10:00:00+00:00")
        self._insert_test_trade(db, "KORD", "filled", -5.0, ts="2024-01-01T11:00:00+00:00")
        for i in range(3):
            self._insert_test_trade(db, "KJFK", "filled", 5.0, ts=f"2024-01-02T{10+i}:00:00+00:00")
        stats = db.get_stations_trade_stats()
        assert stats["KORD"]["win_rate"] == pytest.approx(0.5)  # 1/2
        assert stats["KJFK"]["win_rate"] == pytest.approx(1.0)   # 3/3


# ---------------------------------------------------------------------------
# get_trade_by_order_id
# ---------------------------------------------------------------------------

class TestGetTradeByOrderId:
    """Test Database.get_trade_by_order_id() method."""

    def test_get_trade_by_order_id_found(self):
        """Insert a trade with a known order_id, assert method returns full row dict."""
        db = _db()
        trade_id = db.insert_trade(
            ts="2026-06-20T12:00:00Z",
            station="Boston",
            ticker="TEMP_BOS_202606_H90",
            bracket_low=88.0,
            bracket_high=92.0,
            side="YES",
            predicted_price=65,
            actual_price=72,
            predicted_edge=2.5,
            mode="live",
            capital_before=100.0,
            order_id="test-order-123",
        )

        # Retrieve via get_trade_by_order_id
        row = db.get_trade_by_order_id("test-order-123")

        # Verify row is not None
        assert row is not None
        # Verify it's a dict
        assert isinstance(row, dict)
        # Verify key fields match
        assert row["id"] == trade_id
        assert row["order_id"] == "test-order-123"
        assert row["station"] == "Boston"
        assert row["side"] == "YES"
        assert row["actual_price"] == 72

    def test_get_trade_by_order_id_not_found(self):
        """Assert returns None for unknown order_id."""
        db = _db()
        result = db.get_trade_by_order_id("nonexistent-order-id")
        assert result is None

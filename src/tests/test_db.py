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

"""Unit tests for src/data/db.py.

All tests use an in-memory SQLite database (':memory:') to avoid file I/O,
except test_wal_mode which requires a real file (WAL is a no-op for :memory:).
"""
import os
import sqlite3
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


class TestGetAllSettlements:
    """get_all_settlements returns rows across ALL stations in one query
    (used by the promotion bar in src/model/promotion_gate.py to avoid N+1
    reads when scanning every shadow station+side in one pass)."""

    def test_returns_rows_across_multiple_stations(self):
        db = _db()
        db.insert_settlement(
            ts="2024-01-15T20:00:00+00:00", station="KORD",
            ticker="KORD-1", bracket_low=32.0, bracket_high=36.0,
            actual_high_f=34.5, resolved_yes=1,
        )
        db.insert_settlement(
            ts="2024-01-16T20:00:00+00:00", station="WSSS",
            ticker="WSSS-1", bracket_low=88.0, bracket_high=90.0,
            actual_high_f=89.0, resolved_yes=0,
        )
        rows = db.get_all_settlements(since="2000-01-01")
        tickers = {r["ticker"] for r in rows}
        assert tickers == {"KORD-1", "WSSS-1"}

    def test_since_filters_out_earlier_rows(self):
        db = _db()
        db.insert_settlement(
            ts="2024-01-01T00:00:00+00:00", station="KORD",
            ticker="old", bracket_low=32.0, bracket_high=36.0,
            actual_high_f=34.5, resolved_yes=1,
        )
        db.insert_settlement(
            ts="2024-06-01T00:00:00+00:00", station="KORD",
            ticker="new", bracket_low=32.0, bracket_high=36.0,
            actual_high_f=34.5, resolved_yes=1,
        )
        rows = db.get_all_settlements(since="2024-03-01")
        assert [r["ticker"] for r in rows] == ["new"]

    def test_direction_filter(self):
        db = _db()
        db.insert_settlement(
            ts="2024-01-15T00:00:00+00:00", station="KORD",
            ticker="high-1", bracket_low=32.0, bracket_high=36.0,
            actual_high_f=34.5, resolved_yes=1, direction="high",
        )
        db.insert_settlement(
            ts="2024-01-15T00:00:00+00:00", station="KORD",
            ticker="low-1", bracket_low=32.0, bracket_high=36.0,
            actual_high_f=34.5, resolved_yes=1, direction="low",
        )
        rows = db.get_all_settlements(since="2000-01-01", direction="low")
        assert [r["ticker"] for r in rows] == ["low-1"]

    def test_empty_when_no_settlements(self):
        db = _db()
        assert db.get_all_settlements(since="2000-01-01") == []


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

    def test_get_trailing_deltas_min_date_excludes_earlier_rows(self):
        """min_date raises the window's lower bound when more recent than
        today - window_days (issue #586: excludes pre-basis-regime rows).

        All dates are computed relative to date.today() at test-run time (no
        hardcoded calendar literals), so the test is deterministic regardless
        of which day it actually runs.
        """
        from datetime import date, timedelta
        db = _db()
        today = date.today()

        def _row_on(offset_days, delta_f):
            d = (today - timedelta(days=offset_days)).isoformat()
            db.upsert_intraday_correction(
                city="Busan", station="Busan", source="amos",
                date=d, obs_time=f"{d}T08:00:00+00:00",
                obs_temp_f=70.0, model_temp_f=70.0 - delta_f,
                delta_f=delta_f, corrected_mu_f=70.0 + delta_f, decay_factor=0.9,
            )

        # 20 days ago: inside the 30-day window, but before min_date (10 days ago).
        _row_on(20, delta_f=99.0)
        # 5 days ago: inside the window AND on/after min_date.
        _row_on(5, delta_f=3.0)

        min_date = (today - timedelta(days=10)).isoformat()

        unfiltered = db.get_trailing_deltas("Busan", 30)
        assert len(unfiltered) == 2, "sanity check: both rows are inside the 30-day window"

        filtered = db.get_trailing_deltas("Busan", 30, min_date=min_date)
        assert filtered == [pytest.approx(3.0)], (
            "min_date should exclude the row recorded before it, even though "
            "it is within the trailing window_days"
        )

    def test_get_trailing_deltas_min_date_noop_when_older_than_window(self):
        """min_date older than today - window_days does not widen the window."""
        from datetime import date, timedelta
        db = _db()
        today = date.today()
        d = (today - timedelta(days=5)).isoformat()
        db.upsert_intraday_correction(
            city="Busan", station="Busan", source="amos",
            date=d, obs_time=f"{d}T08:00:00+00:00",
            obs_temp_f=70.0, model_temp_f=68.0,
            delta_f=2.0, corrected_mu_f=72.0, decay_factor=0.9,
        )
        far_past_min_date = (today - timedelta(days=365)).isoformat()
        deltas = db.get_trailing_deltas("Busan", 30, min_date=far_past_min_date)
        assert deltas == [pytest.approx(2.0)]

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


class TestToggleEmosReadyForPromotion:
    """toggle_emos_ready_for_promotion() track scoping (issue #696).

    emos_calibration's UNIQUE key grew to (city, model_mode, forecast_source,
    sigma_source, lead_hours) across #659/#449/#665. These tests lock in that
    the toggle only flips the active track by default, and that all_tracks=True
    reproduces the old city-wide behavior.
    """

    def test_toggles_active_track_only(self):
        """Default scoping flips only the active (forecast_source, sigma_source,
        lead_hours=24) row, leaving other tracks for the same city untouched."""
        db = _db()
        # Issue #799: sigma_source now derives from USE_ENSEMBLE_SIGMA (default
        # True) instead of a separately-settable EMOS_SIGMA_SOURCE key -- pin it
        # off here so the active track resolves to 'fixed', matching this
        # test's fixture rows.
        db.set_config("USE_ENSEMBLE_SIGMA", "false")
        db.upsert_emos_coefficients(
            city="Chicago", model_mode="emos_shadow",
            a=0.0, b=1.0, c=0.5, d=1.0,
            forecast_source="baseline", sigma_source="fixed", lead_hours=24,
            ready_for_promotion=0,
        )
        db.upsert_emos_coefficients(
            city="Chicago", model_mode="emos_shadow",
            a=0.1, b=1.1, c=0.6, d=1.1,
            forecast_source="hrrr_nbm", sigma_source="fixed", lead_hours=24,
            ready_for_promotion=0,
        )
        db.upsert_emos_coefficients(
            city="Chicago", model_mode="emos_shadow",
            a=0.2, b=1.2, c=0.7, d=1.2,
            forecast_source="baseline", sigma_source="ensemble", lead_hours=24,
            ready_for_promotion=0,
        )
        db.upsert_emos_coefficients(
            city="Chicago", model_mode="emos_shadow",
            a=0.3, b=1.3, c=0.8, d=1.3,
            forecast_source="baseline", sigma_source="fixed", lead_hours=6,
            ready_for_promotion=0,
        )

        # Active track defaults: forecast_source="baseline" (FORECAST_STACK
        # unset), sigma_source="fixed" (USE_ENSEMBLE_SIGMA pinned false above),
        # lead_hours=24.
        new_val = db.toggle_emos_ready_for_promotion("Chicago")
        assert new_val == 1

        active = db.get_emos_coefficients(
            "Chicago", "emos_shadow",
            forecast_source="baseline", sigma_source="fixed", lead_hours=24,
        )
        assert active["ready_for_promotion"] == 1

        for forecast_source, sigma_source, lead_hours in (
            ("hrrr_nbm", "fixed", 24),
            ("baseline", "ensemble", 24),
            ("baseline", "fixed", 6),
        ):
            other = db.get_emos_coefficients(
                "Chicago", "emos_shadow",
                forecast_source=forecast_source, sigma_source=sigma_source,
                lead_hours=lead_hours,
            )
            assert other["ready_for_promotion"] == 0, (
                f"track {(forecast_source, sigma_source, lead_hours)} should be untouched"
            )

    def test_active_track_follows_bot_config(self):
        """When FORECAST_STACK/USE_ENSEMBLE_SIGMA bot_config keys are set, the
        default scoping follows them instead of the 'baseline'/'fixed' defaults."""
        db = _db()
        db.set_config("FORECAST_STACK", "hrrr_nbm")
        db.set_config("USE_ENSEMBLE_SIGMA", "true")
        db.upsert_emos_coefficients(
            city="Denver", model_mode="emos_shadow",
            a=0.0, b=1.0, c=0.5, d=1.0,
            forecast_source="hrrr_nbm", sigma_source="ensemble", lead_hours=24,
            ready_for_promotion=0,
        )
        db.upsert_emos_coefficients(
            city="Denver", model_mode="emos_shadow",
            a=0.1, b=1.1, c=0.6, d=1.1,
            forecast_source="baseline", sigma_source="fixed", lead_hours=24,
            ready_for_promotion=0,
        )

        new_val = db.toggle_emos_ready_for_promotion("Denver")
        assert new_val == 1

        active = db.get_emos_coefficients(
            "Denver", "emos_shadow",
            forecast_source="hrrr_nbm", sigma_source="ensemble", lead_hours=24,
        )
        assert active["ready_for_promotion"] == 1
        legacy = db.get_emos_coefficients(
            "Denver", "emos_shadow",
            forecast_source="baseline", sigma_source="fixed", lead_hours=24,
        )
        assert legacy["ready_for_promotion"] == 0

    def test_all_tracks_override_flips_every_row(self):
        """all_tracks=True reproduces the pre-#696 city-wide toggle: every
        (city, model_mode='emos_shadow') row flips together."""
        db = _db()
        db.upsert_emos_coefficients(
            city="Miami", model_mode="emos_shadow",
            a=0.0, b=1.0, c=0.5, d=1.0,
            forecast_source="baseline", sigma_source="fixed", lead_hours=24,
            ready_for_promotion=0,
        )
        db.upsert_emos_coefficients(
            city="Miami", model_mode="emos_shadow",
            a=0.1, b=1.1, c=0.6, d=1.1,
            forecast_source="hrrr_nbm", sigma_source="ensemble", lead_hours=6,
            ready_for_promotion=0,
        )

        new_val = db.toggle_emos_ready_for_promotion("Miami", all_tracks=True)
        assert new_val == 1

        cur = db._conn.execute(
            "SELECT ready_for_promotion FROM emos_calibration "
            "WHERE city='Miami' AND model_mode='emos_shadow'"
        )
        rows = cur.fetchall()
        assert len(rows) == 2
        assert all(r[0] == 1 for r in rows)

    def test_returns_none_when_active_track_row_missing(self):
        """Default scoping returns None when only a non-active track has a row,
        even though the (city, model_mode) pair exists."""
        db = _db()
        db.upsert_emos_coefficients(
            city="Seattle", model_mode="emos_shadow",
            a=0.0, b=1.0, c=0.5, d=1.0,
            forecast_source="hrrr_nbm", sigma_source="fixed", lead_hours=24,
            ready_for_promotion=0,
        )
        # Active track defaults to forecast_source="baseline" — no row there.
        assert db.toggle_emos_ready_for_promotion("Seattle") is None


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


# ---------------------------------------------------------------------------
# poll_runs / record_poll_run (issue #914)
# ---------------------------------------------------------------------------

class TestPollRuns:
    """Database.record_poll_run() writes an unconditional poll heartbeat,
    independent of scan_decisions (which is only written when brackets are
    evaluated). See issue #914 defect 1.
    """

    def test_poll_runs_table_exists(self):
        db = _db()
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='poll_runs'"
        )
        assert cur.fetchone() is not None

    def test_record_poll_run_inserts_row(self):
        db = _db()
        db.record_poll_run("2026-07-30T13:56:00+00:00", "live")
        rows = db._conn.execute("SELECT poll_ts, mode FROM poll_runs").fetchall()
        assert [tuple(r) for r in rows] == [("2026-07-30T13:56:00+00:00", "live")]

    def test_record_poll_run_does_not_require_scan_decisions_row(self):
        """A poll heartbeat must be recordable with zero scan_decisions
        activity -- the two are independent (defect 1's whole point)."""
        db = _db()
        for i in range(3):
            db.record_poll_run(f"2026-07-30T{10+i}:00:00+00:00", "live")
        poll_count = db._conn.execute("SELECT COUNT(*) FROM poll_runs").fetchone()[0]
        scan_count = db._conn.execute("SELECT COUNT(*) FROM scan_decisions").fetchone()[0]
        assert poll_count == 3
        assert scan_count == 0

    def test_default_mode_is_paper(self):
        db = _db()
        db.record_poll_run("2026-07-30T13:56:00+00:00")
        mode = db._conn.execute("SELECT mode FROM poll_runs").fetchone()[0]
        assert mode == "paper"


# ---------------------------------------------------------------------------
# copy_wallet_candidates / insert_wallet_screening / get_recent_wallet_screenings
# (issue #1108, epic #1099)
# ---------------------------------------------------------------------------

class TestCopyWalletCandidates:
    """Append-only wallet-screening-run persistence for the copy-trading
    pipeline. Unlike scan_decisions (upsert-per-key), every screening run
    keeps its own row so the stability check (epic #1099 story 2) can diff
    a wallet's last two runs.
    """

    def _screening_kwargs(self, **overrides):
        kwargs = dict(
            address="0xd3b034d7",
            window="30d",
            screened_at="2026-07-30T13:00:00+00:00",
            n_buy_trades=100,
            n_resolved=7498,
            slippage_bps=25.0,
            win_rate=0.62,
            mean_roi=0.10,
            median_roi=0.334,
            mirrored_dollar_pnl=1000.0,
            flat_dollar_pnl=500.0,
            flat_stake=100.0,
            eligible_to_follow=1,
        )
        kwargs.update(overrides)
        return kwargs

    def test_table_exists(self):
        db = _db()
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='copy_wallet_candidates'"
        )
        assert cur.fetchone() is not None

    def test_insert_then_get_round_trip(self):
        db = _db()
        row_id = db.insert_wallet_screening(**self._screening_kwargs())
        assert isinstance(row_id, int)

        rows = db.get_recent_wallet_screenings("0xd3b034d7")
        assert len(rows) == 1
        row = rows[0]
        assert row["address"] == "0xd3b034d7"
        assert row["window"] == "30d"
        assert row["screened_at"] == "2026-07-30T13:00:00+00:00"
        assert row["n_buy_trades"] == 100
        assert row["n_resolved"] == 7498
        assert row["win_rate"] == 0.62
        assert row["mean_roi"] == 0.10
        assert row["median_roi"] == 0.334
        assert row["mirrored_dollar_pnl"] == 1000.0
        assert row["flat_dollar_pnl"] == 500.0
        assert row["flat_stake"] == 100.0
        assert row["slippage_bps"] == 25.0
        assert row["eligible_to_follow"] == 1

    def test_get_recent_respects_limit(self):
        db = _db()
        for i in range(5):
            db.insert_wallet_screening(
                **self._screening_kwargs(screened_at=f"2026-07-30T1{i}:00:00+00:00")
            )
        rows = db.get_recent_wallet_screenings("0xd3b034d7", limit=2)
        assert len(rows) == 2

    def test_get_recent_orders_newest_first_by_id(self):
        """id DESC, not screened_at DESC -- two runs can share a timestamp."""
        db = _db()
        same_ts = "2026-07-30T13:00:00+00:00"
        first_id = db.insert_wallet_screening(
            **self._screening_kwargs(screened_at=same_ts, n_resolved=7498, median_roi=0.334)
        )
        second_id = db.insert_wallet_screening(
            **self._screening_kwargs(screened_at=same_ts, n_resolved=2271, median_roi=-1.0)
        )
        assert second_id > first_id

        rows = db.get_recent_wallet_screenings("0xd3b034d7", limit=2)
        assert [r["n_resolved"] for r in rows] == [2271, 7498]

    def test_unknown_address_returns_empty_list(self):
        db = _db()
        db.insert_wallet_screening(**self._screening_kwargs())
        assert db.get_recent_wallet_screenings("0xnotarealaddress") == []

    def test_no_rows_returns_empty_list_not_raise(self):
        db = _db()
        assert db.get_recent_wallet_screenings("0xd3b034d7") == []

    def test_two_inserts_same_address_are_append_only_not_overwritten(self):
        """The behavior that differentiates this table from scan_decisions:
        a second insert for the same address must persist as a SEPARATE row,
        reproducing the 0xd3b034d7 reversal pattern (resolved trades
        7,498 -> 2,271, median ROI +33.4% -> -100%) across two runs.
        """
        db = _db()
        db.insert_wallet_screening(
            **self._screening_kwargs(
                screened_at="2026-07-30T13:00:00+00:00",
                n_resolved=7498,
                median_roi=0.334,
                eligible_to_follow=1,
            )
        )
        db.insert_wallet_screening(
            **self._screening_kwargs(
                screened_at="2026-07-31T04:00:00+00:00",
                n_resolved=2271,
                median_roi=-1.0,
                eligible_to_follow=0,
            )
        )
        count = db._conn.execute(
            "SELECT COUNT(*) FROM copy_wallet_candidates WHERE address=?",
            ("0xd3b034d7",),
        ).fetchone()[0]
        assert count == 2

        rows = db.get_recent_wallet_screenings("0xd3b034d7", limit=10)
        assert len(rows) == 2
        assert {r["n_resolved"] for r in rows} == {7498, 2271}

    def test_get_latest_returns_empty_list_when_never_screened(self):
        db = _db()
        assert db.get_latest_wallet_screenings() == []

    def test_get_latest_returns_one_row_per_address(self):
        db = _db()
        db.insert_wallet_screening(**self._screening_kwargs(address="0xaaa"))
        db.insert_wallet_screening(**self._screening_kwargs(address="0xbbb"))
        rows = db.get_latest_wallet_screenings()
        assert {r["address"] for r in rows} == {"0xaaa", "0xbbb"}

    def test_get_latest_picks_most_recent_run_by_id(self):
        db = _db()
        db.insert_wallet_screening(
            **self._screening_kwargs(n_resolved=7498, median_roi=0.334, eligible_to_follow=1)
        )
        db.insert_wallet_screening(
            **self._screening_kwargs(n_resolved=2271, median_roi=-1.0, eligible_to_follow=0)
        )
        rows = db.get_latest_wallet_screenings()
        assert len(rows) == 1
        assert rows[0]["n_resolved"] == 2271
        assert rows[0]["eligible_to_follow"] == 0


# ---------------------------------------------------------------------------
# copy_wallets_followed / copy_signals / copy_positions
# (issue #1121, epic #1101)
# ---------------------------------------------------------------------------

class TestCopyWalletsFollowed:
    """One row per followed wallet, mutated in place -- unlike
    copy_wallet_candidates' append-only history.
    """

    def _followed_kwargs(self, **overrides):
        kwargs = dict(
            address="0xabc",
            stake_per_trade=25.0,
            added_at="2026-09-19T00:00:00+00:00",
        )
        kwargs.update(overrides)
        return kwargs

    def test_table_exists(self):
        db = _db()
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='copy_wallets_followed'"
        )
        assert cur.fetchone() is not None

    def test_insert_then_get_round_trip(self):
        db = _db()
        db.insert_followed_wallet(**self._followed_kwargs())
        rows = db.get_followed_wallets()
        assert len(rows) == 1
        row = rows[0]
        assert row["address"] == "0xabc"
        assert row["stake_per_trade"] == 25.0
        assert row["status"] == "active"
        assert row["paused_reason"] is None
        assert row["added_at"] == "2026-09-19T00:00:00+00:00"
        assert row["last_seen_trade_ts"] is None

    def test_insert_duplicate_address_raises(self):
        db = _db()
        db.insert_followed_wallet(**self._followed_kwargs())
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_followed_wallet(**self._followed_kwargs())

    def test_insert_non_positive_stake_raises(self):
        """stake_per_trade CHECK(stake_per_trade > 0) -- negative and zero
        stakes are both rejected at the DB level, not just by caller
        discipline."""
        db = _db()
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_followed_wallet(**self._followed_kwargs(address="0xneg", stake_per_trade=-5.0))
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_followed_wallet(**self._followed_kwargs(address="0xzero", stake_per_trade=0.0))

    def test_get_followed_wallets_filters_by_status(self):
        db = _db()
        db.insert_followed_wallet(**self._followed_kwargs(address="0xactive"))
        db.insert_followed_wallet(**self._followed_kwargs(address="0xpaused"))
        db.update_followed_wallet_status("0xpaused", "paused", paused_reason="stale")

        active = db.get_followed_wallets("active")
        paused = db.get_followed_wallets("paused")
        assert [r["address"] for r in active] == ["0xactive"]
        assert [r["address"] for r in paused] == ["0xpaused"]

    def test_update_status_does_not_touch_other_columns(self):
        db = _db()
        db.insert_followed_wallet(**self._followed_kwargs())
        db.update_followed_wallet_status("0xabc", "paused", paused_reason="rug pull risk")

        row = db.get_followed_wallets()[0]
        assert row["status"] == "paused"
        assert row["paused_reason"] == "rug pull risk"
        assert row["stake_per_trade"] == 25.0
        assert row["added_at"] == "2026-09-19T00:00:00+00:00"

    def test_update_status_back_to_active_clears_paused_reason(self):
        db = _db()
        db.insert_followed_wallet(**self._followed_kwargs())
        db.update_followed_wallet_status("0xabc", "paused", paused_reason="rug pull risk")
        db.update_followed_wallet_status("0xabc", "active")

        row = db.get_followed_wallets()[0]
        assert row["status"] == "active"
        assert row["paused_reason"] is None

    def test_update_last_seen_trade_ts(self):
        db = _db()
        db.insert_followed_wallet(**self._followed_kwargs())
        db.update_followed_wallet_last_seen("0xabc", 1758000000)

        row = db.get_followed_wallets()[0]
        assert row["last_seen_trade_ts"] == 1758000000


class TestCopySignalsAndPositions:
    """copy_signals (one row per detected BUY) and copy_positions (open,
    unsettled paper positions -- settled in place, never deleted).
    """

    def _signal_kwargs(self, **overrides):
        kwargs = dict(
            address="0xabc",
            market="0xmarket1",
            source_price=0.45,
            detected_at="2026-09-19T00:00:00+00:00",
        )
        kwargs.update(overrides)
        return kwargs

    def _position_kwargs(self, signal_id, **overrides):
        kwargs = dict(
            signal_id=signal_id,
            address="0xabc",
            market="0xmarket1",
            outcome_index=0,
            entry_price=0.45,
            stake_usd=25.0,
            entry_ts="2026-09-19T00:00:00+00:00",
        )
        kwargs.update(overrides)
        return kwargs

    def test_tables_exist(self):
        db = _db()
        for table in ("copy_signals", "copy_positions"):
            cur = db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            assert cur.fetchone() is not None, table

    def test_insert_signal_then_position_round_trip(self):
        db = _db()
        signal_id = db.insert_copy_signal(**self._signal_kwargs())
        assert isinstance(signal_id, int)

        position_id = db.insert_copy_position(**self._position_kwargs(signal_id))
        assert isinstance(position_id, int)

        cur = db._conn.execute("SELECT * FROM copy_signals WHERE id=?", (signal_id,))
        signal_row = dict(cur.fetchone())
        assert signal_row["address"] == "0xabc"
        assert signal_row["market"] == "0xmarket1"
        assert signal_row["source_price"] == 0.45
        assert signal_row["order_placed"] == 0
        assert signal_row["skip_reason"] is None

        cur = db._conn.execute("SELECT * FROM copy_positions WHERE id=?", (position_id,))
        position_row = dict(cur.fetchone())
        assert position_row["signal_id"] == signal_id
        assert position_row["address"] == "0xabc"
        assert position_row["stake_usd"] == 25.0
        assert position_row["status"] == "open"
        assert position_row["settled_pnl_usd"] is None

    def test_get_open_copy_positions_returns_only_open(self):
        db = _db()
        signal_id = db.insert_copy_signal(**self._signal_kwargs())
        open_id = db.insert_copy_position(**self._position_kwargs(signal_id))
        settled_id = db.insert_copy_position(**self._position_kwargs(signal_id))
        db._conn.execute(
            "UPDATE copy_positions SET status='settled', settled_pnl_usd=5.0, "
            "settled_at='2026-09-20T00:00:00+00:00' WHERE id=?",
            (settled_id,),
        )
        db._conn.commit()

        open_rows = db.get_open_copy_positions()
        assert [r["id"] for r in open_rows] == [open_id]

    def test_get_open_copy_positions_filters_by_address(self):
        db = _db()
        signal_a = db.insert_copy_signal(**self._signal_kwargs(address="0xaaa"))
        signal_b = db.insert_copy_signal(**self._signal_kwargs(address="0xbbb"))
        pos_a = db.insert_copy_position(**self._position_kwargs(signal_a, address="0xaaa"))
        db.insert_copy_position(**self._position_kwargs(signal_b, address="0xbbb"))

        rows = db.get_open_copy_positions("0xaaa")
        assert [r["id"] for r in rows] == [pos_a]

    def test_settled_position_retained_not_deleted(self):
        db = _db()
        signal_id = db.insert_copy_signal(**self._signal_kwargs())
        position_id = db.insert_copy_position(**self._position_kwargs(signal_id))
        db._conn.execute(
            "UPDATE copy_positions SET status='settled', settled_pnl_usd=-3.5, "
            "settled_at='2026-09-20T00:00:00+00:00' WHERE id=?",
            (position_id,),
        )
        db._conn.commit()

        assert db.get_open_copy_positions() == []
        cur = db._conn.execute("SELECT * FROM copy_positions WHERE id=?", (position_id,))
        row = dict(cur.fetchone())
        assert row["status"] == "settled"
        assert row["settled_pnl_usd"] == -3.5
        assert row["settled_at"] == "2026-09-20T00:00:00+00:00"

    def test_insert_signal_with_out_of_range_source_price_raises(self):
        """source_price CHECK(source_price >= 0 AND source_price <= 1) --
        a price outside the valid probability range is rejected at the DB
        level, both above 1 and below 0."""
        db = _db()
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_copy_signal(**self._signal_kwargs(source_price=1.5))
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_copy_signal(**self._signal_kwargs(source_price=-0.1))

    def test_insert_signal_with_invalid_outcome_index_raises(self):
        """outcome_index CHECK(... IN (0,1)) -- only the two valid slots
        are accepted; NULL is still fine (checked separately)."""
        db = _db()
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_copy_signal(**self._signal_kwargs(outcome_index=2))
        # NULL (unset) must still be accepted
        db.insert_copy_signal(**self._signal_kwargs())

    def test_insert_signal_with_non_positive_size_usd_raises(self):
        """size_usd CHECK(size_usd IS NULL OR size_usd > 0)."""
        db = _db()
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_copy_signal(**self._signal_kwargs(size_usd=-10.0))
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_copy_signal(**self._signal_kwargs(size_usd=0.0))

    def test_insert_position_with_out_of_range_entry_price_raises(self):
        """entry_price CHECK(entry_price >= 0 AND entry_price <= 1)."""
        db = _db()
        signal_id = db.insert_copy_signal(**self._signal_kwargs())
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_copy_position(**self._position_kwargs(signal_id, entry_price=1.1))

    def test_insert_position_with_non_positive_stake_usd_raises(self):
        """stake_usd CHECK(stake_usd > 0)."""
        db = _db()
        signal_id = db.insert_copy_signal(**self._signal_kwargs())
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_copy_position(**self._position_kwargs(signal_id, stake_usd=0.0))

    def test_insert_position_with_invalid_outcome_index_raises(self):
        """outcome_index CHECK(outcome_index IN (0,1)) -- unlike copy_signals,
        this column is NOT NULL on copy_positions, so only 0/1 are valid."""
        db = _db()
        signal_id = db.insert_copy_signal(**self._signal_kwargs())
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_copy_position(**self._position_kwargs(signal_id, outcome_index=2))

    def test_link_copy_signal_to_position_round_trip(self):
        db = _db()
        signal_id = db.insert_copy_signal(**self._signal_kwargs())
        position_id = db.insert_copy_position(**self._position_kwargs(signal_id))
        db.link_copy_signal_to_position(signal_id, position_id)

        cur = db._conn.execute("SELECT position_id FROM copy_signals WHERE id=?", (signal_id,))
        assert cur.fetchone()["position_id"] == position_id

    def test_copy_signal_exists_for_trade_none_source_trade_id_always_false(self):
        db = _db()
        db.insert_copy_signal(**self._signal_kwargs(source_trade_id=None))
        assert db.copy_signal_exists_for_trade(
            address="0xabc", market="0xmarket1", source_price=0.45, source_trade_id=None,
        ) is False

    def test_copy_signal_exists_for_trade_matches_full_identity(self):
        db = _db()
        db.insert_copy_signal(**self._signal_kwargs(source_trade_id="0xtxhash1"))
        assert db.copy_signal_exists_for_trade(
            address="0xabc", market="0xmarket1", source_price=0.45,
            source_trade_id="0xtxhash1",
        ) is True

    def test_copy_signal_exists_for_trade_false_when_price_differs(self):
        """A single tx can span multiple maker fills at different price
        levels (copy_signals' own schema comment) -- those are distinct
        fills, not duplicates, so a price mismatch must not match."""
        db = _db()
        db.insert_copy_signal(**self._signal_kwargs(source_trade_id="0xtxhash1", source_price=0.45))
        assert db.copy_signal_exists_for_trade(
            address="0xabc", market="0xmarket1", source_price=0.50,
            source_trade_id="0xtxhash1",
        ) is False

    def test_copy_signal_exists_for_trade_false_when_unseen(self):
        db = _db()
        assert db.copy_signal_exists_for_trade(
            address="0xabc", market="0xmarket1", source_price=0.45,
            source_trade_id="0xnever-seen",
        ) is False


class TestSettleCopyPosition:
    """Database.settle_copy_position (issue #1131) -- idempotent status flip."""

    def _open_position(self, db, **overrides):
        signal_id = db.insert_copy_signal(
            address=overrides.pop("address", "0xabc"),
            market="0xmarket1",
            source_price=0.45,
            detected_at="2026-09-19T00:00:00+00:00",
        )
        kwargs = dict(
            signal_id=signal_id,
            address="0xabc",
            market="0xmarket1",
            outcome_index=0,
            entry_price=0.45,
            stake_usd=25.0,
            entry_ts="2026-09-19T00:00:00+00:00",
        )
        kwargs.update(overrides)
        return db.insert_copy_position(**kwargs)

    def test_settle_open_position_updates_row(self):
        db = _db()
        position_id = self._open_position(db)

        db.settle_copy_position(position_id, 12.5, "2026-09-20T00:00:00+00:00")

        cur = db._conn.execute("SELECT * FROM copy_positions WHERE id=?", (position_id,))
        row = dict(cur.fetchone())
        assert row["status"] == "settled"
        assert row["settled_pnl_usd"] == 12.5
        assert row["settled_at"] == "2026-09-20T00:00:00+00:00"

    def test_settle_already_settled_position_is_noop(self):
        """A double-settle attempt (e.g. a retried cycle) must not overwrite
        the original settlement values, and must not raise."""
        db = _db()
        position_id = self._open_position(db)
        db.settle_copy_position(position_id, 12.5, "2026-09-20T00:00:00+00:00")

        db.settle_copy_position(position_id, -999.0, "2026-09-21T00:00:00+00:00")

        cur = db._conn.execute("SELECT * FROM copy_positions WHERE id=?", (position_id,))
        row = dict(cur.fetchone())
        assert row["status"] == "settled"
        assert row["settled_pnl_usd"] == 12.5
        assert row["settled_at"] == "2026-09-20T00:00:00+00:00"

    def test_settle_nonexistent_position_is_noop(self):
        """Settling an id that doesn't exist matches zero rows -- no
        exception, nothing to assert on except that it doesn't blow up."""
        db = _db()
        db.settle_copy_position(999999, 1.0, "2026-09-20T00:00:00+00:00")


class TestGetCopyRealizedPnl:
    """Database.get_copy_realized_pnl_by_wallet / get_copy_realized_pnl_total
    (issue #1131)."""

    def _settled_position(self, db, address, pnl, **overrides):
        signal_id = db.insert_copy_signal(
            address=address,
            market=overrides.pop("market", "0xmarket1"),
            source_price=0.45,
            detected_at="2026-09-19T00:00:00+00:00",
        )
        kwargs = dict(
            signal_id=signal_id,
            address=address,
            market="0xmarket1",
            outcome_index=0,
            entry_price=0.45,
            stake_usd=25.0,
            entry_ts="2026-09-19T00:00:00+00:00",
        )
        kwargs.update(overrides)
        position_id = db.insert_copy_position(**kwargs)
        db.settle_copy_position(position_id, pnl, "2026-09-20T00:00:00+00:00")
        return position_id

    def test_aggregates_multiple_settled_positions_for_one_wallet(self):
        db = _db()
        self._settled_position(db, "0xaaa", 10.0)
        self._settled_position(db, "0xaaa", -4.0)

        rows = db.get_copy_realized_pnl_by_wallet()
        assert len(rows) == 1
        assert rows[0]["address"] == "0xaaa"
        assert rows[0]["n_settled"] == 2
        assert rows[0]["total_pnl_usd"] == pytest.approx(6.0)

    def test_per_wallet_breakdown_across_multiple_wallets(self):
        db = _db()
        self._settled_position(db, "0xaaa", 10.0)
        self._settled_position(db, "0xbbb", 5.0)
        self._settled_position(db, "0xbbb", 5.0)

        rows = {r["address"]: r for r in db.get_copy_realized_pnl_by_wallet()}
        assert rows["0xaaa"]["n_settled"] == 1
        assert rows["0xaaa"]["total_pnl_usd"] == pytest.approx(10.0)
        assert rows["0xbbb"]["n_settled"] == 2
        assert rows["0xbbb"]["total_pnl_usd"] == pytest.approx(10.0)

    def test_filters_by_address(self):
        db = _db()
        self._settled_position(db, "0xaaa", 10.0)
        self._settled_position(db, "0xbbb", 5.0)

        rows = db.get_copy_realized_pnl_by_wallet("0xaaa")
        assert len(rows) == 1
        assert rows[0]["address"] == "0xaaa"
        assert rows[0]["total_pnl_usd"] == pytest.approx(10.0)

    def test_open_positions_excluded(self):
        db = _db()
        signal_id = db.insert_copy_signal(
            address="0xaaa", market="0xmarket1", source_price=0.45,
            detected_at="2026-09-19T00:00:00+00:00",
        )
        db.insert_copy_position(
            signal_id=signal_id, address="0xaaa", market="0xmarket1",
            outcome_index=0, entry_price=0.45, stake_usd=25.0,
            entry_ts="2026-09-19T00:00:00+00:00",
        )

        assert db.get_copy_realized_pnl_by_wallet() == []

    def test_wallet_with_no_settled_rows_absent_from_unfiltered_result(self):
        db = _db()
        self._settled_position(db, "0xaaa", 10.0)
        # 0xbbb has only an open (unsettled) position -- never settled.
        signal_id = db.insert_copy_signal(
            address="0xbbb", market="0xmarket1", source_price=0.45,
            detected_at="2026-09-19T00:00:00+00:00",
        )
        db.insert_copy_position(
            signal_id=signal_id, address="0xbbb", market="0xmarket1",
            outcome_index=0, entry_price=0.45, stake_usd=25.0,
            entry_ts="2026-09-19T00:00:00+00:00",
        )

        rows = db.get_copy_realized_pnl_by_wallet()
        assert [r["address"] for r in rows] == ["0xaaa"]

    def test_filtered_wallet_with_no_settled_rows_returns_empty_list(self):
        db = _db()
        signal_id = db.insert_copy_signal(
            address="0xbbb", market="0xmarket1", source_price=0.45,
            detected_at="2026-09-19T00:00:00+00:00",
        )
        db.insert_copy_position(
            signal_id=signal_id, address="0xbbb", market="0xmarket1",
            outcome_index=0, entry_price=0.45, stake_usd=25.0,
            entry_ts="2026-09-19T00:00:00+00:00",
        )

        assert db.get_copy_realized_pnl_by_wallet("0xbbb") == []

    def test_total_aggregates_across_all_wallets(self):
        db = _db()
        self._settled_position(db, "0xaaa", 10.0)
        self._settled_position(db, "0xbbb", -3.0)
        self._settled_position(db, "0xbbb", 5.0)

        total = db.get_copy_realized_pnl_total()
        assert total == {"n_settled": 3, "total_pnl_usd": pytest.approx(12.0)}

    def test_total_is_zero_when_no_settled_positions(self):
        db = _db()
        assert db.get_copy_realized_pnl_total() == {"n_settled": 0, "total_pnl_usd": 0.0}


class TestGetSettledCopyPositions:
    """Database.get_settled_copy_positions (issue #1140) -- individual
    settled copy_positions rows for one wallet, needed by the wallet-health
    job's per-trade median-ROI computation."""

    def _settled_position(self, db, address, pnl, stake_usd=10.0, market="0xmarket1"):
        signal_id = db.insert_copy_signal(
            address=address, market=market, source_price=0.45,
            detected_at="2026-09-19T00:00:00+00:00",
        )
        position_id = db.insert_copy_position(
            signal_id=signal_id, address=address, market=market, outcome_index=0,
            entry_price=0.45, stake_usd=stake_usd, entry_ts="2026-09-19T00:00:00+00:00",
        )
        db.settle_copy_position(position_id, pnl, "2026-09-20T00:00:00+00:00")
        return position_id

    def test_returns_settled_rows_for_the_given_wallet(self):
        db = _db()
        self._settled_position(db, "0xaaa", 10.0, stake_usd=20.0)
        self._settled_position(db, "0xaaa", -5.0, stake_usd=10.0)

        rows = db.get_settled_copy_positions("0xaaa")

        assert len(rows) == 2
        pnls = {row["settled_pnl_usd"] for row in rows}
        assert pnls == {10.0, -5.0}
        for row in rows:
            assert row["entry_price"] == 0.45
            assert row["stake_usd"] in (20.0, 10.0)

    def test_excludes_other_wallets(self):
        db = _db()
        self._settled_position(db, "0xaaa", 10.0)
        self._settled_position(db, "0xbbb", 5.0)

        rows = db.get_settled_copy_positions("0xaaa")

        assert len(rows) == 1
        assert rows[0]["address"] == "0xaaa"

    def test_excludes_open_unsettled_positions(self):
        db = _db()
        self._settled_position(db, "0xaaa", 10.0)
        signal_id = db.insert_copy_signal(
            address="0xaaa", market="0xopenmarket", source_price=0.45,
            detected_at="2026-09-19T00:00:00+00:00",
        )
        db.insert_copy_position(
            signal_id=signal_id, address="0xaaa", market="0xopenmarket",
            outcome_index=0, entry_price=0.45, stake_usd=10.0,
            entry_ts="2026-09-19T00:00:00+00:00",
        )

        rows = db.get_settled_copy_positions("0xaaa")

        assert len(rows) == 1
        assert all(row["status"] == "settled" for row in rows)

    def test_unknown_address_returns_empty_list(self):
        db = _db()
        assert db.get_settled_copy_positions("0xunknown") == []

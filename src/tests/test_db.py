"""Unit tests for src/data/db.py.

All tests use an in-memory SQLite database (':memory:') to avoid file I/O,
except test_wal_mode and the busy-timeout tests below, which require a real
file (WAL and cross-connection lock contention are both no-ops for
':memory:' -- each ':memory:' connection is its own private database).
"""
import os
import re
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from src.data.db import (
    Database,
    _BUSY_TIMEOUT_SECONDS,
    _UPSERT_RETRY_ATTEMPTS,
    _lock_drop_aggregator,
    connect_with_busy_timeout,
)


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
# Busy timeout (issue #1232 -- requires a real file, same reason as WAL above:
# cross-connection lock contention is meaningless against ':memory:', where
# every connection is its own private database)
# ---------------------------------------------------------------------------

class TestBusyTimeout:
    """Database() sets an explicit busy timeout above sqlite3's 5s default
    (issue #1232) so a writer that finds the write lock already held waits
    instead of immediately raising ``database is locked``.
    """

    def test_busy_timeout_pragma_readback(self):
        """PRAGMA busy_timeout must read back the configured value (in ms)."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            path = tf.name
        db = None
        try:
            db = Database(path)
            cur = db._conn.execute("PRAGMA busy_timeout")
            timeout_ms = cur.fetchone()[0]
            assert timeout_ms == _BUSY_TIMEOUT_SECONDS * 1000, (
                f"Expected {_BUSY_TIMEOUT_SECONDS * 1000}ms, got {timeout_ms}ms"
            )
        finally:
            if db is not None:
                db.close()
            os.unlink(path)
            for ext in ("-wal", "-shm"):
                try:
                    os.unlink(path + ext)
                except FileNotFoundError:
                    pass

    def test_database_locked_regression(self):
        """Regression test for the 2026-09-27 copy-screening failure
        (``sqlite3.OperationalError: database is locked``, issue #1232).

        A second, independent connection to the same file holds an
        IMMEDIATE write transaction for HOLD_SECONDS -- longer than
        sqlite3's old 5s default busy timeout. Meanwhile, a write through a
        ``Database()`` instance against the same file must NOT raise: it
        must wait for the lock to free and then succeed. This is the exact
        failure mode from the 2026-09-27 run (screening died mid-run
        because a write raised instead of waiting).
        """
        # Comfortably longer than the old 5s default and comfortably
        # shorter than the new 30s timeout -- long enough to prove real
        # waiting happened, short enough to keep the suite fast.
        HOLD_SECONDS = 6.0
        assert 5.0 < HOLD_SECONDS < _BUSY_TIMEOUT_SECONDS

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            path = tf.name
        db = None
        blocker_conn = None
        lock_acquired = threading.Event()
        release_lock = threading.Event()

        def _hold_write_lock():
            nonlocal blocker_conn
            blocker_conn = sqlite3.connect(path, timeout=30)
            # A real write (not just BEGIN IMMEDIATE) against an existing
            # table, so this is genuinely the same kind of write-lock
            # contention a second Database() writer would hit in production.
            blocker_conn.execute("BEGIN IMMEDIATE")
            blocker_conn.execute(
                "INSERT INTO risk_state(trade_date, daily_pnl, open_positions, updated_at) "
                "VALUES ('1999-01-01', 0, 0, '')"
            )
            lock_acquired.set()
            release_lock.wait(timeout=HOLD_SECONDS + 10)
            blocker_conn.rollback()

        try:
            db = Database(path)  # creates schema; blocker writes into it below

            blocker = threading.Thread(target=_hold_write_lock)
            blocker.start()
            assert lock_acquired.wait(timeout=5), "Blocker thread never acquired the write lock"

            def _release_after_hold():
                time.sleep(HOLD_SECONDS)
                release_lock.set()

            releaser = threading.Thread(target=_release_after_hold)
            releaser.start()

            started = time.monotonic()
            # Contends with the blocker's held write lock. With the old 5s
            # default this raises `database is locked` well before
            # HOLD_SECONDS elapses; with the new 30s busy timeout it must
            # wait for the blocker to release and then succeed.
            db.insert_observation(
                ts="2024-01-15T12:00:00+00:00",
                station="KORD",
                temp_f=32.0,
                temp_native=0.0,
                unit="C",
                source="metar",
            )
            elapsed = time.monotonic() - started

            releaser.join(timeout=5)
            blocker.join(timeout=5)

            assert elapsed >= HOLD_SECONDS - 0.5, (
                f"Write returned after {elapsed:.2f}s -- expected it to wait "
                f"out the {HOLD_SECONDS}s held write lock, not succeed "
                "immediately (no real contention was exercised)"
            )

            cur = db._conn.execute(
                "SELECT COUNT(*) FROM observations WHERE station='KORD'"
            )
            assert cur.fetchone()[0] == 1
        finally:
            release_lock.set()
            if blocker_conn is not None:
                try:
                    blocker_conn.close()
                except Exception:
                    pass
            if db is not None:
                db.close()
            # Windows can briefly hold the file/WAL side-cars open after
            # close() returns (this test uses two real connections against
            # the same file, unlike the single-connection tests elsewhere
            # in this module) -- retry teardown rather than fail the test
            # on an unrelated cleanup race.
            for fname in (path, path + "-wal", path + "-shm"):
                for _ in range(20):
                    try:
                        os.unlink(fname)
                        break
                    except FileNotFoundError:
                        break
                    except PermissionError:
                        time.sleep(0.1)


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
# upsert_intraday_correction -- lock-timeout retry and aggregation (#1238)
# ---------------------------------------------------------------------------

def _install_flaky_execute(db, *, fail_times, exc=None):
    """Replace ``db._conn`` with a wrapper whose ``.execute()`` raises *exc*
    for the first *fail_times* calls, then delegates to the real connection.

    Returns the mock so callers can assert on ``execute.call_count``.
    """
    real_conn = db._conn
    real_execute = real_conn.execute
    if exc is None:
        exc = sqlite3.OperationalError("database is locked")
    calls = {"n": 0}

    def _side_effect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise exc
        return real_execute(*args, **kwargs)

    wrapped = mock.MagicMock(wraps=real_conn)
    wrapped.execute.side_effect = _side_effect
    db._conn = wrapped
    return wrapped


_CORRECTION_KWARGS = dict(
    city="Tokyo",
    date="2024-01-15",
    obs_time="2024-01-15T09:00:00+00:00",
    obs_temp_f=68.0,
    model_temp_f=65.0,
    delta_f=2.4,
    corrected_mu_f=82.4,
    decay_factor=0.8,
)


class TestUpsertIntradayCorrectionLockHandling:
    """A lock timeout on this write must retry, then drop (never raise) --
    issue #1238. Before this fix, ``sqlite3.OperationalError: database is
    locked`` propagated out of this call, up through ``compute_correction()``
    and ``_build_one_station()``, and aborted the entire poll.
    """

    def test_retries_and_succeeds_once_lock_clears(self):
        """One transient lock failure, then the lock clears -- must retry and
        persist the row rather than giving up after the first failure."""
        db = _db()
        assert _UPSERT_RETRY_ATTEMPTS >= 2, (
            "test assumes at least one retry is configured"
        )
        real_conn = db._conn
        mocked = _install_flaky_execute(db, fail_times=1)

        with mock.patch("src.data.db.time.sleep") as mocked_sleep:
            db.upsert_intraday_correction(**_CORRECTION_KWARGS)
        mocked_sleep.assert_called_once()  # slept before the retry, not busy-looped
        assert mocked.execute.call_count == 2  # 1 failed attempt + 1 successful retry

        db._conn = real_conn  # read back through the real connection, not the mock
        rows = db.get_intraday_corrections("Tokyo", "2024-01-15")
        assert len(rows) == 1, "Write must have succeeded on retry"
        assert rows[0]["corrected_mu_f"] == pytest.approx(82.4)

    def test_does_not_raise_when_lock_persists(self, caplog):
        """Every attempt hits 'database is locked' -- the write is dropped,
        not raised, and the drop is logged."""
        db = _db()
        real_conn = db._conn
        _install_flaky_execute(db, fail_times=_UPSERT_RETRY_ATTEMPTS + 5)
        # Unique city -- _lock_drop_aggregator's counters are keyed by context
        # string and persist for the process, so a city reused across tests
        # would not start at occurrence #1.
        kwargs = {**_CORRECTION_KWARGS, "city": "LockPersistsCity"}

        with mock.patch("src.data.db.time.sleep"):
            with caplog.at_level("WARNING"):
                db.upsert_intraday_correction(**kwargs)  # must not raise

        db._conn = real_conn  # read back through the real connection, not the mock
        rows = db.get_intraday_corrections("LockPersistsCity", "2024-01-15")
        assert rows == [], "Dropped write must not have persisted a row"
        assert any(
            "database is locked" in r.message or "database is locked" in str(r.msg)
            for r in caplog.records
        )

    def test_other_operational_errors_still_raise(self):
        """Only 'database is locked' is swallowed -- any other OperationalError
        (e.g. a genuine schema/syntax problem) must propagate normally."""
        db = _db()
        _install_flaky_execute(
            db, fail_times=1, exc=sqlite3.OperationalError("no such table: bogus")
        )
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            db.upsert_intraday_correction(**_CORRECTION_KWARGS)

    def test_non_lock_exceptions_are_not_retried(self):
        """A non-OperationalError must propagate immediately, with no retry."""
        db = _db()
        mocked = _install_flaky_execute(db, fail_times=99, exc=ValueError("boom"))
        with pytest.raises(ValueError, match="boom"):
            db.upsert_intraday_correction(**_CORRECTION_KWARGS)
        assert mocked.execute.call_count == 1  # no retry attempted

    def test_repeated_lock_failures_are_aggregated_not_logged_every_time(self, caplog):
        """249 identical lock-drops in one day (the pre-fix 09-26 log count)
        must not produce 249 WARNING lines -- issue #1238's acceptance
        criterion that the outcome is "a counted, aggregated warning, not
        anonymous lines"."""
        db = _db()
        _install_flaky_execute(db, fail_times=10_000)  # always locked
        # Unique city -- see test_does_not_raise_when_lock_persists for why.
        kwargs = {**_CORRECTION_KWARGS, "city": "AggregationCity"}

        n_occurrences = 60
        with mock.patch("src.data.db.time.sleep"):
            with caplog.at_level("WARNING"):
                for i in range(n_occurrences):
                    db.upsert_intraday_correction(
                        **{**kwargs, "obs_time": f"2024-01-15T09:{i:02d}:00+00:00"}
                    )

        lock_warnings = [r for r in caplog.records if "database is locked" in r.message]
        assert 0 < len(lock_warnings) < n_occurrences, (
            f"Expected aggregated logging (far fewer than {n_occurrences} "
            f"WARNING lines for {n_occurrences} occurrences), got "
            f"{len(lock_warnings)}"
        )
        # First occurrence is always surfaced immediately -- never silent.
        assert "occurrence #1 " in lock_warnings[0].message


# ---------------------------------------------------------------------------
# connect_with_busy_timeout -- centralised connection helper (#1238)
# ---------------------------------------------------------------------------

class TestConnectWithBusyTimeout:
    """Every first-party writer against meteoedge.db must open its connection
    through this one function so the busy timeout can never drift between
    call sites (issue #1238)."""

    def test_sets_busy_timeout_pragma(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            path = tf.name
        conn = None
        try:
            conn = connect_with_busy_timeout(path)
            timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            assert timeout_ms == _BUSY_TIMEOUT_SECONDS * 1000
        finally:
            if conn is not None:
                conn.close()
            os.unlink(path)
            for ext in ("-wal", "-shm"):
                try:
                    os.unlink(path + ext)
                except FileNotFoundError:
                    pass

    def test_database_class_uses_the_shared_helper(self):
        """Database.__init__ must not configure its own, independent busy
        timeout -- regression guard for the two code paths drifting apart."""
        import inspect

        source = inspect.getsource(Database.__init__)
        assert "connect_with_busy_timeout" in source, (
            "Database.__init__ must open its connection via "
            "connect_with_busy_timeout(), not a separate sqlite3.connect() call"
        )

    @pytest.mark.integration
    def test_second_writer_waits_rather_than_raising(self):
        """Two independent connect_with_busy_timeout() connections to the same
        file: one holds a write transaction, the other must wait for it and
        succeed rather than raising 'database is locked' (issue #1238's
        required integration test)."""
        HOLD_SECONDS = 2.0
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            path = tf.name
        blocker_conn = None
        waiter_conn = None
        lock_acquired = threading.Event()
        release_lock = threading.Event()
        try:
            setup_conn = connect_with_busy_timeout(path)
            setup_conn.execute("CREATE TABLE t(x INTEGER)")
            setup_conn.commit()
            setup_conn.close()

            def _hold_write_lock():
                nonlocal blocker_conn
                blocker_conn = connect_with_busy_timeout(path)
                blocker_conn.execute("BEGIN IMMEDIATE")
                blocker_conn.execute("INSERT INTO t VALUES (1)")
                lock_acquired.set()
                release_lock.wait(timeout=HOLD_SECONDS + 10)
                blocker_conn.rollback()

            blocker = threading.Thread(target=_hold_write_lock)
            blocker.start()
            assert lock_acquired.wait(timeout=5)

            def _release_after_hold():
                time.sleep(HOLD_SECONDS)
                release_lock.set()

            releaser = threading.Thread(target=_release_after_hold)
            releaser.start()

            waiter_conn = connect_with_busy_timeout(path)
            started = time.monotonic()
            waiter_conn.execute("INSERT INTO t VALUES (2)")  # must wait, not raise
            waiter_conn.commit()
            elapsed = time.monotonic() - started

            releaser.join(timeout=5)
            blocker.join(timeout=5)

            assert elapsed >= HOLD_SECONDS - 0.5, (
                "Write returned before the held lock was released -- no real "
                "contention was exercised"
            )
            count = waiter_conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
            assert count == 1  # only the waiter's row; blocker rolled back
        finally:
            release_lock.set()
            for conn in (blocker_conn, waiter_conn):
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass
            # Windows sometimes keeps a brief handle on a just-closed WAL-mode
            # file; best-effort cleanup, not part of what this test verifies.
            for target in (path, path + "-wal", path + "-shm"):
                try:
                    os.unlink(target)
                except OSError:
                    pass


class TestEveryScheduledWriterUsesTheSharedBusyTimeout:
    """Issue #1238 acceptance criterion: "the connection's busy_timeout is the
    intended value on every code path that opens the database (a test that
    fails if a new connection site forgets it)".

    Scans every systemd-timer/service-driven script (the unattended writers
    that can actually contend on meteoedge.db -- as opposed to the one-off,
    human-run backfill/repair scripts) for a raw, read-write
    ``sqlite3.connect()`` call that bypasses ``connect_with_busy_timeout()``.
    ``purge_retention.py`` was exactly this (issue #1238's root cause for the
    01:00-03:00 clustering); this guards against a new one appearing.
    """

    _REPO_ROOT = Path(__file__).resolve().parents[2]
    _SYSTEMD_DIR = _REPO_ROOT / "deploy" / "systemd"
    _EXEC_START_RE = re.compile(r"ExecStart=\S*python3?\s+-u\s+(?:-m\s+)?(\S+)")
    # cryptoedge is a separate subsystem with its own database file
    # (cryptoedge/db.py) -- not a writer against meteoedge.db.
    _EXCLUDED_PREFIXES = ("cryptoedge",)

    def _service_source_files(self) -> "list[Path]":
        files = []
        for svc in sorted(self._SYSTEMD_DIR.glob("*.service")):
            m = self._EXEC_START_RE.search(svc.read_text(encoding="utf-8"))
            if not m:
                continue
            target = m.group(1)
            if target.startswith(self._EXCLUDED_PREFIXES):
                continue
            rel = target.split("MeteoEdge/")[-1] if target.endswith(".py") else target.replace(".", "/") + ".py"
            path = self._REPO_ROOT / rel
            if path.exists():
                files.append(path)
        return files

    def test_systemd_scan_finds_the_known_services(self):
        """Sanity check on the scan itself -- if this starts failing, the
        regex or directory stopped matching deploy/systemd/*.service and the
        real assertion below would silently pass on zero files."""
        found = {p.name for p in self._service_source_files()}
        assert "purge_retention.py" in found
        assert "run.py" in found

    def test_no_unprotected_raw_connect_site(self):
        # Deliberately NOT exempting a whole file just because it mentions
        # connect_with_busy_timeout() somewhere (e.g. in an import or a
        # comment) -- that would pass even if the actual connect() call on
        # the very next line were still the raw, unprotected form. Every
        # individual `sqlite3.connect(` call site must itself be the one
        # inside connect_with_busy_timeout()'s own definition (not reachable
        # here, since none of these scripts are db.py) or read-only.
        offenders = []
        for path in self._service_source_files():
            text = path.read_text(encoding="utf-8")
            if "sqlite3.connect(" not in text:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if "sqlite3.connect(" not in stripped:
                    continue
                if stripped.startswith("#"):
                    continue  # prose referencing the pattern, not a call site
                if "mode=ro" in line or "mode%3Dro" in line:
                    continue  # read-only: WAL readers never block on writers
                offenders.append(f"{path.relative_to(self._REPO_ROOT)}:{lineno}: {stripped}")
        assert offenders == [], (
            "Found a systemd-scheduled script with a raw, read-write "
            "sqlite3.connect() call that doesn't use connect_with_busy_timeout() "
            f"(issue #1238):\n" + "\n".join(offenders)
        )


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
# copy_live_positions stuck-position columns (issue #1391)
# ---------------------------------------------------------------------------

_STUCK_LIVE_COLS = ("stuck_level", "stuck_category", "stuck_first_seen_at")


def _table_info(conn, table: str) -> dict:
    """Map column name -> (type, notnull, default, pk) for ``table``."""
    return {
        row[1]: (row[2], row[3], row[4], row[5])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


class TestCopyLiveStuckColumns:
    """Issue #1391: additive, idempotent stuck_* columns on copy_live_positions."""

    def test_fresh_schema_has_columns_with_defaults(self):
        info = _table_info(_db()._conn, "copy_live_positions")
        assert info["stuck_level"][:2] == ("INTEGER", 1)
        assert info["stuck_level"][2] == "0"
        assert info["stuck_category"][:2] == ("TEXT", 0)
        assert info["stuck_first_seen_at"][:2] == ("TEXT", 0)

    def test_migration_idempotent_when_opened_twice(self, tmp_path):
        path = str(tmp_path / "live_twice.db")
        Database(path).close()
        db = Database(path)
        try:
            info = _table_info(db._conn, "copy_live_positions")
            for col in _STUCK_LIVE_COLS:
                assert col in info
        finally:
            db.close()

    def test_upgraded_old_schema_gains_columns_and_keeps_rows(self, tmp_path):
        path = str(tmp_path / "live_old.db")
        # Hand-built pre-#1391 copy_live_positions (no stuck_* columns).
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE copy_live_positions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id        INTEGER NOT NULL REFERENCES copy_signals(id),
                address          TEXT NOT NULL,
                market           TEXT NOT NULL,
                outcome_index    INTEGER NOT NULL CHECK(outcome_index IN (0,1)),
                order_id         TEXT,
                fill_price       REAL CHECK(fill_price IS NULL OR (fill_price >= 0 AND fill_price <= 1)),
                stake_usd        REAL NOT NULL CHECK(stake_usd > 0),
                status           TEXT NOT NULL DEFAULT 'pending'
                                 CHECK(status IN ('pending','filled','partial','rejected','settled')),
                rejected_reason  TEXT,
                entry_ts         TEXT NOT NULL,
                settled_pnl_usd  REAL,
                settled_at       TEXT
            )
        """)
        conn.execute(
            "INSERT INTO copy_live_positions (signal_id, address, market, outcome_index, "
            "order_id, fill_price, stake_usd, status, entry_ts) "
            "VALUES (1, '0xabc', 'm1', 0, 'ord-1', 0.4, 5.0, 'filled', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
        conn.close()

        db = Database(path)
        try:
            info = _table_info(db._conn, "copy_live_positions")
            for col in _STUCK_LIVE_COLS:
                assert col in info
            assert info["stuck_level"][1] == 1
            assert info["stuck_level"][2] == "0"
            row = db._conn.execute(
                "SELECT order_id, status, stuck_level, stuck_category, stuck_first_seen_at "
                "FROM copy_live_positions"
            ).fetchone()
            assert tuple(row) == ("ord-1", "filled", 0, None, None)
        finally:
            db.close()

    def test_upgraded_stuck_columns_match_fresh_definition(self, tmp_path):
        fresh_path = str(tmp_path / "live_fresh.db")
        Database(fresh_path).close()
        conn = sqlite3.connect(fresh_path)
        try:
            fresh = _table_info(conn, "copy_live_positions")
        finally:
            conn.close()

        old_path = str(tmp_path / "live_upgraded.db")
        conn = sqlite3.connect(old_path)
        conn.execute(
            "CREATE TABLE copy_live_positions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "signal_id INTEGER NOT NULL, address TEXT NOT NULL, market TEXT NOT NULL, "
            "outcome_index INTEGER NOT NULL, order_id TEXT, fill_price REAL, "
            "stake_usd REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
            "rejected_reason TEXT, entry_ts TEXT NOT NULL, settled_pnl_usd REAL, "
            "settled_at TEXT)"
        )
        conn.commit()
        conn.close()
        Database(old_path).close()
        conn = sqlite3.connect(old_path)
        try:
            upgraded = _table_info(conn, "copy_live_positions")
        finally:
            conn.close()

        for col in _STUCK_LIVE_COLS:
            assert upgraded[col] == fresh[col]


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

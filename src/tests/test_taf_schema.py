"""Unit tests for TAF windows schema and database methods."""
import tempfile

import pytest

from src.data.db import Database


def _db() -> Database:
    """Return a fresh in-memory Database instance."""
    return Database(":memory:")


class TestTafWindowsSchema:
    """taf_windows table must be created correctly."""

    def test_taf_windows_table_exists(self):
        """Verify taf_windows table is created during Database initialization."""
        db = _db()
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='taf_windows'"
        )
        assert cur.fetchone() is not None, "taf_windows table not found"

    def test_taf_windows_index_exists(self):
        """Verify idx_taf_city_from index is created."""
        db = _db()
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_taf_city_from'"
        )
        assert cur.fetchone() is not None, "idx_taf_city_from index not found"

    def test_schema_idempotent(self):
        """Creating schema twice does not raise an error."""
        db = _db()
        # Re-run DDL initialization (simulated by re-executing the CREATE statements)
        # This should not raise an error due to IF NOT EXISTS
        with db._conn:
            db._conn.execute(
                "CREATE TABLE IF NOT EXISTS taf_windows ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "city TEXT NOT NULL, issued_at TEXT NOT NULL, "
                "valid_from TEXT NOT NULL, valid_to TEXT NOT NULL, "
                "group_type TEXT NOT NULL, temp REAL, wind_kt REAL, "
                "sig_wx TEXT, raw_text TEXT)"
            )
            db._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_taf_city_from ON taf_windows(city, valid_from)"
            )


class TestInsertTafWindow:
    """insert_taf_window and get_taf_windows round-trip."""

    def test_insert_taf_window_round_trip(self):
        """Insert a TAF window and retrieve it."""
        db = _db()
        row = {
            "city": "KORD",
            "issued_at": "2024-06-07T12:00:00+00:00",
            "valid_from": "2024-06-07T13:00:00+00:00",
            "valid_to": "2024-06-07T19:00:00+00:00",
            "group_type": "Temporary Fluctuation",
            "temp": 28.5,
            "wind_kt": 15.0,
            "sig_wx": "TS",
            "raw_text": "TEMPO 1318/1919 3SM TS OVC025CB",
        }
        db.insert_taf_window(row)

        results = db.get_taf_windows(
            "KORD",
            "2024-06-07T00:00:00+00:00",
            "2024-06-08T00:00:00+00:00",
        )
        assert len(results) == 1
        r = results[0]
        assert r["city"] == "KORD"
        assert r["issued_at"] == "2024-06-07T12:00:00+00:00"
        assert r["valid_from"] == "2024-06-07T13:00:00+00:00"
        assert r["valid_to"] == "2024-06-07T19:00:00+00:00"
        assert r["group_type"] == "Temporary Fluctuation"
        assert r["temp"] == pytest.approx(28.5)
        assert r["wind_kt"] == pytest.approx(15.0)
        assert r["sig_wx"] == "TS"
        assert r["raw_text"] == "TEMPO 1318/1919 3SM TS OVC025CB"

    def test_get_taf_windows_filters_by_city(self):
        """get_taf_windows returns only records for the specified city."""
        db = _db()
        db.insert_taf_window({
            "city": "KORD",
            "issued_at": "2024-06-07T12:00:00+00:00",
            "valid_from": "2024-06-07T13:00:00+00:00",
            "valid_to": "2024-06-07T19:00:00+00:00",
            "group_type": "Base Period",
            "temp": 28.0,
            "wind_kt": 10.0,
            "sig_wx": None,
            "raw_text": None,
        })
        db.insert_taf_window({
            "city": "KJFK",
            "issued_at": "2024-06-07T12:00:00+00:00",
            "valid_from": "2024-06-07T13:00:00+00:00",
            "valid_to": "2024-06-07T19:00:00+00:00",
            "group_type": "Base Period",
            "temp": 25.0,
            "wind_kt": 12.0,
            "sig_wx": None,
            "raw_text": None,
        })

        kord_results = db.get_taf_windows(
            "KORD",
            "2024-06-07T00:00:00+00:00",
            "2024-06-08T00:00:00+00:00",
        )
        assert len(kord_results) == 1
        assert kord_results[0]["city"] == "KORD"

        kjfk_results = db.get_taf_windows(
            "KJFK",
            "2024-06-07T00:00:00+00:00",
            "2024-06-08T00:00:00+00:00",
        )
        assert len(kjfk_results) == 1
        assert kjfk_results[0]["city"] == "KJFK"

    def test_get_taf_windows_filters_by_time_range(self):
        """get_taf_windows filters by valid_from >= from_ts and valid_to <= to_ts."""
        db = _db()
        # Window 1: fully within range
        db.insert_taf_window({
            "city": "KORD",
            "issued_at": "2024-06-07T12:00:00+00:00",
            "valid_from": "2024-06-07T13:00:00+00:00",
            "valid_to": "2024-06-07T16:00:00+00:00",
            "group_type": "Base Period",
            "temp": 28.0,
            "wind_kt": 10.0,
            "sig_wx": None,
            "raw_text": None,
        })
        # Window 2: before range
        db.insert_taf_window({
            "city": "KORD",
            "issued_at": "2024-06-07T12:00:00+00:00",
            "valid_from": "2024-06-06T13:00:00+00:00",
            "valid_to": "2024-06-06T19:00:00+00:00",
            "group_type": "Base Period",
            "temp": 25.0,
            "wind_kt": 8.0,
            "sig_wx": None,
            "raw_text": None,
        })
        # Window 3: after range
        db.insert_taf_window({
            "city": "KORD",
            "issued_at": "2024-06-07T12:00:00+00:00",
            "valid_from": "2024-06-08T13:00:00+00:00",
            "valid_to": "2024-06-08T19:00:00+00:00",
            "group_type": "Base Period",
            "temp": 30.0,
            "wind_kt": 12.0,
            "sig_wx": None,
            "raw_text": None,
        })

        results = db.get_taf_windows(
            "KORD",
            "2024-06-07T00:00:00+00:00",
            "2024-06-07T20:00:00+00:00",
        )
        assert len(results) == 1
        assert results[0]["valid_from"] == "2024-06-07T13:00:00+00:00"

    def test_get_taf_windows_orders_by_valid_from(self):
        """get_taf_windows returns results ordered by valid_from ascending."""
        db = _db()
        db.insert_taf_window({
            "city": "KORD",
            "issued_at": "2024-06-07T12:00:00+00:00",
            "valid_from": "2024-06-07T19:00:00+00:00",
            "valid_to": "2024-06-08T01:00:00+00:00",
            "group_type": "Base Period",
            "temp": 20.0,
            "wind_kt": 5.0,
            "sig_wx": None,
            "raw_text": None,
        })
        db.insert_taf_window({
            "city": "KORD",
            "issued_at": "2024-06-07T12:00:00+00:00",
            "valid_from": "2024-06-07T13:00:00+00:00",
            "valid_to": "2024-06-07T19:00:00+00:00",
            "group_type": "Base Period",
            "temp": 28.0,
            "wind_kt": 10.0,
            "sig_wx": None,
            "raw_text": None,
        })

        results = db.get_taf_windows(
            "KORD",
            "2024-06-07T00:00:00+00:00",
            "2024-06-08T02:00:00+00:00",
        )
        assert len(results) == 2
        assert results[0]["valid_from"] == "2024-06-07T13:00:00+00:00"
        assert results[1]["valid_from"] == "2024-06-07T19:00:00+00:00"

    def test_get_taf_windows_empty_result(self):
        """get_taf_windows returns empty list when no matching records."""
        db = _db()
        results = db.get_taf_windows(
            "KORD",
            "2024-06-07T00:00:00+00:00",
            "2024-06-08T00:00:00+00:00",
        )
        assert results == []

    def test_insert_taf_window_with_nulls(self):
        """insert_taf_window handles NULL values for optional fields."""
        db = _db()
        row = {
            "city": "KJFK",
            "issued_at": "2024-06-07T12:00:00+00:00",
            "valid_from": "2024-06-07T13:00:00+00:00",
            "valid_to": "2024-06-07T19:00:00+00:00",
            "group_type": "Base Period",
            "temp": None,
            "wind_kt": None,
            "sig_wx": None,
            "raw_text": None,
        }
        db.insert_taf_window(row)

        results = db.get_taf_windows(
            "KJFK",
            "2024-06-07T00:00:00+00:00",
            "2024-06-08T00:00:00+00:00",
        )
        assert len(results) == 1
        r = results[0]
        assert r["temp"] is None
        assert r["wind_kt"] is None
        assert r["sig_wx"] is None
        assert r["raw_text"] is None

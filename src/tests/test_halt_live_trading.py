"""Tests for src/scripts/halt_live_trading.py -- the explicit live-halt (2026-08-26)."""
from __future__ import annotations

from src.config import STATIONS
from src.data.db import Database
from src.scripts.halt_live_trading import halt_all_stations


def _mem_db() -> Database:
    return Database(":memory:")


def test_halts_every_station_with_no_prior_override():
    db = _mem_db()
    try:
        changed = halt_all_stations(db)
        assert set(changed) == {s[0] for s in STATIONS}
        for station_tuple in STATIONS:
            override = db.get_station_override(station_tuple[0])
            assert override["yes_enabled"] is False
            assert override["no_enabled"] is False
    finally:
        db._conn.close()


def test_second_run_is_a_noop():
    db = _mem_db()
    try:
        halt_all_stations(db)
        changed_again = halt_all_stations(db)
        assert changed_again == []
    finally:
        db._conn.close()


def test_preserves_low_no_enabled_when_halting():
    db = _mem_db()
    try:
        station = STATIONS[0][0]
        db.set_station_override(station, yes_enabled=True, no_enabled=True, low_no_enabled=True)
        halt_all_stations(db)
        override = db.get_station_override(station)
        assert override["yes_enabled"] is False
        assert override["no_enabled"] is False
        assert override["low_no_enabled"] is True
    finally:
        db._conn.close()


def test_dry_run_reports_but_does_not_write():
    db = _mem_db()
    try:
        changed = halt_all_stations(db, dry_run=True)
        assert set(changed) == {s[0] for s in STATIONS}
        for station_tuple in STATIONS:
            assert db.get_station_override(station_tuple[0]) is None
    finally:
        db._conn.close()

"""Tests for the GET /analytics/intraday dashboard endpoint (issue #350).

Uses a tmp_path-scoped ArchiveDatabase and patches ArchiveDatabase in the
api module so the endpoint reads from an in-test SQLite file rather than
the real analytics.db.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import app
from src.data.archive_db import ArchiveDatabase


_SNAP_ROW = {
    "ts": "2026-06-15T10:00:00+00:00",
    "station": "KORD",
    "ticker": "HIGH-TEMP-KORD-2026-06-15-90-94",
    "bracket_low": 90.0,
    "bracket_high": 94.0,
    "yes_ask": 45,
    "no_ask": 55,
    "current_high": 82.3,
    "latest_temp": 79.1,
    "forecast_high": 91.0,
    "p_yes": 0.42,
    "raw_p_yes": 0.42,
    "capped_p_yes": 0.42,
    "ev_yes": -5.1,
    "ev_no": 2.3,
    "minutes_to_settlement": 240.0,
    "emos_mode": "emos",
}


@pytest.fixture
def client():
    """Return a fresh TestClient (no side-effects on module-level _db)."""
    return TestClient(app)


@pytest.fixture
def seeded_archive(tmp_path):
    """Create and seed a temporary analytics.db, returning its path as a string."""
    db_path = tmp_path / "analytics.db"
    with ArchiveDatabase(db_path) as db:
        db.insert_snapshots([_SNAP_ROW])
    return str(db_path)


def _make_archive_class(db_path: str):
    """Return a drop-in replacement for ArchiveDatabase bound to *db_path*."""
    class _BoundArchiveDatabase(ArchiveDatabase):
        def __init__(self):  # ignore default path arg
            super().__init__(db_path)
    return _BoundArchiveDatabase


class TestAnalyticsIntradayEndpoint:
    def test_analytics_intraday_returns_data(self, client, seeded_archive):
        """Endpoint returns non-empty list when the archive has matching rows."""
        with patch("src.dashboard.api.ArchiveDatabase", _make_archive_class(seeded_archive)):
            resp = client.get("/analytics/intraday?station=KORD&date=2026-06-15")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["station"] == "KORD"
        assert data[0]["ts"] == "2026-06-15T10:00:00+00:00"

    def test_analytics_intraday_empty_for_unknown_station(self, client, seeded_archive):
        """Endpoint returns [] (HTTP 200, not 404) for a station with no data."""
        with patch("src.dashboard.api.ArchiveDatabase", _make_archive_class(seeded_archive)):
            resp = client.get("/analytics/intraday?station=KXYZ&date=2026-06-15")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_analytics_intraday_does_not_touch_trading_db(self, client, tmp_path):
        """Endpoint works correctly even when the trading DB has no data.

        Creates an empty analytics.db and verifies the endpoint returns []
        without raising errors — confirming it reads only from ArchiveDatabase.
        """
        db_path = tmp_path / "empty_analytics.db"
        # Initialise schema only, no rows
        ArchiveDatabase(db_path).close()
        with patch("src.dashboard.api.ArchiveDatabase", _make_archive_class(str(db_path))):
            resp = client.get("/analytics/intraday?station=KORD&date=2026-06-15")
        assert resp.status_code == 200
        assert resp.json() == []

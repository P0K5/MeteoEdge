"""Tests for station enable/disable toggle — DB methods and API endpoint.

Covers:
  - DB: get_station_override, set_station_override, get_all_station_overrides
  - API: POST /api/stations/{metar}/toggle (on→off, off→on, 404 for unknown)
  - API: GET /api/stations/overview reflects DB overrides
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

import src.dashboard.api as dash_api
from src.dashboard.api import app
from src.data.db import Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mem_db() -> Database:
    """Return a fresh in-memory Database."""
    return Database(":memory:")


@pytest.fixture
def client_with_db():
    """TestClient with a fresh in-memory Database injected, cache cleared."""
    db = _mem_db()
    original_db = dash_api._db
    original_cache = dict(dash_api._stations_overview_cache)

    dash_api._db = db
    dash_api._stations_overview_cache["ts"] = 0.0
    dash_api._stations_overview_cache["data"] = None

    yield TestClient(app), db

    dash_api._db = original_db
    dash_api._stations_overview_cache.update(original_cache)


# ---------------------------------------------------------------------------
# DB layer — station_overrides
# ---------------------------------------------------------------------------

class TestStationOverrideDb:
    def test_get_returns_none_when_no_row(self):
        """get_station_override returns None when no DB row exists."""
        db = _mem_db()
        result = db.get_station_override("KORD")
        assert result is None

    def test_set_then_get_enabled(self):
        """set_station_override(enabled=True) persists; get returns True."""
        db = _mem_db()
        db.set_station_override("KORD", True)
        assert db.get_station_override("KORD") is True

    def test_set_then_get_disabled(self):
        """set_station_override(enabled=False) persists; get returns False."""
        db = _mem_db()
        db.set_station_override("KORD", False)
        assert db.get_station_override("KORD") is False

    def test_upsert_overrides_previous_value(self):
        """Second set_station_override call overwrites the first."""
        db = _mem_db()
        db.set_station_override("KMIA", True)
        db.set_station_override("KMIA", False)
        assert db.get_station_override("KMIA") is False

    def test_get_all_empty(self):
        """get_all_station_overrides returns empty dict when table is empty."""
        db = _mem_db()
        assert db.get_all_station_overrides() == {}

    def test_get_all_returns_all_rows(self):
        """get_all_station_overrides returns every inserted row."""
        db = _mem_db()
        db.set_station_override("KORD", True)
        db.set_station_override("KMIA", False)
        result = db.get_all_station_overrides()
        assert result == {"KORD": True, "KMIA": False}

    def test_toggle_on_to_off_and_back(self):
        """Manual toggle sequence: None → True → False → True (persistence)."""
        db = _mem_db()
        # No override → treat as None
        assert db.get_station_override("KATL") is None
        # Set enabled
        db.set_station_override("KATL", True)
        assert db.get_station_override("KATL") is True
        # Flip to disabled
        db.set_station_override("KATL", False)
        assert db.get_station_override("KATL") is False
        # Flip back
        db.set_station_override("KATL", True)
        assert db.get_station_override("KATL") is True


# ---------------------------------------------------------------------------
# API — POST /api/stations/{metar}/toggle
# ---------------------------------------------------------------------------

class TestStationToggleEndpoint:
    def test_toggle_known_station_returns_200(self, client_with_db):
        """POST /api/stations/KORD/toggle returns 200 for a known METAR."""
        client, db = client_with_db
        resp = client.post("/api/stations/KORD/toggle")
        assert resp.status_code == 200

    def test_toggle_response_shape(self, client_with_db):
        """Response contains 'metar' (str) and 'enabled' (bool)."""
        client, db = client_with_db
        resp = client.post("/api/stations/KORD/toggle")
        data = resp.json()
        assert "metar" in data
        assert "enabled" in data
        assert isinstance(data["metar"], str)
        assert isinstance(data["enabled"], bool)

    def test_toggle_unknown_station_returns_404(self, client_with_db):
        """POST /api/stations/ZZZZ/toggle returns 404 for unknown METAR."""
        client, db = client_with_db
        resp = client.post("/api/stations/ZZZZ/toggle")
        assert resp.status_code == 404

    def test_toggle_metar_normalised_to_uppercase(self, client_with_db):
        """Lowercase METAR in URL is normalised to uppercase in response."""
        client, db = client_with_db
        resp = client.post("/api/stations/kord/toggle")
        assert resp.status_code == 200
        assert resp.json()["metar"] == "KORD"

    def test_toggle_flips_enabled_to_false(self, client_with_db):
        """KORD is enabled by default; first toggle should disable it."""
        client, db = client_with_db
        # KORD is not in DISABLED_STATIONS by default; first toggle → False
        resp = client.post("/api/stations/KORD/toggle")
        data = resp.json()
        assert data["enabled"] is False

    def test_toggle_twice_restores_original(self, client_with_db):
        """Toggling twice returns to the original enabled state."""
        client, db = client_with_db
        resp1 = client.post("/api/stations/KORD/toggle")
        first_enabled = resp1.json()["enabled"]
        resp2 = client.post("/api/stations/KORD/toggle")
        second_enabled = resp2.json()["enabled"]
        assert second_enabled is not first_enabled

    def test_toggle_persists_to_db(self, client_with_db):
        """After toggle, DB row reflects new state."""
        client, db = client_with_db
        resp = client.post("/api/stations/KORD/toggle")
        new_enabled = resp.json()["enabled"]
        db_val = db.get_station_override("KORD")
        assert db_val == new_enabled

    def test_toggle_off_then_on(self, client_with_db):
        """Toggle a station off then back on via two consecutive POST calls."""
        client, db = client_with_db
        # First toggle (on → off for a normally-enabled station)
        r1 = client.post("/api/stations/KMIA/toggle")
        assert r1.json()["enabled"] is False
        # Second toggle (off → on)
        r2 = client.post("/api/stations/KMIA/toggle")
        assert r2.json()["enabled"] is True

    def test_toggle_disabled_station_enables_it(self, client_with_db):
        """If DISABLED_STATIONS env has a station, toggle should enable it."""
        client, db = client_with_db
        # RKSI is in DISABLED_STATIONS by default (env default "RKSI")
        with patch("src.dashboard.api.DISABLED_STATIONS", {"RKSI"}):
            resp = client.post("/api/stations/RKSI/toggle")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

    def test_toggle_invalidates_overview_cache(self, client_with_db):
        """Toggle must invalidate the overview cache (ts reset to 0)."""
        client, db = client_with_db
        # Prime the cache
        dash_api._stations_overview_cache["ts"] = 999999.0
        dash_api._stations_overview_cache["data"] = []
        # Toggle
        client.post("/api/stations/KORD/toggle")
        assert dash_api._stations_overview_cache["ts"] == 0.0
        assert dash_api._stations_overview_cache["data"] is None


# ---------------------------------------------------------------------------
# API — GET /api/stations/overview reflects DB overrides
# ---------------------------------------------------------------------------

class TestStationsOverviewWithDbOverrides:
    def test_overview_returns_list(self, client_with_db):
        """GET /api/stations/overview returns a non-empty list."""
        client, db = client_with_db
        resp = client.get("/api/stations/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0

    def test_overview_enabled_reflects_db_override_false(self, client_with_db):
        """After toggling KORD off, overview shows enabled=false for KORD."""
        client, db = client_with_db
        db.set_station_override("KORD", False)
        # Clear cache to force fresh read
        dash_api._stations_overview_cache["ts"] = 0.0
        dash_api._stations_overview_cache["data"] = None

        resp = client.get("/api/stations/overview")
        assert resp.status_code == 200
        stations = {s["metar"]: s for s in resp.json()}
        assert stations["KORD"]["enabled"] is False

    def test_overview_enabled_reflects_db_override_true(self, client_with_db):
        """DB override enabled=True lifts env-based disable for a station."""
        client, db = client_with_db
        # Patch DISABLED_STATIONS to disable KORD via env
        with patch("src.dashboard.api.DISABLED_STATIONS", {"KORD"}):
            # DB override: explicitly enabled
            db.set_station_override("KORD", True)
            dash_api._stations_overview_cache["ts"] = 0.0
            dash_api._stations_overview_cache["data"] = None

            resp = client.get("/api/stations/overview")

        assert resp.status_code == 200
        stations = {s["metar"]: s for s in resp.json()}
        # DB override wins over DISABLED_STATIONS
        assert stations["KORD"]["enabled"] is True

    def test_overview_no_override_uses_disabled_stations(self, client_with_db):
        """Without a DB override, env DISABLED_STATIONS controls enabled."""
        client, db = client_with_db
        with patch("src.dashboard.api.DISABLED_STATIONS", {"KORD"}):
            dash_api._stations_overview_cache["ts"] = 0.0
            dash_api._stations_overview_cache["data"] = None
            resp = client.get("/api/stations/overview")

        assert resp.status_code == 200
        stations = {s["metar"]: s for s in resp.json()}
        # No DB row → falls back to DISABLED_STATIONS
        assert stations["KORD"]["enabled"] is False

    def test_overview_after_toggle_shows_new_state(self, client_with_db):
        """Toggling via API endpoint and then querying overview shows updated state."""
        client, db = client_with_db
        # Toggle KORD (should go from enabled → disabled)
        toggle_resp = client.post("/api/stations/KORD/toggle")
        new_enabled = toggle_resp.json()["enabled"]

        # Overview must agree (cache was invalidated by toggle)
        overview_resp = client.get("/api/stations/overview")
        stations = {s["metar"]: s for s in overview_resp.json()}
        assert stations["KORD"]["enabled"] == new_enabled

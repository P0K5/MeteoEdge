"""Tests for station enable/disable toggle — DB methods and API endpoint.

Covers:
  - DB: get_station_override, set_station_override, get_all_station_overrides
  - API: POST /api/stations/{metar}/toggle (on→off, off→on, 404 for unknown)
  - API: POST /api/stations/{metar}/toggle/yes and /toggle/no (per-side)
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

    def test_set_then_get_both_enabled(self):
        """set_station_override(yes=True, no=True) persists; get returns both True."""
        db = _mem_db()
        db.set_station_override("KORD", yes_enabled=True, no_enabled=True)
        result = db.get_station_override("KORD")
        assert result == {"yes_enabled": True, "no_enabled": True}

    def test_set_then_get_both_disabled(self):
        """set_station_override(yes=False, no=False) persists; get returns both False."""
        db = _mem_db()
        db.set_station_override("KORD", yes_enabled=False, no_enabled=False)
        result = db.get_station_override("KORD")
        assert result == {"yes_enabled": False, "no_enabled": False}

    def test_set_yes_shadow_no_live(self):
        """set_station_override(yes=False, no=True) yields YES shadow, NO live."""
        db = _mem_db()
        db.set_station_override("KORD", yes_enabled=False, no_enabled=True)
        result = db.get_station_override("KORD")
        assert result == {"yes_enabled": False, "no_enabled": True}

    def test_set_yes_live_no_shadow(self):
        """set_station_override(yes=True, no=False) yields YES live, NO shadow."""
        db = _mem_db()
        db.set_station_override("KORD", yes_enabled=True, no_enabled=False)
        result = db.get_station_override("KORD")
        assert result == {"yes_enabled": True, "no_enabled": False}

    def test_upsert_overrides_previous_value(self):
        """Second set_station_override call overwrites the first."""
        db = _mem_db()
        db.set_station_override("KMIA", yes_enabled=True, no_enabled=True)
        db.set_station_override("KMIA", yes_enabled=False, no_enabled=False)
        result = db.get_station_override("KMIA")
        assert result == {"yes_enabled": False, "no_enabled": False}

    def test_get_all_empty(self):
        """get_all_station_overrides returns empty dict when table is empty."""
        db = _mem_db()
        assert db.get_all_station_overrides() == {}

    def test_get_all_returns_all_rows(self):
        """get_all_station_overrides returns every inserted row as dicts."""
        db = _mem_db()
        db.set_station_override("KORD", yes_enabled=True, no_enabled=True)
        db.set_station_override("KMIA", yes_enabled=False, no_enabled=False)
        result = db.get_all_station_overrides()
        assert result == {
            "KORD": {"yes_enabled": True, "no_enabled": True},
            "KMIA": {"yes_enabled": False, "no_enabled": False},
        }

    def test_toggle_on_to_off_and_back(self):
        """Manual toggle sequence: None → both enabled → both disabled → both enabled."""
        db = _mem_db()
        assert db.get_station_override("KATL") is None
        db.set_station_override("KATL", yes_enabled=True, no_enabled=True)
        assert db.get_station_override("KATL") == {"yes_enabled": True, "no_enabled": True}
        db.set_station_override("KATL", yes_enabled=False, no_enabled=False)
        assert db.get_station_override("KATL") == {"yes_enabled": False, "no_enabled": False}
        db.set_station_override("KATL", yes_enabled=True, no_enabled=True)
        assert db.get_station_override("KATL") == {"yes_enabled": True, "no_enabled": True}


# ---------------------------------------------------------------------------
# API — POST /api/stations/{metar}/toggle (both sides, back-compat)
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
        """After toggle, DB row reflects new state on both sides."""
        client, db = client_with_db
        resp = client.post("/api/stations/KORD/toggle")
        new_enabled = resp.json()["enabled"]
        db_val = db.get_station_override("KORD")
        assert db_val == {"yes_enabled": new_enabled, "no_enabled": new_enabled}

    def test_toggle_off_then_on(self, client_with_db):
        """Toggle a station off then back on via two consecutive POST calls."""
        client, db = client_with_db
        r1 = client.post("/api/stations/KMIA/toggle")
        assert r1.json()["enabled"] is False
        r2 = client.post("/api/stations/KMIA/toggle")
        assert r2.json()["enabled"] is True

    def test_toggle_disabled_station_enables_it(self, client_with_db):
        """If DISABLED_STATIONS env has a station, toggle should enable it."""
        client, db = client_with_db
        with patch("src.dashboard.api.DISABLED_STATIONS", {"RKSI"}):
            resp = client.post("/api/stations/RKSI/toggle")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

    def test_toggle_invalidates_overview_cache(self, client_with_db):
        """Toggle must invalidate the overview cache (ts reset to 0)."""
        client, db = client_with_db
        dash_api._stations_overview_cache["ts"] = 999999.0
        dash_api._stations_overview_cache["data"] = []
        client.post("/api/stations/KORD/toggle")
        assert dash_api._stations_overview_cache["ts"] == 0.0
        assert dash_api._stations_overview_cache["data"] is None


# ---------------------------------------------------------------------------
# API — POST /api/stations/{metar}/toggle/yes and /toggle/no
# ---------------------------------------------------------------------------

class TestStationPerSideToggleEndpoints:
    def test_toggle_yes_returns_200(self, client_with_db):
        """POST /api/stations/KORD/toggle/yes returns 200."""
        client, db = client_with_db
        resp = client.post("/api/stations/KORD/toggle/yes")
        assert resp.status_code == 200

    def test_toggle_yes_response_shape(self, client_with_db):
        """POST /toggle/yes response includes yes_enabled and no_enabled."""
        client, db = client_with_db
        resp = client.post("/api/stations/KORD/toggle/yes")
        data = resp.json()
        assert "metar" in data
        assert "yes_enabled" in data
        assert "no_enabled" in data

    def test_toggle_yes_only_flips_yes(self, client_with_db):
        """POST /toggle/yes only changes yes_enabled; no_enabled unchanged."""
        client, db = client_with_db
        # Pre-set both sides live
        db.set_station_override("KORD", yes_enabled=True, no_enabled=True)
        # Toggle only YES
        resp = client.post("/api/stations/KORD/toggle/yes")
        data = resp.json()
        assert data["yes_enabled"] is False
        assert data["no_enabled"] is True  # unchanged

    def test_toggle_no_only_flips_no(self, client_with_db):
        """POST /toggle/no only changes no_enabled; yes_enabled unchanged."""
        client, db = client_with_db
        db.set_station_override("KORD", yes_enabled=True, no_enabled=True)
        resp = client.post("/api/stations/KORD/toggle/no")
        data = resp.json()
        assert data["yes_enabled"] is True   # unchanged
        assert data["no_enabled"] is False

    def test_toggle_yes_404_unknown_metar(self, client_with_db):
        """POST /api/stations/ZZZZ/toggle/yes returns 404."""
        client, db = client_with_db
        resp = client.post("/api/stations/ZZZZ/toggle/yes")
        assert resp.status_code == 404

    def test_toggle_no_404_unknown_metar(self, client_with_db):
        """POST /api/stations/ZZZZ/toggle/no returns 404."""
        client, db = client_with_db
        resp = client.post("/api/stations/ZZZZ/toggle/no")
        assert resp.status_code == 404

    def test_toggle_yes_invalidates_cache(self, client_with_db):
        """POST /toggle/yes invalidates the overview cache."""
        client, db = client_with_db
        dash_api._stations_overview_cache["ts"] = 999999.0
        dash_api._stations_overview_cache["data"] = []
        client.post("/api/stations/KORD/toggle/yes")
        assert dash_api._stations_overview_cache["ts"] == 0.0

    def test_toggle_no_invalidates_cache(self, client_with_db):
        """POST /toggle/no invalidates the overview cache."""
        client, db = client_with_db
        dash_api._stations_overview_cache["ts"] = 999999.0
        dash_api._stations_overview_cache["data"] = []
        client.post("/api/stations/KORD/toggle/no")
        assert dash_api._stations_overview_cache["ts"] == 0.0


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
        """After toggling KORD off (both sides), overview shows enabled=false for KORD."""
        client, db = client_with_db
        db.set_station_override("KORD", yes_enabled=False, no_enabled=False)
        dash_api._stations_overview_cache["ts"] = 0.0
        dash_api._stations_overview_cache["data"] = None

        resp = client.get("/api/stations/overview")
        assert resp.status_code == 200
        stations = {s["metar"]: s for s in resp.json()}
        assert stations["KORD"]["enabled"] is False

    def test_overview_enabled_reflects_db_override_true(self, client_with_db):
        """DB override with both enabled lifts env-based disable for a station."""
        client, db = client_with_db
        with patch("src.dashboard.api.DISABLED_STATIONS", {"KORD"}):
            db.set_station_override("KORD", yes_enabled=True, no_enabled=True)
            dash_api._stations_overview_cache["ts"] = 0.0
            dash_api._stations_overview_cache["data"] = None

            resp = client.get("/api/stations/overview")

        assert resp.status_code == 200
        stations = {s["metar"]: s for s in resp.json()}
        assert stations["KORD"]["enabled"] is True

    def test_overview_partial_enabled_counts_as_enabled(self, client_with_db):
        """Station with yes=False, no=True is counted as enabled (one side live)."""
        client, db = client_with_db
        db.set_station_override("KORD", yes_enabled=False, no_enabled=True)
        dash_api._stations_overview_cache["ts"] = 0.0
        dash_api._stations_overview_cache["data"] = None

        resp = client.get("/api/stations/overview")
        stations = {s["metar"]: s for s in resp.json()}
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
        assert stations["KORD"]["enabled"] is False

    def test_overview_after_toggle_shows_new_state(self, client_with_db):
        """Toggling via API endpoint and then querying overview shows updated state."""
        client, db = client_with_db
        toggle_resp = client.post("/api/stations/KORD/toggle")
        new_enabled = toggle_resp.json()["enabled"]

        overview_resp = client.get("/api/stations/overview")
        stations = {s["metar"]: s for s in overview_resp.json()}
        assert stations["KORD"]["enabled"] == new_enabled

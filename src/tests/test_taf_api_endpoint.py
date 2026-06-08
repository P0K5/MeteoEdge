"""Tests for GET /api/cities/{city}/taf endpoint in src/dashboard/api.py."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.data.db import Database
from src.dashboard.api import app, _db as api_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_window(
    city: str = "Tokyo",
    group_type: str = "Temporary Fluctuation",
    sig_wx: str | None = "TS",
    offset_hours: int = 1,
) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": 1,
        "city": city,
        "issued_at": now.isoformat(),
        "valid_from": (now + timedelta(hours=offset_hours)).isoformat(),
        "valid_to": (now + timedelta(hours=offset_hours + 2)).isoformat(),
        "group_type": group_type,
        "temp": None,
        "wind_kt": 10.0,
        "sig_wx": sig_wx,
        "raw_text": "",
    }


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestGetCityTaf:
    def test_returns_200_with_windows(self, client):
        """Known city with TAF windows → 200 with array."""
        window = _make_window()
        with patch.object(api_db, "get_taf_windows", return_value=[window]):
            r = client.get("/api/cities/Tokyo/taf")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["city"] == "Tokyo"

    def test_taf_disruption_true_for_tempo_ts(self, client):
        """TEMPO window with TS → taf_disruption=True in response."""
        window = _make_window(group_type="Temporary Fluctuation", sig_wx="TS")
        with patch.object(api_db, "get_taf_windows", return_value=[window]):
            r = client.get("/api/cities/Tokyo/taf")
        assert r.json()[0]["taf_disruption"] is True

    def test_taf_disruption_false_for_fm_ts(self, client):
        """FM window with TS → taf_disruption=False (wrong group type)."""
        window = _make_window(group_type="Definite Change", sig_wx="TS")
        with patch.object(api_db, "get_taf_windows", return_value=[window]):
            r = client.get("/api/cities/Tokyo/taf")
        assert r.json()[0]["taf_disruption"] is False

    def test_taf_disruption_false_for_tempo_ra(self, client):
        """TEMPO window with RA only → taf_disruption=False."""
        window = _make_window(group_type="Temporary Fluctuation", sig_wx="RA")
        with patch.object(api_db, "get_taf_windows", return_value=[window]):
            r = client.get("/api/cities/Tokyo/taf")
        assert r.json()[0]["taf_disruption"] is False

    def test_response_includes_all_required_fields(self, client):
        """Response window includes all schema fields."""
        window = _make_window()
        with patch.object(api_db, "get_taf_windows", return_value=[window]):
            r = client.get("/api/cities/Tokyo/taf")
        w = r.json()[0]
        for field in ["city", "issued_at", "valid_from", "valid_to",
                      "group_type", "temp", "wind_kt", "sig_wx", "taf_disruption"]:
            assert field in w, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# 404 case
# ---------------------------------------------------------------------------

class TestCityTaf404:
    def test_returns_404_when_no_data(self, client):
        """Empty taf_windows → 404 with error message."""
        with patch.object(api_db, "get_taf_windows", return_value=[]):
            r = client.get("/api/cities/UnknownCity/taf")
        assert r.status_code == 404
        assert "No TAF data" in r.json()["detail"]


# ---------------------------------------------------------------------------
# hours query param
# ---------------------------------------------------------------------------

class TestHoursParam:
    def test_hours_param_accepted(self, client):
        """?hours=48 is accepted and passed through."""
        window = _make_window()
        with patch.object(api_db, "get_taf_windows", return_value=[window]) as mock_get:
            r = client.get("/api/cities/Tokyo/taf?hours=48")
        assert r.status_code == 200

    def test_hours_clamped_to_48(self, client):
        """?hours=100 is clamped to 48 max."""
        window = _make_window()
        captured_args = {}
        original = api_db.get_taf_windows

        def capture(city, from_ts, to_ts):
            captured_args["from_ts"] = from_ts
            captured_args["to_ts"] = to_ts
            return [window]

        with patch.object(api_db, "get_taf_windows", side_effect=capture):
            r = client.get("/api/cities/Tokyo/taf?hours=100")

        assert r.status_code == 200
        # Verify to_ts - from_ts ≈ 48 hours (not 100)
        from_dt = datetime.fromisoformat(captured_args["from_ts"])
        to_dt = datetime.fromisoformat(captured_args["to_ts"])
        delta_hours = (to_dt - from_dt).total_seconds() / 3600
        assert delta_hours == pytest.approx(48, abs=0.1)

    def test_default_hours_is_24(self, client):
        """Default ?hours=24 when no param given."""
        window = _make_window()
        captured_args = {}

        def capture(city, from_ts, to_ts):
            captured_args["from_ts"] = from_ts
            captured_args["to_ts"] = to_ts
            return [window]

        with patch.object(api_db, "get_taf_windows", side_effect=capture):
            r = client.get("/api/cities/Tokyo/taf")

        from_dt = datetime.fromisoformat(captured_args["from_ts"])
        to_dt = datetime.fromisoformat(captured_args["to_ts"])
        delta_hours = (to_dt - from_dt).total_seconds() / 3600
        assert delta_hours == pytest.approx(24, abs=0.1)

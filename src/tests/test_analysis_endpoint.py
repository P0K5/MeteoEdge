"""Tests for GET /api/analysis/{station} endpoint (issue #513).

Covers:
    - 200 with real-shaped mock data (schema + field types)
    - 404 for unknown station slug
    - 422 for malformed date string
    - 503 when ensemble data is unavailable
    - date defaults to today (UTC) when omitted
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.dashboard import api as dashboard_api
from src.dashboard.api import app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    return TestClient(app)


_ENSEMBLE_200 = {
    "ensemble_mean": 56.7,
    "bias_corrected": 55.3,
    "member_count": 173,
    "range": (53, 61),
    "distribution": {53: 3, 54: 14, 55: 21},
    "active_stack_models": ["nws", "open_meteo"],
}

_BRACKETS_200 = [
    {
        "range": "54–55°F",
        "bracket_low": 54.0,
        "bracket_high": 55.0,
        "polymarket_prob": 0.1,
        "model_prob": 11.0,
        "edge": 10.9,
    }
]


# ---------------------------------------------------------------------------
# 200 — happy path
# ---------------------------------------------------------------------------

class TestAnalysisEndpoint200:
    def test_status_code(self, client):
        with patch("src.dashboard.api.get_ensemble_distribution", return_value=_ENSEMBLE_200):
            with patch("src.dashboard.api.get_bracket_analysis", return_value=_BRACKETS_200):
                r = client.get("/api/analysis/KORD?date=2026-06-29")
        assert r.status_code == 200

    def test_schema_top_level_keys(self, client):
        with patch("src.dashboard.api.get_ensemble_distribution", return_value=_ENSEMBLE_200):
            with patch("src.dashboard.api.get_bracket_analysis", return_value=_BRACKETS_200):
                r = client.get("/api/analysis/KORD?date=2026-06-29")
        data = r.json()
        for key in ("station", "date", "ensemble_mean", "bias_corrected",
                    "member_count", "range", "distribution", "brackets"):
            assert key in data, f"Missing top-level key: {key}"

    def test_station_uppercased(self, client):
        with patch("src.dashboard.api.get_ensemble_distribution", return_value=_ENSEMBLE_200):
            with patch("src.dashboard.api.get_bracket_analysis", return_value=_BRACKETS_200):
                r = client.get("/api/analysis/kord?date=2026-06-29")
        assert r.json()["station"] == "KORD"

    def test_date_echoed(self, client):
        with patch("src.dashboard.api.get_ensemble_distribution", return_value=_ENSEMBLE_200):
            with patch("src.dashboard.api.get_bracket_analysis", return_value=_BRACKETS_200):
                r = client.get("/api/analysis/KORD?date=2026-06-29")
        assert r.json()["date"] == "2026-06-29"

    def test_field_types(self, client):
        with patch("src.dashboard.api.get_ensemble_distribution", return_value=_ENSEMBLE_200):
            with patch("src.dashboard.api.get_bracket_analysis", return_value=_BRACKETS_200):
                r = client.get("/api/analysis/KORD?date=2026-06-29")
        data = r.json()
        assert isinstance(data["ensemble_mean"], float)
        assert isinstance(data["bias_corrected"], float)
        assert isinstance(data["member_count"], int)
        assert isinstance(data["range"], list) and len(data["range"]) == 2
        assert isinstance(data["distribution"], dict)
        assert isinstance(data["brackets"], list)

    def test_distribution_keys_are_strings(self, client):
        """JSON object keys must be strings even though internal dict uses int keys."""
        with patch("src.dashboard.api.get_ensemble_distribution", return_value=_ENSEMBLE_200):
            with patch("src.dashboard.api.get_bracket_analysis", return_value=_BRACKETS_200):
                r = client.get("/api/analysis/KORD?date=2026-06-29")
        dist = r.json()["distribution"]
        for k in dist:
            assert isinstance(k, str), f"Distribution key {k!r} should be a string"

    def test_brackets_schema(self, client):
        with patch("src.dashboard.api.get_ensemble_distribution", return_value=_ENSEMBLE_200):
            with patch("src.dashboard.api.get_bracket_analysis", return_value=_BRACKETS_200):
                r = client.get("/api/analysis/KORD?date=2026-06-29")
        bracket = r.json()["brackets"][0]
        for field in ("range", "bracket_low", "bracket_high",
                      "polymarket_prob", "model_prob", "edge"):
            assert field in bracket, f"Missing bracket field: {field}"


# ---------------------------------------------------------------------------
# 404 — unknown station
# ---------------------------------------------------------------------------

class TestAnalysisEndpoint404:
    def test_unknown_station_returns_404(self, client):
        r = client.get("/api/analysis/ZZZZ?date=2026-06-29")
        assert r.status_code == 404

    def test_unknown_station_detail(self, client):
        r = client.get("/api/analysis/ZZZZ?date=2026-06-29")
        assert "ZZZZ" in r.json().get("detail", "")


# ---------------------------------------------------------------------------
# 422 — malformed date
# ---------------------------------------------------------------------------

class TestAnalysisEndpoint422:
    def test_malformed_date_returns_422(self, client):
        r = client.get("/api/analysis/KORD?date=not-a-date")
        assert r.status_code == 422

    def test_partial_date_returns_422(self, client):
        r = client.get("/api/analysis/KORD?date=2026-13-99")
        assert r.status_code == 422

    def test_wrong_format_returns_422(self, client):
        r = client.get("/api/analysis/KORD?date=06/29/2026")
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# 503 — ensemble unavailable
# ---------------------------------------------------------------------------

class TestAnalysisEndpoint503:
    def test_ensemble_returns_none_gives_503(self, client):
        with patch("src.dashboard.api.get_ensemble_distribution", return_value=None):
            r = client.get("/api/analysis/KORD?date=2026-06-29")
        assert r.status_code == 503

    def test_ensemble_exception_gives_503(self, client):
        with patch("src.dashboard.api.get_ensemble_distribution",
                   side_effect=RuntimeError("DB unavailable")):
            r = client.get("/api/analysis/KORD?date=2026-06-29")
        assert r.status_code == 503

    def test_503_body_has_error_key(self, client):
        with patch("src.dashboard.api.get_ensemble_distribution", return_value=None):
            r = client.get("/api/analysis/KORD?date=2026-06-29")
        detail = r.json().get("detail", {})
        assert "error" in detail
        assert detail["error"] == "ensemble data unavailable"

    def test_503_body_has_station_and_date(self, client):
        with patch("src.dashboard.api.get_ensemble_distribution", return_value=None):
            r = client.get("/api/analysis/KORD?date=2026-06-29")
        detail = r.json().get("detail", {})
        assert detail.get("station") == "KORD"
        assert detail.get("date") == "2026-06-29"


# ---------------------------------------------------------------------------
# date defaults to today (UTC)
# ---------------------------------------------------------------------------

class TestAnalysisEndpointDateDefault:
    def test_omit_date_defaults_to_today(self, client):
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        with patch("src.dashboard.api.get_ensemble_distribution", return_value=_ENSEMBLE_200) as mock_ens:
            with patch("src.dashboard.api.get_bracket_analysis", return_value=_BRACKETS_200):
                r = client.get("/api/analysis/KORD")
        assert r.status_code == 200
        assert r.json()["date"] == today

    def test_omit_date_calls_ensemble_with_today(self, client):
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        with patch("src.dashboard.api.get_ensemble_distribution", return_value=_ENSEMBLE_200) as mock_ens:
            with patch("src.dashboard.api.get_bracket_analysis", return_value=_BRACKETS_200):
                client.get("/api/analysis/KORD")
        call_args = mock_ens.call_args
        assert call_args[0][1] == today  # second positional arg is date string

"""Tests for GET /api/cities/{city}/analysis endpoint in src/dashboard/api.py."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import app, _db as api_db


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _correction_rows(city: str = "Singapore") -> list[dict]:
    """Return sample intraday correction rows for testing."""
    return [
        {
            "city": city,
            "date": "2026-06-09",
            "obs_time": "2026-06-09T14:30:00+00:00",
            "obs_temp_f": 85.5,
            "model_temp_f": 84.2,
            "delta_f": 1.3,
            "corrected_mu_f": 85.1,
            "decay_factor": 0.75,
        },
        {
            "city": city,
            "date": "2026-06-09",
            "obs_time": "2026-06-09T15:30:00+00:00",
            "obs_temp_f": 86.2,
            "model_temp_f": 85.0,
            "delta_f": 1.2,
            "corrected_mu_f": 85.8,
            "decay_factor": 0.6,
        },
    ]


# ---------------------------------------------------------------------------
# TestAnalysisEndpoint
# ---------------------------------------------------------------------------

class TestAnalysisEndpoint:
    def test_analysis_200_with_data(self, client):
        """Query with correction data → GET returns 200 with latest values."""
        rows = _correction_rows("Singapore")
        with patch.object(api_db, "get_intraday_corrections", return_value=rows):
            r = client.get("/api/cities/Singapore/analysis")

        assert r.status_code == 200
        data = r.json()
        assert data["city"] == "Singapore"

        # Should use the most recent (last) correction row
        assert data["corrected_mu_f"] == pytest.approx(85.8, abs=1e-4)
        assert data["bias_delta_f"] == pytest.approx(1.2, abs=1e-4)
        assert data["decay_factor"] == pytest.approx(0.6, abs=1e-4)
        assert data["obs_temp_f"] == pytest.approx(86.2, abs=1e-4)
        assert data["model_temp_at_obs_f"] == pytest.approx(85.0, abs=1e-4)
        assert data["last_correction_time"] == "2026-06-09T15:30:00+00:00"

    def test_analysis_200_no_data(self, client):
        """No correction data for city → GET returns 200 with all None fields."""
        with patch.object(api_db, "get_intraday_corrections", return_value=[]):
            r = client.get("/api/cities/Singapore/analysis")

        assert r.status_code == 200
        data = r.json()
        assert data["city"] == "Singapore"
        assert data["corrected_mu_f"] is None
        assert data["bias_delta_f"] is None
        assert data["decay_factor"] is None
        assert data["obs_temp_f"] is None
        assert data["model_temp_at_obs_f"] is None
        assert data["last_correction_time"] is None

    def test_analysis_200_db_error(self, client):
        """DB error during fetch → GET returns 200 with all None fields."""
        def mock_error(city: str, date: str) -> None:
            raise RuntimeError("DB connection error")

        with patch.object(api_db, "get_intraday_corrections", side_effect=mock_error):
            r = client.get("/api/cities/Singapore/analysis")

        assert r.status_code == 200
        data = r.json()
        assert data["city"] == "Singapore"
        assert data["corrected_mu_f"] is None
        assert data["bias_delta_f"] is None
        assert data["decay_factor"] is None
        assert data["obs_temp_f"] is None
        assert data["model_temp_at_obs_f"] is None
        assert data["last_correction_time"] is None

    def test_analysis_case_insensitive(self, client):
        """GET with lowercase city → city name normalised to title-case."""
        rows = _correction_rows("Singapore")

        captured_cities: list[str] = []

        def mock_get_corrections(city: str, date: str) -> list[dict]:
            captured_cities.append(city)
            return rows

        with patch.object(api_db, "get_intraday_corrections", side_effect=mock_get_corrections):
            r = client.get("/api/cities/singapore/analysis")

        assert r.status_code == 200
        # The DB was called with the title-cased city name
        assert captured_cities == ["Singapore"]
        # The response city field also reflects the normalised name
        assert r.json()["city"] == "Singapore"

    def test_analysis_single_correction_row(self, client):
        """Single correction row → all fields populated from that row."""
        rows = [
            {
                "city": "Boston",
                "date": "2026-06-09",
                "obs_time": "2026-06-09T10:00:00+00:00",
                "obs_temp_f": 72.5,
                "model_temp_f": 71.0,
                "delta_f": 1.5,
                "corrected_mu_f": 72.2,
                "decay_factor": 0.9,
            }
        ]
        with patch.object(api_db, "get_intraday_corrections", return_value=rows):
            r = client.get("/api/cities/Boston/analysis")

        assert r.status_code == 200
        data = r.json()
        assert data["city"] == "Boston"
        assert data["corrected_mu_f"] == pytest.approx(72.2, abs=1e-4)
        assert data["bias_delta_f"] == pytest.approx(1.5, abs=1e-4)
        assert data["decay_factor"] == pytest.approx(0.9, abs=1e-4)
        assert data["obs_temp_f"] == pytest.approx(72.5, abs=1e-4)
        assert data["model_temp_at_obs_f"] == pytest.approx(71.0, abs=1e-4)
        assert data["last_correction_time"] == "2026-06-09T10:00:00+00:00"

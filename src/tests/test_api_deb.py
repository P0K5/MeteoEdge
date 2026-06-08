"""Tests for GET /api/cities/{city}/deb endpoint in src/dashboard/api.py."""
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

def _weight_rows(city: str = "Singapore") -> list[dict]:
    return [
        {"city": city, "model": "nws", "date": "2026-06-08", "weight": 0.62, "rmse": 1.8},
        {"city": city, "model": "open_meteo", "date": "2026-06-07", "weight": 0.38, "rmse": 2.3},
    ]


# ---------------------------------------------------------------------------
# TestDebEndpoint
# ---------------------------------------------------------------------------

class TestDebEndpoint:
    def test_deb_200_with_data(self, client):
        """Insert rows for a test city → GET returns 200 with correct weights."""
        rows = _weight_rows("Singapore")
        with patch.object(api_db, "get_model_weights", return_value=rows):
            r = client.get("/api/cities/Singapore/deb")

        assert r.status_code == 200
        data = r.json()
        assert data["city"] == "Singapore"
        # updated_at should be the max date (first row, since get_model_weights orders DESC)
        assert data["updated_at"] == "2026-06-08"
        assert len(data["weights"]) == 2

        models = {w["model"]: w for w in data["weights"]}
        assert "nws" in models
        assert "open_meteo" in models

        nws = models["nws"]
        assert nws["weight"] == pytest.approx(0.62, abs=1e-4)
        assert nws["rmse_f"] == pytest.approx(1.8, abs=1e-4)
        assert nws["n_samples"] == 0

        om = models["open_meteo"]
        assert om["weight"] == pytest.approx(0.38, abs=1e-4)
        assert om["rmse_f"] == pytest.approx(2.3, abs=1e-4)

    def test_deb_404_no_data(self, client):
        """No rows in model_weights for city → 404 with expected detail message."""
        with patch.object(api_db, "get_model_weights", return_value=[]):
            r = client.get("/api/cities/UnknownCity/deb")

        assert r.status_code == 404
        assert r.json()["detail"] == "no DEB data for city"

    def test_deb_case_insensitive(self, client):
        """GET with lowercase city 'singapore' should normalize to 'Singapore' and return 200."""
        rows = _weight_rows("Singapore")

        captured_cities: list[str] = []

        def mock_get_model_weights(city: str) -> list[dict]:
            captured_cities.append(city)
            return rows

        with patch.object(api_db, "get_model_weights", side_effect=mock_get_model_weights):
            r = client.get("/api/cities/singapore/deb")

        assert r.status_code == 200
        # The DB was called with the title-cased city name
        assert captured_cities == ["Singapore"]
        # The response city field also reflects the normalised name
        assert r.json()["city"] == "Singapore"

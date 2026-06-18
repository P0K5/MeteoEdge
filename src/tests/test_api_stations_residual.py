"""Tests for GET /api/stations/{metar}/residual (issue #340).

Covers:
- Correct response shape for a known METAR with data
- 404 for an unknown METAR
- Empty list when no qualified pairs exist
- station/source/scope fields are present
"""
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# App under test — import with _db stubbed to avoid real DB
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    """Return a TestClient for the dashboard API with _db mocked."""
    import src.dashboard.api as api_mod

    mock_db = MagicMock()
    mock_db._conn = MagicMock()

    with patch.object(api_mod, "_db", mock_db):
        from fastapi.testclient import TestClient
        yield TestClient(api_mod.app), mock_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stats(station="Busan", source="amos", scope="pair"):
    from src.model.residual_correction import ResidualStats
    return ResidualStats(
        city="Busan",
        mean_signed_error=2.5,
        rolling_mae=3.1,
        sample_count=20,
        correction_applied=True,
        live_suppressed=False,
        scope=scope,
        station=station,
        source=source,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStationResidualEndpoint:

    def test_404_for_unknown_metar(self, client):
        tc, _ = client
        resp = tc.get("/api/stations/UNKNOWN/residual")
        assert resp.status_code == 404

    def test_empty_list_when_no_data(self, client):
        """compute_residual_stats_per_pair returns [] → endpoint returns []."""
        tc, mock_db = client
        # Set up last_obs query to return nothing
        mock_db._conn.execute.return_value.fetchall.return_value = []

        with patch("src.dashboard.api.compute_residual_stats_per_pair", return_value=[]):
            # Use a METAR that IS in STATIONS, e.g. KORD = Chicago
            resp = tc.get("/api/stations/KORD/residual")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_correct_shape_with_data(self, client):
        """Endpoint returns correct shape for a single (station, source) pair."""
        tc, mock_db = client
        mock_db._conn.execute.return_value.fetchall.return_value = []

        stats = _make_stats(station="Busan", source="amos", scope="pair")

        with patch("src.dashboard.api.compute_residual_stats_per_pair", return_value=[stats]):
            # RKPK = Busan in STATIONS
            resp = tc.get("/api/stations/RKPK/residual")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        row = data[0]
        assert row["station"] == "Busan"
        assert row["source"] == "amos"
        assert row["scope"] == "pair"
        assert row["sample_count"] == 20
        assert "mean_signed_error" in row
        assert "rolling_mae" in row
        assert "clamped_correction" in row
        assert "correction_applied" in row
        assert "live_suppressed" in row
        assert "last_obs_time" in row

    def test_multiple_pairs_returned(self, client):
        """Multiple (station, source) pairs all appear in the response."""
        tc, mock_db = client
        mock_db._conn.execute.return_value.fetchall.return_value = []

        stats_list = [
            _make_stats(station="Busan", source="amos", scope="pair"),
            _make_stats(station="RKPK", source="metar", scope="pair"),
        ]

        with patch("src.dashboard.api.compute_residual_stats_per_pair", return_value=stats_list):
            resp = tc.get("/api/stations/RKPK/residual")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        sources = {r["source"] for r in data}
        assert sources == {"amos", "metar"}

    def test_last_obs_time_populated(self, client):
        """last_obs_time is populated from DB query when available."""
        tc, mock_db = client
        # Simulate last_obs_time query returning a timestamp
        mock_db._conn.execute.return_value.fetchall.return_value = [
            ("Busan", "amos", "2024-06-01T08:00:00+00:00")
        ]

        stats = _make_stats(station="Busan", source="amos", scope="pair")

        with patch("src.dashboard.api.compute_residual_stats_per_pair", return_value=[stats]):
            resp = tc.get("/api/stations/RKPK/residual")

        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["last_obs_time"] == "2024-06-01T08:00:00+00:00"

    def test_503_when_db_not_available(self):
        """Returns 503 when _db is None."""
        import src.dashboard.api as api_mod
        with patch.object(api_mod, "_db", None):
            tc = TestClient(api_mod.app)
            resp = tc.get("/api/stations/KORD/residual")
        assert resp.status_code == 503

"""Unit tests for the GFS second-model forecast path (issue #309).

Covers:
- src/data/open_meteo.fetch_gfs_forecast_high  — parse, unit handling, None on error
- src/model/deb_weighting.MODELS / EQUAL_WEIGHTS  — gfs now in the tuple
- src/model/deb_hourly_consensus.compute_deb_mu_f — three-model blend logic
- src/model/deb_weighting.compute_weights  — phantom-model exclusion for NWS-less stations

No real HTTP calls are made; all external dependencies are mocked.
"""
from math import isclose
from unittest.mock import MagicMock, patch

import pytest

from src.data.open_meteo import fetch_gfs_forecast_high
from src.model.deb_hourly_consensus import compute_deb_mu_f
from src.model.deb_weighting import (
    EQUAL_WEIGHTS,
    MODELS,
    MIN_SAMPLES,
    compute_weights,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gfs_payload(temps: list) -> dict:
    """Minimal Open-Meteo-shaped payload as returned by the GFS endpoint."""
    times = [f"2026-06-17T{h:02d}:00" for h in range(len(temps))]
    return {"hourly": {"time": times, "temperature_2m": temps}}


# ---------------------------------------------------------------------------
# src/data/open_meteo.fetch_gfs_forecast_high
# ---------------------------------------------------------------------------

class TestFetchGfsForecastHigh:
    """Tests for the new GFS collector function."""

    def test_returns_daily_max_of_first_24_hours(self):
        """Daily high is the max of the first 24 hourly temperatures."""
        temps = [float(60 + i) for i in range(30)]  # 30 hours, monotone rising
        payload = _gfs_payload(temps)
        with patch("src.data.open_meteo.cached_fetch_json", return_value=payload):
            result = fetch_gfs_forecast_high(1.36, 103.99)
        # First 24 values span 60..83; value at index 23 == 83.0
        assert result == 83.0

    def test_temperatures_in_fahrenheit(self):
        """Values are expected to already be in °F (Open-Meteo is called with
        temperature_unit=fahrenheit).  No conversion should be applied."""
        temps = [32.0] * 24  # freezing — should come back as-is
        payload = _gfs_payload(temps)
        with patch("src.data.open_meteo.cached_fetch_json", return_value=payload):
            result = fetch_gfs_forecast_high(35.55, 139.78)
        assert result == 32.0

    def test_returns_none_when_api_unavailable(self):
        """Returns None when cached_fetch_json returns None (network failure)."""
        with patch("src.data.open_meteo.cached_fetch_json", return_value=None):
            assert fetch_gfs_forecast_high(35.55, 139.78) is None

    def test_returns_none_on_empty_temperature_list(self):
        """Returns None when the first 24 slots are all None."""
        payload = {"hourly": {"time": [], "temperature_2m": []}}
        with patch("src.data.open_meteo.cached_fetch_json", return_value=payload):
            assert fetch_gfs_forecast_high(35.55, 139.78) is None

    def test_skips_none_values_in_temp_list(self):
        """None entries in the temperature array are filtered before taking max."""
        temps = [None, None, 70.0, None, 75.0] + [None] * 19
        payload = _gfs_payload(temps)
        with patch("src.data.open_meteo.cached_fetch_json", return_value=payload):
            result = fetch_gfs_forecast_high(35.55, 139.78)
        assert result == 75.0

    def test_uses_gfs_seamless_model_parameter(self):
        """Verify the GFS endpoint URL contains models=gfs_seamless."""
        captured_url = []

        def _mock_fetch(url, **kwargs):
            captured_url.append(url)
            return _gfs_payload([75.0] * 24)

        with patch("src.data.open_meteo.cached_fetch_json", side_effect=_mock_fetch):
            fetch_gfs_forecast_high(1.36, 103.99)

        assert captured_url, "cached_fetch_json was not called"
        assert "gfs_seamless" in captured_url[0], (
            f"Expected 'gfs_seamless' in URL, got: {captured_url[0]}"
        )

    def test_returns_none_on_malformed_payload(self):
        """Returns None and does not raise when the API returns unexpected JSON."""
        with patch("src.data.open_meteo.cached_fetch_json", return_value={"unexpected": True}):
            assert fetch_gfs_forecast_high(35.55, 139.78) is None


# ---------------------------------------------------------------------------
# MODELS and EQUAL_WEIGHTS constants — gfs must be present
# ---------------------------------------------------------------------------

class TestModelsConstants:
    def test_gfs_in_models_tuple(self):
        """'gfs' must be listed in MODELS so DEB/EMOS can consume it."""
        assert "gfs" in MODELS, f"Expected 'gfs' in MODELS, got: {MODELS}"

    def test_equal_weights_has_gfs(self):
        """EQUAL_WEIGHTS must include gfs."""
        assert "gfs" in EQUAL_WEIGHTS, f"Expected 'gfs' in EQUAL_WEIGHTS, got: {EQUAL_WEIGHTS}"

    def test_equal_weights_sums_to_one(self):
        """EQUAL_WEIGHTS values must sum to 1.0."""
        total = sum(EQUAL_WEIGHTS.values())
        assert isclose(total, 1.0, abs_tol=1e-9), f"EQUAL_WEIGHTS sums to {total}, expected 1.0"

    def test_equal_weights_symmetric(self):
        """All models in EQUAL_WEIGHTS must carry equal weight."""
        expected = 1.0 / len(MODELS)
        for m, w in EQUAL_WEIGHTS.items():
            assert isclose(w, expected, abs_tol=1e-9), (
                f"EQUAL_WEIGHTS['{m}'] = {w}, expected {expected}"
            )


# ---------------------------------------------------------------------------
# compute_deb_mu_f — three-model blend
# ---------------------------------------------------------------------------

class TestComputeDebMuFThreeModels:
    """Tests for the updated three-model blend in compute_deb_mu_f."""

    def test_three_model_weighted_blend(self):
        """With all three models, blends proportionally by weight."""
        weights = {"nws": 0.4, "open_meteo": 0.35, "gfs": 0.25}
        result = compute_deb_mu_f(
            forecast_nws=80.0,
            forecast_open_meteo=78.0,
            weights=weights,
            forecast_gfs=82.0,
        )
        expected = 0.4 * 80.0 + 0.35 * 78.0 + 0.25 * 82.0
        assert result is not None
        assert isclose(result, expected, abs_tol=1e-6), f"Expected {expected}, got {result}"

    def test_international_station_no_nws(self):
        """For international stations, NWS is None; blend uses open_meteo + gfs only."""
        weights = {"nws": 0.0, "open_meteo": 0.5, "gfs": 0.5}
        result = compute_deb_mu_f(
            forecast_nws=None,
            forecast_open_meteo=78.0,
            weights=weights,
            forecast_gfs=82.0,
        )
        # NWS dropped; remaining weights renormalised: om=0.5, gfs=0.5 → average of 78 and 82
        assert result is not None
        assert isclose(result, 80.0, abs_tol=1e-6), f"Expected 80.0, got {result}"

    def test_only_gfs_available(self):
        """When only GFS is available, returns it at full weight."""
        weights = {"nws": 0.0, "open_meteo": 0.0, "gfs": 1.0}
        result = compute_deb_mu_f(
            forecast_nws=None,
            forecast_open_meteo=None,
            weights=weights,
            forecast_gfs=82.0,
        )
        assert result == 82.0

    def test_all_none_returns_none(self):
        """Returns None when all three inputs are None."""
        weights = {"nws": 0.33, "open_meteo": 0.33, "gfs": 0.34}
        result = compute_deb_mu_f(None, None, weights, forecast_gfs=None)
        assert result is None

    def test_gfs_default_none_backward_compat(self):
        """forecast_gfs defaults to None — existing two-model callers unaffected."""
        weights = {"nws": 0.6, "open_meteo": 0.4}
        result = compute_deb_mu_f(forecast_nws=82.0, forecast_open_meteo=78.0, weights=weights)
        # nws=0.6*82 + om=0.4*78 = 49.2+31.2 = 80.4
        assert result is not None
        assert isclose(result, 80.4, abs_tol=1e-6), f"Expected 80.4, got {result}"

    def test_zero_total_weight_uses_equal_average(self):
        """When all weights are 0.0, falls back to simple average of available values."""
        weights = {"nws": 0.0, "open_meteo": 0.0, "gfs": 0.0}
        result = compute_deb_mu_f(
            forecast_nws=80.0,
            forecast_open_meteo=80.0,
            weights=weights,
            forecast_gfs=80.0,
        )
        assert result is not None
        assert isclose(result, 80.0, abs_tol=1e-6), f"Expected 80.0, got {result}"


# ---------------------------------------------------------------------------
# compute_weights — phantom-model exclusion for international stations
# ---------------------------------------------------------------------------

class TestComputeWeightsPhantomGuard:
    """Tests that NWS (zero rows) is excluded from the blend for international stations."""

    def _make_db_with_open_meteo_and_gfs(self, n: int) -> MagicMock:
        """Build a mock DB with n days of open_meteo + gfs forecast rows."""
        db = MagicMock()
        db.get_forecast_log_by_lead.return_value = (
            [
                {"date": f"2026-05-{i:02d}", "model": "open_meteo", "forecast_high_f": 86.0 + i * 0.1}
                for i in range(1, n + 1)
            ]
            + [
                {"date": f"2026-05-{i:02d}", "model": "gfs", "forecast_high_f": 85.5 + i * 0.1}
                for i in range(1, n + 1)
            ]
        )
        db.get_settlements.return_value = [
            {"ts": f"2026-05-{i:02d}T12:00:00", "actual_high_f": 86.2 + i * 0.05}
            for i in range(1, n + 1)
        ]
        return db

    def test_nws_phantom_excluded_from_blend(self):
        """With enough open_meteo + gfs rows but zero NWS rows, NWS gets cold-start
        weight and the two active models dominate; all weights sum to 1.0."""
        db = self._make_db_with_open_meteo_and_gfs(MIN_SAMPLES + 2)
        weights = compute_weights(db, "WSSS", "Singapore")
        # nws has zero rows → cold-start → gets a small fraction, not the majority
        assert weights["nws"] < weights["open_meteo"], "cold-start nws should be < open_meteo"
        assert weights["nws"] < weights["gfs"], "cold-start nws should be < gfs"
        # All weights must sum to 1.0
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-9), f"Weights sum to {sum(weights.values())}"

    def test_fallback_when_insufficient_samples(self):
        """Falls back to EQUAL_WEIGHTS when all models have < MIN_SAMPLES pairs."""
        db = self._make_db_with_open_meteo_and_gfs(MIN_SAMPLES - 1)
        weights = compute_weights(db, "WSSS", "Singapore")
        assert weights == EQUAL_WEIGHTS

    def test_gfs_phantom_excluded_from_blend(self):
        """If GFS rows are absent but NWS + open_meteo have enough data,
        GFS gets cold-start weight and the two active models dominate; all sum to 1.0."""
        n = MIN_SAMPLES + 2
        db = MagicMock()
        db.get_forecast_log_by_lead.return_value = (
            [
                {"date": f"2026-05-{i:02d}", "model": "nws", "forecast_high_f": 85.0 + i * 0.1}
                for i in range(1, n + 1)
            ]
            + [
                {"date": f"2026-05-{i:02d}", "model": "open_meteo", "forecast_high_f": 84.0 + i * 0.1}
                for i in range(1, n + 1)
            ]
        )
        db.get_settlements.return_value = [
            {"ts": f"2026-05-{i:02d}T12:00:00", "actual_high_f": 85.2 + i * 0.05}
            for i in range(1, n + 1)
        ]
        weights = compute_weights(db, "KORD", "Chicago")
        # gfs has zero rows → cold-start → gets a small fraction
        assert weights["gfs"] < weights["nws"], "cold-start gfs should be < nws"
        assert weights["gfs"] < weights["open_meteo"], "cold-start gfs should be < open_meteo"
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-9), f"Weights sum to {sum(weights.values())}"

"""Unit tests for the GFS second-model forecast path (issue #309).

Covers:
- src/data/open_meteo.fetch_gfs_forecast_high  — parse, unit handling, None on error
- src/data/open_meteo.fetch_gfs_with_spread     — real single-model gfs_seamless
  fetch, distinct from the open_meteo multi-model fetch (issue #548)
- src/model/deb_weighting.MODELS / EQUAL_WEIGHTS  — "gfs" removed from the
  DEB channel set by issue #761 (double-counted the same physical GFS model
  as "open_meteo"); ingestion via fetch_gfs_forecast_high/fetch_gfs_with_spread
  is unaffected and still tested below
- src/model/deb_hourly_consensus.compute_deb_mu_f — blend logic accepts a
  "gfs" weight key generically (does not consult the DEB registry)
- src/model/deb_weighting.compute_weights  — phantom-model exclusion for NWS-less stations

No real HTTP calls are made; all external dependencies are mocked.
"""
from math import isclose
from unittest.mock import MagicMock, patch

import pytest

from src.data.open_meteo import (
    fetch_gfs_forecast_high,
    fetch_gfs_with_spread,
    fetch_open_meteo_with_spread,
)
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
# fetch_gfs_with_spread — real single-model gfs_seamless fetch (issue #548)
# ---------------------------------------------------------------------------

class TestFetchGfsWithSpread:
    """Regression tests for issue #548: fetch_gfs_with_spread() must no longer
    be a byte-identical duplicate of fetch_open_meteo_with_spread(). It must
    issue its own request scoped to models=gfs_seamless and return sigma_f as
    None (single deterministic model, no ensemble spread)."""

    def test_requests_gfs_seamless_only(self):
        """The URL fetch_gfs_with_spread issues must request models=gfs_seamless
        and must NOT request the multi-model set used by open_meteo."""
        captured_urls = []

        def _mock_fetch(url, **kwargs):
            captured_urls.append(url)
            return {"hourly": {"temperature_2m": [70.0] * 48}}

        with patch("src.data.open_meteo.cached_fetch_json", side_effect=_mock_fetch):
            fetch_gfs_with_spread(1.36, 103.99, lead_hours=24)

        assert captured_urls, "cached_fetch_json was not called"
        assert "models=gfs_seamless" in captured_urls[0], (
            f"Expected 'models=gfs_seamless' in URL, got: {captured_urls[0]}"
        )
        assert "ecmwf_ifs04" not in captured_urls[0]
        assert "jma_seamless" not in captured_urls[0]
        assert "best_match" not in captured_urls[0]

    def test_issues_distinct_request_from_open_meteo_multimodel_fetch(self):
        """fetch_gfs_with_spread and fetch_open_meteo_with_spread must issue
        different requests for the same inputs — the core #548 regression:
        previously fetch_gfs_with_spread just called
        fetch_open_meteo_with_spread() verbatim, which fans out to FOUR HTTP
        requests (ecmwf_ifs04, gfs_seamless, jma_seamless, best_match). Now
        fetch_gfs_with_spread must issue exactly ONE request, scoped to
        gfs_seamless — proving it is no longer delegating to the multi-model
        fetch."""

        def _mock_fetch(url, **kwargs):
            return {"hourly": {"temperature_2m": [70.0] * 48}}

        with patch("src.data.open_meteo.cached_fetch_json", side_effect=_mock_fetch) as mocked:
            fetch_gfs_with_spread(1.36, 103.99, lead_hours=24)
            gfs_call_count = mocked.call_count

        with patch("src.data.open_meteo.cached_fetch_json", side_effect=_mock_fetch) as mocked:
            fetch_open_meteo_with_spread(1.36, 103.99, lead_hours=24)
            om_call_count = mocked.call_count
            om_urls = [c.args[0] for c in mocked.call_args_list]

        # The #548 bug: fetch_gfs_with_spread used to make the same 4 calls as
        # fetch_open_meteo_with_spread. It must now make exactly 1.
        assert gfs_call_count == 1, f"Expected 1 HTTP call, got {gfs_call_count}"
        assert om_call_count == 4, f"Expected open_meteo to still query 4 models, got {om_call_count}"
        assert any("models=gfs_seamless" in u for u in om_urls), (
            "open_meteo multi-model fetch should still include gfs_seamless "
            "as one of its constituent models"
        )

    def test_sigma_is_always_none(self):
        """A single deterministic GFS run has no ensemble spread — sigma_f
        must always be None, never a synthesized/placeholder value."""
        with patch(
            "src.data.open_meteo.cached_fetch_json",
            return_value={"hourly": {"temperature_2m": [70.0] * 48}},
        ):
            result = fetch_gfs_with_spread(35.55, 139.78, lead_hours=24)
        assert result is not None
        mu_f, sigma_f = result
        assert sigma_f is None
        assert isinstance(mu_f, float)

    def test_lead_hours_ge_20_selects_tomorrow_window(self):
        """lead_hours >= 20 selects the second 24h block (tomorrow)."""
        temps = [60.0] * 24 + [90.0] * 24  # today low, tomorrow high
        with patch(
            "src.data.open_meteo.cached_fetch_json",
            return_value={"hourly": {"temperature_2m": temps}},
        ):
            result = fetch_gfs_with_spread(35.55, 139.78, lead_hours=24)
        assert result == (90.0, None)

    def test_lead_hours_lt_20_selects_today_window(self):
        """lead_hours < 20 selects the first 24h block (today)."""
        temps = [60.0] * 24 + [90.0] * 24
        with patch(
            "src.data.open_meteo.cached_fetch_json",
            return_value={"hourly": {"temperature_2m": temps}},
        ):
            result = fetch_gfs_with_spread(35.55, 139.78, lead_hours=6)
        assert result == (60.0, None)

    def test_returns_none_when_api_unavailable(self):
        with patch("src.data.open_meteo.cached_fetch_json", return_value=None):
            assert fetch_gfs_with_spread(35.55, 139.78, lead_hours=24) is None

    def test_returns_none_on_all_null_temperatures(self):
        temps = [None] * 48
        with patch(
            "src.data.open_meteo.cached_fetch_json",
            return_value={"hourly": {"temperature_2m": temps}},
        ):
            assert fetch_gfs_with_spread(35.55, 139.78, lead_hours=24) is None

    def test_returns_none_on_malformed_payload(self):
        with patch(
            "src.data.open_meteo.cached_fetch_json", return_value={"unexpected": True}
        ):
            assert fetch_gfs_with_spread(35.55, 139.78, lead_hours=24) is None


# ---------------------------------------------------------------------------
# MODELS and EQUAL_WEIGHTS constants — "gfs" removed by issue #761
# ---------------------------------------------------------------------------

class TestModelsConstants:
    def test_gfs_not_in_models_tuple(self):
        """Issue #761: 'gfs' was removed from MODELS — it double-counted the
        same physical GFS model as 'open_meteo'."""
        assert "gfs" not in MODELS, f"Expected 'gfs' absent from MODELS, got: {MODELS}"

    def test_equal_weights_does_not_have_gfs(self):
        """EQUAL_WEIGHTS must not include gfs (issue #761)."""
        assert "gfs" not in EQUAL_WEIGHTS, f"Expected 'gfs' absent from EQUAL_WEIGHTS, got: {EQUAL_WEIGHTS}"

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
        """Build a mock DB with n days of open_meteo forecast rows, plus "gfs"
        rows (issue #761: "gfs" is no longer a registered DEB channel, so
        these rows are included here specifically to confirm compute_weights
        silently ignores them rather than raising or double-counting)."""
        db = MagicMock()
        db.get_forecast_log_by_lead.return_value = (
            [
                {"date": f"2026-07-{i:02d}", "model": "open_meteo", "forecast_high_f": 86.0 + i * 0.1}
                for i in range(1, n + 1)
            ]
            + [
                {"date": f"2026-07-{i:02d}", "model": "gfs", "forecast_high_f": 85.5 + i * 0.1}
                for i in range(1, n + 1)
            ]
        )
        db.get_settlements.return_value = [
            {"ts": f"2026-07-{i:02d}T12:00:00", "actual_high_f": 86.2 + i * 0.05}
            for i in range(1, n + 1)
        ]
        db.get_obs_highs_range.return_value = {
            f"2026-07-{i:02d}": 86.2 + i * 0.05
            for i in range(1, n + 1)
        }
        return db

    def test_nws_phantom_excluded_from_blend(self):
        """With enough open_meteo rows (plus ignored "gfs" rows) but zero NWS
        rows, NWS gets cold-start weight and open_meteo dominates; all
        weights sum to 1.0, and "gfs" contributes no weight."""
        db = self._make_db_with_open_meteo_and_gfs(MIN_SAMPLES + 2)
        weights = compute_weights(db, "WSSS", "Singapore")
        assert "gfs" not in weights, "issue #761: 'gfs' must not appear in DEB weights"
        # nws has zero rows → cold-start → gets a small fraction, not the majority
        assert weights["nws"] < weights["open_meteo"], "cold-start nws should be < open_meteo"
        # All weights must sum to 1.0
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-9), f"Weights sum to {sum(weights.values())}"

    def test_fallback_when_insufficient_samples(self):
        """Falls back to EQUAL_WEIGHTS when all models have < MIN_SAMPLES pairs."""
        db = self._make_db_with_open_meteo_and_gfs(MIN_SAMPLES - 1)
        weights = compute_weights(db, "WSSS", "Singapore")
        assert weights == EQUAL_WEIGHTS
        assert "gfs" not in weights

    def test_hrrr_phantom_excluded_from_blend(self):
        """If HRRR rows are absent but NWS + open_meteo have enough data,
        HRRR gets cold-start weight and the two active models dominate; all sum to 1.0."""
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
        db.get_obs_highs_range.return_value = {
            f"2026-05-{i:02d}": 85.2 + i * 0.05
            for i in range(1, n + 1)
        }
        weights = compute_weights(db, "KORD", "Chicago")
        assert "gfs" not in weights, "issue #761: 'gfs' must not appear in DEB weights"
        # hrrr has zero rows → cold-start → gets a small fraction
        assert weights["hrrr"] < weights["nws"], "cold-start hrrr should be < nws"
        assert weights["hrrr"] < weights["open_meteo"], "cold-start hrrr should be < open_meteo"
        assert isclose(sum(weights.values()), 1.0, abs_tol=1e-9), f"Weights sum to {sum(weights.values())}"

"""Tests for EMOS regime filter (issue #494).

Verifies that fetch_training_data correctly scopes the ensemble μ to the
model tags defined by FORECAST_STACK_MODELS, preventing shadow-source
contamination of live-calibrated EMOS coefficients.

Coverage:
1. baseline regime excludes shadow models (regression lock)
2. hrrr_nbm regime includes the correct four models
3. full vs baseline regimes produce different μ values
4. unscoped call (no regime, no forecast_source) raises ValueError
5. forecast_source string equality filter still works (back-compat)
6. save_coefficients is called with the correct forecast_source per stack
"""
from __future__ import annotations

import math
from unittest.mock import MagicMock

import pytest

from src.data.db import Database
from src.model.emos_calibration import (
    InsufficientDataError,
    fetch_training_data,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_db() -> Database:
    return Database(":memory:")


def _insert_forecast(
    db: Database,
    station: str,
    model: str,
    date_str: str,
    forecast_high_f: float,
    lead_hours: int = 24,
) -> None:
    db.upsert_forecast_log_v2(
        station=station,
        model=model,
        date=date_str,
        forecast_high_f=forecast_high_f,
        lead_hours=lead_hours,
        sigma_f=2.0,
    )


def _insert_obs(db: Database, station: str, date_str: str, temp_f: float) -> None:
    db.insert_observation(
        ts=f"{date_str}T18:00:00+00:00",
        station=station,
        temp_f=temp_f,
        temp_native=temp_f,
        unit="F",
        source="metar",
    )


def _build_four_model_db() -> Database:
    """Return a DB with nws=72, open_meteo=74, hrrr=80, nbm=82 on 2026-01-01."""
    db = _fresh_db()
    station = "KORD"
    date_str = "2026-01-01"
    _insert_forecast(db, station, "nws", date_str, 72.0)
    _insert_forecast(db, station, "open_meteo", date_str, 74.0)
    _insert_forecast(db, station, "hrrr", date_str, 80.0)
    _insert_forecast(db, station, "nbm", date_str, 82.0)
    _insert_obs(db, station, date_str, 71.0)  # actual — only needed for join
    return db


# ---------------------------------------------------------------------------
# 1. baseline regime excludes shadow models
# ---------------------------------------------------------------------------

class TestBaselineExcludesShadowModels:
    """Regression lock: baseline regime must never include hrrr/nbm rows."""

    def test_baseline_excludes_shadow_models(self):
        db = _build_four_model_db()
        result = fetch_training_data(
            "Chicago", db, min_samples=1,
            regime=frozenset({"nws", "open_meteo"}),
        )
        assert len(result) == 1
        mu, _sigma, _y = result[0]
        expected_mu = (72.0 + 74.0) / 2  # 73.0
        assert math.isclose(mu, expected_mu, rel_tol=1e-6), (
            f"Expected mu={expected_mu} for baseline regime, got {mu}"
        )


# ---------------------------------------------------------------------------
# 2. hrrr_nbm regime includes the correct four models
# ---------------------------------------------------------------------------

class TestHrrrNbmRegime:
    def test_hrrr_nbm_regime_includes_correct_models(self):
        db = _build_four_model_db()
        result = fetch_training_data(
            "Chicago", db, min_samples=1,
            regime=frozenset({"nws", "open_meteo", "hrrr", "nbm"}),
        )
        assert len(result) == 1
        mu, _sigma, _y = result[0]
        expected_mu = (72.0 + 74.0 + 80.0 + 82.0) / 4  # 77.0
        assert math.isclose(mu, expected_mu, rel_tol=1e-6), (
            f"Expected mu={expected_mu} for hrrr_nbm regime, got {mu}"
        )


# ---------------------------------------------------------------------------
# 3. full vs baseline differ
# ---------------------------------------------------------------------------

class TestFullVsBaselineDiffer:
    def test_full_vs_baseline_differ(self):
        db = _build_four_model_db()
        result_baseline = fetch_training_data(
            "Chicago", db, min_samples=1,
            regime=frozenset({"nws", "open_meteo"}),
        )
        result_full = fetch_training_data(
            "Chicago", db, min_samples=1,
            regime=frozenset({"nws", "open_meteo", "hrrr", "nbm"}),
        )
        mu_baseline = result_baseline[0][0]
        mu_full = result_full[0][0]
        assert not math.isclose(mu_baseline, mu_full, rel_tol=1e-6), (
            f"baseline mu ({mu_baseline}) and full mu ({mu_full}) should differ"
        )


# ---------------------------------------------------------------------------
# 4. unscoped call raises ValueError
# ---------------------------------------------------------------------------

class TestUnscopedCallRaises:
    def test_unscoped_call_raises(self):
        db = _build_four_model_db()
        with pytest.raises(ValueError, match="fetch_training_data requires"):
            fetch_training_data("Chicago", db, min_samples=1)


# ---------------------------------------------------------------------------
# 5. forecast_source back-compat (no regime)
# ---------------------------------------------------------------------------

class TestForecastSourceBackcompat:
    def test_forecast_source_backcompat(self):
        """forecast_source string equality filter works unchanged when regime is None."""
        db = _build_four_model_db()
        # Only nws rows should be used (forecast_high_f=72)
        result = fetch_training_data(
            "Chicago", db, min_samples=1,
            forecast_source="nws",
        )
        assert len(result) == 1
        mu, _sigma, _y = result[0]
        assert math.isclose(mu, 72.0, rel_tol=1e-6), (
            f"Expected mu=72.0 for forecast_source='nws', got {mu}"
        )


# ---------------------------------------------------------------------------
# 6. save_coefficients called with correct forecast_source per stack
# ---------------------------------------------------------------------------

class TestCoefficientsStoredPerStack:
    def test_coefficients_stored_per_stack(self):
        """save_coefficients passes forecast_source to db.upsert_emos_coefficients."""
        from src.model.emos_calibration import save_coefficients

        mock_db = MagicMock()
        stack = "baseline"

        save_coefficients(
            city="Chicago",
            a=0.0, b=1.0, c=0.5, d=1.0,
            crps_score=0.07,
            db=mock_db,
            forecast_source=stack,
        )
        mock_db.upsert_emos_coefficients.assert_called_once()
        call_kwargs = mock_db.upsert_emos_coefficients.call_args.kwargs
        assert call_kwargs["forecast_source"] == stack, (
            f"Expected forecast_source='{stack}', got {call_kwargs.get('forecast_source')!r}"
        )

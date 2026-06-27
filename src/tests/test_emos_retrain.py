"""Tests for EMOS retrain tooling (#463).

All tests use synthetic data only — no live DB, no real forecast data.
Guardrail: this file MUST NOT call fit_emos() against production data.
"""
import math
import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.model.emos_calibration import (
    EmosFit,
    InsufficientDataError,
    MIN_TRAINING_ROWS,
    _crps_gaussian,
    _mean_crps,
    check_ready_for_promotion,
    fit_emos,
    load_coefficients,
    load_coefficients_shadow,
    save_coefficients,
)
from src.model.emos_mode import (
    LEGACY_SOURCE,
    apply_emos,
    get_active_source,
    get_coefficients_for_city,
    set_active_source,
)


# ---------------------------------------------------------------------------
# Lightweight fake DB backed by a temp SQLite file with the real schema
# ---------------------------------------------------------------------------

def _make_db(rows=None):
    """Create a Database instance with emos tables populated with synthetic rows."""
    from src.data.db import Database

    tmp = tempfile.mktemp(suffix=".db")
    db = Database(path=tmp)

    if rows:
        db._conn.execute(
            "INSERT OR IGNORE INTO emos_calibration "
            "(city, forecast_source, a, b, c, d, crps_score, model_mode, ready_for_promotion, fitted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        db._conn.commit()

    return db


def _db_with_intraday(n_rows: int, model_mu: float = 80.0, obs_high: float = 82.0):
    """DB with synthetic intraday_corrections rows for fit_emos() testing."""
    from src.data.db import Database

    tmp = tempfile.mktemp(suffix=".db")
    db = Database(path=tmp)

    import random
    from datetime import datetime, timedelta, timezone
    random.seed(42)
    today = datetime.now(timezone.utc).date()
    for i in range(n_rows):
        d = today - timedelta(days=n_rows - i)
        date = d.isoformat()
        noisy_mu = model_mu + random.uniform(-2, 2)
        noisy_obs = obs_high + random.uniform(-3, 3)
        db._conn.execute(
            "INSERT OR REPLACE INTO intraday_corrections "
            "(city, date, obs_time, obs_temp_f, model_temp_f, delta_f, corrected_mu_f, decay_factor) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("Chicago", date, "12:00", noisy_obs, noisy_mu, noisy_obs - noisy_mu, noisy_mu, 0.9),
        )
    db._conn.commit()
    return db


# ---------------------------------------------------------------------------
# _crps_gaussian
# ---------------------------------------------------------------------------

class TestCrpsGaussian:
    def test_perfect_forecast(self):
        """CRPS is minimised (not zero) for a point observation at the mean."""
        score = _crps_gaussian(mu=80.0, sigma=2.0, obs=80.0)
        assert score >= 0
        assert score < 1.5  # well below typical scores for bad forecasts

    def test_positive_always(self):
        for obs in [70.0, 80.0, 90.0, 100.0]:
            assert _crps_gaussian(80.0, 3.0, obs) >= 0

    def test_worse_when_obs_far(self):
        close = _crps_gaussian(80.0, 3.0, 81.0)
        far = _crps_gaussian(80.0, 3.0, 95.0)
        assert far > close

    def test_wider_sigma_reduces_penalty_for_far_obs(self):
        narrow = _crps_gaussian(80.0, 1.0, 90.0)
        wide = _crps_gaussian(80.0, 5.0, 90.0)
        assert wide < narrow


# ---------------------------------------------------------------------------
# _mean_crps
# ---------------------------------------------------------------------------

class TestMeanCrps:
    def test_returns_float(self):
        rows = [(80.0, 3.0, 82.0, "2026-01-01")] * 5
        score = _mean_crps(rows, a=0.0, b=1.0, c=1.0, d=1.0)
        assert isinstance(score, float)
        assert score >= 0

    def test_identity_params_reasonable(self):
        rows = [(float(80 + i), 3.0, float(82 + i), "2026-01-01") for i in range(10)]
        score = _mean_crps(rows, a=0.0, b=1.0, c=1.0, d=1.0)
        assert 0 < score < 5


# ---------------------------------------------------------------------------
# InsufficientDataError
# ---------------------------------------------------------------------------

class TestInsufficientDataError:
    def test_raised_when_too_few_rows(self):
        db = _db_with_intraday(n_rows=MIN_TRAINING_ROWS - 1)
        with pytest.raises(InsufficientDataError):
            fit_emos(db, city="Chicago", forecast_source="nws_open_meteo")

    def test_not_raised_when_sufficient(self):
        db = _db_with_intraday(n_rows=MIN_TRAINING_ROWS)
        fit = fit_emos(db, city="Chicago", forecast_source="nws_open_meteo")
        assert isinstance(fit, EmosFit)

    def test_error_message_includes_city(self):
        db = _db_with_intraday(n_rows=5)
        with pytest.raises(InsufficientDataError, match="Chicago"):
            fit_emos(db, city="Chicago", forecast_source="nws_open_meteo")


# ---------------------------------------------------------------------------
# fit_emos
# ---------------------------------------------------------------------------

class TestFitEmos:
    def test_returns_emosfif_with_four_params(self):
        db = _db_with_intraday(MIN_TRAINING_ROWS)
        fit = fit_emos(db, city="Chicago", forecast_source="nws_open_meteo")
        assert hasattr(fit, "a") and hasattr(fit, "b")
        assert hasattr(fit, "c") and hasattr(fit, "d")
        assert hasattr(fit, "crps_score")

    def test_crps_score_nonnegative(self):
        db = _db_with_intraday(MIN_TRAINING_ROWS)
        fit = fit_emos(db, city="Chicago", forecast_source="nws_open_meteo")
        assert fit.crps_score >= 0

    def test_b_slope_positive(self):
        # b should stay > 0 (model has positive skill)
        db = _db_with_intraday(MIN_TRAINING_ROWS)
        fit = fit_emos(db, city="Chicago", forecast_source="nws_open_meteo")
        assert fit.b > 0

    def test_different_sources_produce_independent_fits(self):
        db = _db_with_intraday(MIN_TRAINING_ROWS)
        fit1 = fit_emos(db, city="Chicago", forecast_source="nws_open_meteo")
        fit2 = fit_emos(db, city="Chicago", forecast_source="hrrr_nbm")
        # Both should succeed independently
        assert isinstance(fit1, EmosFit)
        assert isinstance(fit2, EmosFit)


# ---------------------------------------------------------------------------
# save_coefficients / load_coefficients
# ---------------------------------------------------------------------------

class TestSaveLoadCoefficients:
    def test_round_trip(self):
        db = _make_db()
        save_coefficients(db, "Chicago", "nws_open_meteo", 0.5, 0.98, 1.1, 0.9, 2.3)
        # Shadow: not returned by load_coefficients (ready_for_promotion=0)
        assert load_coefficients(db, "Chicago", "nws_open_meteo") is None

    def test_shadow_load(self):
        db = _make_db()
        save_coefficients(db, "Chicago", "nws_open_meteo", 0.5, 0.98, 1.1, 0.9, 2.3)
        fit = load_coefficients_shadow(db, "Chicago", "nws_open_meteo")
        assert fit is not None
        assert math.isclose(fit.a, 0.5)
        assert math.isclose(fit.b, 0.98)

    def test_model_mode_is_shadow(self):
        db = _make_db()
        save_coefficients(db, "Miami", "ecmwf_icon", 0.0, 1.0, 0.5, 1.0, 1.8)
        row = db._conn.execute(
            "SELECT model_mode, ready_for_promotion FROM emos_calibration "
            "WHERE city='Miami' AND forecast_source='ecmwf_icon'"
        ).fetchone()
        assert row["model_mode"] == "emos_shadow"
        assert row["ready_for_promotion"] == 0

    def test_upsert_updates_coefficients(self):
        db = _make_db()
        save_coefficients(db, "Chicago", "nws_open_meteo", 0.0, 1.0, 1.0, 1.0, 3.0)
        save_coefficients(db, "Chicago", "nws_open_meteo", 0.5, 0.99, 1.2, 0.95, 2.5)
        fit = load_coefficients_shadow(db, "Chicago", "nws_open_meteo")
        assert math.isclose(fit.a, 0.5)

    def test_different_sources_independent(self):
        db = _make_db()
        save_coefficients(db, "Chicago", "nws_open_meteo", 0.0, 1.0, 1.0, 1.0, 3.0)
        save_coefficients(db, "Chicago", "hrrr_nbm", 1.0, 0.95, 1.5, 0.8, 2.0)
        fit1 = load_coefficients_shadow(db, "Chicago", "nws_open_meteo")
        fit2 = load_coefficients_shadow(db, "Chicago", "hrrr_nbm")
        assert math.isclose(fit1.a, 0.0)
        assert math.isclose(fit2.a, 1.0)

    def test_legacy_not_overwritten_by_new_source(self):
        db = _make_db()
        save_coefficients(db, "Chicago", "nws_open_meteo", 0.0, 1.0, 1.0, 1.0, 3.0)
        save_coefficients(db, "Chicago", "hrrr_nbm", 9.9, 0.5, 2.0, 0.5, 5.0)
        # Legacy row unchanged
        legacy = load_coefficients_shadow(db, "Chicago", "nws_open_meteo")
        assert math.isclose(legacy.a, 0.0)

    def test_missing_returns_none(self):
        db = _make_db()
        assert load_coefficients(db, "Atlanta", "nws_open_meteo") is None
        assert load_coefficients_shadow(db, "Atlanta", "nws_open_meteo") is None


# ---------------------------------------------------------------------------
# check_ready_for_promotion
# ---------------------------------------------------------------------------

class TestCheckReadyForPromotion:
    def _db_with_promoted(self, cities):
        db = _make_db()
        for city in cities:
            db._conn.execute(
                "INSERT INTO emos_calibration "
                "(city, forecast_source, a, b, c, d, crps_score, model_mode, ready_for_promotion, fitted_at) "
                "VALUES (?, 'hrrr_nbm', 0, 1, 1, 1, 2.0, 'emos_shadow', 1, '2026-01-01')",
                (city,),
            )
        db._conn.commit()
        return db

    def test_all_cities_promoted(self):
        db = self._db_with_promoted(["Chicago", "Miami"])
        assert check_ready_for_promotion(db, "hrrr_nbm", ["Chicago", "Miami"]) is True

    def test_partial_cities_not_ready(self):
        db = self._db_with_promoted(["Chicago"])
        assert check_ready_for_promotion(db, "hrrr_nbm", ["Chicago", "Miami"]) is False

    def test_empty_cities_returns_false(self):
        db = _make_db()
        assert check_ready_for_promotion(db, "hrrr_nbm", []) is False

    def test_shadow_not_counted(self):
        db = _make_db()
        save_coefficients(db, "Chicago", "hrrr_nbm", 0, 1, 1, 1, 2.0)  # ready=0
        assert check_ready_for_promotion(db, "hrrr_nbm", ["Chicago"]) is False


# ---------------------------------------------------------------------------
# emos_mode: get/set active source
# ---------------------------------------------------------------------------

class TestEmosMode:
    def test_default_returns_legacy(self):
        db = _make_db()
        assert get_active_source(db, "Chicago") == LEGACY_SOURCE

    def test_set_and_get(self):
        db = _make_db()
        set_active_source(db, "Chicago", "hrrr_nbm")
        assert get_active_source(db, "Chicago") == "hrrr_nbm"

    def test_upsert_overwrites(self):
        db = _make_db()
        set_active_source(db, "Chicago", "hrrr_nbm")
        set_active_source(db, "Chicago", "ecmwf_icon")
        assert get_active_source(db, "Chicago") == "ecmwf_icon"

    def test_different_cities_independent(self):
        db = _make_db()
        set_active_source(db, "Chicago", "hrrr_nbm")
        assert get_active_source(db, "Miami") == LEGACY_SOURCE


# ---------------------------------------------------------------------------
# get_coefficients_for_city (priority fallback chain)
# ---------------------------------------------------------------------------

class TestGetCoefficientsForCity:
    def _db_promoted(self, city, source, a=0.5):
        db = _make_db()
        db._conn.execute(
            "INSERT INTO emos_calibration "
            "(city, forecast_source, a, b, c, d, crps_score, model_mode, ready_for_promotion, fitted_at) "
            "VALUES (?, ?, ?, 1.0, 1.0, 1.0, 2.0, 'emos_shadow', 1, '2026-01-01')",
            (city, source, a),
        )
        db._conn.commit()
        return db

    def test_returns_none_when_nothing_available(self):
        db = _make_db()
        assert get_coefficients_for_city(db, "Chicago") is None

    def test_returns_legacy_when_available(self):
        db = self._db_promoted("Chicago", LEGACY_SOURCE, a=0.1)
        fit = get_coefficients_for_city(db, "Chicago")
        assert fit is not None
        assert math.isclose(fit.a, 0.1)

    def test_explicit_source_takes_priority(self):
        db = self._db_promoted("Chicago", LEGACY_SOURCE, a=0.1)
        db._conn.execute(
            "INSERT INTO emos_calibration "
            "(city, forecast_source, a, b, c, d, crps_score, model_mode, ready_for_promotion, fitted_at) "
            "VALUES ('Chicago', 'hrrr_nbm', 9.9, 1.0, 1.0, 1.0, 2.0, 'emos_shadow', 1, '2026-01-01')"
        )
        db._conn.commit()
        fit = get_coefficients_for_city(db, "Chicago", forecast_source="hrrr_nbm")
        assert math.isclose(fit.a, 9.9)

    def test_falls_back_to_legacy_when_active_source_not_promoted(self):
        db = self._db_promoted("Chicago", LEGACY_SOURCE, a=0.1)
        set_active_source(db, "Chicago", "hrrr_nbm")  # active but no promoted row
        fit = get_coefficients_for_city(db, "Chicago")
        assert math.isclose(fit.a, 0.1)  # fell back to legacy


# ---------------------------------------------------------------------------
# apply_emos
# ---------------------------------------------------------------------------

class TestApplyEmos:
    def test_identity_params(self):
        fit = EmosFit(a=0.0, b=1.0, c=0.0, d=1.0, crps_score=1.0)
        mu, sigma = apply_emos(80.0, 3.0, fit)
        assert math.isclose(mu, 80.0)
        assert math.isclose(sigma, 3.0)

    def test_sigma_floored_at_0_1(self):
        fit = EmosFit(a=0.0, b=1.0, c=-99.0, d=0.001, crps_score=1.0)
        _, sigma = apply_emos(80.0, 3.0, fit)
        assert sigma >= 0.1

    def test_mu_corrected(self):
        fit = EmosFit(a=2.0, b=0.98, c=1.0, d=1.0, crps_score=1.0)
        mu, _ = apply_emos(80.0, 3.0, fit)
        assert math.isclose(mu, 2.0 + 0.98 * 80.0)

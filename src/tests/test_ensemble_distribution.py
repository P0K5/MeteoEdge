"""Unit tests for src/model/ensemble_distribution.py (issue #511).

Covers:
- shape: all keys present, sum(distribution.values()) == member_count
- no data -> None
- single member -> single bucket with count 1
- lowest lead_hours wins when duplicate (station, model, date) rows exist
- never calls live forecast fetchers
- EMOS fallback when no emos_calibration row exists (warns, uses raw mean)
- FORECAST_STACK filtering for ensemble_mean / active_stack_models
- DEB-weighted mean from model_weights (issue #552 — single source of truth,
  same table deb_weighting.get_weights() reads for live trading decisions)
- DEB fallback to equal weights when no model_weights row exists (debug log)
- DEB staleness guard: freshest model_weights row older than the allowed
  threshold falls back to equal weights with a visible warning (issue #552)

All DB-touching tests use Database(':memory:') — no live DB or network calls.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from src.data.db import Database
from src.model.ensemble_distribution import get_ensemble_distribution

STATION = "KORD"
CITY = "Chicago"
DATE = "2026-06-29"


def _db() -> Database:
    return Database(":memory:")


def _log(db, model, forecast_high_f, lead_hours=24, date=DATE, station=STATION):
    db.upsert_forecast_log_v2(
        station=station,
        model=model,
        date=date,
        forecast_high_f=forecast_high_f,
        lead_hours=lead_hours,
    )


def _log_weight(db, model, weight, city=CITY, age_days=0, rmse=0.0, sample_count=20):
    """Insert a model_weights row *age_days* days old (relative to today).

    Uses date.today() rather than a hardcoded calendar date so the test
    never depends on wall-clock date at the time it's run (staleness is
    computed relative to "today" in the implementation under test).
    """
    weight_date = (date.today() - timedelta(days=age_days)).isoformat()
    db.upsert_model_weight(
        city=city, model=model, date=weight_date, weight=weight,
        rmse=rmse, sample_count=sample_count,
    )


def test_ensemble_distribution_shape():
    db = _db()
    _log(db, "nws", 55.7)
    _log(db, "open_meteo", 57.2)

    result = get_ensemble_distribution(STATION, DATE, db)

    assert result is not None
    for key in (
        "ensemble_mean", "bias_corrected", "member_count",
        "range", "distribution", "active_stack_models",
    ):
        assert key in result
    assert sum(result["distribution"].values()) == result["member_count"]
    assert result["member_count"] == 2


def test_ensemble_distribution_no_data():
    db = _db()
    result = get_ensemble_distribution(STATION, DATE, db)
    assert result is None


def test_ensemble_distribution_single_member():
    db = _db()
    _log(db, "nws", 61.3)

    result = get_ensemble_distribution(STATION, DATE, db)

    assert result["member_count"] == 1
    assert result["distribution"] == {61: 1}
    assert result["range"] == (61.3, 61.3)


def test_ensemble_distribution_lowest_lead_hours():
    db = _db()
    _log(db, "nws", 50.0, lead_hours=24)
    _log(db, "nws", 53.0, lead_hours=6)

    result = get_ensemble_distribution(STATION, DATE, db)

    # Only one model row should be selected (the lowest lead_hours: 53.0)
    assert result["member_count"] == 1
    assert result["distribution"] == {53: 1}


def test_ensemble_distribution_no_live_fetchers():
    db = _db()
    _log(db, "nws", 55.0)
    _log(db, "open_meteo", 56.0)

    with patch("src.data.hrrr.fetch_hrrr_hourly") as mock_hrrr, \
         patch("src.data.gefs.fetch_gefs_ensemble") as mock_gefs, \
         patch("src.data.ecmwf_open.fetch_ecmwf_daily_high") as mock_ecmwf:
        result = get_ensemble_distribution(STATION, DATE, db)

    assert result is not None
    mock_hrrr.assert_not_called()
    mock_gefs.assert_not_called()
    mock_ecmwf.assert_not_called()


def test_ensemble_distribution_emos_fallback(caplog):
    db = _db()
    _log(db, "nws", 60.0)
    _log(db, "open_meteo", 62.0)
    # No emos_calibration row written -> fallback expected.

    with caplog.at_level(logging.WARNING):
        result = get_ensemble_distribution(STATION, DATE, db)

    assert result["bias_corrected"] == pytest.approx(result["ensemble_mean"])
    assert any("emos_calibration" in rec.message for rec in caplog.records)


def test_ensemble_distribution_emos_applied():
    db = _db()
    _log(db, "nws", 60.0)
    _log(db, "open_meteo", 60.0)
    db.upsert_emos_coefficients(
        city=CITY, model_mode="emos_shadow",
        a=2.0, b=1.0, c=0.5, d=1.0,
        forecast_source="nws_open_meteo",
    )

    result = get_ensemble_distribution(STATION, DATE, db)

    # mean is 60.0 (equal weights baseline) -> bias_corrected = a + b*mean = 62.0
    assert result["ensemble_mean"] == pytest.approx(60.0)
    assert result["bias_corrected"] == pytest.approx(62.0)


def test_ensemble_distribution_respects_forecast_stack():
    db = _db()
    db.set_config("FORECAST_STACK", "baseline")
    _log(db, "nws", 50.0)
    _log(db, "open_meteo", 60.0)
    _log(db, "hrrr", 100.0)
    _log(db, "gefs", 200.0)

    result = get_ensemble_distribution(STATION, DATE, db)

    # Equal weights across nws/open_meteo (no model_weights row) -> mean = 55.0
    assert result["ensemble_mean"] == pytest.approx(55.0)
    assert result["active_stack_models"] == ["nws", "open_meteo"]
    assert result["member_count"] == 4  # all 4 models counted in distribution


def test_ensemble_distribution_deb_weights():
    db = _db()
    db.set_config("FORECAST_STACK", "baseline")
    _log(db, "nws", 50.0)
    _log(db, "open_meteo", 60.0)
    _log_weight(db, "nws", 0.7, age_days=0)
    _log_weight(db, "open_meteo", 0.3, age_days=0)

    result = get_ensemble_distribution(STATION, DATE, db)

    expected = 50.0 * 0.7 + 60.0 * 0.3
    assert result["ensemble_mean"] == pytest.approx(expected)


def test_ensemble_distribution_deb_weights_one_day_old_not_stale():
    """A model_weights row exactly at the staleness threshold (1 day old) is
    still used — the guard only fires when the row is *older* than that."""
    db = _db()
    db.set_config("FORECAST_STACK", "baseline")
    _log(db, "nws", 50.0)
    _log(db, "open_meteo", 60.0)
    _log_weight(db, "nws", 0.7, age_days=1)
    _log_weight(db, "open_meteo", 0.3, age_days=1)

    result = get_ensemble_distribution(STATION, DATE, db)

    expected = 50.0 * 0.7 + 60.0 * 0.3
    assert result["ensemble_mean"] == pytest.approx(expected)


def test_ensemble_distribution_deb_fallback(caplog):
    db = _db()
    db.set_config("FORECAST_STACK", "baseline")
    _log(db, "nws", 50.0)
    _log(db, "open_meteo", 60.0)
    # No model_weights row written -> equal-weight fallback expected.

    with caplog.at_level(logging.DEBUG):
        result = get_ensemble_distribution(STATION, DATE, db)

    assert result["ensemble_mean"] == pytest.approx(55.0)
    assert any("equal weights" in rec.message for rec in caplog.records)


def test_ensemble_distribution_deb_weights_stale_fallback(caplog):
    """Issue #552: a model_weights row older than the staleness threshold
    must not be served — fall back to equal weights and log a warning so a
    silent write stall (like the one that caused #552) surfaces immediately.
    """
    db = _db()
    db.set_config("FORECAST_STACK", "baseline")
    _log(db, "nws", 50.0)
    _log(db, "open_meteo", 60.0)
    # Weights are 5 days stale, mirroring the real #552 incident.
    _log_weight(db, "nws", 0.7, age_days=5)
    _log_weight(db, "open_meteo", 0.3, age_days=5)

    with caplog.at_level(logging.WARNING):
        result = get_ensemble_distribution(STATION, DATE, db)

    # Equal-weight fallback, not the stale 0.7/0.3 blend.
    assert result["ensemble_mean"] == pytest.approx(55.0)
    assert any(
        "stale" in rec.message and CITY in rec.message
        for rec in caplog.records
    )


def test_ensemble_distribution_deb_weights_no_overlap_with_stack():
    """A fresh model_weights row that doesn't overlap the active stack (e.g.
    only has weight for a model not currently in FORECAST_STACK) falls back
    to equal weights across the allowed models, not an empty/zero blend."""
    db = _db()
    db.set_config("FORECAST_STACK", "baseline")
    _log(db, "nws", 50.0)
    _log(db, "open_meteo", 60.0)
    _log_weight(db, "hrrr", 1.0, age_days=0)  # not in the baseline stack

    result = get_ensemble_distribution(STATION, DATE, db)

    assert result["ensemble_mean"] == pytest.approx(55.0)

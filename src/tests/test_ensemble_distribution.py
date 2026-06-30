"""Unit tests for src/model/ensemble_distribution.py (issue #511).

Covers:
- shape: all keys present, sum(distribution.values()) == member_count
- no data -> None
- single member -> single bucket with count 1
- lowest lead_hours wins when duplicate (station, model, date) rows exist
- never calls live forecast fetchers
- EMOS fallback when no emos_calibration row exists (warns, uses raw mean)
- FORECAST_STACK filtering for ensemble_mean / active_stack_models
- DEB-weighted mean from deb_weight_log
- DEB fallback to equal weights when no deb_weight_log row exists (debug log)

All DB-touching tests use Database(':memory:') — no live DB or network calls.
"""
from __future__ import annotations

import json
import logging
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

    # Equal weights across nws/open_meteo (no deb_weight_log row) -> mean = 55.0
    assert result["ensemble_mean"] == pytest.approx(55.0)
    assert result["active_stack_models"] == ["nws", "open_meteo"]
    assert result["member_count"] == 4  # all 4 models counted in distribution


def test_ensemble_distribution_deb_weights():
    db = _db()
    db.set_config("FORECAST_STACK", "baseline")
    _log(db, "nws", 50.0)
    _log(db, "open_meteo", 60.0)
    db.log_deb_weights(CITY, DATE, json.dumps({"nws": 0.7, "open_meteo": 0.3}))

    result = get_ensemble_distribution(STATION, DATE, db)

    expected = 50.0 * 0.7 + 60.0 * 0.3
    assert result["ensemble_mean"] == pytest.approx(expected)


def test_ensemble_distribution_deb_fallback(caplog):
    db = _db()
    db.set_config("FORECAST_STACK", "baseline")
    _log(db, "nws", 50.0)
    _log(db, "open_meteo", 60.0)
    # No deb_weight_log row written -> equal-weight fallback expected.

    with caplog.at_level(logging.DEBUG):
        result = get_ensemble_distribution(STATION, DATE, db)

    assert result["ensemble_mean"] == pytest.approx(55.0)
    assert any("equal weights" in rec.message for rec in caplog.records)

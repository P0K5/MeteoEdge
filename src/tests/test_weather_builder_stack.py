"""Tests for scan-time per-model forecast carrying (issue #760).

Covers:
- src.weather.builder._extra_stack_model_highs: reads the already-captured
  model_forecast_log rows (lowest lead per model) restricted to whichever
  models the ACTIVE FORECAST_STACK needs beyond the baseline pair, with no
  DB query at all when the active stack is baseline.
- src.weather.builder._build_one_station: wires _extra_stack_model_highs into
  the WeatherState it returns (hrrr_forecast_f / nbm_forecast_f /
  ecmwf_forecast_f / icon_forecast_f).
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.data.db import Database

STATION = "KORD"
CITY = "Chicago"
TODAY = datetime.now(timezone.utc).date().isoformat()


def _db() -> Database:
    return Database(":memory:")


def _log(db, model, forecast_high_f, lead_hours=24, date=TODAY, station=STATION):
    db.upsert_forecast_log_v2(
        station=station, model=model, date=date,
        forecast_high_f=forecast_high_f, lead_hours=lead_hours,
    )


# ---------------------------------------------------------------------------
# _extra_stack_model_highs
# ---------------------------------------------------------------------------

class TestExtraStackModelHighs:
    def test_baseline_stack_returns_empty_and_skips_the_query_entirely(self):
        """Issue #760 DoD: baseline serving is byte-for-byte unchanged --
        this must not even query model_forecast_log when the active stack
        (unset, defaulting to baseline) doesn't need it."""
        from src.weather.builder import _extra_stack_model_highs

        db = _db()  # FORECAST_STACK unset -> baseline default
        _log(db, "hrrr", 76.0)  # captured independent of active stack

        with patch.object(db, "get_forecast_log_for_date") as mock_query:
            result = _extra_stack_model_highs(STATION, db)

        assert result == {}
        mock_query.assert_not_called()

    def test_baseline_stack_explicit_returns_empty(self):
        from src.weather.builder import _extra_stack_model_highs

        db = _db()
        db.set_config("FORECAST_STACK", "baseline")
        _log(db, "hrrr", 76.0)

        assert _extra_stack_model_highs(STATION, db) == {}

    def test_hrrr_nbm_stack_returns_hrrr_and_nbm_only(self):
        from src.weather.builder import _extra_stack_model_highs

        db = _db()
        db.set_config("FORECAST_STACK", "hrrr_nbm")
        _log(db, "nws", 80.0)
        _log(db, "open_meteo", 82.0)
        _log(db, "hrrr", 76.0)
        _log(db, "nbm", 78.0)
        # Captured but out of scope for hrrr_nbm -- must not leak in.
        _log(db, "ecmwf", 99.0)

        result = _extra_stack_model_highs(STATION, db)
        assert result == {"hrrr": 76.0, "nbm": 78.0}

    def test_intl_ecmwf_icon_stack_returns_ecmwf_and_icon_only(self):
        from src.weather.builder import _extra_stack_model_highs

        db = _db()
        db.set_config("FORECAST_STACK", "intl_ecmwf_icon")
        _log(db, "nws", 20.0)
        _log(db, "open_meteo", 22.0)
        _log(db, "ecmwf", 19.0)
        _log(db, "icon", 21.0)
        _log(db, "hrrr", 99.0)  # out of scope for intl_ecmwf_icon

        result = _extra_stack_model_highs(STATION, db)
        assert result == {"ecmwf": 19.0, "icon": 21.0}

    def test_uses_lowest_lead_hours_per_model_same_as_ensemble_distribution(self):
        """Same source/selection rule as get_ensemble_distribution: lowest
        lead_hours row wins when a model has multiple captures for the date."""
        from src.weather.builder import _extra_stack_model_highs

        db = _db()
        db.set_config("FORECAST_STACK", "hrrr_nbm")
        _log(db, "hrrr", 90.0, lead_hours=24)
        _log(db, "hrrr", 76.0, lead_hours=6)  # closest to valid -- should win

        result = _extra_stack_model_highs(STATION, db)
        assert result["hrrr"] == pytest.approx(76.0)

    def test_no_rows_for_date_returns_empty(self):
        from src.weather.builder import _extra_stack_model_highs

        db = _db()
        db.set_config("FORECAST_STACK", "hrrr_nbm")
        assert _extra_stack_model_highs(STATION, db) == {}

    def test_unrecognised_stack_name_returns_empty(self):
        from src.weather.builder import _extra_stack_model_highs

        db = _db()
        db.set_config("FORECAST_STACK", "nonexistent_stack")
        _log(db, "hrrr", 76.0)

        assert _extra_stack_model_highs(STATION, db) == {}


# ---------------------------------------------------------------------------
# _build_one_station -- WeatherState wiring
# ---------------------------------------------------------------------------

_FAKE_METAR = [
    {"temp": 30.0, "reportTime": "2026-06-24T02:00:00+00:00"},
]


def _patch_build_deps():
    return [
        patch("src.weather.builder.fetch_all_metars_today", return_value=_FAKE_METAR),
        patch(
            "src.weather.builder.compute_daily_high",
            return_value=(86.0, datetime(2026, 6, 24, 18, 0, tzinfo=timezone.utc)),
        ),
        patch("src.weather.builder.fetch_nws_forecast_high", return_value=80.0),
        patch("src.weather.builder.fetch_secondary_forecast", return_value=84.0),
        patch("src.weather.builder.fetch_gfs_forecast_high", return_value=82.0),
        patch("src.weather.builder.fetch_hourly_temp_now", return_value=None),
        patch("src.weather.builder.compute_deb_mu_f", return_value=82.0),
        patch(
            "src.weather.builder.now_local",
            return_value=datetime(2026, 6, 24, 13, 0, tzinfo=timezone.utc),
        ),
        patch(
            "src.weather.builder.sunset_local",
            return_value=datetime(2026, 6, 24, 20, 0, tzinfo=timezone.utc),
        ),
        patch("src.weather.builder.get_canonical_station_feeds", return_value=[STATION]),
        patch("src.weather.builder.get_source_priority", return_value=[]),
        patch("src.weather.builder.refresh_weights"),
        patch(
            "src.weather.builder.get_weights",
            return_value={"nws": 0.5, "open_meteo": 0.5, "gfs": 0.0},
        ),
        patch("src.weather.builder.compute_correction", return_value=None),
        patch("src.weather.builder.apply_residual_correction", return_value=(82.0, {})),
    ]


class TestBuildOneStationCarriesStackForecasts:
    def test_hrrr_nbm_active_stack_populates_hrrr_and_nbm_attrs(self):
        from src.weather.builder import _build_one_station

        db = _db()
        db.set_config("FORECAST_STACK", "hrrr_nbm")
        _log(db, "hrrr", 76.0)
        _log(db, "nbm", 78.0)

        patches = _patch_build_deps()
        for p in patches:
            p.start()
        try:
            state = _build_one_station(STATION, 41.98, -87.91, CITY, unit="F", db=db)
        finally:
            for p in patches:
                p.stop()

        assert state is not None
        assert state.hrrr_forecast_f == pytest.approx(76.0)
        assert state.nbm_forecast_f == pytest.approx(78.0)
        assert state.ecmwf_forecast_f is None
        assert state.icon_forecast_f is None
        # Baseline members are unaffected by the expanded-stack plumbing.
        assert state.forecast_high_f == pytest.approx(80.0)
        assert state.secondary_forecast_f == pytest.approx(84.0)

    def test_baseline_active_stack_leaves_new_attrs_none(self):
        """Issue #760 DoD: baseline serving is byte-for-byte unchanged --
        even with hrrr/nbm rows captured for the station, WeatherState must
        not carry them while FORECAST_STACK stays baseline (default)."""
        from src.weather.builder import _build_one_station

        db = _db()  # FORECAST_STACK unset -> baseline default
        _log(db, "hrrr", 76.0)
        _log(db, "nbm", 78.0)

        patches = _patch_build_deps()
        for p in patches:
            p.start()
        try:
            state = _build_one_station(STATION, 41.98, -87.91, CITY, unit="F", db=db)
        finally:
            for p in patches:
                p.stop()

        assert state is not None
        assert state.hrrr_forecast_f is None
        assert state.nbm_forecast_f is None
        assert state.ecmwf_forecast_f is None
        assert state.icon_forecast_f is None
        assert state.forecast_high_f == pytest.approx(80.0)
        assert state.secondary_forecast_f == pytest.approx(84.0)

    def test_no_db_leaves_new_attrs_none(self):
        """db=None (e.g. some pricing-path callers) must not blow up and must
        leave the new attributes at their WeatherState defaults."""
        from src.weather.builder import _build_one_station

        patches = _patch_build_deps()
        for p in patches:
            p.start()
        try:
            state = _build_one_station(STATION, 41.98, -87.91, CITY, unit="F", db=None)
        finally:
            for p in patches:
                p.stop()

        assert state is not None
        assert state.hrrr_forecast_f is None
        assert state.nbm_forecast_f is None
        assert state.ecmwf_forecast_f is None
        assert state.icon_forecast_f is None

"""Unit tests for src/data/nbm.py.

All network / herbie / cfgrib calls are mocked so the suite runs fully
offline and without the heavy GRIB dependencies installed.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.data.nbm import (
    NbmForecast,
    _floor_to_nbm_cycle,
    _fxx_range_for_date,
    _is_conus,
    _kelvin_to_f,
    fetch_nbm_daily_high,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DENVER_LAT = 39.73
DENVER_LON = -104.99
CYCLE_DT = datetime(2024, 6, 15, 18, 0, 0, tzinfo=timezone.utc)
TARGET_DATE = date(2024, 6, 16)

# Typical summer Denver TMAX in Kelvin (≈ 90 °F)
TMAX_K = 305.37  # (305.37 - 273.15) * 9/5 + 32 ≈ 89.9 °F


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestKelvinToF:
    def test_freezing(self):
        assert abs(_kelvin_to_f(273.15) - 32.0) < 0.01

    def test_boiling(self):
        assert abs(_kelvin_to_f(373.15) - 212.0) < 0.01

    def test_typical_summer(self):
        # 305.37 K ≈ 89.9 °F
        assert abs(_kelvin_to_f(TMAX_K) - 89.9) < 0.2


class TestIsConus:
    def test_denver_is_conus(self):
        assert _is_conus(DENVER_LAT, DENVER_LON) is True

    def test_london_is_not_conus(self):
        assert _is_conus(51.5, -0.12) is False

    def test_north_alaska_is_not_conus(self):
        assert _is_conus(71.0, -156.0) is False

    def test_south_boundary(self):
        # Exactly on boundary → inside
        assert _is_conus(20.0, -100.0) is True

    def test_outside_south(self):
        assert _is_conus(19.9, -100.0) is False


class TestFloorToNbmCycle:
    def test_already_on_cycle(self):
        dt = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        assert _floor_to_nbm_cycle(dt) == dt

    def test_mid_cycle(self):
        dt = datetime(2024, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
        expected = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        assert _floor_to_nbm_cycle(dt) == expected

    def test_just_before_next_cycle(self):
        dt = datetime(2024, 6, 15, 17, 59, 0, tzinfo=timezone.utc)
        expected = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        assert _floor_to_nbm_cycle(dt) == expected


class TestFxxRangeForDate:
    def test_covers_target_date(self):
        # Cycle at 18 UTC on 2024-06-15; target = 2024-06-16
        # fxx 6 → 2024-06-16T00Z, fxx 29 → 2024-06-16T23Z
        fxx_vals = _fxx_range_for_date(CYCLE_DT, TARGET_DATE)
        assert 6 in fxx_vals
        assert 29 in fxx_vals

    def test_does_not_include_wrong_date(self):
        fxx_vals = _fxx_range_for_date(CYCLE_DT, TARGET_DATE)
        # fxx=5 → 2024-06-15T23Z (day before target) should NOT be included
        assert 5 not in fxx_vals
        # fxx=30 → 2024-06-17T00Z (day after) should NOT be included
        assert 30 not in fxx_vals


# ---------------------------------------------------------------------------
# fetch_nbm_daily_high — happy path
# ---------------------------------------------------------------------------


def _make_mock_cycle_resolver(cycle_dt):
    return patch("src.data.nbm._resolve_nbm_cycle", return_value=cycle_dt)


class TestFetchNbmDailyHighHappyPath:
    """Happy path: TMAX_2m is available via herbie."""

    def test_returns_nbm_forecast(self, tmp_path):
        with (
            _make_mock_cycle_resolver(CYCLE_DT),
            patch("src.data.nbm._get_cache_dir", return_value=tmp_path),
            patch("src.data.nbm._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.nbm._fetch_tmax_herbie", return_value=TMAX_K),
        ):
            result = fetch_nbm_daily_high(DENVER_LAT, DENVER_LON, target_date=TARGET_DATE)

        assert result is not None
        assert isinstance(result, NbmForecast)
        assert abs(result.forecast_high_f - _kelvin_to_f(TMAX_K)) < 0.01
        assert result.valid_date == TARGET_DATE
        assert result.cycle_ts == CYCLE_DT

    def test_forecast_high_reasonable_fahrenheit(self, tmp_path):
        """A summer Denver TMAX should land in 70–110 °F."""
        with (
            _make_mock_cycle_resolver(CYCLE_DT),
            patch("src.data.nbm._get_cache_dir", return_value=tmp_path),
            patch("src.data.nbm._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.nbm._fetch_tmax_herbie", return_value=TMAX_K),
        ):
            result = fetch_nbm_daily_high(DENVER_LAT, DENVER_LON, target_date=TARGET_DATE)

        assert result is not None
        assert 70.0 <= result.forecast_high_f <= 110.0

    def test_cache_hit_skips_herbie(self, tmp_path):
        """When a fresh cache file exists, herbie must not be called."""
        from src.data.nbm import _cache_path_nbm
        cache_file = _cache_path_nbm(tmp_path, CYCLE_DT, TARGET_DATE, DENVER_LAT, DENVER_LON)
        cache_file.write_text(f"{_kelvin_to_f(TMAX_K):.4f}")

        mock_tmax = MagicMock()
        with (
            _make_mock_cycle_resolver(CYCLE_DT),
            patch("src.data.nbm._get_cache_dir", return_value=tmp_path),
            patch("src.data.nbm._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.nbm._fetch_tmax_herbie", mock_tmax),
        ):
            result = fetch_nbm_daily_high(DENVER_LAT, DENVER_LON, target_date=TARGET_DATE)

        mock_tmax.assert_not_called()
        assert result is not None
        assert abs(result.forecast_high_f - _kelvin_to_f(TMAX_K)) < 0.01


# ---------------------------------------------------------------------------
# Stale-cycle fallback
# ---------------------------------------------------------------------------


class TestStaleCycleFallback:
    """When TMAX is unavailable, fall back to hourly TMP_2m max."""

    def test_falls_back_to_tmp_hourly_max(self, tmp_path):
        hourly_max_k = 304.0  # ≈ 87.5 °F

        with (
            _make_mock_cycle_resolver(CYCLE_DT),
            patch("src.data.nbm._get_cache_dir", return_value=tmp_path),
            patch("src.data.nbm._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.nbm._fetch_tmax_herbie", return_value=None),
            patch("src.data.nbm._fetch_tmp_hourly_max", return_value=hourly_max_k),
        ):
            result = fetch_nbm_daily_high(DENVER_LAT, DENVER_LON, target_date=TARGET_DATE)

        assert result is not None
        assert abs(result.forecast_high_f - _kelvin_to_f(hourly_max_k)) < 0.01
        assert result.valid_date == TARGET_DATE
        assert result.cycle_ts == CYCLE_DT

    def test_returns_none_when_both_sources_unavailable(self, tmp_path):
        with (
            _make_mock_cycle_resolver(CYCLE_DT),
            patch("src.data.nbm._get_cache_dir", return_value=tmp_path),
            patch("src.data.nbm._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.nbm._fetch_tmax_herbie", return_value=None),
            patch("src.data.nbm._fetch_tmp_hourly_max", return_value=None),
        ):
            result = fetch_nbm_daily_high(DENVER_LAT, DENVER_LON, target_date=TARGET_DATE)

        assert result is None


# ---------------------------------------------------------------------------
# Out-of-CONUS returns None
# ---------------------------------------------------------------------------


class TestOutOfConus:
    def test_london_returns_none(self):
        result = fetch_nbm_daily_high(51.5, -0.12, target_date=TARGET_DATE)
        assert result is None

    def test_tokyo_returns_none(self):
        result = fetch_nbm_daily_high(35.68, 139.69, target_date=TARGET_DATE)
        assert result is None

    def test_alaska_returns_none(self):
        # Northern Alaska — outside CONUS lat/lon bounds
        result = fetch_nbm_daily_high(71.0, -156.0, target_date=TARGET_DATE)
        assert result is None


# ---------------------------------------------------------------------------
# No available cycle returns None
# ---------------------------------------------------------------------------


class TestNoCycleAvailable:
    def test_returns_none_when_no_cycle(self, tmp_path):
        with (
            patch("src.data.nbm._resolve_nbm_cycle", return_value=None),
            patch("src.data.nbm._get_cache_dir", return_value=tmp_path),
            patch("src.data.nbm._get_cache_ttl_hours", return_value=6.0),
        ):
            result = fetch_nbm_daily_high(DENVER_LAT, DENVER_LON, target_date=TARGET_DATE)

        assert result is None


# ---------------------------------------------------------------------------
# Default target_date = tomorrow
# ---------------------------------------------------------------------------


class TestStrDateCoercion:
    """fetch_nbm_daily_high must accept target_date as an ISO string (bug #500)."""

    def test_string_target_date_does_not_raise(self, tmp_path):
        """Passing target_date as a string must not cause AttributeError."""
        with (
            _make_mock_cycle_resolver(CYCLE_DT),
            patch("src.data.nbm._get_cache_dir", return_value=tmp_path),
            patch("src.data.nbm._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.nbm._fetch_tmax_herbie", return_value=TMAX_K),
        ):
            result = fetch_nbm_daily_high(
                DENVER_LAT, DENVER_LON, target_date="2026-07-01"
            )
        assert result is not None
        from datetime import date as _date
        assert result.valid_date == _date(2026, 7, 1)

    def test_string_date_produces_same_result_as_date_object(self, tmp_path):
        """Result from string and date-object target_date must be equivalent."""
        from datetime import date as _date
        td = _date(2026, 7, 1)
        with (
            _make_mock_cycle_resolver(CYCLE_DT),
            patch("src.data.nbm._get_cache_dir", return_value=tmp_path),
            patch("src.data.nbm._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.nbm._fetch_tmax_herbie", return_value=TMAX_K),
        ):
            result_str = fetch_nbm_daily_high(
                DENVER_LAT, DENVER_LON, target_date="2026-07-01"
            )
            result_date = fetch_nbm_daily_high(
                DENVER_LAT, DENVER_LON, target_date=td
            )
        assert result_str is not None
        assert result_date is not None
        assert result_str.valid_date == result_date.valid_date


class TestDefaultTargetDate:
    def test_defaults_to_tomorrow(self, tmp_path):
        captured = {}

        def mock_tmax(cycle_dt, target_date, lat, lon):
            captured["target_date"] = target_date
            return TMAX_K

        with (
            _make_mock_cycle_resolver(CYCLE_DT),
            patch("src.data.nbm._get_cache_dir", return_value=tmp_path),
            patch("src.data.nbm._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.nbm._fetch_tmax_herbie", side_effect=mock_tmax),
        ):
            fetch_nbm_daily_high(DENVER_LAT, DENVER_LON)

        expected_tomorrow = date.today() + timedelta(days=1)
        assert captured.get("target_date") == expected_tomorrow

"""Unit tests for src/data/hrrr.py.

All network calls and GRIB I/O are mocked; the suite runs fully offline without
herbie or cfgrib installed.

Patch targets
-------------
hrrr.py imports grib_cache at call time via ``from src.data import grib_cache``.
We therefore patch at ``src.data.grib_cache.<name>`` so the mock is seen by
the module under test.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.data.hrrr import (
    HourlyTemp,
    _is_conus,
    _kelvin_to_fahrenheit,
    fetch_hrrr_hourly,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CYCLE_DT = datetime(2024, 6, 15, 18, 0, 0, tzinfo=timezone.utc)
DENVER_LAT = 39.73
DENVER_LON = -104.99
# Tokyo — outside CONUS (wrong longitude)
TOKYO_LAT = 35.68
TOKYO_LON = 139.69
# Honolulu — latitude OK but longitude outside CONUS range
HONOLULU_LAT = 21.30
HONOLULU_LON = -157.86


# ---------------------------------------------------------------------------
# _kelvin_to_fahrenheit
# ---------------------------------------------------------------------------

class TestKelvinToFahrenheit:
    def test_freezing(self):
        # 273.15 K == 32 °F
        assert abs(_kelvin_to_fahrenheit(273.15) - 32.0) < 1e-6

    def test_boiling(self):
        # 373.15 K == 212 °F
        assert abs(_kelvin_to_fahrenheit(373.15) - 212.0) < 1e-6

    def test_sane_summer_value(self):
        # 295.15 K ≈ 71.6 °F
        result = _kelvin_to_fahrenheit(295.15)
        assert 70.0 < result < 73.0


# ---------------------------------------------------------------------------
# _is_conus
# ---------------------------------------------------------------------------

class TestIsConus:
    def test_denver_is_conus(self):
        assert _is_conus(DENVER_LAT, DENVER_LON) is True

    def test_tokyo_not_conus(self):
        assert _is_conus(TOKYO_LAT, TOKYO_LON) is False

    def test_honolulu_not_conus(self):
        # Honolulu lon -157.86 is west of -130 boundary
        assert _is_conus(HONOLULU_LAT, HONOLULU_LON) is False

    def test_lat_below_conus(self):
        assert _is_conus(10.0, -100.0) is False

    def test_lat_above_conus(self):
        assert _is_conus(60.0, -100.0) is False

    def test_boundary_lat_low(self):
        assert _is_conus(20.0, -100.0) is True

    def test_boundary_lat_high(self):
        assert _is_conus(55.0, -100.0) is True

    def test_boundary_lon_west(self):
        assert _is_conus(40.0, -130.0) is True

    def test_boundary_lon_east(self):
        assert _is_conus(40.0, -60.0) is True


# ---------------------------------------------------------------------------
# fetch_hrrr_hourly — happy path
# ---------------------------------------------------------------------------

class TestFetchHrrrHourlyHappyPath:
    """All 18 forecast hours return a valid temperature."""

    def _make_fetch_side_effect(self, kelvin: float):
        """Always return the same Kelvin value regardless of fxx."""
        def _side_effect(var, lat, lon, fxx=0, db=None):
            return kelvin
        return _side_effect

    def test_returns_18_results(self):
        kelvin = 295.15  # sane summer value
        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch(
                "src.data.grib_cache.fetch_hrrr_field",
                side_effect=self._make_fetch_side_effect(kelvin),
            ),
        ):
            results = fetch_hrrr_hourly(DENVER_LAT, DENVER_LON, station="KDEN")

        assert len(results) == 18

    def test_result_type(self):
        kelvin = 295.15
        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch(
                "src.data.grib_cache.fetch_hrrr_field",
                side_effect=self._make_fetch_side_effect(kelvin),
            ),
        ):
            results = fetch_hrrr_hourly(DENVER_LAT, DENVER_LON)

        for r in results:
            assert isinstance(r, HourlyTemp)
            assert isinstance(r.ts_utc, datetime)
            assert r.ts_utc.tzinfo is not None  # must be timezone-aware
            assert isinstance(r.temp_f, float)

    def test_valid_times_are_cycle_plus_fxx(self):
        kelvin = 295.15
        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch(
                "src.data.grib_cache.fetch_hrrr_field",
                side_effect=self._make_fetch_side_effect(kelvin),
            ),
        ):
            results = fetch_hrrr_hourly(DENVER_LAT, DENVER_LON)

        for i, r in enumerate(results):
            expected_fxx = i + 1  # fxx=1..18
            expected_ts = CYCLE_DT + timedelta(hours=expected_fxx)
            assert r.ts_utc == expected_ts, (
                f"fxx={expected_fxx}: expected {expected_ts}, got {r.ts_utc}"
            )

    def test_kelvin_converted_to_fahrenheit(self):
        kelvin = 273.15  # exactly 32 °F
        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch(
                "src.data.grib_cache.fetch_hrrr_field",
                side_effect=self._make_fetch_side_effect(kelvin),
            ),
        ):
            results = fetch_hrrr_hourly(DENVER_LAT, DENVER_LON)

        assert len(results) == 18
        for r in results:
            assert abs(r.temp_f - 32.0) < 1e-6


# ---------------------------------------------------------------------------
# fetch_hrrr_hourly — missing-cycle fallback
# ---------------------------------------------------------------------------

class TestFetchHrrrHourlyMissingCycle:
    """When _resolve_latest_cycle returns None, fetch_hrrr_hourly returns []."""

    def test_returns_empty_list_not_none(self):
        with patch("src.data.grib_cache._resolve_latest_cycle", return_value=None):
            result = fetch_hrrr_hourly(DENVER_LAT, DENVER_LON, station="KDEN")

        assert result == []
        assert result is not None  # must be empty list, not None

    def test_does_not_call_fetch_hrrr_field_when_no_cycle(self):
        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=None),
            patch("src.data.grib_cache.fetch_hrrr_field") as mock_fetch,
        ):
            fetch_hrrr_hourly(DENVER_LAT, DENVER_LON)

        mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# fetch_hrrr_hourly — out-of-CONUS returns empty list
# ---------------------------------------------------------------------------

class TestFetchHrrrHourlyOutOfConus:
    def test_tokyo_returns_empty_list(self):
        # bounds check happens before any network call — no mock needed for grib
        with patch("src.data.grib_cache._resolve_latest_cycle") as mock_cycle:
            result = fetch_hrrr_hourly(TOKYO_LAT, TOKYO_LON, station="RJTT")

        assert result == []
        mock_cycle.assert_not_called()

    def test_honolulu_returns_empty_list(self):
        with patch("src.data.grib_cache._resolve_latest_cycle") as mock_cycle:
            result = fetch_hrrr_hourly(HONOLULU_LAT, HONOLULU_LON)

        assert result == []
        mock_cycle.assert_not_called()

    def test_out_of_conus_never_calls_fetch(self):
        with (
            patch("src.data.grib_cache._resolve_latest_cycle") as mock_cycle,
            patch("src.data.grib_cache.fetch_hrrr_field") as mock_fetch,
        ):
            result = fetch_hrrr_hourly(TOKYO_LAT, TOKYO_LON)

        assert result == []
        mock_cycle.assert_not_called()
        mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# fetch_hrrr_hourly — partial fetch failures
# ---------------------------------------------------------------------------

class TestFetchHrrrHourlyPartialFailure:
    """Some forecast hours fail; only successful ones are returned."""

    def test_partial_results_returned(self):
        # Return a value for even fxx, None for odd
        def _side_effect(var, lat, lon, fxx=0, db=None):
            return 295.15 if fxx % 2 == 0 else None

        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache.fetch_hrrr_field", side_effect=_side_effect),
        ):
            results = fetch_hrrr_hourly(DENVER_LAT, DENVER_LON)

        # fxx 2,4,6,8,10,12,14,16,18 → 9 results
        assert len(results) == 9

    def test_all_hours_fail_returns_empty_list(self):
        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache.fetch_hrrr_field", return_value=None),
        ):
            result = fetch_hrrr_hourly(DENVER_LAT, DENVER_LON)

        assert result == []

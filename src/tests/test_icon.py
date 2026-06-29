"""Unit tests for src/data/icon.py.

All network calls and GRIB I/O are mocked; the suite runs fully offline without
herbie or cfgrib installed.

Patch targets
-------------
icon.py imports grib_cache at call time via ``from src.data import grib_cache``.
We therefore patch at ``src.data.grib_cache.<name>`` so the mock is seen by
the module under test.  _resolve_icon_cycle is patched on the icon module itself
since it lives there.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.data.icon import (
    _fetch_icon_grib,
    _is_eu_domain,
    _kelvin_to_fahrenheit,
    _resolve_icon_cycle,
    fetch_icon_hourly,
)
from src.data.hrrr import HourlyTemp

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CYCLE_DT = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

# Milan Malpensa — well within ICON-EU domain
MILAN_LAT = 45.63
MILAN_LON = 8.72
MILAN_STATION = "LIMC"

# Denver, CO — outside EU domain
DENVER_LAT = 39.73
DENVER_LON = -104.99

# Tokyo — outside EU domain
TOKYO_LAT = 35.68
TOKYO_LON = 139.69

# Helsinki — inside EU domain
HELSINKI_LAT = 60.32
HELSINKI_LON = 24.96
HELSINKI_STATION = "EFHK"


# ---------------------------------------------------------------------------
# _kelvin_to_fahrenheit
# ---------------------------------------------------------------------------

class TestKelvinToFahrenheit:
    def test_freezing(self):
        assert abs(_kelvin_to_fahrenheit(273.15) - 32.0) < 1e-6

    def test_boiling(self):
        assert abs(_kelvin_to_fahrenheit(373.15) - 212.0) < 1e-6

    def test_sane_european_summer(self):
        # 293.15 K = 20 °C = 68 °F
        result = _kelvin_to_fahrenheit(293.15)
        assert abs(result - 68.0) < 1e-6


# ---------------------------------------------------------------------------
# _is_eu_domain
# ---------------------------------------------------------------------------

class TestIsEuDomain:
    def test_milan_is_eu(self):
        assert _is_eu_domain(MILAN_LAT, MILAN_LON) is True

    def test_helsinki_is_eu(self):
        assert _is_eu_domain(HELSINKI_LAT, HELSINKI_LON) is True

    def test_london_is_eu(self):
        # EGLC: 51.50, -0.05
        assert _is_eu_domain(51.50, -0.05) is True

    def test_denver_not_eu(self):
        assert _is_eu_domain(DENVER_LAT, DENVER_LON) is False

    def test_tokyo_not_eu(self):
        assert _is_eu_domain(TOKYO_LAT, TOKYO_LON) is False

    def test_lat_below_eu(self):
        # Below 29 N
        assert _is_eu_domain(28.0, 10.0) is False

    def test_lat_above_eu(self):
        # Above 72 N
        assert _is_eu_domain(73.0, 10.0) is False

    def test_lon_too_far_west(self):
        # West of -25
        assert _is_eu_domain(50.0, -30.0) is False

    def test_lon_too_far_east(self):
        # East of 45
        assert _is_eu_domain(50.0, 50.0) is False

    def test_boundary_lat_min(self):
        assert _is_eu_domain(29.0, 10.0) is True

    def test_boundary_lat_max(self):
        assert _is_eu_domain(72.0, 10.0) is True

    def test_boundary_lon_min(self):
        assert _is_eu_domain(50.0, -25.0) is True

    def test_boundary_lon_max(self):
        assert _is_eu_domain(50.0, 45.0) is True


# ---------------------------------------------------------------------------
# fetch_icon_hourly — happy path (European station)
# ---------------------------------------------------------------------------

class TestFetchIconHourlyHappyPath:
    """All 24 forecast hours return a valid temperature."""

    def _make_grib_slice_path(self) -> Path:
        return Path("/fake/icon_TMP_2m_2024.grib2")

    def test_returns_24_results(self):
        kelvin = 293.15  # 20 °C = 68 °F
        fake_path = self._make_grib_slice_path()
        with (
            patch("src.data.icon._resolve_icon_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.grib_cache._get_cache_dir", return_value=Path(".grib_cache")),
            patch("src.data.icon._fetch_icon_grib", return_value=fake_path),
            patch("src.data.grib_cache._read_grib_nearest", return_value=kelvin),
        ):
            results = fetch_icon_hourly(MILAN_LAT, MILAN_LON, station=MILAN_STATION)

        assert len(results) == 24

    def test_result_type(self):
        kelvin = 293.15
        fake_path = self._make_grib_slice_path()
        with (
            patch("src.data.icon._resolve_icon_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.grib_cache._get_cache_dir", return_value=Path(".grib_cache")),
            patch("src.data.icon._fetch_icon_grib", return_value=fake_path),
            patch("src.data.grib_cache._read_grib_nearest", return_value=kelvin),
        ):
            results = fetch_icon_hourly(MILAN_LAT, MILAN_LON)

        for r in results:
            assert isinstance(r, HourlyTemp)
            assert isinstance(r.ts_utc, datetime)
            assert isinstance(r.temp_f, float)

    def test_valid_times_are_cycle_plus_fxx(self):
        kelvin = 293.15
        fake_path = self._make_grib_slice_path()
        with (
            patch("src.data.icon._resolve_icon_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.grib_cache._get_cache_dir", return_value=Path(".grib_cache")),
            patch("src.data.icon._fetch_icon_grib", return_value=fake_path),
            patch("src.data.grib_cache._read_grib_nearest", return_value=kelvin),
        ):
            results = fetch_icon_hourly(MILAN_LAT, MILAN_LON)

        for i, r in enumerate(results):
            expected_fxx = i + 1  # fxx=1..24
            expected_ts = CYCLE_DT + timedelta(hours=expected_fxx)
            assert r.ts_utc == expected_ts, (
                f"fxx={expected_fxx}: expected {expected_ts}, got {r.ts_utc}"
            )

    def test_kelvin_converted_to_fahrenheit(self):
        kelvin = 273.15  # exactly 32 °F
        fake_path = self._make_grib_slice_path()
        with (
            patch("src.data.icon._resolve_icon_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.grib_cache._get_cache_dir", return_value=Path(".grib_cache")),
            patch("src.data.icon._fetch_icon_grib", return_value=fake_path),
            patch("src.data.grib_cache._read_grib_nearest", return_value=kelvin),
        ):
            results = fetch_icon_hourly(MILAN_LAT, MILAN_LON)

        assert len(results) == 24
        for r in results:
            assert abs(r.temp_f - 32.0) < 1e-6

    def test_station_label_passed_for_logging(self):
        """Ensure the function accepts a station kwarg without error."""
        kelvin = 280.0
        fake_path = self._make_grib_slice_path()
        with (
            patch("src.data.icon._resolve_icon_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.grib_cache._get_cache_dir", return_value=Path(".grib_cache")),
            patch("src.data.icon._fetch_icon_grib", return_value=fake_path),
            patch("src.data.grib_cache._read_grib_nearest", return_value=kelvin),
        ):
            results = fetch_icon_hourly(HELSINKI_LAT, HELSINKI_LON, station=HELSINKI_STATION)

        assert len(results) == 24


# ---------------------------------------------------------------------------
# fetch_icon_hourly — non-EU station returns []
# ---------------------------------------------------------------------------

class TestFetchIconHourlyNonEU:
    def test_denver_returns_empty_list(self):
        with patch("src.data.icon._resolve_icon_cycle") as mock_cycle:
            result = fetch_icon_hourly(DENVER_LAT, DENVER_LON, station="KDEN")

        assert result == []
        mock_cycle.assert_not_called()

    def test_tokyo_returns_empty_list(self):
        with patch("src.data.icon._resolve_icon_cycle") as mock_cycle:
            result = fetch_icon_hourly(TOKYO_LAT, TOKYO_LON)

        assert result == []
        mock_cycle.assert_not_called()

    def test_non_eu_never_calls_grib_fetch(self):
        with (
            patch("src.data.icon._resolve_icon_cycle") as mock_cycle,
            patch("src.data.icon._fetch_icon_grib") as mock_fetch,
        ):
            result = fetch_icon_hourly(DENVER_LAT, DENVER_LON)

        assert result == []
        mock_cycle.assert_not_called()
        mock_fetch.assert_not_called()

    def test_south_of_domain_returns_empty_list(self):
        # 20 N is below the 29 N min
        result = fetch_icon_hourly(20.0, 10.0)
        assert result == []

    def test_east_of_domain_returns_empty_list(self):
        # Eastern Turkey / Iran — outside ICON-EU east boundary
        result = fetch_icon_hourly(40.0, 50.0)
        assert result == []


# ---------------------------------------------------------------------------
# fetch_icon_hourly — missing cycle fallback
# ---------------------------------------------------------------------------

class TestFetchIconHourlyMissingCycle:
    def test_returns_empty_list_not_none(self):
        with patch("src.data.icon._resolve_icon_cycle", return_value=None):
            result = fetch_icon_hourly(MILAN_LAT, MILAN_LON, station=MILAN_STATION)

        assert result == []
        assert result is not None  # must be empty list, not None

    def test_does_not_call_grib_fetch_when_no_cycle(self):
        with (
            patch("src.data.icon._resolve_icon_cycle", return_value=None),
            patch("src.data.icon._fetch_icon_grib") as mock_fetch,
        ):
            fetch_icon_hourly(MILAN_LAT, MILAN_LON)

        mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# fetch_icon_hourly — partial fetch failures
# ---------------------------------------------------------------------------

class TestFetchIconHourlyPartialFailure:
    """Some forecast hours fail; only successful ones are returned."""

    def test_partial_results_returned_when_some_grib_slices_missing(self):
        fake_path = Path("/fake/icon.grib2")
        kelvin = 293.15

        # Return None for odd fxx, a path for even fxx
        def _grib_side_effect(cycle_dt, fxx, cache_dir, ttl_hours):
            return fake_path if fxx % 2 == 0 else None

        with (
            patch("src.data.icon._resolve_icon_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.grib_cache._get_cache_dir", return_value=Path(".grib_cache")),
            patch("src.data.icon._fetch_icon_grib", side_effect=_grib_side_effect),
            patch("src.data.grib_cache._read_grib_nearest", return_value=kelvin),
        ):
            results = fetch_icon_hourly(MILAN_LAT, MILAN_LON)

        # fxx 2,4,6,8,10,12,14,16,18,20,22,24 → 12 results
        assert len(results) == 12

    def test_all_hours_fail_returns_empty_list(self):
        with (
            patch("src.data.icon._resolve_icon_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.grib_cache._get_cache_dir", return_value=Path(".grib_cache")),
            patch("src.data.icon._fetch_icon_grib", return_value=None),
        ):
            result = fetch_icon_hourly(MILAN_LAT, MILAN_LON)

        assert result == []

    def test_grib_read_returns_none_skips_hour(self):
        fake_path = Path("/fake/icon.grib2")
        with (
            patch("src.data.icon._resolve_icon_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.grib_cache._get_cache_dir", return_value=Path(".grib_cache")),
            patch("src.data.icon._fetch_icon_grib", return_value=fake_path),
            patch("src.data.grib_cache._read_grib_nearest", return_value=None),
        ):
            result = fetch_icon_hourly(MILAN_LAT, MILAN_LON)

        assert result == []

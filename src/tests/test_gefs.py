"""Unit tests for src/data/gefs.py.

All network calls and GRIB I/O are mocked; the suite runs fully offline without
herbie or cfgrib installed.

Patch targets
-------------
gefs.py imports grib_cache at call time via ``from src.data import grib_cache``.
We patch at ``src.data.grib_cache.<name>`` so the mock is seen by the module
under test.  _read_grib_nearest is also patched at module level.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from src.data.gefs import (
    GEFSResult,
    _GEFS_MEMBERS,
    _forecast_hours_for_date,
    fetch_gefs_ensemble_daily_high,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# A GEFS 00Z cycle; target date falls within its forecast window.
CYCLE_DT = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
TARGET_DATE = date(2024, 6, 16)  # next calendar day — fxx ~24..47 cover it

DENVER_LAT = 39.73
DENVER_LON = -104.99

# A "sane" Kelvin value for summer (~22 °C)
SUMMER_K = 295.15
# 273.15 K == exactly 32 °F
FREEZING_K = 273.15


# ---------------------------------------------------------------------------
# _forecast_hours_for_date
# ---------------------------------------------------------------------------

class TestForecastHoursForDate:
    def test_target_next_day_gives_nonempty_list(self):
        hours = _forecast_hours_for_date(CYCLE_DT, TARGET_DATE)
        assert len(hours) > 0

    def test_all_fxx_are_multiples_of_3(self):
        hours = _forecast_hours_for_date(CYCLE_DT, TARGET_DATE)
        for fxx in hours:
            assert fxx % 3 == 0, f"fxx={fxx} is not a multiple of 3"

    def test_valid_times_fall_on_target_date(self):
        from datetime import timedelta
        hours = _forecast_hours_for_date(CYCLE_DT, TARGET_DATE)
        for fxx in hours:
            valid = CYCLE_DT + timedelta(hours=fxx)
            assert valid.date() == TARGET_DATE, (
                f"fxx={fxx} valid={valid.date()} != {TARGET_DATE}"
            )

    def test_target_same_day_as_cycle(self):
        same_day = CYCLE_DT.date()
        hours = _forecast_hours_for_date(CYCLE_DT, same_day)
        # fxx=0 is the analysis hour — it should be included
        assert 0 in hours

    def test_target_far_future_returns_empty(self):
        far_date = date(2025, 1, 1)
        hours = _forecast_hours_for_date(CYCLE_DT, far_date)
        assert hours == []


# ---------------------------------------------------------------------------
# GEFSResult dataclass
# ---------------------------------------------------------------------------

class TestGEFSResult:
    def test_default_empty(self):
        r = GEFSResult()
        assert r.member_highs == []
        assert r.incomplete is False

    def test_with_values(self):
        r = GEFSResult(member_highs=[85.1, 87.3], incomplete=False)
        assert len(r.member_highs) == 2
        assert r.incomplete is False

    def test_incomplete_flag(self):
        r = GEFSResult(member_highs=[], incomplete=True)
        assert r.incomplete is True


# ---------------------------------------------------------------------------
# fetch_gefs_ensemble_daily_high — missing cycle
# ---------------------------------------------------------------------------

class TestFetchGefsNoAvailableCycle:
    def test_returns_empty_result_when_no_cycle(self):
        with patch("src.data.grib_cache._resolve_latest_cycle", return_value=None):
            result = fetch_gefs_ensemble_daily_high(
                DENVER_LAT, DENVER_LON, "KDEN", TARGET_DATE
            )

        assert isinstance(result, GEFSResult)
        assert result.member_highs == []
        assert result.incomplete is True

    def test_no_grib_calls_when_no_cycle(self):
        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=None),
            patch("src.data.grib_cache._fetch_grib_slice") as mock_fetch,
        ):
            fetch_gefs_ensemble_daily_high(DENVER_LAT, DENVER_LON, None, TARGET_DATE)

        mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# fetch_gefs_ensemble_daily_high — happy path (all members succeed)
# ---------------------------------------------------------------------------

class TestFetchGefsHappyPath:
    """All members return a valid temperature for every forecast hour."""

    def _make_patches(self, tmp_path: Path, kelvin: float):
        """Return a list of context-manager patches for a fully successful run."""
        fake_path = tmp_path / "fake.grib2"
        fake_path.write_bytes(b"fake")

        return [
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", return_value=fake_path),
            patch("src.data.grib_cache._read_grib_nearest", return_value=kelvin),
        ]

    def test_returns_gefs_result(self, tmp_path):
        patches = self._make_patches(tmp_path, SUMMER_K)
        with patches[0], patches[1], patches[2]:
            result = fetch_gefs_ensemble_daily_high(
                DENVER_LAT, DENVER_LON, "KDEN", TARGET_DATE
            )
        assert isinstance(result, GEFSResult)

    def test_returns_31_members_when_all_succeed(self, tmp_path):
        patches = self._make_patches(tmp_path, SUMMER_K)
        with patches[0], patches[1], patches[2]:
            result = fetch_gefs_ensemble_daily_high(
                DENVER_LAT, DENVER_LON, "KDEN", TARGET_DATE
            )
        # 1 control + 30 perturbations = 31 members total
        assert len(result.member_highs) == 31

    def test_all_member_highs_are_floats(self, tmp_path):
        patches = self._make_patches(tmp_path, SUMMER_K)
        with patches[0], patches[1], patches[2]:
            result = fetch_gefs_ensemble_daily_high(
                DENVER_LAT, DENVER_LON, "KDEN", TARGET_DATE
            )
        for val in result.member_highs:
            assert isinstance(val, float)

    def test_kelvin_converted_to_fahrenheit_freezing(self, tmp_path):
        """273.15 K should convert to exactly 32 °F."""
        patches = self._make_patches(tmp_path, FREEZING_K)
        with patches[0], patches[1], patches[2]:
            result = fetch_gefs_ensemble_daily_high(
                DENVER_LAT, DENVER_LON, "KDEN", TARGET_DATE
            )
        for val in result.member_highs:
            assert abs(val - 32.0) < 1e-6, f"Expected 32.0 °F, got {val}"

    def test_incomplete_false_when_all_succeed(self, tmp_path):
        patches = self._make_patches(tmp_path, SUMMER_K)
        with patches[0], patches[1], patches[2]:
            result = fetch_gefs_ensemble_daily_high(
                DENVER_LAT, DENVER_LON, "KDEN", TARGET_DATE
            )
        assert result.incomplete is False

    def test_no_station_label_works(self, tmp_path):
        """Passing station=None should not raise."""
        patches = self._make_patches(tmp_path, SUMMER_K)
        with patches[0], patches[1], patches[2]:
            result = fetch_gefs_ensemble_daily_high(
                DENVER_LAT, DENVER_LON, None, TARGET_DATE
            )
        assert isinstance(result, GEFSResult)


# ---------------------------------------------------------------------------
# fetch_gefs_ensemble_daily_high — partial member failures
# ---------------------------------------------------------------------------

class TestFetchGefsPartialFailures:
    def test_incomplete_true_when_some_members_fail(self, tmp_path):
        """If some members' grib slices return None, incomplete=True."""
        fake_path = tmp_path / "fake.grib2"
        fake_path.write_bytes(b"fake")

        call_count = {"n": 0}

        def selective_fetch(model, var, cycle_dt, fxx, cache_dir, ttl_hours):
            call_count["n"] += 1
            # Fail every other call at the slice level
            return fake_path if call_count["n"] % 2 == 0 else None

        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", side_effect=selective_fetch),
            patch("src.data.grib_cache._read_grib_nearest", return_value=SUMMER_K),
        ):
            result = fetch_gefs_ensemble_daily_high(
                DENVER_LAT, DENVER_LON, "KDEN", TARGET_DATE
            )

        assert result.incomplete is True

    def test_all_members_fail_returns_empty(self, tmp_path):
        """If every member fails, member_highs is empty and incomplete=True."""
        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", return_value=None),
        ):
            result = fetch_gefs_ensemble_daily_high(
                DENVER_LAT, DENVER_LON, "KDEN", TARGET_DATE
            )

        assert result.member_highs == []
        assert result.incomplete is True

    def test_partial_members_returned(self, tmp_path):
        """When only some members have any data, only those are in member_highs."""
        fake_path = tmp_path / "fake.grib2"
        fake_path.write_bytes(b"fake")

        # Only the first 5 members succeed (return a path); the rest get None
        call_count = {"n": 0}
        fxx_per_member = len(_forecast_hours_for_date(CYCLE_DT, TARGET_DATE))
        success_threshold = 5 * fxx_per_member

        def selective_fetch(model, var, cycle_dt, fxx, cache_dir, ttl_hours):
            call_count["n"] += 1
            return fake_path if call_count["n"] <= success_threshold else None

        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", side_effect=selective_fetch),
            patch("src.data.grib_cache._read_grib_nearest", return_value=SUMMER_K),
        ):
            result = fetch_gefs_ensemble_daily_high(
                DENVER_LAT, DENVER_LON, "KDEN", TARGET_DATE
            )

        assert 0 < len(result.member_highs) <= 31
        assert result.incomplete is True

    def test_read_error_marks_incomplete(self, tmp_path):
        """An exception from _read_grib_nearest should mark incomplete=True."""
        fake_path = tmp_path / "fake.grib2"
        fake_path.write_bytes(b"fake")

        read_count = {"n": 0}

        def selective_read(path, lat, lon):
            read_count["n"] += 1
            if read_count["n"] <= 3:
                raise RuntimeError("eccodes not found")
            return SUMMER_K

        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", return_value=fake_path),
            patch("src.data.grib_cache._read_grib_nearest", side_effect=selective_read),
        ):
            result = fetch_gefs_ensemble_daily_high(
                DENVER_LAT, DENVER_LON, "KDEN", TARGET_DATE
            )

        assert result.incomplete is True


# ---------------------------------------------------------------------------
# fetch_gefs_ensemble_daily_high — daily-high semantics
# ---------------------------------------------------------------------------

class TestFetchGefsDailyHighSemantics:
    def test_max_kelvin_used_not_first(self, tmp_path):
        """The daily-high should be the max across forecast hours, not the first."""
        fake_path = tmp_path / "fake.grib2"
        fake_path.write_bytes(b"fake")

        target_fxx = _forecast_hours_for_date(CYCLE_DT, TARGET_DATE)
        assert len(target_fxx) >= 2, "Need at least 2 forecast hours for this test"

        # Vary Kelvin by fxx index so we can identify which was the max
        kelvin_by_call = {}
        call_count = {"n": 0}

        def varying_read(path, lat, lon):
            n = call_count["n"] % len(target_fxx)
            call_count["n"] += 1
            # Return an ascending sequence: 280, 281, 282, ... K
            return 280.0 + float(n)

        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", return_value=fake_path),
            patch("src.data.grib_cache._read_grib_nearest", side_effect=varying_read),
        ):
            result = fetch_gefs_ensemble_daily_high(
                DENVER_LAT, DENVER_LON, "KDEN", TARGET_DATE
            )

        # Each member's daily high should be the max (last value) in the sequence
        expected_max_k = 280.0 + float(len(target_fxx) - 1)
        from src.data.hrrr import _kelvin_to_fahrenheit as k2f
        expected_f = k2f(expected_max_k)

        for val in result.member_highs:
            assert abs(val - expected_f) < 1e-6, (
                f"Expected daily-high {expected_f:.4f} °F, got {val:.4f}"
            )


# ---------------------------------------------------------------------------
# SUPPORTED_VARS in grib_cache
# ---------------------------------------------------------------------------

class TestGribCacheSupportedVars:
    def test_gefs_tmp_2m_present(self):
        from src.data.grib_cache import SUPPORTED_VARS
        assert "GEFS_TMP_2m" in SUPPORTED_VARS

    def test_gefs_tmp_2m_matcher(self):
        from src.data.grib_cache import SUPPORTED_VARS
        assert SUPPORTED_VARS["GEFS_TMP_2m"] == ":TMP:2 m above ground:"

    def test_existing_vars_still_present(self):
        """Adding GEFS_TMP_2m must not remove existing variables."""
        from src.data.grib_cache import SUPPORTED_VARS
        assert "TMP_2m" in SUPPORTED_VARS
        assert "DPT_2m" in SUPPORTED_VARS

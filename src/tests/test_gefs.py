"""Unit tests for src/data/gefs.py.

All network calls and GRIB I/O are mocked; the suite runs fully offline
without herbie or cfgrib installed.

Core focus: verifying that each GEFS ensemble member is fetched with its own
distinct ``member`` kwarg and cache key, fixing issue #479 where all 31
members shared one cached GRIB slice.

Patch targets: gefs.py imports grib_cache at call time via
``from src.data import grib_cache``.  We patch at
``src.data.grib_cache.<name>`` so the mock is seen by the module under test.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.data.gefs import (
    GEFS_MEMBERS,
    GEFSMemberForecast,
    fetch_gefs_ensemble,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CYCLE_DT = datetime(2024, 6, 15, 6, 0, 0, tzinfo=timezone.utc)
DENVER_LAT = 39.73
DENVER_LON = -104.99
SUMMER_K = 295.15   # ~22 °C


# ---------------------------------------------------------------------------
# GEFS_MEMBERS sanity
# ---------------------------------------------------------------------------

class TestGEFSMembers:
    def test_31_members_total(self):
        assert len(GEFS_MEMBERS) == 31

    def test_control_member_is_first(self):
        assert GEFS_MEMBERS[0] == "gec00"

    def test_perturbed_members_gep01_to_gep30(self):
        expected = [f"gep{i:02d}" for i in range(1, 31)]
        assert GEFS_MEMBERS[1:] == expected

    def test_all_members_unique(self):
        assert len(set(GEFS_MEMBERS)) == 31


# ---------------------------------------------------------------------------
# GEFSMemberForecast dataclass
# ---------------------------------------------------------------------------

class TestGEFSMemberForecast:
    def test_fields_stored_correctly(self):
        ts = datetime(2024, 6, 15, 12, tzinfo=timezone.utc)
        r = GEFSMemberForecast(member="gec00", ts_utc=ts, temp_k=295.0)
        assert r.member == "gec00"
        assert r.ts_utc == ts
        assert r.temp_k == 295.0


# ---------------------------------------------------------------------------
# _cache_key: distinct per-member keys (the root cause of #479)
# ---------------------------------------------------------------------------

class TestCacheKeyDistinctPerMember:
    def test_31_distinct_keys_for_31_members(self):
        from src.data.grib_cache import _cache_key

        keys = {
            _cache_key("gefs", "TMP_2m", CYCLE_DT, 6, member=m)
            for m in GEFS_MEMBERS
        }
        assert len(keys) == 31

    def test_member_embedded_in_key(self):
        from src.data.grib_cache import _cache_key

        key = _cache_key("gefs", "TMP_2m", CYCLE_DT, 6, member="gec00")
        assert "gec00" in key

    def test_different_members_produce_different_keys(self):
        from src.data.grib_cache import _cache_key

        key1 = _cache_key("gefs", "TMP_2m", CYCLE_DT, 6, member="gec00")
        key2 = _cache_key("gefs", "TMP_2m", CYCLE_DT, 6, member="gep01")
        assert key1 != key2

    def test_no_member_key_differs_from_member_key(self):
        from src.data.grib_cache import _cache_key

        key_with = _cache_key("gefs", "TMP_2m", CYCLE_DT, 6, member="gec00")
        key_without = _cache_key("gefs", "TMP_2m", CYCLE_DT, 6)
        assert key_with != key_without


# ---------------------------------------------------------------------------
# fetch_gefs_ensemble — no available cycle
# ---------------------------------------------------------------------------

class TestFetchGefsNoAvailableCycle:
    def test_returns_empty_list_when_no_cycle(self):
        with patch("src.data.grib_cache._resolve_latest_cycle", return_value=None):
            result = fetch_gefs_ensemble(DENVER_LAT, DENVER_LON, "KDEN")
        assert result == []

    def test_no_grib_slice_calls_when_no_cycle(self):
        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=None),
            patch("src.data.grib_cache._fetch_grib_slice") as mock_fetch,
        ):
            fetch_gefs_ensemble(DENVER_LAT, DENVER_LON)
        mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# fetch_gefs_ensemble — 31 distinct member kwargs (core #479 fix)
# ---------------------------------------------------------------------------

class TestFetchGefsDistinctMemberKwargs:
    """_fetch_grib_slice must be called with 31 different member kwargs."""

    def test_31_distinct_member_kwargs_passed(self, tmp_path):
        fake_path = tmp_path / "fake.grib2"
        fake_path.write_bytes(b"fake")

        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", return_value=fake_path) as mock_fetch,
            patch("src.data.grib_cache._read_grib_nearest", return_value=SUMMER_K),
        ):
            fetch_gefs_ensemble(DENVER_LAT, DENVER_LON, "KDEN", fxx=6)

        called_members = [c.kwargs["member"] for c in mock_fetch.call_args_list]
        assert len(set(called_members)) == 31, (
            f"Expected 31 distinct member kwargs; got {len(set(called_members))}: "
            f"{sorted(set(called_members))}"
        )

    def test_member_kwargs_match_GEFS_MEMBERS(self, tmp_path):
        fake_path = tmp_path / "fake.grib2"
        fake_path.write_bytes(b"fake")

        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", return_value=fake_path) as mock_fetch,
            patch("src.data.grib_cache._read_grib_nearest", return_value=SUMMER_K),
        ):
            fetch_gefs_ensemble(DENVER_LAT, DENVER_LON, fxx=6)

        called_members = sorted(c.kwargs["member"] for c in mock_fetch.call_args_list)
        assert called_members == sorted(GEFS_MEMBERS)


# ---------------------------------------------------------------------------
# fetch_gefs_ensemble — happy path results
# ---------------------------------------------------------------------------

class TestFetchGefsHappyPath:
    def test_returns_31_forecasts_when_all_succeed(self, tmp_path):
        fake_path = tmp_path / "fake.grib2"
        fake_path.write_bytes(b"fake")

        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", return_value=fake_path),
            patch("src.data.grib_cache._read_grib_nearest", return_value=SUMMER_K),
        ):
            result = fetch_gefs_ensemble(DENVER_LAT, DENVER_LON, "KDEN", fxx=6)

        assert len(result) == 31

    def test_result_members_match_GEFS_MEMBERS(self, tmp_path):
        fake_path = tmp_path / "fake.grib2"
        fake_path.write_bytes(b"fake")

        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", return_value=fake_path),
            patch("src.data.grib_cache._read_grib_nearest", return_value=SUMMER_K),
        ):
            result = fetch_gefs_ensemble(DENVER_LAT, DENVER_LON, fxx=6)

        assert sorted(r.member for r in result) == sorted(GEFS_MEMBERS)

    def test_temp_k_preserved(self, tmp_path):
        fake_path = tmp_path / "fake.grib2"
        fake_path.write_bytes(b"fake")

        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", return_value=fake_path),
            patch("src.data.grib_cache._read_grib_nearest", return_value=SUMMER_K),
        ):
            result = fetch_gefs_ensemble(DENVER_LAT, DENVER_LON, fxx=6)

        for r in result:
            assert r.temp_k == SUMMER_K

    def test_valid_time_is_cycle_plus_fxx(self, tmp_path):
        fake_path = tmp_path / "fake.grib2"
        fake_path.write_bytes(b"fake")
        fxx = 6

        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", return_value=fake_path),
            patch("src.data.grib_cache._read_grib_nearest", return_value=SUMMER_K),
        ):
            result = fetch_gefs_ensemble(DENVER_LAT, DENVER_LON, fxx=fxx)

        expected_ts = CYCLE_DT + timedelta(hours=fxx)
        for r in result:
            assert r.ts_utc == expected_ts

    def test_station_none_does_not_raise(self, tmp_path):
        fake_path = tmp_path / "fake.grib2"
        fake_path.write_bytes(b"fake")

        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", return_value=fake_path),
            patch("src.data.grib_cache._read_grib_nearest", return_value=SUMMER_K),
        ):
            result = fetch_gefs_ensemble(DENVER_LAT, DENVER_LON, station=None)

        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# fetch_gefs_ensemble — partial failures
# ---------------------------------------------------------------------------

class TestFetchGefsPartialFailures:
    def test_all_members_fail_returns_empty_list(self):
        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", return_value=None),
        ):
            result = fetch_gefs_ensemble(DENVER_LAT, DENVER_LON, fxx=6)

        assert result == []

    def test_skip_failed_members_return_rest(self, tmp_path):
        fake_path = tmp_path / "fake.grib2"
        fake_path.write_bytes(b"fake")
        call_count = {"n": 0}

        def selective_fetch(model, var, cycle_dt, fxx, cache_dir, ttl_hours, member=None):
            call_count["n"] += 1
            return fake_path if call_count["n"] % 2 == 0 else None

        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", side_effect=selective_fetch),
            patch("src.data.grib_cache._read_grib_nearest", return_value=SUMMER_K),
        ):
            result = fetch_gefs_ensemble(DENVER_LAT, DENVER_LON, fxx=6)

        assert 0 < len(result) < 31

    def test_skip_member_on_read_exception(self, tmp_path):
        fake_path = tmp_path / "fake.grib2"
        fake_path.write_bytes(b"fake")
        call_count = {"n": 0}

        def selective_read(path, lat, lon):
            call_count["n"] += 1
            if call_count["n"] <= 5:
                raise RuntimeError("eccodes not found")
            return SUMMER_K

        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", return_value=fake_path),
            patch("src.data.grib_cache._read_grib_nearest", side_effect=selective_read),
        ):
            result = fetch_gefs_ensemble(DENVER_LAT, DENVER_LON, fxx=6)

        assert len(result) == 26  # 31 members - 5 that raised


# ---------------------------------------------------------------------------
# SUPPORTED_VARS in grib_cache includes GEFS entry
# ---------------------------------------------------------------------------

class TestGribCacheSupportedVars:
    def test_gefs_tmp_2m_present(self):
        from src.data.grib_cache import SUPPORTED_VARS
        assert "GEFS_TMP_2m" in SUPPORTED_VARS

    def test_gefs_tmp_2m_matcher(self):
        from src.data.grib_cache import SUPPORTED_VARS
        assert SUPPORTED_VARS["GEFS_TMP_2m"] == ":TMP:2 m above ground:"

    def test_existing_hrrr_vars_still_present(self):
        from src.data.grib_cache import SUPPORTED_VARS
        assert "TMP_2m" in SUPPORTED_VARS
        assert "DPT_2m" in SUPPORTED_VARS

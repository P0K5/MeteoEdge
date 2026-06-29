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
    _member_label,
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

    def test_control_member_is_int_zero(self):
        """herbie 2025.12.0 requires integer members; control is 0."""
        assert GEFS_MEMBERS[0] == 0

    def test_perturbed_members_are_ints_1_to_30(self):
        assert GEFS_MEMBERS[1:] == list(range(1, 31))

    def test_all_members_unique(self):
        assert len(set(GEFS_MEMBERS)) == 31

    def test_all_members_are_ints(self):
        assert all(isinstance(m, int) for m in GEFS_MEMBERS)


class TestMemberLabel:
    def test_control_label(self):
        assert _member_label(0) == "gec00"

    def test_perturbed_labels(self):
        assert _member_label(1) == "gep01"
        assert _member_label(10) == "gep10"
        assert _member_label(30) == "gep30"

    def test_all_labels_are_strings(self):
        assert all(isinstance(_member_label(i), str) for i in range(31))


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

        # member kwarg to _fetch_grib_slice is the string label (cache key)
        called_members = [c.kwargs["member"] for c in mock_fetch.call_args_list]
        assert len(set(called_members)) == 31, (
            f"Expected 31 distinct member kwargs; got {len(set(called_members))}: "
            f"{sorted(set(called_members))}"
        )

    def test_member_kwargs_are_string_labels(self, tmp_path):
        """_fetch_grib_slice receives string labels (e.g. 'gec00') for cache keys."""
        fake_path = tmp_path / "fake.grib2"
        fake_path.write_bytes(b"fake")

        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", return_value=fake_path) as mock_fetch,
            patch("src.data.grib_cache._read_grib_nearest", return_value=SUMMER_K),
        ):
            fetch_gefs_ensemble(DENVER_LAT, DENVER_LON, fxx=6)

        called_members = [c.kwargs["member"] for c in mock_fetch.call_args_list]
        assert all(isinstance(m, str) for m in called_members), (
            "member kwarg to _fetch_grib_slice must be a string label"
        )
        expected_labels = sorted(_member_label(i) for i in GEFS_MEMBERS)
        assert sorted(called_members) == expected_labels

    def test_herbie_member_kwargs_are_ints(self, tmp_path):
        """herbie_member kwarg passed to _fetch_grib_slice must be an int."""
        fake_path = tmp_path / "fake.grib2"
        fake_path.write_bytes(b"fake")

        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", return_value=fake_path) as mock_fetch,
            patch("src.data.grib_cache._read_grib_nearest", return_value=SUMMER_K),
        ):
            fetch_gefs_ensemble(DENVER_LAT, DENVER_LON, fxx=6)

        herbie_members = [c.kwargs["herbie_member"] for c in mock_fetch.call_args_list]
        assert all(isinstance(m, int) for m in herbie_members), (
            "herbie_member kwarg must be int for herbie 2025.12.0 GEFS"
        )
        assert sorted(herbie_members) == list(range(31))


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

    def test_result_members_are_string_labels(self, tmp_path):
        """GEFSMemberForecast.member must be human-readable string labels."""
        fake_path = tmp_path / "fake.grib2"
        fake_path.write_bytes(b"fake")

        with (
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", return_value=fake_path),
            patch("src.data.grib_cache._read_grib_nearest", return_value=SUMMER_K),
        ):
            result = fetch_gefs_ensemble(DENVER_LAT, DENVER_LON, fxx=6)

        result_members = sorted(r.member for r in result)
        expected_labels = sorted(_member_label(i) for i in GEFS_MEMBERS)
        assert result_members == expected_labels
        # Spot-check: control member label
        assert "gec00" in result_members

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

        def selective_fetch(model, var, cycle_dt, fxx, cache_dir, ttl_hours,
                            member=None, herbie_member=None):
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


# ---------------------------------------------------------------------------
# Bug #500 — _resolve_latest_cycle passes member=0 (int) for GEFS
# ---------------------------------------------------------------------------

class TestResolveLatestCycleGefsMemberInt:
    """_resolve_latest_cycle must pass member=0 to Herbie when model='gefs'.

    Without this, herbie 2025.12.0 raises AttributeError on a memberless
    GEFS Herbie object, causing the resolver to always return None.
    """

    def _herbie_sys_patch(self, MockHerbieClass):
        """Context manager that injects MockHerbieClass into sys.modules['herbie']."""
        import sys
        import types
        fake_module = types.ModuleType("herbie")
        fake_module.Herbie = MockHerbieClass
        return patch.dict(sys.modules, {"herbie": fake_module})

    def test_herbie_called_with_member_zero_for_gefs(self):
        from unittest.mock import MagicMock
        from src.data.grib_cache import _resolve_latest_cycle

        MockHerbie = MagicMock()
        instance = MagicMock()
        instance.grib = "s3://fake/path.grib2"
        MockHerbie.return_value = instance

        with self._herbie_sys_patch(MockHerbie):
            _resolve_latest_cycle("gefs", fxx=6)

        for c in MockHerbie.call_args_list:
            assert c.kwargs.get("member") == 0, (
                f"Expected member=0 (int) in Herbie kwargs for GEFS; got: {c.kwargs}"
            )

    def test_herbie_member_is_int_not_string(self):
        """member kwarg forwarded to Herbie resolver must be int, not str."""
        from unittest.mock import MagicMock
        from src.data.grib_cache import _resolve_latest_cycle

        MockHerbie = MagicMock()
        instance = MagicMock()
        instance.grib = "s3://fake/path.grib2"
        MockHerbie.return_value = instance

        with self._herbie_sys_patch(MockHerbie):
            _resolve_latest_cycle("gefs")

        for c in MockHerbie.call_args_list:
            member_val = c.kwargs.get("member")
            assert isinstance(member_val, int), (
                f"member kwarg to Herbie must be int for GEFS; got {type(member_val)}"
            )

    def test_non_gefs_models_do_not_pass_member(self):
        """Non-GEFS models must NOT have member injected."""
        from unittest.mock import MagicMock
        from src.data.grib_cache import _resolve_latest_cycle

        MockHerbie = MagicMock()
        instance = MagicMock()
        instance.grib = "s3://fake/path.grib2"
        MockHerbie.return_value = instance

        with self._herbie_sys_patch(MockHerbie):
            _resolve_latest_cycle("hrrr")

        for c in MockHerbie.call_args_list:
            assert "member" not in c.kwargs, (
                f"Non-GEFS model should not have member kwarg; got: {c.kwargs}"
            )

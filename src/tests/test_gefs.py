"""Unit tests for src/data/gefs.py and the GEFS-related changes in grib_cache.py.

All network calls and herbie/cfgrib I/O are mocked so the suite runs offline
and without the heavy GRIB dependencies installed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from src.data.gefs import GEFS_MEMBERS, GEFSMemberForecast, fetch_gefs_ensemble
from src.data.grib_cache import _cache_key

# ---------------------------------------------------------------------------
# Fixtures / shared constants
# ---------------------------------------------------------------------------

CYCLE_DT = datetime(2024, 6, 15, 18, 0, 0, tzinfo=timezone.utc)
DENVER_LAT = 39.73
DENVER_LON = -104.99


# ---------------------------------------------------------------------------
# _cache_key — member parameter
# ---------------------------------------------------------------------------

class TestCacheKeyWithMember:
    def test_member_embedded_in_key(self):
        key = _cache_key("gefs", "TMP_2m", CYCLE_DT, fxx=6, member="gec00")
        assert key == "gefs_gec00_TMP_2m_20240615T18Z_f006"

    def test_different_members_produce_different_keys(self):
        key_control = _cache_key("gefs", "TMP_2m", CYCLE_DT, fxx=6, member="gec00")
        key_p01 = _cache_key("gefs", "TMP_2m", CYCLE_DT, fxx=6, member="gep01")
        key_p30 = _cache_key("gefs", "TMP_2m", CYCLE_DT, fxx=6, member="gep30")
        assert key_control != key_p01
        assert key_control != key_p30
        assert key_p01 != key_p30

    def test_no_member_preserves_original_format(self):
        """Passing member=None must not change the existing HRRR key format."""
        key = _cache_key("hrrr", "TMP_2m", CYCLE_DT, fxx=0)
        assert key == "hrrr_TMP_2m_20240615T18Z_f000"

    def test_all_31_members_unique_keys(self):
        """Every GEFS member must produce a distinct cache key."""
        keys = [
            _cache_key("gefs", "TMP_2m", CYCLE_DT, fxx=6, member=m)
            for m in GEFS_MEMBERS
        ]
        assert len(keys) == len(set(keys)), "Duplicate cache keys detected across GEFS members"

    def test_member_count(self):
        """GEFS_MEMBERS must contain exactly 31 entries (1 control + 30 perturbed)."""
        assert len(GEFS_MEMBERS) == 31
        assert GEFS_MEMBERS[0] == "gec00"
        assert GEFS_MEMBERS[1] == "gep01"
        assert GEFS_MEMBERS[-1] == "gep30"


# ---------------------------------------------------------------------------
# fetch_gefs_ensemble — _fetch_grib_slice called with distinct member per member
# ---------------------------------------------------------------------------

class TestFetchGefsEnsemble:
    """Verify that fetch_gefs_ensemble forwards a unique member to _fetch_grib_slice
    for each of the 31 GEFS members.
    """

    def _make_fake_path(self, tmp_path: Path) -> Path:
        p = tmp_path / "fake.grib2"
        p.write_bytes(b"fake")
        return p

    def test_fetch_grib_slice_called_with_31_distinct_members(self, tmp_path):
        """_fetch_grib_slice must be called once per member with distinct member kwargs."""
        fake_path = self._make_fake_path(tmp_path)

        with (
            patch("src.data.grib_cache._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.grib_cache._get_cache_dir", return_value=tmp_path),
            patch("src.data.grib_cache._evict_expired"),
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch(
                "src.data.grib_cache._fetch_grib_slice", return_value=fake_path
            ) as mock_fetch,
            patch("src.data.grib_cache._read_grib_nearest", return_value=295.0),
        ):
            results = fetch_gefs_ensemble(DENVER_LAT, DENVER_LON)

        assert mock_fetch.call_count == 31

        # Extract the 'member' kwarg from each call
        called_members = [c.kwargs["member"] for c in mock_fetch.call_args_list]
        assert len(called_members) == 31
        assert len(set(called_members)) == 31, (
            f"Expected 31 distinct member values; got: {sorted(set(called_members))}"
        )

    def test_all_expected_members_are_passed(self, tmp_path):
        """The exact set of member strings passed must match GEFS_MEMBERS."""
        fake_path = self._make_fake_path(tmp_path)

        with (
            patch("src.data.grib_cache._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.grib_cache._get_cache_dir", return_value=tmp_path),
            patch("src.data.grib_cache._evict_expired"),
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch(
                "src.data.grib_cache._fetch_grib_slice", return_value=fake_path
            ) as mock_fetch,
            patch("src.data.grib_cache._read_grib_nearest", return_value=295.0),
        ):
            fetch_gefs_ensemble(DENVER_LAT, DENVER_LON)

        called_members = sorted(c.kwargs["member"] for c in mock_fetch.call_args_list)
        assert called_members == sorted(GEFS_MEMBERS)

    def test_returns_31_results_when_all_members_succeed(self, tmp_path):
        fake_path = self._make_fake_path(tmp_path)

        with (
            patch("src.data.grib_cache._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.grib_cache._get_cache_dir", return_value=tmp_path),
            patch("src.data.grib_cache._evict_expired"),
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", return_value=fake_path),
            patch("src.data.grib_cache._read_grib_nearest", return_value=295.0),
        ):
            results = fetch_gefs_ensemble(DENVER_LAT, DENVER_LON)

        assert len(results) == 31
        assert all(isinstance(r, GEFSMemberForecast) for r in results)

    def test_result_members_match_gefs_members(self, tmp_path):
        """Each result.member must correspond to a distinct GEFS member identifier."""
        fake_path = self._make_fake_path(tmp_path)

        with (
            patch("src.data.grib_cache._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.grib_cache._get_cache_dir", return_value=tmp_path),
            patch("src.data.grib_cache._evict_expired"),
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", return_value=fake_path),
            patch("src.data.grib_cache._read_grib_nearest", return_value=295.0),
        ):
            results = fetch_gefs_ensemble(DENVER_LAT, DENVER_LON)

        result_members = sorted(r.member for r in results)
        assert result_members == sorted(GEFS_MEMBERS)

    def test_returns_empty_when_no_cycle(self, tmp_path):
        with (
            patch("src.data.grib_cache._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.grib_cache._get_cache_dir", return_value=tmp_path),
            patch("src.data.grib_cache._evict_expired"),
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=None),
        ):
            results = fetch_gefs_ensemble(DENVER_LAT, DENVER_LON)

        assert results == []

    def test_skips_members_where_fetch_returns_none(self, tmp_path):
        """Members whose _fetch_grib_slice returns None must be silently skipped."""
        fake_path = self._make_fake_path(tmp_path)

        # Only return a path for the control member; None for all perturbed members
        def selective_fetch(**kwargs):
            return fake_path if kwargs.get("member") == "gec00" else None

        with (
            patch("src.data.grib_cache._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.grib_cache._get_cache_dir", return_value=tmp_path),
            patch("src.data.grib_cache._evict_expired"),
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch(
                "src.data.grib_cache._fetch_grib_slice", side_effect=selective_fetch
            ),
            patch("src.data.grib_cache._read_grib_nearest", return_value=295.0),
        ):
            results = fetch_gefs_ensemble(DENVER_LAT, DENVER_LON)

        assert len(results) == 1
        assert results[0].member == "gec00"

"""Unit tests for src/data/grib_cache.py.

All network calls and herbie/cfgrib I/O are mocked so the suite runs offline
and without the heavy GRIB dependencies installed.

Temperature range under test:
  HRRR stores temperatures in Kelvin.  For Denver in summer a sane TMP_2m
  reading is 273 K – 318 K (0 °C – 45 °C).  The unit test asserts this range
  after a successful fetch.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.data.grib_cache import (
    SUPPORTED_VARS,
    _cache_key,
    _cache_path,
    _evict_expired,
    _is_cache_valid,
    _get_cache_dir,
    _get_cache_ttl_hours,
    fetch_hrrr_field,
    _read_grib_nearest,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DENVER_LAT = 39.73
DENVER_LON = -104.99
CYCLE_DT = datetime(2024, 6, 15, 18, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# _cache_key / _cache_path
# ---------------------------------------------------------------------------

class TestCacheKey:
    def test_format(self):
        key = _cache_key("hrrr", "TMP_2m", CYCLE_DT, fxx=0)
        assert key == "hrrr_TMP_2m_20240615T18Z_f000"

    def test_nonzero_fxx(self):
        key = _cache_key("hrrr", "DPT_2m", CYCLE_DT, fxx=6)
        assert key == "hrrr_DPT_2m_20240615T18Z_f006"

    def test_cache_path_extension(self, tmp_path):
        key = _cache_key("hrrr", "TMP_2m", CYCLE_DT, fxx=0)
        path = _cache_path(tmp_path, key)
        assert path.suffix == ".grib2"
        assert path.parent == tmp_path


# ---------------------------------------------------------------------------
# _is_cache_valid
# ---------------------------------------------------------------------------

class TestIsCacheValid:
    def test_missing_file_is_invalid(self, tmp_path):
        path = tmp_path / "nonexistent.grib2"
        assert _is_cache_valid(path, ttl_hours=6.0) is False

    def test_fresh_file_is_valid(self, tmp_path):
        path = tmp_path / "fresh.grib2"
        path.write_bytes(b"dummy")
        assert _is_cache_valid(path, ttl_hours=6.0) is True

    def test_stale_file_is_invalid(self, tmp_path):
        path = tmp_path / "stale.grib2"
        path.write_bytes(b"dummy")
        # Backdate modification time by 7 hours
        stale_mtime = time.time() - 7 * 3600
        import os
        os.utime(path, (stale_mtime, stale_mtime))
        assert _is_cache_valid(path, ttl_hours=6.0) is False


# ---------------------------------------------------------------------------
# _evict_expired
# ---------------------------------------------------------------------------

class TestEvictExpired:
    def test_removes_stale_files(self, tmp_path):
        stale = tmp_path / "old.grib2"
        stale.write_bytes(b"data")
        stale_mtime = time.time() - 8 * 3600
        import os
        os.utime(stale, (stale_mtime, stale_mtime))

        fresh = tmp_path / "new.grib2"
        fresh.write_bytes(b"data")

        _evict_expired(tmp_path, ttl_hours=6.0)

        assert not stale.exists()
        assert fresh.exists()

    def test_noop_on_missing_dir(self, tmp_path):
        missing = tmp_path / "does_not_exist"
        # Should not raise
        _evict_expired(missing, ttl_hours=6.0)


# ---------------------------------------------------------------------------
# _get_cache_ttl_hours / _get_cache_dir (env-var path)
# ---------------------------------------------------------------------------

class TestConfigHelpers:
    def test_default_ttl(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("GRIB_CACHE_TTL_HOURS", None)
            ttl = _get_cache_ttl_hours(db=None)
        assert ttl == 6.0

    def test_env_override_ttl(self):
        with patch.dict("os.environ", {"GRIB_CACHE_TTL_HOURS": "12.5"}):
            assert _get_cache_ttl_hours(db=None) == 12.5

    def test_default_cache_dir(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("GRIB_CACHE_DIR", None)
            d = _get_cache_dir(db=None)
        assert d == Path(".grib_cache")

    def test_env_override_cache_dir(self, tmp_path):
        with patch.dict("os.environ", {"GRIB_CACHE_DIR": str(tmp_path)}):
            assert _get_cache_dir(db=None) == tmp_path


# ---------------------------------------------------------------------------
# SUPPORTED_VARS
# ---------------------------------------------------------------------------

class TestSupportedVars:
    def test_tmp_2m_present(self):
        assert "TMP_2m" in SUPPORTED_VARS

    def test_dpt_2m_present(self):
        assert "DPT_2m" in SUPPORTED_VARS

    def test_matchers_contain_colon_format(self):
        for key, matcher in SUPPORTED_VARS.items():
            assert matcher.startswith(":"), f"{key} matcher should start with ':'"


# ---------------------------------------------------------------------------
# fetch_hrrr_field — full mock (offline, no herbie/cfgrib required)
# ---------------------------------------------------------------------------

class TestFetchHrrrField:
    """Tests for fetch_hrrr_field() with all I/O mocked.

    _read_grib_nearest is patched directly so tests run without cfgrib/eccodes
    installed and without any network access.
    """

    def _patch_all(self, tmp_path, grib_return_value):
        """Return a context manager that patches all external I/O."""
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch("src.data.grib_cache._get_cache_dir", return_value=tmp_path))
        stack.enter_context(patch("src.data.grib_cache._get_cache_ttl_hours", return_value=6.0))
        stack.enter_context(patch("src.data.grib_cache._evict_expired"))
        stack.enter_context(patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT))
        fake_path = tmp_path / "fake.grib2"
        fake_path.write_bytes(b"fake")
        stack.enter_context(patch("src.data.grib_cache._fetch_grib_slice", return_value=fake_path))
        stack.enter_context(
            patch("src.data.grib_cache._read_grib_nearest", return_value=grib_return_value)
        )
        return stack

    def test_returns_sane_kelvin_temperature(self, tmp_path):
        """fetch_hrrr_field TMP_2m at Denver must be in 273–318 K."""
        tmp_k = 295.15  # ~22 °C — sane summer value for Denver

        with self._patch_all(tmp_path, tmp_k):
            result = fetch_hrrr_field("TMP_2m", DENVER_LAT, DENVER_LON)

        assert result is not None
        assert 273.0 <= result <= 318.0, (
            f"TMP_2m={result} K is outside sane range 273–318 K"
        )

    def test_returns_none_when_cycle_unavailable(self, tmp_path):
        with (
            patch("src.data.grib_cache._get_cache_dir", return_value=tmp_path),
            patch("src.data.grib_cache._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.grib_cache._evict_expired"),
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=None),
        ):
            result = fetch_hrrr_field("TMP_2m", DENVER_LAT, DENVER_LON)
        assert result is None

    def test_returns_none_when_fetch_fails(self, tmp_path):
        with (
            patch("src.data.grib_cache._get_cache_dir", return_value=tmp_path),
            patch("src.data.grib_cache._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.grib_cache._evict_expired"),
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", return_value=None),
        ):
            result = fetch_hrrr_field("TMP_2m", DENVER_LAT, DENVER_LON)
        assert result is None

    def test_returns_none_on_grib_read_error(self, tmp_path):
        fake_path = tmp_path / "fake.grib2"
        fake_path.write_bytes(b"fake")

        with (
            patch("src.data.grib_cache._get_cache_dir", return_value=tmp_path),
            patch("src.data.grib_cache._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.grib_cache._evict_expired"),
            patch("src.data.grib_cache._resolve_latest_cycle", return_value=CYCLE_DT),
            patch("src.data.grib_cache._fetch_grib_slice", return_value=fake_path),
            patch(
                "src.data.grib_cache._read_grib_nearest",
                side_effect=RuntimeError("eccodes not found"),
            ),
        ):
            result = fetch_hrrr_field("TMP_2m", DENVER_LAT, DENVER_LON)
        assert result is None

    def test_dpt_2m_also_supported(self, tmp_path):
        """DPT_2m (dewpoint) should also return a sane Kelvin value."""
        dpt_k = 285.0  # ~12 °C dewpoint — reasonable

        with self._patch_all(tmp_path, dpt_k):
            result = fetch_hrrr_field("DPT_2m", DENVER_LAT, DENVER_LON)

        assert result is not None
        assert 240.0 <= result <= 320.0

"""Regression tests for GRIB cycle resolution with herbie 2025.12.0 H.grib=None fix.

Root cause: H.idx returns None (not raises) for an unpublished cycle, so the old
try/except pattern around H.idx accepted grib=None cycles.  The fix checks
H.grib is not None explicitly so that unpublished cycles are correctly skipped.

These tests inject a fake herbie module so they run offline without herbie installed.
"""

import sys
import types
from unittest.mock import MagicMock
import pytest


def _make_herbie_module(herbie_cls):
    mod = types.ModuleType("herbie")
    mod.Herbie = herbie_cls
    return mod


def _published():
    h = MagicMock()
    h.grib = MagicMock()  # not None — published
    return h


def _unpublished():
    h = MagicMock()
    h.grib = None  # None — not yet published
    return h


class TestGribNoneStepBack:
    """H.grib=None must cause the resolver to step back, not return the unpublished cycle."""

    def test_grib_cache_steps_back_on_grib_none(self):
        from src.data.grib_cache import _resolve_latest_cycle

        call_count = 0

        def fake_herbie(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _unpublished() if call_count == 1 else _published()

        sys.modules["herbie"] = _make_herbie_module(fake_herbie)
        try:
            result = _resolve_latest_cycle("hrrr")
        finally:
            sys.modules.pop("herbie", None)

        assert result is not None, "Should return fallback cycle when newest grib=None"
        assert result.tzinfo is None
        assert call_count >= 2, "Should have stepped back at least once"

    def test_grib_cache_never_returns_grib_none_cycle(self):
        """Resolver must not return a cycle for which grib=None."""
        from src.data.grib_cache import _resolve_latest_cycle

        returned_dts = []
        call_count = 0

        def fake_herbie(dt, **kwargs):
            nonlocal call_count
            call_count += 1
            returned_dts.append(dt)
            return _unpublished() if call_count == 1 else _published()

        sys.modules["herbie"] = _make_herbie_module(fake_herbie)
        try:
            result = _resolve_latest_cycle("hrrr")
        finally:
            sys.modules.pop("herbie", None)

        # The cycle returned must be the second candidate (step-back), not the first
        assert result is not None
        assert len(returned_dts) >= 2
        # result should be 1 hour earlier than the first candidate
        from datetime import timedelta
        assert result == returned_dts[0] - timedelta(hours=1)

    def test_nbm_steps_back_on_grib_none(self):
        from src.data.nbm import _resolve_nbm_cycle

        call_count = 0

        def fake_herbie(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _unpublished() if call_count == 1 else _published()

        sys.modules["herbie"] = _make_herbie_module(fake_herbie)
        try:
            result = _resolve_nbm_cycle(fxx=0)
        finally:
            sys.modules.pop("herbie", None)

        assert result is not None
        assert result.tzinfo is None
        assert call_count >= 2

    def test_ecmwf_steps_back_on_grib_none(self):
        from src.data.ecmwf_open import _resolve_ecmwf_cycle

        call_count = 0

        def fake_herbie(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _unpublished() if call_count == 1 else _published()

        sys.modules["herbie"] = _make_herbie_module(fake_herbie)
        try:
            result = _resolve_ecmwf_cycle(fxx=1)
        finally:
            sys.modules.pop("herbie", None)

        assert result is not None
        assert result.tzinfo is None
        assert call_count >= 2

    def test_icon_steps_back_on_grib_none(self):
        from src.data.icon import _resolve_icon_cycle

        call_count = 0

        def fake_herbie(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _unpublished() if call_count == 1 else _published()

        sys.modules["herbie"] = _make_herbie_module(fake_herbie)
        try:
            result = _resolve_icon_cycle(fxx=1)
        finally:
            sys.modules.pop("herbie", None)

        assert result is not None
        assert result.tzinfo is None
        assert call_count >= 2

    def test_returns_none_when_all_grib_none(self):
        """When every candidate returns grib=None, resolver returns None (no crash)."""
        from src.data.grib_cache import _resolve_latest_cycle

        sys.modules["herbie"] = _make_herbie_module(lambda *a, **kw: _unpublished())
        try:
            result = _resolve_latest_cycle("hrrr")
        finally:
            sys.modules.pop("herbie", None)

        assert result is None


class TestGefsLookback:
    """GEFS resolver must use 24h look-back (not 6h) to find published cycles."""

    def test_gefs_lookback_exceeds_6h(self):
        """GEFS resolver must keep trying beyond 6 hours to find a published cycle."""
        from src.data.grib_cache import _resolve_latest_cycle

        # First 7 candidates return grib=None, 8th returns published
        call_count = 0

        def fake_herbie(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _published() if call_count >= 8 else _unpublished()

        sys.modules["herbie"] = _make_herbie_module(fake_herbie)
        try:
            result = _resolve_latest_cycle("gefs")
        finally:
            sys.modules.pop("herbie", None)

        assert result is not None, "GEFS resolver should find cycle at 8th attempt (within 24h window)"
        assert call_count >= 8


class TestFetchHrrrFieldCycleReuse:
    """fetch_hrrr_field must accept a pre-resolved cycle_dt to avoid re-resolution."""

    def test_accepts_pre_resolved_cycle_dt(self, tmp_path):
        from datetime import datetime, timezone
        from unittest.mock import patch
        from src.data.grib_cache import fetch_hrrr_field

        cycle = datetime(2024, 6, 15, 18, 0, 0)
        fake_path = tmp_path / "fake.grib2"
        fake_path.write_bytes(b"fake")

        with (
            patch("src.data.grib_cache._get_cache_dir", return_value=tmp_path),
            patch("src.data.grib_cache._get_cache_ttl_hours", return_value=6.0),
            patch("src.data.grib_cache._evict_expired"),
            patch("src.data.grib_cache._resolve_latest_cycle") as mock_resolve,
            patch("src.data.grib_cache._fetch_grib_slice", return_value=fake_path),
            patch("src.data.grib_cache._read_grib_nearest", return_value=295.0),
        ):
            result = fetch_hrrr_field("TMP_2m", 39.73, -104.99, fxx=1, cycle_dt=cycle)

        mock_resolve.assert_not_called()
        assert result == 295.0

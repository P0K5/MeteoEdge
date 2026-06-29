"""Regression tests for Herbie tz-naive fix (issue #496).

Herbie 2025.12.0 raises TypeError when passed a tz-aware datetime.
These tests assert:
  A. The datetime passed to Herbie has tzinfo is None.
  B. The resolver steps back to the next older cycle when the newest is unavailable.

Because each resolver imports Herbie lazily inside the function body
(``from herbie import Herbie``), we inject a fake ``herbie`` module into
``sys.modules`` so that the lazy import picks up our mock without requiring
the real herbie package to be installed.
"""
import sys
import types
from unittest.mock import MagicMock
import pytest


def _make_available_herbie():
    h = MagicMock()
    h.idx = True
    return h


def _make_herbie_module(herbie_cls):
    """Return a minimal fake 'herbie' module exposing herbie_cls as Herbie."""
    mod = types.ModuleType("herbie")
    mod.Herbie = herbie_cls
    return mod


class TestHerbieReceivesTzNaive:
    def test_grib_cache_resolve_passes_tz_naive(self):
        from src.data.grib_cache import _resolve_latest_cycle
        calls = []

        def fake_herbie(*args, **kwargs):
            calls.append(args[0])
            return _make_available_herbie()

        fake_mod = _make_herbie_module(fake_herbie)
        sys.modules["herbie"] = fake_mod
        try:
            _resolve_latest_cycle("hrrr")
        finally:
            sys.modules.pop("herbie", None)

        assert calls, "Herbie was never called"
        dt = calls[0]
        assert dt.tzinfo is None, f"Expected tz-naive, got tzinfo={dt.tzinfo}"

    def test_nbm_resolve_passes_tz_naive(self):
        from src.data.nbm import _resolve_nbm_cycle
        calls = []

        def fake_herbie(*args, **kwargs):
            calls.append(args[0])
            return _make_available_herbie()

        fake_mod = _make_herbie_module(fake_herbie)
        sys.modules["herbie"] = fake_mod
        try:
            _resolve_nbm_cycle(fxx=0)
        finally:
            sys.modules.pop("herbie", None)

        assert calls, "Herbie was never called"
        dt = calls[0]
        assert dt.tzinfo is None, f"Expected tz-naive, got tzinfo={dt.tzinfo}"

    def test_ecmwf_resolve_passes_tz_naive(self):
        from src.data.ecmwf_open import _resolve_ecmwf_cycle
        calls = []

        def fake_herbie(*args, **kwargs):
            calls.append(args[0])
            return _make_available_herbie()

        fake_mod = _make_herbie_module(fake_herbie)
        sys.modules["herbie"] = fake_mod
        try:
            _resolve_ecmwf_cycle(fxx=1)
        finally:
            sys.modules.pop("herbie", None)

        assert calls, "Herbie was never called"
        dt = calls[0]
        assert dt.tzinfo is None, f"Expected tz-naive, got tzinfo={dt.tzinfo}"

    def test_icon_resolve_passes_tz_naive(self):
        # _resolve_icon_cycle now uses httpx.head (not herbie); assert the
        # returned datetime is tz-naive (consistent with other resolvers).
        import sys
        import types
        from unittest.mock import MagicMock
        from src.data.icon import _resolve_icon_cycle

        resp = MagicMock()
        resp.status_code = 200

        fake_httpx = types.ModuleType("httpx")
        fake_httpx.head = lambda url, **kwargs: resp
        sys.modules["httpx"] = fake_httpx
        try:
            result = _resolve_icon_cycle()
        finally:
            sys.modules.pop("httpx", None)

        assert result is not None
        assert result.tzinfo is not None, "Expected tz-aware UTC from _resolve_icon_cycle"


class TestResolverStepBack:
    def test_grib_cache_steps_back_when_newest_unavailable(self):
        """When the first (newest) cycle raises, resolver returns the next older one."""
        from src.data.grib_cache import _resolve_latest_cycle
        call_count = 0

        def fake_herbie(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("cycle not yet published")
            return _make_available_herbie()

        fake_mod = _make_herbie_module(fake_herbie)
        sys.modules["herbie"] = fake_mod
        try:
            result = _resolve_latest_cycle("hrrr")
        finally:
            sys.modules.pop("herbie", None)

        assert result is not None, "Expected a fallback cycle datetime, got None"
        assert result.tzinfo is None, "Returned cycle must be tz-naive"
        assert call_count >= 2, "Expected at least 2 Herbie calls (step-back triggered)"

    def test_nbm_steps_back_when_newest_unavailable(self):
        """NBM resolver steps back to the prior cycle on first failure."""
        from src.data.nbm import _resolve_nbm_cycle
        call_count = 0

        def fake_herbie(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("not yet published")
            return _make_available_herbie()

        fake_mod = _make_herbie_module(fake_herbie)
        sys.modules["herbie"] = fake_mod
        try:
            result = _resolve_nbm_cycle(fxx=0)
        finally:
            sys.modules.pop("herbie", None)

        assert result is not None
        assert result.tzinfo is None
        assert call_count >= 2

"""Tests for forecast ingestion in capture_forecasts.py.

Covers:
- GEFS ensemble (#490): happy path, fetch failure.
- HRRR, NBM, ECMWF, ICON shadow sources (#492): 3 tests each —
  happy path writes row, domain/availability skip does not write,
  fetch failure does not raise.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.data.db import Database
from src.scripts.capture_forecasts import _capture_station


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db() -> Database:
    """Return an in-memory Database for testing."""
    return Database(":memory:")


def _capture_kwargs(**overrides) -> dict:
    """Return a minimal set of kwargs for _capture_station."""
    defaults = dict(
        db=None,
        station="KORD",
        lat=41.98,
        lon=-87.90,
        target_date="2026-06-27",
        lead_hours=24,
        issued_at="2026-06-26T12:00:00+00:00",
        dry_run=False,
    )
    defaults.update(overrides)
    return defaults


def _silence_other_sources(model_under_test: str) -> dict:
    """Return a dict of patches that silence all sources except the one under test."""
    patches = {
        "src.scripts.capture_forecasts.fetch_nws_with_spread": None,
        "src.scripts.capture_forecasts.fetch_open_meteo_with_spread": None,
        "src.scripts.capture_forecasts.fetch_secondary_forecast": None,
        "src.scripts.capture_forecasts.fetch_gfs_with_spread": None,
        "src.scripts.capture_forecasts.fetch_gfs_forecast_high": None,
        "src.scripts.capture_forecasts.fetch_gefs_ensemble": [],
    }
    # Remove the entry for the model under test so its patch is not overridden
    key_map = {
        "gefs": "src.scripts.capture_forecasts.fetch_gefs_ensemble",
    }
    if model_under_test in key_map:
        del patches[key_map[model_under_test]]
    return patches


# ---------------------------------------------------------------------------
# Fake GEFSMemberForecast objects
# ---------------------------------------------------------------------------

class _FakeMember:
    """Minimal stand-in for GEFSMemberForecast — only temp_k is needed."""

    def __init__(self, temp_k: float) -> None:
        self.temp_k = temp_k


# 72 °F → Kelvin: K = (72 - 32) * 5/9 + 273.15 = 295.37…
_TEMP_F = 72.0
_TEMP_K = (_TEMP_F - 32.0) * 5.0 / 9.0 + 273.15
_MEMBERS_30 = [_FakeMember(_TEMP_K)] * 30


# ---------------------------------------------------------------------------
# Fake HourlyTemp objects (HRRR / ICON)
# ---------------------------------------------------------------------------

class _FakeHourlyTemp:
    """Minimal stand-in for HourlyTemp — only temp_f is needed."""

    def __init__(self, temp_f: float) -> None:
        self.temp_f = temp_f


_HOURLY_ROWS = [_FakeHourlyTemp(72.0)] * 6


# ---------------------------------------------------------------------------
# Fake NbmForecast / EcmwfForecast objects
# ---------------------------------------------------------------------------

class _FakeNbmForecast:
    forecast_high_f = 74.0


class _FakeEcmwfForecast:
    forecast_high_f = 73.5


# ---------------------------------------------------------------------------
# Shared silence context manager helper
# ---------------------------------------------------------------------------

from contextlib import contextmanager


@contextmanager
def _silence_base_sources():
    """Silence all sources so individual tests only exercise the one under test."""
    with (
        patch("src.scripts.capture_forecasts.fetch_nws_with_spread", return_value=None),
        patch("src.scripts.capture_forecasts.fetch_open_meteo_with_spread", return_value=None),
        patch("src.scripts.capture_forecasts.fetch_secondary_forecast", return_value=None),
        patch("src.scripts.capture_forecasts.fetch_gfs_with_spread", return_value=None),
        patch("src.scripts.capture_forecasts.fetch_gfs_forecast_high", return_value=None),
        patch("src.scripts.capture_forecasts.fetch_gefs_ensemble", return_value=[]),
        patch("src.data.hrrr.fetch_hrrr_hourly", return_value=[]),
        patch("src.data.nbm.fetch_nbm_daily_high", return_value=None),
        patch("src.data.ecmwf_open.fetch_ecmwf_daily_high", return_value=None),
        patch("src.data.icon.fetch_icon_hourly", return_value=[]),
    ):
        yield


# ---------------------------------------------------------------------------
# TestGefsCapture
# ---------------------------------------------------------------------------

class TestGefsCapture:
    """GEFS ensemble ingestion into _capture_station."""

    def test_happy_path_writes_gefs_row(self):
        """When fetch_gefs_ensemble returns 30 members, upsert_forecast_log_v2
        must be called with model='gefs' and sigma_f from compute_ensemble_sigma."""
        db = _db()
        mock_upsert = MagicMock()
        db.upsert_forecast_log_v2 = mock_upsert

        with (
            _silence_base_sources(),
            patch("src.scripts.capture_forecasts.fetch_gefs_ensemble", return_value=_MEMBERS_30),
            patch("src.scripts.capture_forecasts.compute_ensemble_sigma", return_value=3.5),
        ):
            _capture_station(**_capture_kwargs(db=db))

        # Find the call with model='gefs'
        gefs_calls = [
            c for c in mock_upsert.call_args_list
            if c.kwargs.get("model") == "gefs"
        ]
        assert len(gefs_calls) == 1, (
            f"Expected exactly 1 upsert call with model='gefs', got {len(gefs_calls)}"
        )
        call_kwargs = gefs_calls[0].kwargs
        assert call_kwargs["sigma_f"] == pytest.approx(3.5)
        assert call_kwargs["station"] == "KORD"
        assert call_kwargs["lead_hours"] == 24

    def test_fetch_failure_does_not_write_and_does_not_raise(self):
        """When fetch_gefs_ensemble raises, upsert_forecast_log_v2 must NOT be
        called with model='gefs' and no exception must propagate to the caller."""
        db = _db()
        mock_upsert = MagicMock()
        db.upsert_forecast_log_v2 = mock_upsert

        with (
            _silence_base_sources(),
            patch(
                "src.scripts.capture_forecasts.fetch_gefs_ensemble",
                side_effect=Exception("GEFS timeout"),
            ),
        ):
            # Must not raise
            _capture_station(**_capture_kwargs(db=db))

        gefs_calls = [
            c for c in mock_upsert.call_args_list
            if c.kwargs.get("model") == "gefs"
        ]
        assert len(gefs_calls) == 0, (
            f"Expected no upsert call with model='gefs' on failure, got {len(gefs_calls)}"
        )


# ---------------------------------------------------------------------------
# TestHrrrCapture
# ---------------------------------------------------------------------------

class TestHrrrCapture:
    """HRRR shadow ingestion into _capture_station (#492)."""

    def test_happy_path_writes_hrrr_row(self):
        """When fetch_hrrr_hourly returns rows, upsert_forecast_log_v2 must be
        called with model='hrrr' and the mean temp_f as forecast_high_f."""
        db = _db()
        mock_upsert = MagicMock()
        db.upsert_forecast_log_v2 = mock_upsert

        with (
            _silence_base_sources(),
            patch("src.data.hrrr.fetch_hrrr_hourly", return_value=_HOURLY_ROWS),
        ):
            _capture_station(**_capture_kwargs(db=db))

        hrrr_calls = [c for c in mock_upsert.call_args_list if c.kwargs.get("model") == "hrrr"]
        assert len(hrrr_calls) == 1, f"Expected 1 hrrr upsert, got {len(hrrr_calls)}"
        kw = hrrr_calls[0].kwargs
        assert kw["forecast_high_f"] == pytest.approx(72.0)
        assert kw["sigma_f"] is None
        assert kw["station"] == "KORD"

    def test_out_of_domain_does_not_write(self):
        """When fetch_hrrr_hourly returns [] (out of CONUS), no upsert for 'hrrr'."""
        db = _db()
        mock_upsert = MagicMock()
        db.upsert_forecast_log_v2 = mock_upsert

        with (
            _silence_base_sources(),
            patch("src.data.hrrr.fetch_hrrr_hourly", return_value=[]),
        ):
            _capture_station(**_capture_kwargs(db=db))

        hrrr_calls = [c for c in mock_upsert.call_args_list if c.kwargs.get("model") == "hrrr"]
        assert len(hrrr_calls) == 0, "Expected no hrrr upsert when out of domain"

    def test_fetch_failure_does_not_raise(self):
        """When fetch_hrrr_hourly raises, no exception propagates and no upsert for 'hrrr'."""
        db = _db()
        mock_upsert = MagicMock()
        db.upsert_forecast_log_v2 = mock_upsert

        with (
            _silence_base_sources(),
            patch("src.data.hrrr.fetch_hrrr_hourly", side_effect=RuntimeError("HRRR fetch failed")),
        ):
            _capture_station(**_capture_kwargs(db=db))  # must not raise

        hrrr_calls = [c for c in mock_upsert.call_args_list if c.kwargs.get("model") == "hrrr"]
        assert len(hrrr_calls) == 0, "Expected no hrrr upsert on fetch failure"


# ---------------------------------------------------------------------------
# TestNbmCapture
# ---------------------------------------------------------------------------

class TestNbmCapture:
    """NBM shadow ingestion into _capture_station (#492)."""

    def test_happy_path_writes_nbm_row(self):
        """When fetch_nbm_daily_high returns a result, upsert_forecast_log_v2 must be
        called with model='nbm' and forecast_high_f from the result."""
        db = _db()
        mock_upsert = MagicMock()
        db.upsert_forecast_log_v2 = mock_upsert

        with (
            _silence_base_sources(),
            patch("src.data.nbm.fetch_nbm_daily_high", return_value=_FakeNbmForecast()),
        ):
            _capture_station(**_capture_kwargs(db=db))

        nbm_calls = [c for c in mock_upsert.call_args_list if c.kwargs.get("model") == "nbm"]
        assert len(nbm_calls) == 1, f"Expected 1 nbm upsert, got {len(nbm_calls)}"
        kw = nbm_calls[0].kwargs
        assert kw["forecast_high_f"] == pytest.approx(74.0)
        assert kw["sigma_f"] is None
        assert kw["station"] == "KORD"

    def test_out_of_domain_does_not_write(self):
        """When fetch_nbm_daily_high returns None (out of CONUS), no upsert for 'nbm'."""
        db = _db()
        mock_upsert = MagicMock()
        db.upsert_forecast_log_v2 = mock_upsert

        with (
            _silence_base_sources(),
            patch("src.data.nbm.fetch_nbm_daily_high", return_value=None),
        ):
            _capture_station(**_capture_kwargs(db=db))

        nbm_calls = [c for c in mock_upsert.call_args_list if c.kwargs.get("model") == "nbm"]
        assert len(nbm_calls) == 0, "Expected no nbm upsert when out of domain"

    def test_fetch_failure_does_not_raise(self):
        """When fetch_nbm_daily_high raises, no exception propagates and no upsert for 'nbm'."""
        db = _db()
        mock_upsert = MagicMock()
        db.upsert_forecast_log_v2 = mock_upsert

        with (
            _silence_base_sources(),
            patch("src.data.nbm.fetch_nbm_daily_high", side_effect=RuntimeError("NBM fetch failed")),
        ):
            _capture_station(**_capture_kwargs(db=db))  # must not raise

        nbm_calls = [c for c in mock_upsert.call_args_list if c.kwargs.get("model") == "nbm"]
        assert len(nbm_calls) == 0, "Expected no nbm upsert on fetch failure"


# ---------------------------------------------------------------------------
# TestEcmwfCapture
# ---------------------------------------------------------------------------

class TestEcmwfCapture:
    """ECMWF shadow ingestion into _capture_station (#492)."""

    def test_happy_path_writes_ecmwf_row(self):
        """When fetch_ecmwf_daily_high returns a result, upsert_forecast_log_v2 must be
        called with model='ecmwf' and forecast_high_f from the result."""
        db = _db()
        mock_upsert = MagicMock()
        db.upsert_forecast_log_v2 = mock_upsert

        with (
            _silence_base_sources(),
            patch("src.data.ecmwf_open.fetch_ecmwf_daily_high", return_value=_FakeEcmwfForecast()),
        ):
            _capture_station(**_capture_kwargs(db=db))

        ecmwf_calls = [c for c in mock_upsert.call_args_list if c.kwargs.get("model") == "ecmwf"]
        assert len(ecmwf_calls) == 1, f"Expected 1 ecmwf upsert, got {len(ecmwf_calls)}"
        kw = ecmwf_calls[0].kwargs
        assert kw["forecast_high_f"] == pytest.approx(73.5)
        assert kw["sigma_f"] is None
        assert kw["station"] == "KORD"

    def test_unavailable_does_not_write(self):
        """When fetch_ecmwf_daily_high returns None, no upsert for 'ecmwf'."""
        db = _db()
        mock_upsert = MagicMock()
        db.upsert_forecast_log_v2 = mock_upsert

        with (
            _silence_base_sources(),
            patch("src.data.ecmwf_open.fetch_ecmwf_daily_high", return_value=None),
        ):
            _capture_station(**_capture_kwargs(db=db))

        ecmwf_calls = [c for c in mock_upsert.call_args_list if c.kwargs.get("model") == "ecmwf"]
        assert len(ecmwf_calls) == 0, "Expected no ecmwf upsert when unavailable"

    def test_fetch_failure_does_not_raise(self):
        """When fetch_ecmwf_daily_high raises, no exception propagates and no upsert for 'ecmwf'."""
        db = _db()
        mock_upsert = MagicMock()
        db.upsert_forecast_log_v2 = mock_upsert

        with (
            _silence_base_sources(),
            patch("src.data.ecmwf_open.fetch_ecmwf_daily_high", side_effect=RuntimeError("ECMWF timeout")),
        ):
            _capture_station(**_capture_kwargs(db=db))  # must not raise

        ecmwf_calls = [c for c in mock_upsert.call_args_list if c.kwargs.get("model") == "ecmwf"]
        assert len(ecmwf_calls) == 0, "Expected no ecmwf upsert on fetch failure"


# ---------------------------------------------------------------------------
# TestIconCapture
# ---------------------------------------------------------------------------

class TestIconCapture:
    """ICON shadow ingestion into _capture_station (#492)."""

    def test_happy_path_writes_icon_row(self):
        """When fetch_icon_hourly returns rows, upsert_forecast_log_v2 must be
        called with model='icon' and the mean temp_f as forecast_high_f."""
        db = _db()
        mock_upsert = MagicMock()
        db.upsert_forecast_log_v2 = mock_upsert

        with (
            _silence_base_sources(),
            patch("src.data.icon.fetch_icon_hourly", return_value=_HOURLY_ROWS),
        ):
            _capture_station(**_capture_kwargs(db=db))

        icon_calls = [c for c in mock_upsert.call_args_list if c.kwargs.get("model") == "icon"]
        assert len(icon_calls) == 1, f"Expected 1 icon upsert, got {len(icon_calls)}"
        kw = icon_calls[0].kwargs
        assert kw["forecast_high_f"] == pytest.approx(72.0)
        assert kw["sigma_f"] is None
        assert kw["station"] == "KORD"

    def test_out_of_domain_does_not_write(self):
        """When fetch_icon_hourly returns [] (outside EU domain), no upsert for 'icon'."""
        db = _db()
        mock_upsert = MagicMock()
        db.upsert_forecast_log_v2 = mock_upsert

        with (
            _silence_base_sources(),
            patch("src.data.icon.fetch_icon_hourly", return_value=[]),
        ):
            _capture_station(**_capture_kwargs(db=db))

        icon_calls = [c for c in mock_upsert.call_args_list if c.kwargs.get("model") == "icon"]
        assert len(icon_calls) == 0, "Expected no icon upsert when out of EU domain"

    def test_fetch_failure_does_not_raise(self):
        """When fetch_icon_hourly raises, no exception propagates and no upsert for 'icon'."""
        db = _db()
        mock_upsert = MagicMock()
        db.upsert_forecast_log_v2 = mock_upsert

        with (
            _silence_base_sources(),
            patch("src.data.icon.fetch_icon_hourly", side_effect=RuntimeError("ICON fetch failed")),
        ):
            _capture_station(**_capture_kwargs(db=db))  # must not raise

        icon_calls = [c for c in mock_upsert.call_args_list if c.kwargs.get("model") == "icon"]
        assert len(icon_calls) == 0, "Expected no icon upsert on fetch failure"

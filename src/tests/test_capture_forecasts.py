"""Tests for GEFS ensemble ingestion in capture_forecasts.py — issue #490.

Verifies:
- Happy path: fetch_gefs_ensemble returns members → upsert_forecast_log_v2 called
  with model='gefs' and correct sigma_f.
- Fetch failure: fetch_gefs_ensemble raises an exception → upsert_forecast_log_v2 is
  NOT called with model='gefs' and no exception propagates.
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
            patch("src.scripts.capture_forecasts.fetch_gefs_ensemble", return_value=_MEMBERS_30),
            patch("src.scripts.capture_forecasts.compute_ensemble_sigma", return_value=3.5),
            # Silence the NWS / Open-Meteo / GFS fetches so they don't make network calls
            patch("src.scripts.capture_forecasts.fetch_nws_with_spread", return_value=None),
            patch("src.scripts.capture_forecasts.fetch_open_meteo_with_spread", return_value=None),
            patch("src.scripts.capture_forecasts.fetch_secondary_forecast", return_value=None),
            patch("src.scripts.capture_forecasts.fetch_gfs_with_spread", return_value=None),
            patch("src.scripts.capture_forecasts.fetch_gfs_forecast_high", return_value=None),
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
            patch(
                "src.scripts.capture_forecasts.fetch_gefs_ensemble",
                side_effect=Exception("GEFS timeout"),
            ),
            patch("src.scripts.capture_forecasts.fetch_nws_with_spread", return_value=None),
            patch("src.scripts.capture_forecasts.fetch_open_meteo_with_spread", return_value=None),
            patch("src.scripts.capture_forecasts.fetch_secondary_forecast", return_value=None),
            patch("src.scripts.capture_forecasts.fetch_gfs_with_spread", return_value=None),
            patch("src.scripts.capture_forecasts.fetch_gfs_forecast_high", return_value=None),
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

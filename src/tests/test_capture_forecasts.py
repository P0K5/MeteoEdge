"""Tests for forecast ingestion in capture_forecasts.py.

Covers:
- GFS single-model source (#548): (mu, None) return shape logs cleanly and
  persists NULL sigma_f; fallback not used when primary succeeds.
- GEFS ensemble (#490): happy path, fetch failure.
- HRRR, NBM, ECMWF, ICON shadow sources (#492): 3 tests each —
  happy path writes row, domain/availability skip does not write,
  fetch failure does not raise.
- Per-fetch timeout + capture-loop resilience (#717): a hung fetch times out
  and the loop proceeds; a single station/lead-time failure never kills the
  rest of run_captures().
"""
from __future__ import annotations

import logging
import time

from unittest.mock import MagicMock, patch

import pytest

import src.scripts.capture_forecasts as capture_forecasts_module
from src.data.db import Database
from src.scripts.capture_forecasts import _capture_station, run_captures


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
        patch("src.scripts.capture_forecasts.fetch_gefs_ensemble", return_value=[]),
        patch("src.data.hrrr.fetch_hrrr_hourly", return_value=[]),
        patch("src.data.nbm.fetch_nbm_daily_high", return_value=None),
        patch("src.data.ecmwf_open.fetch_ecmwf_daily_high", return_value=None),
        patch("src.data.icon.fetch_icon_hourly", return_value=[]),
    ):
        yield


# ---------------------------------------------------------------------------
# TestGfsCapture (issue #548 — single-model gfs_seamless, sigma always None)
# ---------------------------------------------------------------------------

class TestGfsCapture:
    """GFS capture with the post-#548 (mu_f, None) return shape.

    fetch_gfs_with_spread() now returns sigma=None (single deterministic
    model, no member spread). The capture log line previously formatted sigma
    with "%.2fF", which raises TypeError on None inside the logging machinery
    — these tests format every emitted record to catch that regression.

    The near-redundant fetch_gfs_forecast_high fallback was removed in #568
    because it hits the same upstream as the primary and offers no meaningful
    recovery scenario.
    """

    def test_none_sigma_logs_cleanly_and_persists_null(self, caplog):
        """(mu, None) must log without a formatting error and upsert sigma_f=None."""
        db = _db()
        mock_upsert = MagicMock()
        db.upsert_forecast_log_v2 = mock_upsert

        with (
            _silence_base_sources(),
            patch(
                "src.scripts.capture_forecasts.fetch_gfs_with_spread",
                return_value=(78.0, None),
            ),
            caplog.at_level(logging.INFO, logger="src.scripts.capture_forecasts"),
        ):
            _capture_station(**_capture_kwargs(db=db))

        # Force-format every captured record: with the old "%.2fF" format this
        # raises TypeError on the None sigma (the bug this test guards against).
        messages = [r.getMessage() for r in caplog.records]
        gfs_messages = [m for m in messages if " gfs " in m]
        assert gfs_messages, f"Expected a gfs capture log line, got: {messages}"
        assert any("sigma=None" in m for m in gfs_messages), (
            f"Expected 'sigma=None' in gfs log line, got: {gfs_messages}"
        )

        gfs_calls = [c for c in mock_upsert.call_args_list if c.kwargs.get("model") == "gfs"]
        assert len(gfs_calls) == 1, f"Expected 1 gfs upsert, got {len(gfs_calls)}"
        kw = gfs_calls[0].kwargs
        assert kw["forecast_high_f"] == pytest.approx(78.0)
        assert kw["sigma_f"] is None
        assert kw["station"] == "KORD"
        assert kw["lead_hours"] == 24

    def test_float_sigma_still_formats(self, caplog):
        """Defensive: if a future sigma policy (#555) returns a float again,
        the log line must render it as a formatted value, not crash."""
        db = _db()
        mock_upsert = MagicMock()
        db.upsert_forecast_log_v2 = mock_upsert

        with (
            _silence_base_sources(),
            patch(
                "src.scripts.capture_forecasts.fetch_gfs_with_spread",
                return_value=(78.0, 2.5),
            ),
            caplog.at_level(logging.INFO, logger="src.scripts.capture_forecasts"),
        ):
            _capture_station(**_capture_kwargs(db=db))

        messages = [r.getMessage() for r in caplog.records]
        assert any("sigma=2.50F" in m for m in messages), (
            f"Expected 'sigma=2.50F' in log output, got: {messages}"
        )
        gfs_calls = [c for c in mock_upsert.call_args_list if c.kwargs.get("model") == "gfs"]
        assert gfs_calls[0].kwargs["sigma_f"] == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# TestGefsCapture
# ---------------------------------------------------------------------------

class TestGefsCapture:
    """GEFS ensemble ingestion into _capture_station."""

    def test_happy_path_writes_gefs_row(self):
        """When fetch_gefs_ensemble returns 30 members, upsert_forecast_log_v2
        must be called with model='gefs' and sigma_f from raw_member_sigma
        (#555: capture-time sigma is the raw/unfloored member stdev, not the
        floored compute_ensemble_sigma() value)."""
        db = _db()
        mock_upsert = MagicMock()
        db.upsert_forecast_log_v2 = mock_upsert

        with (
            _silence_base_sources(),
            patch("src.scripts.capture_forecasts.fetch_gefs_ensemble", return_value=_MEMBERS_30),
            patch("src.scripts.capture_forecasts.raw_member_sigma", return_value=3.5),
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

    def test_none_sigma_logs_cleanly_and_persists_null(self, caplog):
        """When raw_member_sigma() returns None (e.g. <2 usable members), the
        capture log line must render 'sigma=None' without a formatting error
        and upsert_forecast_log_v2 must be called with sigma_f=None — mirrors
        the defensive-formatting guard already tested for the gfs channel."""
        db = _db()
        mock_upsert = MagicMock()
        db.upsert_forecast_log_v2 = mock_upsert

        with (
            _silence_base_sources(),
            patch("src.scripts.capture_forecasts.fetch_gefs_ensemble", return_value=_MEMBERS_30),
            patch("src.scripts.capture_forecasts.raw_member_sigma", return_value=None),
            caplog.at_level(logging.INFO, logger="src.scripts.capture_forecasts"),
        ):
            _capture_station(**_capture_kwargs(db=db))

        messages = [r.getMessage() for r in caplog.records]
        gefs_messages = [m for m in messages if " gefs " in m]
        assert gefs_messages, f"Expected a gefs capture log line, got: {messages}"
        assert any("sigma=None" in m for m in gefs_messages), (
            f"Expected 'sigma=None' in gefs log line, got: {gefs_messages}"
        )

        gefs_calls = [c for c in mock_upsert.call_args_list if c.kwargs.get("model") == "gefs"]
        assert len(gefs_calls) == 1, f"Expected 1 gefs upsert, got {len(gefs_calls)}"
        assert gefs_calls[0].kwargs["sigma_f"] is None

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


# ---------------------------------------------------------------------------
# TestFetchTimeout (issue #717 — hung herbie/GRIB/network fetch must not
# stall the capture loop indefinitely)
# ---------------------------------------------------------------------------

def _hang(seconds: float):
    """Return a fetch stand-in that sleeps *seconds* then returns a valid result.

    Used to simulate a hung network/GRIB download: with
    CAPTURE_FETCH_TIMEOUT_SECONDS patched much smaller than *seconds*,
    _call_with_timeout must give up and raise before this ever returns.
    """
    def _fn(*args, **kwargs):
        time.sleep(seconds)
        return (72.0, 1.0)
    return _fn


class TestFetchTimeout:
    """A hung fetch inside _capture_station must time out, log, and let the
    loop continue -- never block indefinitely and never raise out of
    _capture_station."""

    def test_hung_nws_fetch_times_out_and_loop_continues(self, caplog):
        """NWS fetch hangs well past the (patched, tiny) timeout -- _capture_station
        must return promptly, log a warning, and still attempt the other channels."""
        db = _db()
        mock_upsert = MagicMock()
        db.upsert_forecast_log_v2 = mock_upsert

        with (
            patch.object(capture_forecasts_module, "CAPTURE_FETCH_TIMEOUT_SECONDS", 0.05),
            patch("src.scripts.capture_forecasts.fetch_nws_with_spread", side_effect=_hang(2.0)),
            # Silence every other channel so this test isolates the nws timeout path.
            patch("src.scripts.capture_forecasts.fetch_open_meteo_with_spread", return_value=None),
            patch("src.scripts.capture_forecasts.fetch_secondary_forecast", return_value=None),
            patch("src.scripts.capture_forecasts.fetch_gfs_with_spread", return_value=(70.0, None)),
            patch("src.scripts.capture_forecasts.fetch_gefs_ensemble", return_value=[]),
            patch("src.data.hrrr.fetch_hrrr_hourly", return_value=[]),
            patch("src.data.nbm.fetch_nbm_daily_high", return_value=None),
            patch("src.data.ecmwf_open.fetch_ecmwf_daily_high", return_value=None),
            patch("src.data.icon.fetch_icon_hourly", return_value=[]),
            caplog.at_level(logging.WARNING),
        ):
            start = time.monotonic()
            _capture_station(**_capture_kwargs(db=db))  # must not raise, must not block
            elapsed = time.monotonic() - start

        # Returned promptly -- nowhere near the 2s the hung fetch would have taken.
        assert elapsed < 1.0, f"Expected a quick return after timeout, took {elapsed:.2f}s"

        # Timeout was logged for the nws channel.
        assert any(
            "nws fetch failed/timed out" in r.getMessage() for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

        # No upsert for nws (timed out), but the loop proceeded to gfs and wrote it.
        nws_calls = [c for c in mock_upsert.call_args_list if c.kwargs.get("model") == "nws"]
        gfs_calls = [c for c in mock_upsert.call_args_list if c.kwargs.get("model") == "gfs"]
        assert len(nws_calls) == 0, "Expected no nws upsert after a timeout"
        assert len(gfs_calls) == 1, "Expected the loop to continue to the gfs channel"

    def test_hung_hrrr_fetch_times_out_without_raising(self, caplog):
        """Same guard for a channel that already had its own try/except (HRRR) --
        the timeout must be caught by that existing handler, not escape it."""
        db = _db()
        mock_upsert = MagicMock()
        db.upsert_forecast_log_v2 = mock_upsert

        with (
            _silence_base_sources(),
            patch.object(capture_forecasts_module, "CAPTURE_FETCH_TIMEOUT_SECONDS", 0.05),
            patch("src.data.hrrr.fetch_hrrr_hourly", side_effect=_hang(2.0)),
            caplog.at_level(logging.WARNING),
        ):
            start = time.monotonic()
            _capture_station(**_capture_kwargs(db=db))
            elapsed = time.monotonic() - start

        assert elapsed < 1.0, f"Expected a quick return after timeout, took {elapsed:.2f}s"
        assert any(
            "hrrr ingestion failed" in r.getMessage() for r in caplog.records
        ), [r.getMessage() for r in caplog.records]
        hrrr_calls = [c for c in mock_upsert.call_args_list if c.kwargs.get("model") == "hrrr"]
        assert len(hrrr_calls) == 0


# ---------------------------------------------------------------------------
# TestRunCapturesResilience (issue #717 — a single station/model failure must
# never kill the whole capture run)
# ---------------------------------------------------------------------------

class TestRunCapturesResilience:
    """run_captures() must keep iterating over stations even when
    _capture_station() raises unexpectedly for one of them."""

    def test_one_station_failure_does_not_stop_the_run(self, caplog):
        fake_stations = [
            ("KORD", 41.98, -87.90, "Chicago"),
            ("KDEN", 39.85, -104.66, "Denver"),
        ]

        call_log: list[str] = []

        def _fake_capture_station(*, station, **kwargs):
            call_log.append(station)
            if station == "KORD":
                raise RuntimeError("simulated unexpected capture failure")
            # KDEN succeeds silently.

        with (
            patch.object(capture_forecasts_module, "STATIONS", fake_stations),
            patch.object(capture_forecasts_module, "_capture_station", side_effect=_fake_capture_station),
            caplog.at_level(logging.ERROR),
        ):
            run_captures(db=None, dry_run=True, force=True)

        # Both stations were attempted despite the first one raising.
        assert call_log == ["KORD", "KDEN"], call_log
        assert any(
            "capture failed unexpectedly" in r.getMessage() for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

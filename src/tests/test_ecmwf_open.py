"""Unit tests for src/data/ecmwf_open.py.

All network calls and herbie I/O are mocked; the suite runs fully offline
without herbie or cfgrib installed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.data.ecmwf_open import (
    EcmwfForecast,
    _ECMWF_ATTRIBUTION,
    _kelvin_to_f,
    _resolve_ecmwf_cycle,
    fetch_ecmwf_daily_high,
    fetch_ecmwf_hourly,
)
from src.data.hrrr import HourlyTemp

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW_UTC = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
# Use a cycle 24h in the past so TOMORROW falls within the 120h forecast window
CYCLE_DT = (_NOW_UTC - timedelta(hours=24)).replace(tzinfo=timezone.utc)
LONDON_LAT = 51.5074
LONDON_LON = -0.1278
TOKYO_LAT = 35.6762
TOKYO_LON = 139.6503
TOMORROW = (datetime.now(timezone.utc) + timedelta(days=1)).date()


# ---------------------------------------------------------------------------
# _kelvin_to_f
# ---------------------------------------------------------------------------


class TestKelvinToF:
    def test_freezing(self):
        assert abs(_kelvin_to_f(273.15) - 32.0) < 1e-6

    def test_boiling(self):
        assert abs(_kelvin_to_f(373.15) - 212.0) < 1e-6

    def test_summer_value(self):
        result = _kelvin_to_f(295.15)
        assert 70.0 < result < 73.0


# ---------------------------------------------------------------------------
# fetch_ecmwf_hourly — happy path
# ---------------------------------------------------------------------------


class TestFetchEcmwfHourlyHappyPath:
    """All 24 forecast hours return a valid temperature."""

    def _make_2t_side_effect(self, kelvin: float):
        def _side_effect(cycle_dt, fxx, lat, lon):
            return kelvin
        return _side_effect

    def test_returns_24_results(self):
        kelvin = 295.15
        with (
            patch(
                "src.data.ecmwf_open._resolve_ecmwf_cycle",
                return_value=CYCLE_DT,
            ),
            patch(
                "src.data.ecmwf_open._fetch_ecmwf_2t",
                side_effect=self._make_2t_side_effect(kelvin),
            ),
        ):
            results = fetch_ecmwf_hourly(LONDON_LAT, LONDON_LON, station="EGLL")

        assert len(results) == 24

    def test_result_type(self):
        kelvin = 295.15
        with (
            patch("src.data.ecmwf_open._resolve_ecmwf_cycle", return_value=CYCLE_DT),
            patch(
                "src.data.ecmwf_open._fetch_ecmwf_2t",
                side_effect=self._make_2t_side_effect(kelvin),
            ),
        ):
            results = fetch_ecmwf_hourly(LONDON_LAT, LONDON_LON)

        for r in results:
            assert isinstance(r, HourlyTemp)
            assert isinstance(r.ts_utc, datetime)
            assert r.ts_utc.tzinfo is not None
            assert isinstance(r.temp_f, float)

    def test_valid_times_are_cycle_plus_fxx(self):
        kelvin = 295.15
        with (
            patch("src.data.ecmwf_open._resolve_ecmwf_cycle", return_value=CYCLE_DT),
            patch(
                "src.data.ecmwf_open._fetch_ecmwf_2t",
                side_effect=self._make_2t_side_effect(kelvin),
            ),
        ):
            results = fetch_ecmwf_hourly(LONDON_LAT, LONDON_LON)

        for i, r in enumerate(results):
            expected_fxx = i + 1  # fxx=1..24
            expected_ts = CYCLE_DT + timedelta(hours=expected_fxx)
            assert r.ts_utc == expected_ts

    def test_kelvin_converted_to_fahrenheit(self):
        kelvin = 273.15  # exactly 32 °F
        with (
            patch("src.data.ecmwf_open._resolve_ecmwf_cycle", return_value=CYCLE_DT),
            patch(
                "src.data.ecmwf_open._fetch_ecmwf_2t",
                side_effect=self._make_2t_side_effect(kelvin),
            ),
        ):
            results = fetch_ecmwf_hourly(LONDON_LAT, LONDON_LON)

        assert len(results) == 24
        for r in results:
            assert abs(r.temp_f - 32.0) < 1e-6

    def test_global_location_tokyo(self):
        """ECMWF covers global domain — Tokyo should work."""
        kelvin = 300.0
        with (
            patch("src.data.ecmwf_open._resolve_ecmwf_cycle", return_value=CYCLE_DT),
            patch(
                "src.data.ecmwf_open._fetch_ecmwf_2t",
                side_effect=self._make_2t_side_effect(kelvin),
            ),
        ):
            results = fetch_ecmwf_hourly(TOKYO_LAT, TOKYO_LON, station="RJTT")

        assert len(results) == 24


# ---------------------------------------------------------------------------
# fetch_ecmwf_hourly — missing cycle fallback
# ---------------------------------------------------------------------------


class TestFetchEcmwfHourlyMissingCycle:
    def test_returns_empty_list_when_no_cycle(self):
        with patch("src.data.ecmwf_open._resolve_ecmwf_cycle", return_value=None):
            result = fetch_ecmwf_hourly(LONDON_LAT, LONDON_LON, station="EGLL")

        assert result == []
        assert result is not None

    def test_no_fetch_when_no_cycle(self):
        with (
            patch("src.data.ecmwf_open._resolve_ecmwf_cycle", return_value=None),
            patch("src.data.ecmwf_open._fetch_ecmwf_2t") as mock_fetch,
        ):
            fetch_ecmwf_hourly(LONDON_LAT, LONDON_LON)

        mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# fetch_ecmwf_daily_high — happy path
# ---------------------------------------------------------------------------


class TestFetchEcmwfDailyHighHappyPath:
    def _make_2t_side_effect(self, kelvin: float):
        def _side_effect(cycle_dt, fxx, lat, lon):
            return kelvin
        return _side_effect

    def test_returns_ecmwf_forecast(self, tmp_path):
        kelvin = 305.0  # hot day
        target = TOMORROW
        with (
            patch("src.data.ecmwf_open._resolve_ecmwf_cycle", return_value=CYCLE_DT),
            patch(
                "src.data.ecmwf_open._fetch_ecmwf_2t",
                side_effect=self._make_2t_side_effect(kelvin),
            ),
            patch("src.data.ecmwf_open._get_cache_dir", return_value=tmp_path),
        ):
            result = fetch_ecmwf_daily_high(LONDON_LAT, LONDON_LON, target_date=target)

        assert isinstance(result, EcmwfForecast)
        assert result.valid_date == target
        assert result.cycle_ts == CYCLE_DT
        assert isinstance(result.forecast_high_f, float)

    def test_high_is_max_of_hourly(self, tmp_path):
        """forecast_high_f should be the max across the 24h window."""
        # Return incrementally increasing temps
        call_count = [0]

        def _varying(cycle_dt, fxx, lat, lon):
            # Only return values for hours within TOMORROW's window
            valid_dt = cycle_dt + timedelta(hours=fxx)
            target_start = datetime(
                TOMORROW.year, TOMORROW.month, TOMORROW.day, tzinfo=timezone.utc
            )
            target_end = target_start + timedelta(days=1)
            if target_start <= valid_dt < target_end:
                call_count[0] += 1
                # Return increasing values (last one will be max)
                return 273.15 + call_count[0]
            return None

        with (
            patch("src.data.ecmwf_open._resolve_ecmwf_cycle", return_value=CYCLE_DT),
            patch("src.data.ecmwf_open._fetch_ecmwf_2t", side_effect=_varying),
            patch("src.data.ecmwf_open._get_cache_dir", return_value=tmp_path),
        ):
            result = fetch_ecmwf_daily_high(LONDON_LAT, LONDON_LON, target_date=TOMORROW)

        assert result is not None
        # The max kelvin was 273.15 + call_count[0], which converts to call_count[0] * 9/5 + 32
        expected_f = _kelvin_to_f(273.15 + call_count[0])
        assert abs(result.forecast_high_f - expected_f) < 1e-4

    def test_attribution_string(self, tmp_path):
        kelvin = 295.0
        with (
            patch("src.data.ecmwf_open._resolve_ecmwf_cycle", return_value=CYCLE_DT),
            patch(
                "src.data.ecmwf_open._fetch_ecmwf_2t",
                side_effect=self._make_2t_side_effect(kelvin),
            ),
            patch("src.data.ecmwf_open._get_cache_dir", return_value=tmp_path),
        ):
            result = fetch_ecmwf_daily_high(LONDON_LAT, LONDON_LON, target_date=TOMORROW)

        assert result is not None
        assert result.attribution == _ECMWF_ATTRIBUTION
        assert result.attribution == "ECMWF Open Data, CC-BY-4.0"


# ---------------------------------------------------------------------------
# fetch_ecmwf_daily_high — missing cycle fallback
# ---------------------------------------------------------------------------


class TestFetchEcmwfDailyHighMissingCycle:
    def test_returns_none_when_no_cycle(self, tmp_path):
        with (
            patch("src.data.ecmwf_open._resolve_ecmwf_cycle", return_value=None),
            patch("src.data.ecmwf_open._get_cache_dir", return_value=tmp_path),
        ):
            result = fetch_ecmwf_daily_high(LONDON_LAT, LONDON_LON, target_date=TOMORROW)

        assert result is None

    def test_returns_none_when_all_fetches_fail(self, tmp_path):
        with (
            patch("src.data.ecmwf_open._resolve_ecmwf_cycle", return_value=CYCLE_DT),
            patch("src.data.ecmwf_open._fetch_ecmwf_2t", return_value=None),
            patch("src.data.ecmwf_open._get_cache_dir", return_value=tmp_path),
        ):
            result = fetch_ecmwf_daily_high(LONDON_LAT, LONDON_LON, target_date=TOMORROW)

        assert result is None


# ---------------------------------------------------------------------------
# Attribution string — present in EcmwfForecast constant
# ---------------------------------------------------------------------------


class TestAttribution:
    def test_attribution_constant(self):
        assert _ECMWF_ATTRIBUTION == "ECMWF Open Data, CC-BY-4.0"

    def test_attribution_in_dataclass_field(self):
        fc = EcmwfForecast(
            forecast_high_f=72.0,
            valid_date=date.today(),
            cycle_ts=CYCLE_DT,
            attribution=_ECMWF_ATTRIBUTION,
        )
        assert fc.attribution == "ECMWF Open Data, CC-BY-4.0"

    def test_attribution_logged_on_hourly_fetch(self, caplog):
        import logging

        with (
            patch("src.data.ecmwf_open._resolve_ecmwf_cycle", return_value=None),
            caplog.at_level(logging.INFO, logger="src.data.ecmwf_open"),
        ):
            fetch_ecmwf_hourly(LONDON_LAT, LONDON_LON)

        assert any("ECMWF Open Data, CC-BY-4.0" in r.message for r in caplog.records)

    def test_attribution_logged_on_daily_high_fetch(self, caplog, tmp_path):
        import logging

        with (
            patch("src.data.ecmwf_open._resolve_ecmwf_cycle", return_value=None),
            patch("src.data.ecmwf_open._get_cache_dir", return_value=tmp_path),
            caplog.at_level(logging.INFO, logger="src.data.ecmwf_open"),
        ):
            fetch_ecmwf_daily_high(LONDON_LAT, LONDON_LON, target_date=TOMORROW)

        assert any("ECMWF Open Data, CC-BY-4.0" in r.message for r in caplog.records)

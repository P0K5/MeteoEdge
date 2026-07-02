"""Tests for the low-side weather builder (issue #554).

Root cause of #554: the low-side scanner block in scanner.py (shadow-only,
Epic C) was fully implemented and unit-tested in isolation, but nothing in
the codebase ever built a `weather_low` dict or wired it (together with a
`prob_low_fn`) into the production poll_once() -> scan_markets() call. The
low-side block was therefore unreachable dead code in production and no
low-direction shadow trade was ever recorded, despite 600+ high-side trades.

This file covers the new data-flow pieces added to fix that:
  - src.data.metar.compute_daily_low_window  (running minimum in a window)
  - src.data.metar.low_window_bounds         (sunset -> sunrise window)
  - src.weather.builder.build_weather_low_for_scanning (per-station assembly)

See test_run_wiring.py::TestPollOncePassesWeatherLow for the regression test
covering the actual wiring bug (poll_once() -> scan_markets()).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import pytz

from src.data.metar import compute_daily_low_window, low_window_bounds


# ---------------------------------------------------------------------------
# compute_daily_low_window — pure running-minimum logic
# ---------------------------------------------------------------------------

class TestComputeDailyLowWindow:
    TZ = "America/Chicago"

    def test_finds_running_minimum_within_window(self):
        tz = pytz.timezone(self.TZ)
        window_start = tz.localize(datetime(2026, 6, 24, 20, 0))  # 8pm local (sunset)
        metars = [
            {"temp": 22.0, "reportTime": "2026-06-24T21:00:00-05:00"},  # 71.6F, in window
            {"temp": 15.0, "reportTime": "2026-06-25T03:00:00-05:00"},  # 59.0F, in window (lowest)
            {"temp": 18.0, "reportTime": "2026-06-25T05:30:00-05:00"},  # 64.4F, in window
            # Before window_start (yesterday's daytime heat) — must be excluded even
            # though it's numerically lower than the daytime norm would suggest.
            {"temp": -5.0, "reportTime": "2026-06-24T15:00:00-05:00"},
        ]
        result = compute_daily_low_window(metars, self.TZ, window_start)
        assert result is not None
        low_f, low_time = result
        assert low_f == pytest.approx(59.0, abs=0.1)
        assert low_time.hour == 3

    def test_returns_none_when_no_observations_in_window(self):
        tz = pytz.timezone(self.TZ)
        window_start = tz.localize(datetime(2026, 6, 24, 20, 0))
        metars = [{"temp": 5.0, "reportTime": "2026-06-24T15:00:00-05:00"}]  # before window
        assert compute_daily_low_window(metars, self.TZ, window_start) is None

    def test_returns_none_for_empty_metar_list(self):
        tz = pytz.timezone(self.TZ)
        window_start = tz.localize(datetime(2026, 6, 24, 20, 0))
        assert compute_daily_low_window([], self.TZ, window_start) is None

    def test_ignores_malformed_entries(self):
        tz = pytz.timezone(self.TZ)
        window_start = tz.localize(datetime(2026, 6, 24, 20, 0))
        metars = [
            {"temp": None, "reportTime": "2026-06-25T02:00:00-05:00"},
            {"reportTime": "2026-06-25T02:00:00-05:00"},  # missing temp
            {"temp": 10.0},  # missing time
            {"temp": 12.0, "reportTime": "2026-06-25T02:00:00-05:00"},  # 53.6F, only valid entry
        ]
        result = compute_daily_low_window(metars, self.TZ, window_start)
        assert result is not None
        assert result[0] == pytest.approx(53.6, abs=0.1)

    def test_observation_exactly_at_window_start_is_included(self):
        tz = pytz.timezone(self.TZ)
        window_start = tz.localize(datetime(2026, 6, 24, 20, 0))
        metars = [{"temp": 10.0, "reportTime": "2026-06-24T20:00:00-05:00"}]
        result = compute_daily_low_window(metars, self.TZ, window_start)
        assert result is not None
        assert result[0] == pytest.approx(50.0, abs=0.1)


# ---------------------------------------------------------------------------
# low_window_bounds — sunset -> sunrise window selection
# ---------------------------------------------------------------------------

KHOU_LAT, KHOU_LON = 29.6454, -95.2789


class TestLowWindowBounds:
    def test_after_sunset_uses_todays_sunset_to_tomorrows_sunrise(self):
        """Late evening (well after Houston's ~20:2x CDT June sunset): window should
        be [today's sunset, tomorrow's sunrise], and 'now' should fall inside it."""
        fixed_now_utc = datetime(2026, 6, 25, 4, 0, tzinfo=timezone.utc)  # 23:00 CDT
        local_tz = pytz.timezone("America/Chicago")
        now_local = fixed_now_utc.astimezone(local_tz)

        with patch("src.data.metar.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: (
                fixed_now_utc.astimezone(tz) if tz is not None else fixed_now_utc
            )
            window_start, window_end = low_window_bounds("KHOU", KHOU_LAT, KHOU_LON)

        assert window_start.date() == now_local.date(), "window should start at today's sunset"
        assert window_end.date() == now_local.date() + timedelta(days=1), (
            "window should end at tomorrow's sunrise"
        )
        assert window_start < window_end
        assert window_start <= now_local, "sunset must already have occurred"

    def test_before_sunrise_uses_yesterdays_sunset_to_todays_sunrise(self):
        """Pre-dawn (well before Houston's ~06:1x CDT June sunrise): window should
        still be last night's -- [yesterday's sunset, today's sunrise] -- with 'now'
        still inside it (sunrise hasn't happened yet)."""
        fixed_now_utc = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)  # 03:00 CDT
        local_tz = pytz.timezone("America/Chicago")
        now_local = fixed_now_utc.astimezone(local_tz)

        with patch("src.data.metar.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: (
                fixed_now_utc.astimezone(tz) if tz is not None else fixed_now_utc
            )
            window_start, window_end = low_window_bounds("KHOU", KHOU_LAT, KHOU_LON)

        assert window_start.date() == now_local.date() - timedelta(days=1), (
            "window should start at yesterday's sunset"
        )
        assert window_end.date() == now_local.date(), "window should end at today's sunrise"
        assert window_start < now_local < window_end, (
            "now (03:00) should fall inside last night's still-open window"
        )


# ---------------------------------------------------------------------------
# build_weather_low_for_scanning / _build_one_station_low
# ---------------------------------------------------------------------------

KORD_STATION = ("KORD", 41.9742, -87.9073, "Chicago", "KORD", "F", "America/Chicago")
# Milan is a real STATIONS entry with no "lowest temperature in ..." Polymarket
# mapping (see POLYMARKET_CITY_TO_STATION_LOW in src/strategy/scanner.py) --
# used to verify non-low-market stations are skipped entirely.
LIMC_STATION = ("LIMC", 45.6306, 8.7281, "Milan", "LIMC", "C", "Europe/Rome")

_FAKE_METAR = [{"temp": 15.0, "reportTime": "2026-06-25T03:00:00-05:00"}]


def _patch_low_build_deps(metar_data=None, low_result=(59.0, None)):
    if metar_data is None:
        metar_data = _FAKE_METAR
    window_start = pytz.timezone("America/Chicago").localize(datetime(2026, 6, 24, 20, 0))
    window_end = pytz.timezone("America/Chicago").localize(datetime(2026, 6, 25, 5, 30))
    low_time = pytz.timezone("America/Chicago").localize(datetime(2026, 6, 25, 3, 0))
    resolved_low_result = None
    if low_result is not None:
        resolved_low_result = (low_result[0], low_time)
    return [
        patch("src.weather.builder.fetch_all_metars_today", return_value=metar_data),
        patch("src.weather.builder.low_window_bounds", return_value=(window_start, window_end)),
        patch("src.weather.builder.compute_daily_low_window", return_value=resolved_low_result),
        patch("src.weather.builder.now_local",
              return_value=pytz.timezone("America/Chicago").localize(datetime(2026, 6, 25, 3, 5))),
    ]


class TestBuildWeatherLowForScanning:
    def test_builds_state_for_low_market_station(self):
        from src.weather.builder import build_weather_low_for_scanning

        patches = _patch_low_build_deps()
        with patches[0], patches[1], patches[2], patches[3]:
            result = build_weather_low_for_scanning(stations=[KORD_STATION])

        assert "KORD" in result
        state = result["KORD"]
        assert state.station == "KORD"
        assert state.current_low_f == pytest.approx(59.0)
        assert state.forecast_low_f is None
        assert state.latest_temp_f == pytest.approx((15.0 * 9 / 5) + 32)

    def test_skips_station_without_low_market_mapping(self):
        """Milan (LIMC) has no 'lowest temperature in Milan' Polymarket market --
        build_weather_low_for_scanning must not build state for it."""
        from src.weather.builder import build_weather_low_for_scanning

        patches = _patch_low_build_deps()
        with patches[0], patches[1], patches[2], patches[3]:
            result = build_weather_low_for_scanning(stations=[LIMC_STATION])

        assert result == {}

    def test_skips_station_with_no_metar_data(self):
        from src.weather.builder import build_weather_low_for_scanning

        patches = _patch_low_build_deps(metar_data=[])
        with patches[0], patches[1], patches[2], patches[3]:
            result = build_weather_low_for_scanning(stations=[KORD_STATION])

        assert result == {}

    def test_skips_station_with_no_observations_in_window(self):
        from src.weather.builder import build_weather_low_for_scanning

        patches = _patch_low_build_deps(low_result=None)
        with patches[0], patches[1], patches[2], patches[3]:
            result = build_weather_low_for_scanning(stations=[KORD_STATION])

        assert result == {}

    def test_mixed_station_list_only_builds_low_market_stations(self):
        from src.weather.builder import build_weather_low_for_scanning

        patches = _patch_low_build_deps()
        with patches[0], patches[1], patches[2], patches[3]:
            result = build_weather_low_for_scanning(stations=[KORD_STATION, LIMC_STATION])

        assert set(result.keys()) == {"KORD"}

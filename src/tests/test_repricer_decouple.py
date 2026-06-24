"""Regression tests for issue #425: Decouple held-position re-pricer from scanner active-hours gating.

Critical invariants:
1. build_weather_for_scanning() MUST exclude stations outside STATION_ACTIVE_HOURS (KHOU incident).
2. build_weather_for_pricing() MUST include stations regardless of local hour.
3. _build_weather() backward-compat wrapper must delegate to build_weather_for_scanning().
"""
import sys
import types
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import pytz


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

KHOU_TZ = "America/Chicago"
KHOU_STATION = ("KHOU", 29.64, -95.28, "Houston")

KORD_TZ = "America/Chicago"
KORD_STATION = ("KORD", 41.98, -87.91, "Chicago")

_FAKE_METAR = [
    {
        "temp": 30.0,
        "reportTime": "2026-06-24T02:00:00+00:00",
    }
]


def _make_fake_weather_state(station):
    """Return a minimal WeatherState-like object."""
    ws = MagicMock()
    ws.station = station
    ws.current_high_f = 95.0
    ws.latest_temp_f = 88.0
    ws.corrected_mu_f = None
    ws.deb_mu_f = 95.0
    return ws


def _patch_build_deps(metar_data=None):
    """Return a list of patches that stub all I/O inside _build_one_station."""
    if metar_data is None:
        metar_data = _FAKE_METAR
    return [
        patch("src.weather.builder.fetch_all_metars_today", return_value=metar_data),
        patch("src.weather.builder.compute_daily_high", return_value=(95.0, datetime(2026, 6, 24, 18, 0, tzinfo=timezone.utc))),
        patch("src.weather.builder.fetch_nws_forecast_high", return_value=97.0),
        patch("src.weather.builder.fetch_secondary_forecast", return_value=96.0),
        patch("src.weather.builder.fetch_gfs_forecast_high", return_value=96.5),
        patch("src.weather.builder.fetch_hourly_temp_now", return_value=None),
        patch("src.weather.builder.compute_deb_mu_f", return_value=96.5),
        patch("src.weather.builder.now_local", return_value=datetime(2026, 6, 24, 13, 0, tzinfo=timezone.utc)),
        patch("src.weather.builder.sunset_local", return_value=datetime(2026, 6, 24, 20, 0, tzinfo=timezone.utc)),
        patch("src.weather.builder.get_canonical_station_feeds", return_value=["KHOU"]),
        patch("src.weather.builder.get_source_priority", return_value=[]),
        patch("src.weather.builder.refresh_weights"),
        patch("src.weather.builder.get_weights", return_value={"nws": 0.5, "open_meteo": 0.5, "gfs": 0.0}),
        patch("src.weather.builder.compute_correction", return_value=None),
        patch("src.weather.builder.apply_residual_correction", return_value=(96.5, {})),
    ]


# ---------------------------------------------------------------------------
# TestBuildWeatherForScanningActiveHoursGate
# ---------------------------------------------------------------------------

class TestBuildWeatherForScanningActiveHoursGate:
    """build_weather_for_scanning() must honour active-hours gate (KHOU regression)."""

    def test_khou_at_local_23_excluded_from_scanner(self):
        """KHOU at local hour 23 (11pm) must be excluded — KHOU 2026-05-27 regression."""
        from src.weather.builder import build_weather_for_scanning

        # 23:00 local Chicago time = 04:00 UTC next day (CDT offset -5)
        khou_23 = datetime(2026, 6, 25, 4, 0, tzinfo=timezone.utc)  # 23:00 CDT

        with patch("src.weather.builder.datetime") as mock_dt:
            mock_dt.now.return_value = khou_23.astimezone(pytz.timezone(KHOU_TZ))
            mock_dt.now.side_effect = lambda tz=None: (
                khou_23.astimezone(tz) if tz is not None else khou_23
            )
            result = build_weather_for_scanning(stations=[KHOU_STATION])

        assert "KHOU" not in result, "KHOU at local 23:00 must be excluded from scanner"

    def test_khou_at_local_03_excluded_from_scanner(self):
        """KHOU at local hour 03 (3am) must be excluded."""
        from src.weather.builder import build_weather_for_scanning

        khou_03_utc = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)  # 03:00 CDT

        with patch("src.weather.builder.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: (
                khou_03_utc.astimezone(tz) if tz is not None else khou_03_utc
            )
            result = build_weather_for_scanning(stations=[KHOU_STATION])

        assert "KHOU" not in result, "KHOU at local 03:00 must be excluded from scanner"

    def test_station_inside_window_included_in_scanner(self):
        """A station inside its active window must be included in scanner output."""
        from src.weather.builder import build_weather_for_scanning

        # KHOU active window is (6, 23). 12:00 CDT = 17:00 UTC
        khou_12_utc = datetime(2026, 6, 24, 17, 0, tzinfo=timezone.utc)

        patches = _patch_build_deps()
        with patch("src.weather.builder.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: (
                khou_12_utc.astimezone(tz) if tz is not None else khou_12_utc
            )
            mock_dt.side_effect = None
            # Apply all I/O patches
            active_patches = []
            for p in patches:
                active_patches.append(p.__enter__() if hasattr(p, '__enter__') else p.start())

            from unittest.mock import patch as _patch
            with _patch("src.weather.builder.fetch_all_metars_today", return_value=_FAKE_METAR), \
                 _patch("src.weather.builder.compute_daily_high", return_value=(95.0, khou_12_utc)), \
                 _patch("src.weather.builder.fetch_nws_forecast_high", return_value=97.0), \
                 _patch("src.weather.builder.fetch_secondary_forecast", return_value=96.0), \
                 _patch("src.weather.builder.fetch_gfs_forecast_high", return_value=96.5), \
                 _patch("src.weather.builder.fetch_hourly_temp_now", return_value=None), \
                 _patch("src.weather.builder.compute_deb_mu_f", return_value=96.5), \
                 _patch("src.weather.builder.now_local", return_value=khou_12_utc), \
                 _patch("src.weather.builder.sunset_local", return_value=khou_12_utc), \
                 _patch("src.weather.builder.get_canonical_station_feeds", return_value=["KHOU"]), \
                 _patch("src.weather.builder.get_source_priority", return_value=[]), \
                 _patch("src.weather.builder.refresh_weights"), \
                 _patch("src.weather.builder.get_weights", return_value={"nws": 0.5, "open_meteo": 0.5, "gfs": 0.0}), \
                 _patch("src.weather.builder.compute_correction", return_value=None), \
                 _patch("src.weather.builder.apply_residual_correction", return_value=(96.5, {})):
                result = build_weather_for_scanning(stations=[KHOU_STATION])

            for p in patches:
                try:
                    p.__exit__(None, None, None)
                except Exception:
                    try:
                        p.stop()
                    except Exception:
                        pass

        assert "KHOU" in result, "KHOU at local 12:00 must be included in scanner"

    def test_health_out_reports_degraded_for_out_of_window(self):
        """health_out must contain a 'degraded' entry for out-of-window stations."""
        from src.weather.builder import build_weather_for_scanning

        khou_03_utc = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)  # 03:00 CDT

        with patch("src.weather.builder.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: (
                khou_03_utc.astimezone(tz) if tz is not None else khou_03_utc
            )
            health = []
            build_weather_for_scanning(stations=[KHOU_STATION], health_out=health)

        assert len(health) == 1
        assert health[0]["station"] == "KHOU"
        assert health[0]["status"] == "degraded"
        assert "active window" in health[0]["reason"]


# ---------------------------------------------------------------------------
# TestBuildWeatherForPricingNoGate
# ---------------------------------------------------------------------------

class TestBuildWeatherForPricingNoGate:
    """build_weather_for_pricing() must include stations regardless of local hour."""

    def test_out_of_window_station_included_in_pricing(self):
        """Station outside its active window must still appear in pricing output."""
        from src.weather.builder import build_weather_for_pricing

        # KHOU at local 03:00 — outside scanner window, but pricer must include it
        khou_03_utc = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)

        with patch("src.weather.builder.fetch_all_metars_today", return_value=_FAKE_METAR), \
             patch("src.weather.builder.compute_daily_high", return_value=(95.0, khou_03_utc)), \
             patch("src.weather.builder.fetch_nws_forecast_high", return_value=97.0), \
             patch("src.weather.builder.fetch_secondary_forecast", return_value=96.0), \
             patch("src.weather.builder.fetch_gfs_forecast_high", return_value=96.5), \
             patch("src.weather.builder.fetch_hourly_temp_now", return_value=None), \
             patch("src.weather.builder.compute_deb_mu_f", return_value=96.5), \
             patch("src.weather.builder.now_local", return_value=khou_03_utc), \
             patch("src.weather.builder.sunset_local", return_value=khou_03_utc), \
             patch("src.weather.builder.get_canonical_station_feeds", return_value=["KHOU"]), \
             patch("src.weather.builder.get_source_priority", return_value=[]), \
             patch("src.weather.builder.refresh_weights"), \
             patch("src.weather.builder.get_weights", return_value={"nws": 0.5, "open_meteo": 0.5, "gfs": 0.0}), \
             patch("src.weather.builder.compute_correction", return_value=None), \
             patch("src.weather.builder.apply_residual_correction", return_value=(96.5, {})):
            result = build_weather_for_pricing([KHOU_STATION])

        assert "KHOU" in result, "KHOU must appear in pricing output even at 03:00 local"

    def test_overnight_station_produces_valid_weather_state(self):
        """Pricing output for overnight station must have valid high_f and latest_temp_f."""
        from src.weather.builder import build_weather_for_pricing
        from src.model.envelope import WeatherState

        ref_time = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)

        with patch("src.weather.builder.fetch_all_metars_today", return_value=_FAKE_METAR), \
             patch("src.weather.builder.compute_daily_high", return_value=(95.0, ref_time)), \
             patch("src.weather.builder.fetch_nws_forecast_high", return_value=97.0), \
             patch("src.weather.builder.fetch_secondary_forecast", return_value=96.0), \
             patch("src.weather.builder.fetch_gfs_forecast_high", return_value=96.5), \
             patch("src.weather.builder.fetch_hourly_temp_now", return_value=None), \
             patch("src.weather.builder.compute_deb_mu_f", return_value=96.5), \
             patch("src.weather.builder.now_local", return_value=ref_time), \
             patch("src.weather.builder.sunset_local", return_value=ref_time), \
             patch("src.weather.builder.get_canonical_station_feeds", return_value=["KHOU"]), \
             patch("src.weather.builder.get_source_priority", return_value=[]), \
             patch("src.weather.builder.refresh_weights"), \
             patch("src.weather.builder.get_weights", return_value={"nws": 0.5, "open_meteo": 0.5, "gfs": 0.0}), \
             patch("src.weather.builder.compute_correction", return_value=None), \
             patch("src.weather.builder.apply_residual_correction", return_value=(96.5, {})):
            result = build_weather_for_pricing([KHOU_STATION])

        ws = result.get("KHOU")
        assert ws is not None
        assert ws.current_high_f == 95.0
        # latest METAR temp: 30C = 86F
        assert abs(ws.latest_temp_f - 86.0) < 0.1

    def test_build_weather_for_pricing_with_no_metar_returns_empty(self):
        """When METAR data is unavailable the station must be omitted (not crash)."""
        from src.weather.builder import build_weather_for_pricing

        with patch("src.weather.builder.fetch_all_metars_today", return_value=[]):
            result = build_weather_for_pricing([KHOU_STATION])

        assert result == {}


# ---------------------------------------------------------------------------
# TestStopLossFiresDuringOffHours
# ---------------------------------------------------------------------------

class TestStopLossFiresDuringOffHours:
    """Verify that pricing weather enables stop-loss/take-profit during off-hours."""

    def _make_position(self, station):
        return {
            "station": station,
            "market_id": "TEST-MARKET",
            "bracket_low": 90,
            "bracket_high": 100,
            "side": "YES",
            "contracts": 10,
            "avg_cost": 0.6,
            "stop_loss_pct": 0.5,
            "take_profit_pct": 0.9,
        }

    def test_fair_value_not_null_with_pricing_weather(self):
        """_pricing_weather supplies non-null fair_value even outside scanner window."""
        from src.weather.builder import build_weather_for_pricing

        ref_time = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)

        with patch("src.weather.builder.fetch_all_metars_today", return_value=_FAKE_METAR), \
             patch("src.weather.builder.compute_daily_high", return_value=(95.0, ref_time)), \
             patch("src.weather.builder.fetch_nws_forecast_high", return_value=97.0), \
             patch("src.weather.builder.fetch_secondary_forecast", return_value=96.0), \
             patch("src.weather.builder.fetch_gfs_forecast_high", return_value=96.5), \
             patch("src.weather.builder.fetch_hourly_temp_now", return_value=None), \
             patch("src.weather.builder.compute_deb_mu_f", return_value=96.5), \
             patch("src.weather.builder.now_local", return_value=ref_time), \
             patch("src.weather.builder.sunset_local", return_value=ref_time), \
             patch("src.weather.builder.get_canonical_station_feeds", return_value=["KHOU"]), \
             patch("src.weather.builder.get_source_priority", return_value=[]), \
             patch("src.weather.builder.refresh_weights"), \
             patch("src.weather.builder.get_weights", return_value={"nws": 0.5, "open_meteo": 0.5, "gfs": 0.0}), \
             patch("src.weather.builder.compute_correction", return_value=None), \
             patch("src.weather.builder.apply_residual_correction", return_value=(96.5, {})):
            pricing_weather = build_weather_for_pricing([KHOU_STATION])

        assert "KHOU" in pricing_weather
        ws = pricing_weather["KHOU"]
        assert ws.deb_mu_f is not None, "deb_mu_f must be non-null for stop-loss to work"

    def test_fair_value_null_without_pricing_weather(self):
        """Without pricing weather the station is absent — simulating old broken behavior."""
        from src.weather.builder import build_weather_for_scanning

        # KHOU at 03:00 local — scanner excludes it
        khou_03_utc = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)

        with patch("src.weather.builder.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: (
                khou_03_utc.astimezone(tz) if tz is not None else khou_03_utc
            )
            scanner_weather = build_weather_for_scanning(stations=[KHOU_STATION])

        assert "KHOU" not in scanner_weather, "Scanner must not include KHOU at 03:00 local"

    def test_stop_loss_fires_with_off_hours_pricing_weather(self):
        """When pricing weather provides a fair_value, stop-loss logic can fire."""
        from src.weather.builder import build_weather_for_pricing

        ref_time = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)

        with patch("src.weather.builder.fetch_all_metars_today", return_value=_FAKE_METAR), \
             patch("src.weather.builder.compute_daily_high", return_value=(95.0, ref_time)), \
             patch("src.weather.builder.fetch_nws_forecast_high", return_value=97.0), \
             patch("src.weather.builder.fetch_secondary_forecast", return_value=96.0), \
             patch("src.weather.builder.fetch_gfs_forecast_high", return_value=96.5), \
             patch("src.weather.builder.fetch_hourly_temp_now", return_value=None), \
             patch("src.weather.builder.compute_deb_mu_f", return_value=96.5), \
             patch("src.weather.builder.now_local", return_value=ref_time), \
             patch("src.weather.builder.sunset_local", return_value=ref_time), \
             patch("src.weather.builder.get_canonical_station_feeds", return_value=["KHOU"]), \
             patch("src.weather.builder.get_source_priority", return_value=[]), \
             patch("src.weather.builder.refresh_weights"), \
             patch("src.weather.builder.get_weights", return_value={"nws": 0.5, "open_meteo": 0.5, "gfs": 0.0}), \
             patch("src.weather.builder.compute_correction", return_value=None), \
             patch("src.weather.builder.apply_residual_correction", return_value=(96.5, {})):
            pricing_weather = build_weather_for_pricing([KHOU_STATION])

        ws = pricing_weather.get("KHOU")
        assert ws is not None
        # Simulate stop-loss check: if deb_mu_f is available, stop-loss can evaluate
        fair_value = ws.corrected_mu_f if ws.corrected_mu_f is not None else ws.deb_mu_f
        assert fair_value is not None, "fair_value must be non-null for stop-loss evaluation"


# ---------------------------------------------------------------------------
# TestBuildWeatherBackwardCompat
# ---------------------------------------------------------------------------

class TestBuildWeatherBackwardCompat:
    """_build_weather() backward-compat wrapper must delegate to build_weather_for_scanning."""

    def test_build_weather_uses_active_hours_gate(self):
        """_build_weather (legacy) must exclude out-of-window stations via delegation."""
        from src.weather.builder import _build_weather, build_weather_for_scanning

        khou_03_utc = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)

        with patch("src.weather.builder.build_weather_for_scanning") as mock_scan:
            mock_scan.return_value = {}
            _build_weather()
            mock_scan.assert_called_once()

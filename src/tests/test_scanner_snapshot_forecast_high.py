"""Unit tests for issue #1488 — forecast_high snapshot field fallback chain.

Tests that scan_decisions snapshot correctly uses the forecast_high fallback chain:
corrected_mu_f > deb_mu_f > forecast_high_f (mirroring line 845 of scanner.py).
"""
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from src.strategy.scanner import scan_markets
from src.model.envelope import WeatherState


class TestSnapshotForecastHighFallback:
    """Tests for snapshot forecast_high field using corrected_mu_f/deb_mu_f fallback chain."""

    _FROZEN_NOW = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)

    def _weather_state(self, forecast_high_f=80.0, corrected_mu_f=None, deb_mu_f=None) -> WeatherState:
        """Build a WeatherState with optional corrected_mu_f and deb_mu_f overrides."""
        return WeatherState(
            station="KMIA",
            now_local=self._FROZEN_NOW,
            sunset_local=self._FROZEN_NOW.replace(hour=20),
            current_high_f=75.0,
            current_high_time=self._FROZEN_NOW,
            latest_temp_f=72.0,
            latest_temp_time=self._FROZEN_NOW,
            forecast_high_f=forecast_high_f,
            corrected_mu_f=corrected_mu_f,
            deb_mu_f=deb_mu_f,
        )

    def _miami_market(self, group_title: str = "80-85°F") -> dict:
        """Build a Miami highest-temp market."""
        today = datetime.now(timezone.utc).date()
        end_dt = datetime(
            today.year, today.month, today.day, 23, 59, 59, tzinfo=timezone.utc
        )
        return {
            "conditionId": "0xmia_forecast_high",
            "question": "Will the highest temperature in Miami be 80-85°F?",
            "groupItemTitle": group_title,
            "outcomes": '["Yes","No"]',
            "outcomePrices": '["0.60","0.40"]',
            "clobTokenIds": '["tok_yes","tok_no"]',
            "endDate": end_dt.isoformat(),
        }

    def test_snapshot_uses_corrected_mu_when_both_set(self):
        """When both corrected_mu_f and forecast_high_f are set, snapshot
        should use corrected_mu_f (line 919 precedence test)."""
        weather = {
            "KMIA": self._weather_state(
                forecast_high_f=80.0,
                corrected_mu_f=78.5,  # Different value, higher priority
            )
        }
        market = self._miami_market()

        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0):
            _candidates, snapshots = scan_markets(weather, [market])

        assert len(snapshots) == 1
        snap = snapshots[0]
        # Should use corrected_mu_f=78.5, not forecast_high_f=80.0
        assert snap["forecast_high"] == pytest.approx(78.5)

    def test_snapshot_uses_deb_mu_when_forecast_high_none(self):
        """When forecast_high_f is None but deb_mu_f is set, snapshot should
        use deb_mu_f as fallback (line 919 fallback chain)."""
        weather = {
            "KMIA": self._weather_state(
                forecast_high_f=None,  # Missing the usual forecast
                deb_mu_f=79.0,  # DEB value available
            )
        }
        market = self._miami_market()

        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0):
            _candidates, snapshots = scan_markets(weather, [market])

        assert len(snapshots) == 1
        snap = snapshots[0]
        # Should use deb_mu_f=79.0, not None
        assert snap["forecast_high"] == pytest.approx(79.0)

    def test_snapshot_uses_corrected_mu_even_when_deb_and_forecast_set(self):
        """Corrected_mu_f takes precedence over deb_mu_f and forecast_high_f."""
        weather = {
            "KMIA": self._weather_state(
                forecast_high_f=80.0,
                deb_mu_f=79.0,
                corrected_mu_f=77.5,  # Highest priority
            )
        }
        market = self._miami_market()

        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0):
            _candidates, snapshots = scan_markets(weather, [market])

        assert len(snapshots) == 1
        snap = snapshots[0]
        # Should use corrected_mu_f=77.5
        assert snap["forecast_high"] == pytest.approx(77.5)

    def test_snapshot_uses_forecast_high_as_last_resort(self):
        """When corrected_mu_f and deb_mu_f are both None, use forecast_high_f."""
        weather = {
            "KMIA": self._weather_state(
                forecast_high_f=80.0,
                corrected_mu_f=None,
                deb_mu_f=None,
            )
        }
        market = self._miami_market()

        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0):
            _candidates, snapshots = scan_markets(weather, [market])

        assert len(snapshots) == 1
        snap = snapshots[0]
        # Should use forecast_high_f=80.0
        assert snap["forecast_high"] == pytest.approx(80.0)

    def test_snapshot_stays_none_when_all_missing(self):
        """When all three forecast values are None, snapshot forecast_high
        should remain None (not silently become 0.0)."""
        weather = {
            "KMIA": self._weather_state(
                forecast_high_f=None,
                corrected_mu_f=None,
                deb_mu_f=None,
            )
        }
        market = self._miami_market()

        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0):
            _candidates, snapshots = scan_markets(weather, [market])

        assert len(snapshots) == 1
        snap = snapshots[0]
        # Should stay None, not 0.0
        assert snap["forecast_high"] is None

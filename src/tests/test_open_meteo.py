"""Tests for src/data/open_meteo.py."""
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from src.data.open_meteo import fetch_secondary_forecast, fetch_hourly_temp_now


def _make_hourly_data(hours_offset_list, temps):
    """Build mock Open-Meteo hourly payload. hours_offset_list is relative to now."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    times = [(now + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in hours_offset_list]
    return {"hourly": {"time": times, "temperature_2m": temps, "weather_code": [0]*len(temps)}}


class TestFetchHourlyTempNow:
    def test_returns_nearest_past_hour(self):
        data = _make_hourly_data([-3, -2, -1, 0, 1], [70.0, 72.0, 75.0, 77.0, 80.0])
        with patch("src.data.open_meteo.cached_fetch_json", return_value=data):
            result = fetch_hourly_temp_now(35.0, 139.0)
        # hour offset 0 is current hour (<=now), offset 1 is in the future
        assert result == 77.0

    def test_returns_none_on_empty_data(self):
        with patch("src.data.open_meteo.cached_fetch_json", return_value=None):
            assert fetch_hourly_temp_now(35.0, 139.0) is None

    def test_secondary_forecast_unchanged(self):
        data = _make_hourly_data(list(range(-5, 25)), [float(60 + i) for i in range(30)])
        with patch("src.data.open_meteo.cached_fetch_json", return_value=data):
            result = fetch_secondary_forecast(35.0, 139.0)
        # Should be max of first 24 temps
        expected = max(float(60 + i) for i in range(24))
        assert result == expected

    def test_no_past_slots_returns_none(self):
        data = _make_hourly_data([1, 2, 3], [80.0, 82.0, 84.0])
        with patch("src.data.open_meteo.cached_fetch_json", return_value=data):
            assert fetch_hourly_temp_now(35.0, 139.0) is None

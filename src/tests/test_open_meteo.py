"""Tests for src/data/open_meteo.py."""
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from src.data.open_meteo import fetch_secondary_forecast, fetch_hourly_temp_now


def _make_hourly_data(hours_offset_list, temps):
    """Build mock Open-Meteo hourly payload. hours_offset_list is relative to now."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    times = [(now + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in hours_offset_list]
    return {"hourly": {"time": times, "temperature_2m": temps, "weather_code": [0]*len(temps)}}


def _make_hourly_data_tz(utc_offset_seconds, hours_offset_list, temps):
    """Build a mock Open-Meteo ``timezone=auto`` payload for a non-UTC station.

    Mirrors the real API: ``hourly.time[]`` entries are naive strings in the
    station's *local* time, and ``utc_offset_seconds`` (top-level) is the
    station's current UTC offset. ``hours_offset_list`` is relative to "now"
    expressed in that local time.
    """
    now_utc = datetime.now(timezone.utc)
    local_now = (now_utc + timedelta(seconds=utc_offset_seconds)).replace(
        minute=0, second=0, microsecond=0
    )
    times = [(local_now + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in hours_offset_list]
    return {
        "utc_offset_seconds": utc_offset_seconds,
        "hourly": {"time": times, "temperature_2m": temps, "weather_code": [0] * len(temps)},
    }


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

    def test_west_of_utc_station_picks_current_local_hour(self):
        """Regression for issue #810.

        ``timezone=auto`` timestamps are local, not UTC. For a west-of-UTC
        station (e.g. UTC-5, like Chicago), the naive-as-UTC bug treats
        local times as if they were already UTC, which for negative offsets
        makes hours *ahead* of local-now look like they're still in the
        past — so the buggy code picks the last (future, warmest) entry in
        the list instead of the true current-hour temperature.
        """
        data = _make_hourly_data_tz(
            -18000,  # UTC-5
            [-3, -2, -1, 0, 1],
            [70.0, 72.0, 75.0, 77.0, 80.0],
        )
        with patch("src.data.open_meteo.cached_fetch_json", return_value=data):
            result = fetch_hourly_temp_now(41.98, -87.9)  # KORD
        # Correct current local hour is offset 0 (77.0), not the future
        # offset +1 slot (80.0) that the timezone-naive bug would return.
        assert result == 77.0

    def test_east_of_utc_station_picks_current_local_hour(self):
        """Regression for issue #810 (positive-offset / east-of-UTC case).

        For an east-of-UTC station (e.g. UTC+9, like Tokyo), the naive-as-UTC
        bug makes local "now" and earlier hours look like they're in the
        *future* relative to true UTC now, so the buggy code finds no past
        slot at all and returns None, discarding a perfectly good reading.
        """
        data = _make_hourly_data_tz(
            32400,  # UTC+9
            [-3, -2, -1, 0, 1],
            [70.0, 72.0, 75.0, 77.0, 80.0],
        )
        with patch("src.data.open_meteo.cached_fetch_json", return_value=data):
            result = fetch_hourly_temp_now(35.68, 139.77)  # Tokyo
        assert result == 77.0

"""Tests for src/data/nws.py.

Covers fetch_nws_forecast_low (issue #583) -- the low-side counterpart of
fetch_nws_forecast_high, wired into build_weather_low_for_scanning() so the
low-side shadow candidates get a real forecast signal instead of always
None. See src/tests/test_weather_low_builder.py for the builder-level wiring
tests.
"""
from unittest.mock import patch

from src.data.nws import fetch_nws_forecast_low, fetch_nws_forecast_high


def _make_periods(temps_f):
    return {
        "properties": {
            "periods": [
                {"temperature": t, "temperatureUnit": "F"} for t in temps_f
            ]
        }
    }


class TestFetchNwsForecastLow:
    def test_returns_min_of_forecast_window(self):
        data = _make_periods([70, 65, 58, 60, 72, 68])
        with patch("src.data.nws.get_nws_forecast_url", return_value="http://fake"), \
                patch("src.data.nws.cached_fetch_json", return_value=data):
            result = fetch_nws_forecast_low(41.97, -87.9)
        assert result == 58

    def test_only_considers_first_18_periods(self):
        # 20 periods; the minimum (10) sits outside the first 18 -- must be ignored.
        temps = [70] * 18 + [10, 90]
        data = _make_periods(temps)
        with patch("src.data.nws.get_nws_forecast_url", return_value="http://fake"), \
                patch("src.data.nws.cached_fetch_json", return_value=data):
            result = fetch_nws_forecast_low(41.97, -87.9)
        assert result == 70

    def test_ignores_non_fahrenheit_periods(self):
        data = {
            "properties": {
                "periods": [
                    {"temperature": 5, "temperatureUnit": "C"},   # excluded
                    {"temperature": 60, "temperatureUnit": "F"},
                    {"temperature": 55, "temperatureUnit": "F"},
                ]
            }
        }
        with patch("src.data.nws.get_nws_forecast_url", return_value="http://fake"), \
                patch("src.data.nws.cached_fetch_json", return_value=data):
            result = fetch_nws_forecast_low(41.97, -87.9)
        assert result == 55

    def test_returns_none_when_no_forecast_url(self):
        with patch("src.data.nws.get_nws_forecast_url", return_value=None):
            assert fetch_nws_forecast_low(41.97, -87.9) is None

    def test_returns_none_when_fetch_fails(self):
        with patch("src.data.nws.get_nws_forecast_url", return_value="http://fake"), \
                patch("src.data.nws.cached_fetch_json", return_value=None):
            assert fetch_nws_forecast_low(41.97, -87.9) is None

    def test_returns_none_on_malformed_payload(self):
        with patch("src.data.nws.get_nws_forecast_url", return_value="http://fake"), \
                patch("src.data.nws.cached_fetch_json", return_value={"unexpected": True}):
            assert fetch_nws_forecast_low(41.97, -87.9) is None

    def test_low_is_never_greater_than_high_for_same_forecast_window(self):
        """Sanity check that low and high sourcing are consistent: for the same
        forecast payload, fetch_nws_forecast_low must never exceed
        fetch_nws_forecast_high (min <= max over the same window)."""
        data = _make_periods([70, 65, 58, 60, 72, 68])
        with patch("src.data.nws.get_nws_forecast_url", return_value="http://fake"), \
                patch("src.data.nws.cached_fetch_json", return_value=data):
            low = fetch_nws_forecast_low(41.97, -87.9)
            high = fetch_nws_forecast_high(41.97, -87.9)
        assert low <= high

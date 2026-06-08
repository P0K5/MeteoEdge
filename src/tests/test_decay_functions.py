"""Unit tests for src/model/decay_functions.py."""
from datetime import datetime, timezone

import pytest

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from src.model.decay_functions import (
    linear_decay,
    exponential_decay,
    get_decay_factor,
    PEAK_WINDOWS,
    DEFAULT_PEAK_WINDOW,
    MARKET_OPEN_HOUR,
)

# Use Tokyo (Asia/Tokyo = UTC+9) as reference city
CITY = "Tokyo"
TZ = ZoneInfo("Asia/Tokyo")
PEAK_START, PEAK_END = PEAK_WINDOWS[CITY]


def _local_to_utc(hour: float, city_tz_str: str = "Asia/Tokyo") -> datetime:
    """Build a UTC datetime from a local hour (fractional ok) in city timezone."""
    h = int(hour)
    m = int((hour - h) * 60)
    tz = ZoneInfo(city_tz_str)
    local_dt = datetime(2024, 6, 15, h, m, 0, tzinfo=tz)
    return local_dt.astimezone(timezone.utc)


class TestLinearDecay:
    def test_returns_one_at_market_open(self):
        obs = _local_to_utc(MARKET_OPEN_HOUR)
        assert linear_decay(CITY, obs) == pytest.approx(1.0)

    def test_returns_zero_at_peak_start(self):
        obs = _local_to_utc(PEAK_START)
        assert linear_decay(CITY, obs) == pytest.approx(0.0)

    def test_returns_zero_inside_peak_window(self):
        obs = _local_to_utc(PEAK_START + 1)
        assert linear_decay(CITY, obs) == pytest.approx(0.0)

    def test_midpoint_is_approximately_half(self):
        midpoint_hour = MARKET_OPEN_HOUR + (PEAK_START - MARKET_OPEN_HOUR) / 2
        obs = _local_to_utc(midpoint_hour)
        result = linear_decay(CITY, obs)
        assert abs(result - 0.5) < 0.05


class TestExponentialDecay:
    def test_result_always_in_unit_interval(self):
        for h in range(0, PEAK_START + 3):
            obs = _local_to_utc(h)
            result = exponential_decay(CITY, obs)
            assert 0.0 <= result <= 1.0, f"Out of range at hour {h}: {result}"

    def test_decays_faster_than_linear_at_midpoint(self):
        midpoint_hour = MARKET_OPEN_HOUR + (PEAK_START - MARKET_OPEN_HOUR) / 2
        obs = _local_to_utc(midpoint_hour)
        assert exponential_decay(CITY, obs) < linear_decay(CITY, obs)

    def test_returns_zero_at_peak_start(self):
        obs = _local_to_utc(PEAK_START)
        assert exponential_decay(CITY, obs) == pytest.approx(0.0)


class TestGetDecayFactor:
    def test_dispatches_to_linear_by_default(self):
        obs = _local_to_utc(6)
        assert get_decay_factor(CITY, obs) == linear_decay(CITY, obs)

    def test_dispatches_to_exponential(self):
        obs = _local_to_utc(6)
        assert get_decay_factor(CITY, obs, decay_type="exponential") == exponential_decay(CITY, obs)


class TestUnknownCity:
    def test_unknown_city_uses_default_no_exception(self):
        obs = _local_to_utc(6)
        # Should not raise; uses DEFAULT_PEAK_WINDOW
        result = linear_decay("UnknownCity", obs)
        assert 0.0 <= result <= 1.0

"""Unit tests for src/model/decay_functions.py — decay function edge cases."""
from datetime import datetime, timezone

import pytest

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

try:
    from src.model.decay_functions import (
        linear_decay,
        exponential_decay,
        get_decay_factor,
        PEAK_WINDOWS,
        DEFAULT_PEAK_WINDOW,
        MARKET_OPEN_HOUR,
    )
    HAS_DECAY = True
except ImportError:
    HAS_DECAY = False

pytestmark = pytest.mark.skipif(not HAS_DECAY, reason="decay_functions not yet implemented")

CITY = "Tokyo"
TZ = ZoneInfo("Asia/Tokyo")
PEAK_START = PEAK_WINDOWS["Tokyo"][0] if HAS_DECAY else 13


def _local_hour_utc(hour: int) -> datetime:
    """Return UTC datetime for given local hour in Tokyo timezone."""
    local_dt = datetime(2024, 6, 15, hour, 0, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    return local_dt.astimezone(timezone.utc)


class TestLinearDecayEdgeCases:
    def test_linear_returns_one_at_market_open(self):
        """obs at 00:00 local → linear_decay returns 1.0."""
        obs = _local_hour_utc(0)
        assert linear_decay(CITY, obs) == pytest.approx(1.0)

    def test_linear_returns_zero_at_peak_start(self):
        """obs at peak_start_hour local → returns 0.0."""
        obs = _local_hour_utc(PEAK_START)
        assert linear_decay(CITY, obs) == pytest.approx(0.0)

    def test_linear_returns_zero_inside_peak_window(self):
        """obs inside peak window → always 0.0."""
        obs = _local_hour_utc(PEAK_START + 1)
        assert linear_decay(CITY, obs) == pytest.approx(0.0)


class TestExponentialDecayEdgeCases:
    def test_exponential_always_in_unit_interval(self):
        """All inputs → result in [0.0, 1.0]."""
        for h in range(0, 20):
            obs = _local_hour_utc(h)
            result = exponential_decay(CITY, obs)
            assert 0.0 <= result <= 1.0, f"Out of bounds at hour {h}: {result}"

    def test_exponential_faster_than_linear_at_midpoint(self):
        """At midpoint, exponential decay < linear decay."""
        midpoint = PEAK_START // 2
        obs = _local_hour_utc(midpoint)
        assert exponential_decay(CITY, obs) < linear_decay(CITY, obs)


class TestUnknownCity:
    def test_unknown_city_uses_default_no_exception(self):
        """Unknown city → no exception, result in [0.0, 1.0]."""
        obs = _local_hour_utc(6)
        result = linear_decay("UnknownCity_XYZ", obs)
        assert 0.0 <= result <= 1.0

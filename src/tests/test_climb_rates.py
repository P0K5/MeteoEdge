"""Unit tests for src/model/climb_rates.py."""
from datetime import datetime

import pytest

from src.model.climb_rates import CLIMB_BY_MONTH, expected_additional_rise


class TestClimbByMonth:
    def test_has_all_twelve_months(self):
        assert len(CLIMB_BY_MONTH) == 12

    def test_months_are_one_to_twelve(self):
        assert set(CLIMB_BY_MONTH.keys()) == set(range(1, 13))

    def test_each_month_has_24_hours(self):
        for month, table in CLIMB_BY_MONTH.items():
            assert len(table) == 24, f"month {month} has {len(table)} hour entries, expected 24"

    def test_hour_zero_has_high_climb(self):
        """Midnight has the most rise still ahead."""
        for month in range(1, 13):
            assert CLIMB_BY_MONTH[month][0] > 0

    def test_hour_19_and_later_are_zero(self):
        """After 7pm no further rise is expected."""
        for month in range(1, 13):
            for hour in range(19, 24):
                assert CLIMB_BY_MONTH[month][hour] == 0.0, (
                    f"month {month} hour {hour} should be 0.0"
                )


class TestExpectedAdditionalRise:
    def _dt(self, month: int, hour: int) -> datetime:
        return datetime(2026, month, 1, hour, 0)

    def test_may_hour_14_returns_4_5(self):
        result = expected_additional_rise(self._dt(5, 14))
        assert result == 4.5

    def test_may_hour_0_returns_22(self):
        result = expected_additional_rise(self._dt(5, 0))
        assert result == 22.0

    def test_hour_19_returns_zero(self):
        result = expected_additional_rise(self._dt(5, 19))
        assert result == 0.0

    def test_all_months_return_same_may_values(self):
        """All months currently use May as placeholder — values must match."""
        for month in range(1, 13):
            result = expected_additional_rise(self._dt(month, 10))
            assert result == 8.0, f"month {month} hour 10: expected 8.0, got {result}"

    def test_returns_float(self):
        result = expected_additional_rise(self._dt(7, 12))
        assert isinstance(result, float)

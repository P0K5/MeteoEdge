"""Unit tests for src/model/cooling_rates.py."""
from datetime import datetime

import pytest

from src.model.cooling_rates import COOLING_BY_MONTH, expected_additional_drop


class TestCoolingByMonth:
    def test_has_all_twelve_months(self):
        assert len(COOLING_BY_MONTH) == 12

    def test_months_are_one_to_twelve(self):
        assert set(COOLING_BY_MONTH.keys()) == set(range(1, 13))

    def test_each_month_has_24_hours(self):
        for month, table in COOLING_BY_MONTH.items():
            assert len(table) == 24, (
                f"month {month} has {len(table)} hour entries, expected 24"
            )

    def test_hour_19_has_nonzero_drop(self):
        """Pre-midnight has the most cooling ahead."""
        for month in range(1, 13):
            assert COOLING_BY_MONTH[month][19] > 0

    def test_daytime_hours_are_zero(self):
        """Daytime hours 09–18 should be 0.0 — daily low has already occurred."""
        for month in range(1, 13):
            for hour in range(9, 19):
                assert COOLING_BY_MONTH[month][hour] == 0.0, (
                    f"month {month} hour {hour} should be 0.0"
                )


class TestExpectedAdditionalDrop:
    def _dt(self, month: int, hour: int) -> datetime:
        return datetime(2026, month, 1, hour, 0)

    def test_no_station_daytime_returns_zero(self):
        """Daytime hours with no station should return 0.0."""
        for hour in range(9, 19):
            result = expected_additional_drop(self._dt(6, hour))
            assert result == 0.0, f"hour {hour}: expected 0.0, got {result}"

    def test_no_station_premidnight_returns_nonzero(self):
        """Pre-midnight hours with no station should return positive drop."""
        result = expected_additional_drop(self._dt(1, 21))
        assert result > 0.0

    def test_no_station_postmidnight_returns_nonzero(self):
        """Post-midnight hours with no station should return positive drop."""
        result = expected_additional_drop(self._dt(1, 2))
        assert result > 0.0

    def test_station_lookup_hit_kord_winter(self):
        """KORD January hour 19 should return the per-station value."""
        from src.data.cooling_lookup import COOLING_LOOKUP
        expected = COOLING_LOOKUP["KORD"][1][19]
        result = expected_additional_drop(self._dt(1, 19), station="KORD")
        assert result == expected

    def test_station_lookup_hit_kden_summer(self):
        """KDEN June hour 22 should return the per-station value."""
        from src.data.cooling_lookup import COOLING_LOOKUP
        expected = COOLING_LOOKUP["KDEN"][6][22]
        result = expected_additional_drop(self._dt(6, 22), station="KDEN")
        assert result == expected

    def test_station_lookup_hit_kmia_winter(self):
        """KMIA December hour 20 should return the per-station value."""
        from src.data.cooling_lookup import COOLING_LOOKUP
        expected = COOLING_LOOKUP["KMIA"][12][20]
        result = expected_additional_drop(self._dt(12, 20), station="KMIA")
        assert result == expected

    def test_station_lookup_hit_katl_fall(self):
        """KATL October hour 0 should return the per-station value."""
        from src.data.cooling_lookup import COOLING_LOOKUP
        expected = COOLING_LOOKUP["KATL"][10][0]
        result = expected_additional_drop(self._dt(10, 0), station="KATL")
        assert result == expected

    def test_station_fallback_unknown_station(self):
        """Unknown station falls back to _DEFAULT_COOLING_LOOKUP."""
        from src.model.cooling_rates import _DEFAULT_COOLING_LOOKUP
        result = expected_additional_drop(self._dt(6, 21), station="KUNKNOWN")
        assert result == _DEFAULT_COOLING_LOOKUP.get(21, 0.0)

    def test_station_fallback_daytime_returns_zero(self):
        """Unknown station at daytime hours should still return 0.0."""
        result = expected_additional_drop(self._dt(7, 14), station="KUNKNOWN")
        assert result == 0.0

    def test_no_station_path_returns_float(self):
        """No-station path must return a float."""
        result = expected_additional_drop(self._dt(3, 20))
        assert isinstance(result, float)

    def test_station_path_returns_float(self):
        """Station path must return a float."""
        result = expected_additional_drop(self._dt(3, 20), station="KORD")
        assert isinstance(result, float)

    def test_all_hours_return_non_negative_no_station(self):
        """All hours for no-station path must return non-negative values."""
        for month in range(1, 13):
            for hour in range(24):
                result = expected_additional_drop(self._dt(month, hour))
                assert result >= 0.0, (
                    f"month {month} hour {hour}: expected >= 0.0, got {result}"
                )

    def test_all_hours_return_non_negative_kord(self):
        """All hours for KORD must return non-negative values."""
        for month in range(1, 13):
            for hour in range(24):
                result = expected_additional_drop(self._dt(month, hour), station="KORD")
                assert result >= 0.0, (
                    f"KORD month {month} hour {hour}: expected >= 0.0, got {result}"
                )

    def test_all_hours_return_non_negative_kden(self):
        """All hours for KDEN must return non-negative values."""
        for month in range(1, 13):
            for hour in range(24):
                result = expected_additional_drop(self._dt(month, hour), station="KDEN")
                assert result >= 0.0, (
                    f"KDEN month {month} hour {hour}: expected >= 0.0, got {result}"
                )

    def test_all_hours_return_non_negative_kmia(self):
        """All hours for KMIA must return non-negative values."""
        for month in range(1, 13):
            for hour in range(24):
                result = expected_additional_drop(self._dt(month, hour), station="KMIA")
                assert result >= 0.0, (
                    f"KMIA month {month} hour {hour}: expected >= 0.0, got {result}"
                )

    def test_all_hours_return_non_negative_katl(self):
        """All hours for KATL must return non-negative values."""
        for month in range(1, 13):
            for hour in range(24):
                result = expected_additional_drop(self._dt(month, hour), station="KATL")
                assert result >= 0.0, (
                    f"KATL month {month} hour {hour}: expected >= 0.0, got {result}"
                )

    def test_pre_midnight_larger_than_postmidnight_kord_winter(self):
        """Pre-midnight drop should exceed post-midnight drop (KORD January)."""
        pre_midnight = expected_additional_drop(self._dt(1, 21), station="KORD")
        post_midnight = expected_additional_drop(self._dt(1, 2), station="KORD")
        assert pre_midnight > post_midnight, (
            f"Pre-midnight {pre_midnight} should exceed post-midnight {post_midnight}"
        )

    def test_winter_larger_than_summer_kord_premidnight(self):
        """Winter pre-midnight drop should exceed summer drop (KORD)."""
        winter = expected_additional_drop(self._dt(1, 21), station="KORD")
        summer = expected_additional_drop(self._dt(7, 21), station="KORD")
        assert winter > summer, (
            f"Winter {winter} should exceed summer {summer} for KORD"
        )

    def test_kord_larger_than_kmia_winter(self):
        """Chicago should have larger winter overnight drops than Miami."""
        kord = expected_additional_drop(self._dt(1, 21), station="KORD")
        kmia = expected_additional_drop(self._dt(1, 21), station="KMIA")
        assert kord > kmia, (
            f"KORD winter {kord} should exceed KMIA winter {kmia}"
        )

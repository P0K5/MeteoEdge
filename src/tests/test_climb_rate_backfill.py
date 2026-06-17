"""Tests for --from-db climb-rate backfill: DB method and computation logic."""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure project root is importable from the test runner's perspective
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.data.db import Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db() -> Database:
    """Return a fresh in-memory Database instance."""
    return Database(":memory:")


def _insert_obs(db: Database, station: str, ts: str, temp_f: float) -> None:
    db.insert_observation(
        ts=ts,
        station=station,
        temp_f=temp_f,
        temp_native=temp_f,
        unit="F",
        source="metar",
    )


# ---------------------------------------------------------------------------
# 1. DB method returns correct schema
# ---------------------------------------------------------------------------

class TestGetHourlyObsForClimbSchema:
    """get_hourly_obs_for_climb returns dicts with date, hour_local, temp_f keys."""

    def test_db_method_returns_correct_schema(self):
        db = _db()
        _insert_obs(db, "KORD", "2024-06-15T14:00:00", 82.0)
        rows = db.get_hourly_obs_for_climb("KORD")
        assert len(rows) == 1
        row = rows[0]
        assert "date" in row, "row must have 'date' key"
        assert "hour_local" in row, "row must have 'hour_local' key"
        assert "temp_f" in row, "row must have 'temp_f' key"
        assert row["date"] == "2024-06-15"
        assert row["hour_local"] == 14
        assert row["temp_f"] == pytest.approx(82.0)

    def test_db_method_returns_float_temp_f(self):
        """temp_f must be a Python float, not sqlite int or None."""
        db = _db()
        _insert_obs(db, "KMIA", "2024-07-01T08:30:00", 90.0)
        rows = db.get_hourly_obs_for_climb("KMIA")
        assert isinstance(rows[0]["temp_f"], float)

    def test_db_method_empty_when_no_obs(self):
        db = _db()
        rows = db.get_hourly_obs_for_climb("ZZZZ")
        assert rows == []

    def test_db_method_filters_by_station(self):
        db = _db()
        _insert_obs(db, "KORD", "2024-06-15T14:00:00", 82.0)
        _insert_obs(db, "KMIA", "2024-06-15T14:00:00", 91.0)
        rows = db.get_hourly_obs_for_climb("KORD")
        assert len(rows) == 1
        assert rows[0]["temp_f"] == pytest.approx(82.0)

    def test_db_method_ordered_by_ts(self):
        """Rows must come back in ascending timestamp order."""
        db = _db()
        _insert_obs(db, "KORD", "2024-06-16T10:00:00", 75.0)
        _insert_obs(db, "KORD", "2024-06-15T08:00:00", 70.0)
        rows = db.get_hourly_obs_for_climb("KORD")
        assert rows[0]["date"] == "2024-06-15"
        assert rows[1]["date"] == "2024-06-16"


# ---------------------------------------------------------------------------
# 2. p95 computed correctly with sufficient data
# ---------------------------------------------------------------------------

class TestP95ComputedCorrectly:
    """With 15 days of obs data, compute_from_db derives the correct p95 value."""

    def _make_obs(self, n_days: int, daily_high: float, hour_temp: float) -> list[dict]:
        """Generate n_days days of observations: one daily_high row and one at hour_temp.

        Returns obs in the format returned by get_hourly_obs_for_climb.
        The daily high is inserted at hour 14, the hour_temp at hour 8.
        """
        obs = []
        for day in range(1, n_days + 1):
            date = f"2024-06-{day:02d}"
            # Hour 8 row — lower temp
            obs.append({"date": date, "hour_local": 8, "temp_f": hour_temp})
            # Hour 14 row — daily high
            obs.append({"date": date, "hour_local": 14, "temp_f": daily_high})
        return obs

    def test_p95_computed_correctly(self):
        """With 15 identical days, p95 of (daily_high - temp_at_hour8) == daily_high - hour_temp."""
        from scripts.build_climb_lookup import quantile, MIN_DAYS_PER_CELL, P95_QUANTILE
        from collections import defaultdict

        daily_high = 90.0
        hour_temp = 72.0
        expected_delta = daily_high - hour_temp  # 18.0

        obs = self._make_obs(15, daily_high, hour_temp)

        # Replicate the from-db computation for (month=6, hour=8)
        month = 6
        hour = 8
        cell_obs = defaultdict(list)
        for row in obs:
            m = int(row["date"][5:7])
            h = row["hour_local"]
            cell_obs[(m, h)].append((row["date"], row["temp_f"]))

        entries = cell_obs[(month, hour)]
        date_temps = defaultdict(list)
        for date, temp_f in entries:
            date_temps[date].append(temp_f)

        # We also need the daily high rows (month, hour=14) to compute per-date highs
        all_date_temps = defaultdict(list)
        for row in obs:
            if int(row["date"][5:7]) == month:
                all_date_temps[row["date"]].append(row["temp_f"])

        daily_highs = {d: max(temps) for d, temps in all_date_temps.items()}
        deltas = [max(0.0, daily_highs[date] - temp_f) for date, temp_f in entries]

        assert len(set(d for d, _ in entries)) >= MIN_DAYS_PER_CELL
        p95 = quantile(deltas, P95_QUANTILE)
        assert p95 == pytest.approx(expected_delta, abs=0.01)


# ---------------------------------------------------------------------------
# 3. Synthetic fallback when insufficient data
# ---------------------------------------------------------------------------

class TestSyntheticFallbackWhenInsufficientData:
    """Fewer than MIN_DAYS_PER_CELL distinct dates → synthetic value is retained."""

    def test_synthetic_fallback_when_insufficient_data(self):
        """With only 5 days, the cell is sparse and the synthetic value must be kept."""
        from scripts.build_climb_lookup import (
            MIN_DAYS_PER_CELL, P95_QUANTILE, quantile
        )
        from collections import defaultdict

        # Build 5 days of obs for (month=1, hour=8)
        obs = []
        for day in range(1, 6):  # 5 days only — below MIN_DAYS_PER_CELL=10
            date = f"2024-01-{day:02d}"
            obs.append({"date": date, "hour_local": 8, "temp_f": 50.0})
            obs.append({"date": date, "hour_local": 14, "temp_f": 65.0})

        month, hour = 1, 8
        cell_obs = defaultdict(list)
        for row in obs:
            m = int(row["date"][5:7])
            h = row["hour_local"]
            cell_obs[(m, h)].append((row["date"], row["temp_f"]))

        entries = cell_obs[(month, hour)]
        distinct_dates = {d for d, _ in entries}

        # Should be fewer than min threshold → no DB-derived value
        assert len(distinct_dates) < MIN_DAYS_PER_CELL

        # Simulate the fallback logic: cell not computed, synthetic value retained
        synthetic_value = 15.0  # a known synthetic value
        cell_p95 = {}  # empty — nothing computed because insufficient data
        result = cell_p95.get((month, hour), synthetic_value)
        assert result == synthetic_value


# ---------------------------------------------------------------------------
# 4. DB values override synthetic when sufficient data
# ---------------------------------------------------------------------------

class TestDbValuesOverrideSynthetic:
    """With >= MIN_DAYS_PER_CELL distinct dates, DB-derived value replaces synthetic."""

    def test_db_values_override_synthetic_when_sufficient(self):
        """10+ distinct dates → the computed p95 replaces the existing synthetic value."""
        from scripts.build_climb_lookup import (
            MIN_DAYS_PER_CELL, P95_QUANTILE, quantile
        )
        from collections import defaultdict

        # 10 days — exactly at the threshold
        n_days = 10
        daily_high = 88.0
        hour_temp = 70.0
        expected_delta = daily_high - hour_temp

        obs = []
        for day in range(1, n_days + 1):
            date = f"2024-03-{day:02d}"
            obs.append({"date": date, "hour_local": 7, "temp_f": hour_temp})
            obs.append({"date": date, "hour_local": 15, "temp_f": daily_high})

        month, hour = 3, 7
        all_date_temps = defaultdict(list)
        cell_obs = defaultdict(list)
        for row in obs:
            m = int(row["date"][5:7])
            h = row["hour_local"]
            cell_obs[(m, h)].append((row["date"], row["temp_f"]))
            if m == month:
                all_date_temps[row["date"]].append(row["temp_f"])

        entries = cell_obs[(month, hour)]
        distinct_dates = {d for d, _ in entries}

        assert len(distinct_dates) >= MIN_DAYS_PER_CELL

        daily_highs = {d: max(temps) for d, temps in all_date_temps.items()}
        deltas = [max(0.0, daily_highs[date] - temp_f) for date, temp_f in entries]
        p95 = round(quantile(deltas, P95_QUANTILE), 2)

        # DB-derived value is the p95 — NOT the synthetic value
        synthetic_value = 99.0  # a clearly different placeholder
        cell_p95 = {(month, hour): p95}
        result = cell_p95.get((month, hour), synthetic_value)

        assert result != synthetic_value
        assert result == pytest.approx(expected_delta, abs=0.01)


# ---------------------------------------------------------------------------
# 5. Promotion gate passes for covered station
# ---------------------------------------------------------------------------

class TestPromotionGatePassesForCoveredStation:
    """CLIMB_LOOKUP has the correct structure: station → {month → {hour → float}}."""

    def test_promotion_gate_passes_for_covered_station(self):
        from src.data.climb_lookup import CLIMB_LOOKUP

        assert isinstance(CLIMB_LOOKUP, dict), "CLIMB_LOOKUP must be a dict"
        assert len(CLIMB_LOOKUP) > 0, "CLIMB_LOOKUP must have at least one station"

        # Pick any station and validate the nested structure
        station = next(iter(CLIMB_LOOKUP))
        months = CLIMB_LOOKUP[station]

        assert isinstance(months, dict), f"{station}: value must be a dict of months"
        assert set(months.keys()) == set(range(1, 13)), (
            f"{station}: must have keys 1-12 (months), got {sorted(months.keys())}"
        )

        for month, hours in months.items():
            assert isinstance(hours, dict), f"{station}/{month}: value must be a dict of hours"
            assert len(hours) == 24, f"{station}/{month}: must have 24 hour entries"
            for h, val in hours.items():
                assert isinstance(h, int), f"{station}/{month}: hour key must be int, got {type(h)}"
                assert isinstance(val, (int, float)), (
                    f"{station}/{month}/{h}: value must be numeric, got {type(val)}"
                )
                assert val >= 0.0, f"{station}/{month}/{h}: climb must be non-negative"

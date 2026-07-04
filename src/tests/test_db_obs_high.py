"""Unit tests for observation daily high computation with station-local date grouping.

Tests:
- get_daily_obs_high() groups observations by station-local calendar day, not UTC
- get_obs_highs_range() groups observations by station-local calendar day, not UTC
- Observations near local midnight land on the correct local date
"""
from src.data.db import Database


def _db() -> Database:
    """Return a fresh in-memory Database instance."""
    return Database(":memory:")


class TestGetDailyObsHigh:
    """get_daily_obs_high() must group by station-local calendar day."""

    def test_get_daily_obs_high_simple(self):
        """Test basic get_daily_obs_high with a simple UTC date."""
        db = _db()
        db.insert_observation(
            ts="2024-06-15T14:00:00+00:00",
            station="KORD",
            temp_f=75.0,
            temp_native=75.0,
            unit="F",
            source="metar",
        )
        db.insert_observation(
            ts="2024-06-15T16:00:00+00:00",
            station="KORD",
            temp_f=78.0,
            temp_native=78.0,
            unit="F",
            source="metar",
        )
        # KORD is in America/Chicago (UTC-5 in June), so 2024-06-15T14:00:00 UTC
        # is 2024-06-15T09:00:00 CDT (local).
        # Both observations fall on the local date 2024-06-15.
        high = db.get_daily_obs_high("KORD", "2024-06-15")
        assert high == 78.0

    def test_get_daily_obs_high_near_local_midnight(self):
        """Test with observation just after local midnight (before UTC midnight).

        KATL is UTC-4 in June. An observation at 2024-06-15T03:30:00 UTC is
        2024-06-14T23:30:00 EDT locally (just before local midnight).
        It should be attributed to 2024-06-14 local date, not 2024-06-15.
        """
        db = _db()
        # Insert observation at 2024-06-15T03:30:00 UTC
        # = 2024-06-14T23:30:00 EDT locally (before local midnight)
        db.insert_observation(
            ts="2024-06-15T03:30:00+00:00",
            station="KATL",
            temp_f=72.0,
            temp_native=72.0,
            unit="F",
            source="metar",
        )
        # Insert observation at 2024-06-15T04:30:00 UTC
        # = 2024-06-15T00:30:00 EDT locally (just after local midnight)
        db.insert_observation(
            ts="2024-06-15T04:30:00+00:00",
            station="KATL",
            temp_f=70.0,
            temp_native=70.0,
            unit="F",
            source="metar",
        )
        # Query for local date 2024-06-14: should get 72.0 (first observation)
        high_14 = db.get_daily_obs_high("KATL", "2024-06-14")
        assert high_14 == 72.0

        # Query for local date 2024-06-15: should get 70.0 (second observation)
        high_15 = db.get_daily_obs_high("KATL", "2024-06-15")
        assert high_15 == 70.0

    def test_get_daily_obs_high_no_observations(self):
        """Return None when no observations exist for the date."""
        db = _db()
        high = db.get_daily_obs_high("KORD", "2024-06-15")
        assert high is None

    def test_get_daily_obs_high_unknown_station(self):
        """Return None when station has no known timezone."""
        db = _db()
        db.insert_observation(
            ts="2024-06-15T14:00:00+00:00",
            station="UNKN",
            temp_f=75.0,
            temp_native=75.0,
            unit="F",
            source="metar",
        )
        high = db.get_daily_obs_high("UNKN", "2024-06-15")
        assert high is None

    def test_get_daily_obs_high_multiple_observations_same_day(self):
        """Return the maximum when multiple observations on the same local day."""
        db = _db()
        db.insert_observation(
            ts="2024-06-15T10:00:00+00:00",
            station="KORD",
            temp_f=71.0,
            temp_native=71.0,
            unit="F",
            source="metar",
        )
        db.insert_observation(
            ts="2024-06-15T16:00:00+00:00",
            station="KORD",
            temp_f=78.0,
            temp_native=78.0,
            unit="F",
            source="metar",
        )
        high = db.get_daily_obs_high("KORD", "2024-06-15")
        assert high == 78.0

    def test_get_daily_obs_high_returns_max(self):
        """Return the maximum temperature for the date."""
        db = _db()
        db.insert_observation(
            ts="2024-06-15T10:00:00+00:00",
            station="KORD",
            temp_f=72.0,
            temp_native=72.0,
            unit="F",
            source="metar",
        )
        db.insert_observation(
            ts="2024-06-15T14:00:00+00:00",
            station="KORD",
            temp_f=85.0,
            temp_native=85.0,
            unit="F",
            source="metar",
        )
        db.insert_observation(
            ts="2024-06-15T18:00:00+00:00",
            station="KORD",
            temp_f=82.0,
            temp_native=82.0,
            unit="F",
            source="metar",
        )
        high = db.get_daily_obs_high("KORD", "2024-06-15")
        assert high == 85.0


class TestGetObsHighsRange:
    """get_obs_highs_range() must group by station-local calendar day."""

    def test_get_obs_highs_range_simple(self):
        """Test basic get_obs_highs_range with multiple dates."""
        db = _db()
        db.insert_observation(
            ts="2024-06-14T14:00:00+00:00",
            station="KORD",
            temp_f=75.0,
            temp_native=75.0,
            unit="F",
            source="metar",
        )
        db.insert_observation(
            ts="2024-06-15T14:00:00+00:00",
            station="KORD",
            temp_f=78.0,
            temp_native=78.0,
            unit="F",
            source="metar",
        )
        db.insert_observation(
            ts="2024-06-16T14:00:00+00:00",
            station="KORD",
            temp_f=82.0,
            temp_native=82.0,
            unit="F",
            source="metar",
        )
        result = db.get_obs_highs_range("KORD", "2024-06-15")
        assert result == {
            "2024-06-15": 78.0,
            "2024-06-16": 82.0,
        }

    def test_get_obs_highs_range_near_local_midnight(self):
        """Test get_obs_highs_range with observations straddling local midnight.

        KATL is UTC-4 in June.
        - 2024-06-15T03:30:00 UTC = 2024-06-14T23:30:00 EDT (local date 2024-06-14)
        - 2024-06-15T04:30:00 UTC = 2024-06-15T00:30:00 EDT (local date 2024-06-15)
        """
        db = _db()
        db.insert_observation(
            ts="2024-06-15T03:30:00+00:00",
            station="KATL",
            temp_f=72.0,
            temp_native=72.0,
            unit="F",
            source="metar",
        )
        db.insert_observation(
            ts="2024-06-15T04:30:00+00:00",
            station="KATL",
            temp_f=70.0,
            temp_native=70.0,
            unit="F",
            source="metar",
        )
        result = db.get_obs_highs_range("KATL", "2024-06-14")
        assert result == {
            "2024-06-14": 72.0,
            "2024-06-15": 70.0,
        }

    def test_get_obs_highs_range_filters_by_since_date(self):
        """Filter to dates >= since_date."""
        db = _db()
        db.insert_observation(
            ts="2024-06-13T14:00:00+00:00",
            station="KORD",
            temp_f=70.0,
            temp_native=70.0,
            unit="F",
            source="metar",
        )
        db.insert_observation(
            ts="2024-06-14T14:00:00+00:00",
            station="KORD",
            temp_f=75.0,
            temp_native=75.0,
            unit="F",
            source="metar",
        )
        db.insert_observation(
            ts="2024-06-15T14:00:00+00:00",
            station="KORD",
            temp_f=78.0,
            temp_native=78.0,
            unit="F",
            source="metar",
        )
        result = db.get_obs_highs_range("KORD", "2024-06-14")
        assert result == {
            "2024-06-14": 75.0,
            "2024-06-15": 78.0,
        }

    def test_get_obs_highs_range_returns_max_per_date(self):
        """Return the maximum temperature for each date."""
        db = _db()
        db.insert_observation(
            ts="2024-06-15T10:00:00+00:00",
            station="KORD",
            temp_f=72.0,
            temp_native=72.0,
            unit="F",
            source="metar",
        )
        db.insert_observation(
            ts="2024-06-15T14:00:00+00:00",
            station="KORD",
            temp_f=85.0,
            temp_native=85.0,
            unit="F",
            source="metar",
        )
        db.insert_observation(
            ts="2024-06-15T18:00:00+00:00",
            station="KORD",
            temp_f=82.0,
            temp_native=82.0,
            unit="F",
            source="metar",
        )
        result = db.get_obs_highs_range("KORD", "2024-06-15")
        assert result == {"2024-06-15": 85.0}

    def test_get_obs_highs_range_empty_for_unknown_station(self):
        """Return empty dict for unknown station."""
        db = _db()
        db.insert_observation(
            ts="2024-06-15T14:00:00+00:00",
            station="UNKN",
            temp_f=75.0,
            temp_native=75.0,
            unit="F",
            source="metar",
        )
        result = db.get_obs_highs_range("UNKN", "2024-06-15")
        assert result == {}

    def test_get_obs_highs_range_multiple_observations_per_date(self):
        """Handle multiple observations on the same local date."""
        db = _db()
        db.insert_observation(
            ts="2024-06-15T10:00:00+00:00",
            station="KORD",
            temp_f=72.0,
            temp_native=72.0,
            unit="F",
            source="metar",
        )
        db.insert_observation(
            ts="2024-06-15T14:00:00+00:00",
            station="KORD",
            temp_f=78.0,
            temp_native=78.0,
            unit="F",
            source="metar",
        )
        db.insert_observation(
            ts="2024-06-15T16:00:00+00:00",
            station="KORD",
            temp_f=75.0,
            temp_native=75.0,
            unit="F",
            source="metar",
        )
        result = db.get_obs_highs_range("KORD", "2024-06-15")
        assert result == {"2024-06-15": 78.0}

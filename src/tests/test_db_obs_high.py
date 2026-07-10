"""Unit tests for observation daily high computation with station-local date grouping.

Tests:
- get_daily_obs_high() groups observations by station-local calendar day, not UTC
- get_obs_highs_range() groups observations by station-local calendar day, not UTC
- Observations near local midnight land on the correct local date
- Issue #558: training_eligible=false stations short-circuit to None/{}
- Issue #558: WSSS unions the METAR (ICAO-keyed) and MSS (city-keyed) feeds
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


class TestTrainingEligibilityExclusion:
    """Issue #558: training_eligible=false stations must be excluded from
    both get_daily_obs_high() and get_obs_highs_range() regardless of any
    observation rows present for them."""

    def test_get_daily_obs_high_zsjn_returns_none(self):
        """ZSJN (Jinan) is training_eligible=false — always None, even with rows present."""
        db = _db()
        db.insert_observation(
            ts="2024-06-15T14:00:00+00:00",
            station="ZSJN",
            temp_f=90.0,
            temp_native=32.2,
            unit="C",
            source="metar",
        )
        assert db.get_daily_obs_high("ZSJN", "2024-06-15") is None

    def test_get_obs_highs_range_zgsz_returns_empty(self):
        """ZGSZ (Shenzhen) is training_eligible=false — always {}, even with rows present."""
        db = _db()
        db.insert_observation(
            ts="2024-06-15T14:00:00+00:00",
            station="ZGSZ",
            temp_f=95.0,
            temp_native=35.0,
            unit="C",
            source="metar",
        )
        assert db.get_obs_highs_range("ZGSZ", "2024-06-14") == {}

    def test_get_daily_obs_high_zhhh_returns_none(self):
        """Wuhan (ZHHH) is training_eligible=false."""
        db = _db()
        db.insert_observation(
            ts="2024-06-15T14:00:00+00:00",
            station="ZHHH",
            temp_f=95.0,
            temp_native=35.0,
            unit="C",
            source="metar",
        )
        assert db.get_daily_obs_high("ZHHH", "2024-06-15") is None

    def test_get_daily_obs_high_zhcc_returns_none(self):
        """Zhengzhou (ZHCC) is training_eligible=false."""
        db = _db()
        db.insert_observation(
            ts="2024-06-15T14:00:00+00:00",
            station="ZHCC",
            temp_f=95.0,
            temp_native=35.0,
            unit="C",
            source="metar",
        )
        assert db.get_daily_obs_high("ZHCC", "2024-06-15") is None

    def test_eligible_station_unaffected(self):
        """A station whose city has no training_eligible flag behaves as before."""
        db = _db()
        db.insert_observation(
            ts="2024-06-15T14:00:00+00:00",
            station="KORD",
            temp_f=78.0,
            temp_native=78.0,
            unit="F",
            source="metar",
        )
        assert db.get_daily_obs_high("KORD", "2024-06-15") == 78.0


class TestWsssMultiSourceVerification:
    """Issue #558: WSSS verification must union METAR (ICAO-keyed) rows with
    Singapore MSS (city-keyed) rows, picking up the higher-cadence feed's max
    even when it differs from the METAR-only max."""

    def test_get_daily_obs_high_picks_up_mss_max_over_metar(self):
        """A Singapore-keyed (MSS) row with a higher temp than any WSSS-keyed
        (METAR) row must win — proves both feed keys are queried, not just WSSS."""
        db = _db()
        # WSSS-keyed METAR row: local high 88.0F
        db.insert_observation(
            ts="2024-06-15T06:00:00+00:00",
            station="WSSS",
            temp_f=88.0,
            temp_native=31.1,
            unit="C",
            source="metar",
        )
        # Singapore-keyed MSS row on the same local day, higher temp: 91.0F
        db.insert_observation(
            ts="2024-06-15T07:15:00+00:00",
            station="Singapore",
            temp_f=91.0,
            temp_native=32.8,
            unit="C",
            source="mss",
        )
        high = db.get_daily_obs_high("WSSS", "2024-06-15")
        assert high == 91.0, (
            "Expected the MSS-keyed (Singapore) row's max to win over the "
            "METAR-keyed (WSSS) row's max"
        )

    def test_get_daily_obs_high_metar_only_row_still_seen(self):
        """If the METAR-keyed row has the higher temp, it must still win
        (both feeds unioned, not just the high-cadence one)."""
        db = _db()
        db.insert_observation(
            ts="2024-06-15T06:00:00+00:00",
            station="WSSS",
            temp_f=93.0,
            temp_native=33.9,
            unit="C",
            source="metar",
        )
        db.insert_observation(
            ts="2024-06-15T07:15:00+00:00",
            station="Singapore",
            temp_f=90.0,
            temp_native=32.2,
            unit="C",
            source="mss",
        )
        high = db.get_daily_obs_high("WSSS", "2024-06-15")
        assert high == 93.0

    def test_get_obs_highs_range_unions_both_feed_keys(self):
        """get_obs_highs_range() must also union WSSS + Singapore keyed rows."""
        db = _db()
        db.insert_observation(
            ts="2024-06-15T06:00:00+00:00",
            station="WSSS",
            temp_f=88.0,
            temp_native=31.1,
            unit="C",
            source="metar",
        )
        db.insert_observation(
            ts="2024-06-15T07:15:00+00:00",
            station="Singapore",
            temp_f=91.0,
            temp_native=32.8,
            unit="C",
            source="mss",
        )
        result = db.get_obs_highs_range("WSSS", "2024-06-14")
        assert result == {"2024-06-15": 91.0}

"""Unit tests for observation daily high computation with station-local date grouping.

Tests:
- get_daily_obs_high() groups observations by station-local calendar day, not UTC
- get_obs_highs_range() groups observations by station-local calendar day, not UTC
- Observations near local midnight land on the correct local date
- Issue #558: training_eligible=false stations short-circuit to None/{}
- Issue #558: WSSS unions the METAR (ICAO-keyed) and MSS (city-keyed) feeds
- Issue #741: raw_json LIKE '%source_fallback%' is a second exclusion signal
- Issue #766: training_eligible_since date-scoped re-entry (Shenzhen/ZGSZ)
"""
import json

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

    def test_get_obs_highs_range_zgsz_returns_empty_before_cutover(self):
        """ZGSZ (Shenzhen) is training_eligible_since=2026-07-14 (issue #766) --
        a pre-cutover date range returns {}, even with rows present."""
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


class TestTrainingEligibleSinceCutover:
    """Issue #766: training_eligible_since date-scoped re-entry for Shenzhen
    (ZGSZ) -- pre-cutover dates stay excluded, post-cutover dates are
    included, following its 2026-07-14 METAR cadence upgrade."""

    def test_get_daily_obs_high_zgsz_excluded_before_cutover(self):
        """A date before the 2026-07-14 cutover still returns None."""
        db = _db()
        db.insert_observation(
            ts="2026-07-13T14:00:00+00:00",
            station="ZGSZ",
            temp_f=95.0,
            temp_native=35.0,
            unit="C",
            source="metar",
        )
        assert db.get_daily_obs_high("ZGSZ", "2026-07-13") is None

    def test_get_daily_obs_high_zgsz_included_on_cutover_date(self):
        """The cutover date itself (2026-07-14) is included."""
        db = _db()
        db.insert_observation(
            ts="2026-07-14T14:00:00+00:00",
            station="ZGSZ",
            temp_f=95.0,
            temp_native=35.0,
            unit="C",
            source="metar",
        )
        assert db.get_daily_obs_high("ZGSZ", "2026-07-14") == 95.0

    def test_get_daily_obs_high_zgsz_included_after_cutover(self):
        """A date well after the cutover is included."""
        db = _db()
        db.insert_observation(
            ts="2026-07-20T14:00:00+00:00",
            station="ZGSZ",
            temp_f=90.0,
            temp_native=32.2,
            unit="C",
            source="metar",
        )
        assert db.get_daily_obs_high("ZGSZ", "2026-07-20") == 90.0

    def test_get_obs_highs_range_zgsz_drops_pre_cutover_dates_only(self):
        """A range spanning the cutover keeps post-cutover dates and drops
        pre-cutover ones, even when since_date itself is before the cutover."""
        db = _db()
        db.insert_observation(
            ts="2026-07-13T14:00:00+00:00",
            station="ZGSZ",
            temp_f=95.0,
            temp_native=35.0,
            unit="C",
            source="metar",
        )
        db.insert_observation(
            ts="2026-07-14T14:00:00+00:00",
            station="ZGSZ",
            temp_f=90.0,
            temp_native=32.2,
            unit="C",
            source="metar",
        )
        db.insert_observation(
            ts="2026-07-15T14:00:00+00:00",
            station="ZGSZ",
            temp_f=91.0,
            temp_native=32.8,
            unit="C",
            source="metar",
        )
        result = db.get_obs_highs_range("ZGSZ", "2026-07-01")
        assert result == {"2026-07-14": 90.0, "2026-07-15": 91.0}


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


class TestFallbackTruthExclusion:
    """Issue #731: is_official=0 (Open-Meteo fallback) rows must be excluded
    from the daily-high TRUTH selectors, so modelled fallback data can never
    override a real METAR reading as the observed high EMOS/DEB train against.

    Uses RJTT/Tokyo: canonical feeds are ["Tokyo", "RJTT"] and Tokyo is
    training_eligible, matching the production Seoul/Busan/Tokyo case where the
    city-keyed feed is Open-Meteo fallback and the ICAO-keyed feed is real
    METAR.
    """

    def _raw_insert(self, db, ts, station, temp_f, is_official):
        """Insert an observation with an explicit is_official value (including
        NULL), which the public insert_observation() helper does not expose."""
        with db._lock:
            db._conn.execute(
                "INSERT INTO observations (ts, station, temp_f, temp_native, unit, "
                "source, is_official) VALUES (?,?,?,?,?,?,?)",
                (ts, station, temp_f, temp_f, "C", "test", is_official),
            )
            db._conn.commit()

    def test_daily_high_ignores_higher_fallback_row(self):
        """A higher is_official=0 fallback row must NOT override the real METAR
        high (the circular-truth bug #731 fixes)."""
        db = _db()
        # Real METAR (is_official defaults to 1) under the ICAO key.
        db.insert_observation(
            ts="2024-06-15T03:00:00+00:00", station="RJTT",
            temp_f=85.0, temp_native=29.4, unit="C", source="metar",
        )
        # Higher modelled fallback under the city key, is_official=0.
        self._raw_insert(db, "2024-06-15T04:00:00+00:00", "Tokyo", 99.0, 0)

        # Truth is the real METAR high, not the higher fallback.
        assert db.get_daily_obs_high("RJTT", "2024-06-15") == 85.0

    def test_obs_highs_range_ignores_higher_fallback_row(self):
        db = _db()
        db.insert_observation(
            ts="2024-06-15T03:00:00+00:00", station="RJTT",
            temp_f=85.0, temp_native=29.4, unit="C", source="metar",
        )
        self._raw_insert(db, "2024-06-15T04:00:00+00:00", "Tokyo", 99.0, 0)

        result = db.get_obs_highs_range("RJTT", "2024-06-14")
        assert result == {"2024-06-15": 85.0}

    def test_official_fallback_free_max_still_correct(self):
        """When the real feed's own row is the max, it is returned unchanged
        (the fix only removes fallback rows, nothing else)."""
        db = _db()
        db.insert_observation(
            ts="2024-06-15T03:00:00+00:00", station="RJTT",
            temp_f=90.0, temp_native=32.2, unit="C", source="metar",
        )
        self._raw_insert(db, "2024-06-15T04:00:00+00:00", "Tokyo", 80.0, 0)
        assert db.get_daily_obs_high("RJTT", "2024-06-15") == 90.0

    def test_fallback_only_station_yields_no_truth(self):
        """If ALL rows are fallback (is_official=0), the truth selectors return
        no value -- modelled data is never treated as an observation."""
        db = _db()
        self._raw_insert(db, "2024-06-15T03:00:00+00:00", "Tokyo", 88.0, 0)
        self._raw_insert(db, "2024-06-15T04:00:00+00:00", "Tokyo", 91.0, 0)
        assert db.get_daily_obs_high("RJTT", "2024-06-15") is None
        assert db.get_obs_highs_range("RJTT", "2024-06-14") == {}

    def test_null_is_official_treated_as_official(self):
        """Legacy rows predating the is_official column default (NULL) must be
        treated as official, not dropped."""
        db = _db()
        self._raw_insert(db, "2024-06-15T03:00:00+00:00", "RJTT", 87.0, None)
        assert db.get_daily_obs_high("RJTT", "2024-06-15") == 87.0
        assert db.get_obs_highs_range("RJTT", "2024-06-14") == {"2024-06-15": 87.0}


class TestFallbackRawJsonSecondSignal:
    """Issue #741: the raw_json LIKE '%source_fallback%' condition is a second,
    independent exclusion signal alongside is_official=0 -- fallback writers
    (e.g. the former amos Open-Meteo fallback) set both markers, but this
    proves the raw_json condition alone is sufficient even if is_official
    were mistakenly left at its default (1) on a fallback row."""

    def _raw_insert_with_raw_json(self, db, ts, station, temp_f, is_official, raw_json):
        with db._lock:
            db._conn.execute(
                "INSERT INTO observations (ts, station, temp_f, temp_native, unit, "
                "source, is_official, raw_json) VALUES (?,?,?,?,?,?,?,?)",
                (ts, station, temp_f, temp_f, "C", "test", is_official, raw_json),
            )
            db._conn.commit()

    def test_fallback_row_above_metar_max_excluded_by_raw_json_alone(self):
        """A row tagged is_official=1 (mistakenly, as if the flag were never
        set) but with raw_json containing 'source_fallback' and a HIGHER temp
        than the real METAR row must still be excluded -- the truth query
        returns the METAR value, not the fallback."""
        db = _db()
        # Real METAR under the ICAO key.
        db.insert_observation(
            ts="2024-06-15T03:00:00+00:00", station="RJTT",
            temp_f=85.0, temp_native=29.4, unit="C", source="metar",
        )
        # Higher fallback-tagged row under the city key, is_official
        # incorrectly left at 1 -- only the raw_json marker identifies it.
        self._raw_insert_with_raw_json(
            db, "2024-06-15T04:00:00+00:00", "Tokyo", 99.0, 1,
            json.dumps({"source_fallback": "open-meteo", "station": "Tokyo"}),
        )

        assert db.get_daily_obs_high("RJTT", "2024-06-15") == 85.0
        assert db.get_obs_highs_range("RJTT", "2024-06-14") == {"2024-06-15": 85.0}

    def test_row_with_unrelated_raw_json_not_excluded(self):
        """A row with raw_json set but NOT containing 'source_fallback' must
        not be dropped -- the filter is substring-specific, not
        raw_json-presence-based."""
        db = _db()
        self._raw_insert_with_raw_json(
            db, "2024-06-15T03:00:00+00:00", "RJTT", 90.0, 1,
            json.dumps({"stnId": "112", "ta": "32.2"}),
        )
        assert db.get_daily_obs_high("RJTT", "2024-06-15") == 90.0
        assert db.get_obs_highs_range("RJTT", "2024-06-14") == {"2024-06-15": 90.0}

    def test_null_raw_json_not_excluded(self):
        """Rows with no raw_json at all (the common case) must not be dropped
        by the new condition."""
        db = _db()
        db.insert_observation(
            ts="2024-06-15T03:00:00+00:00", station="RJTT",
            temp_f=85.0, temp_native=29.4, unit="C", source="metar",
        )
        assert db.get_daily_obs_high("RJTT", "2024-06-15") == 85.0

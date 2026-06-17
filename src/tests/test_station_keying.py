"""Tests for issue #308: unified station keying and dense-feed daily-high detection.

Verifies:
1. ``get_canonical_station_feeds`` maps ICAO codes to the correct DB key lists.
2. ``get_observations_multi_station`` queries rows across multiple station keys.
3. ``compute_daily_high_from_db_observations`` correctly finds the peak from DB rows.
4. Peak detection in ``_build_weather`` picks up an intra-30-min MSS spike that
   METAR (30-min cadence) would have missed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytz

from src.config import get_canonical_station_feeds
from src.data.db import Database
from src.data.metar import compute_daily_high_from_db_observations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db() -> Database:
    """Return an in-memory Database."""
    return Database(":memory:")


def _utc(hour: int, minute: int = 0, day_offset: int = 0) -> str:
    """Build a UTC ISO timestamp string for today at the given hour/minute."""
    now_utc = datetime.now(timezone.utc).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    ) + timedelta(days=day_offset)
    return now_utc.isoformat()


def _sgt(hour: int, minute: int = 0) -> str:
    """Build a Singapore-time (UTC+8) timestamp for today at the given SGT hour."""
    sgt_offset = timezone(timedelta(hours=8))
    now_sgt = datetime.now(sgt_offset).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return now_sgt.isoformat()


# ---------------------------------------------------------------------------
# 1. get_canonical_station_feeds — key resolution
# ---------------------------------------------------------------------------

class TestGetCanonicalStationFeeds:
    """get_canonical_station_feeds maps ICAO → list of DB station keys."""

    def test_singapore_returns_city_and_icao(self):
        """WSSS (Singapore) has an MSS high-cadence feed → [city, ICAO]."""
        feeds = get_canonical_station_feeds("WSSS")
        assert feeds == ["Singapore", "WSSS"], f"Unexpected: {feeds}"

    def test_seoul_returns_city_and_icao(self):
        """RKSI (Seoul) has an AMOS feed → [city, ICAO]."""
        feeds = get_canonical_station_feeds("RKSI")
        assert feeds == ["Seoul", "RKSI"], f"Unexpected: {feeds}"

    def test_busan_returns_city_and_icao(self):
        """RKPK (Busan) has an AMOS feed → [city, ICAO]."""
        feeds = get_canonical_station_feeds("RKPK")
        assert feeds == ["Busan", "RKPK"], f"Unexpected: {feeds}"

    def test_tokyo_returns_city_and_icao(self):
        """RJTT (Tokyo) has a JMA AMeDAS feed → [city, ICAO]."""
        feeds = get_canonical_station_feeds("RJTT")
        assert feeds == ["Tokyo", "RJTT"], f"Unexpected: {feeds}"

    def test_metar_only_station_returns_icao_only(self):
        """KORD (Chicago) has no city-keyed high-cadence feed → [ICAO]."""
        feeds = get_canonical_station_feeds("KORD")
        assert feeds == ["KORD"]

    def test_unknown_station_returns_icao_only(self):
        """An ICAO not in STATIONS should just return itself."""
        feeds = get_canonical_station_feeds("ZZZZ")
        assert feeds == ["ZZZZ"]

    def test_city_key_comes_first(self):
        """City key (high-cadence) must be first in the list for priority ordering."""
        for icao in ("WSSS", "RKSI", "RKPK", "RJTT"):
            feeds = get_canonical_station_feeds(icao)
            assert len(feeds) == 2
            # First entry is NOT the ICAO (it is the city name)
            assert feeds[0] != icao
            # Second entry is the ICAO
            assert feeds[1] == icao


# ---------------------------------------------------------------------------
# 2. get_observations_multi_station — DB union query
# ---------------------------------------------------------------------------

class TestGetObservationsMultiStation:
    """Database.get_observations_multi_station unions rows from multiple keys."""

    def test_returns_rows_from_all_keys(self):
        """Rows keyed under 'Singapore' and 'WSSS' are both returned."""
        db = _db()
        ts1 = _utc(10, 0)
        ts2 = _utc(10, 15)
        db.insert_observation(
            ts=ts1, station="Singapore", temp_f=86.0, temp_native=30.0,
            unit="C", source="mss", cadence_min=1, is_official=1,
        )
        db.insert_observation(
            ts=ts2, station="WSSS", temp_f=84.0, temp_native=28.9,
            unit="C", source="metar", cadence_min=30, is_official=1,
        )
        rows = db.get_observations_multi_station(
            ["Singapore", "WSSS"], since=_utc(0, 0)
        )
        stations_found = {r["station"] for r in rows}
        assert stations_found == {"Singapore", "WSSS"}
        assert len(rows) == 2

    def test_since_filter_works(self):
        """Only rows at or after 'since' are returned."""
        db = _db()
        # Old row (yesterday)
        db.insert_observation(
            ts=_utc(10, 0, day_offset=-1), station="Singapore",
            temp_f=90.0, temp_native=32.2, unit="C", source="mss",
            cadence_min=1, is_official=1,
        )
        # Today's row
        ts_today = _utc(10, 0)
        db.insert_observation(
            ts=ts_today, station="Singapore",
            temp_f=85.0, temp_native=29.4, unit="C", source="mss",
            cadence_min=1, is_official=1,
        )
        rows = db.get_observations_multi_station(
            ["Singapore"], since=_utc(0, 0)
        )
        assert len(rows) == 1
        assert rows[0]["temp_f"] == pytest.approx(85.0)

    def test_returns_empty_when_no_matching_stations(self):
        """No rows match the given station keys → empty list."""
        db = _db()
        db.insert_observation(
            ts=_utc(10, 0), station="KORD", temp_f=75.0, temp_native=23.9,
            unit="C", source="metar", cadence_min=30, is_official=1,
        )
        rows = db.get_observations_multi_station(
            ["Singapore", "WSSS"], since=_utc(0, 0)
        )
        assert rows == []

    def test_returns_empty_for_empty_stations_list(self):
        """Empty station list → empty result (no crash)."""
        db = _db()
        rows = db.get_observations_multi_station([], since=_utc(0, 0))
        assert rows == []

    def test_ordered_by_ts_ascending(self):
        """Rows are returned oldest-first (ts ASC)."""
        db = _db()
        for minute in (30, 0, 15):
            db.insert_observation(
                ts=_utc(10, minute), station="Singapore",
                temp_f=80.0 + minute, temp_native=27.0, unit="C",
                source="mss", cadence_min=1, is_official=1,
            )
        rows = db.get_observations_multi_station(
            ["Singapore"], since=_utc(0, 0)
        )
        tss = [r["ts"] for r in rows]
        assert tss == sorted(tss), "Rows not in ascending timestamp order"


# ---------------------------------------------------------------------------
# 3. compute_daily_high_from_db_observations
# ---------------------------------------------------------------------------

class TestComputeDailyHighFromDbObservations:
    """compute_daily_high_from_db_observations picks the peak from DB rows."""

    _TZ = "Asia/Singapore"

    def _make_obs(self, hour: int, minute: int, temp_f: float) -> dict:
        """Build a minimal DB observation dict in SGT."""
        sgt_offset = timezone(timedelta(hours=8))
        ts = datetime.now(sgt_offset).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        return {"ts": ts.isoformat(), "temp_f": temp_f}

    def test_returns_peak_temp(self):
        """Returns the highest temp_f among today's observations."""
        obs = [
            self._make_obs(10, 0, 84.0),
            self._make_obs(11, 0, 90.0),  # peak
            self._make_obs(12, 0, 87.0),
        ]
        result = compute_daily_high_from_db_observations(obs, self._TZ, min_local_hour=6)
        assert result is not None
        high_f, _ = result
        assert high_f == pytest.approx(90.0)

    def test_min_local_hour_filter(self):
        """Observations before min_local_hour are excluded."""
        obs = [
            self._make_obs(4, 0, 99.0),   # too early
            self._make_obs(10, 0, 85.0),  # valid
        ]
        result = compute_daily_high_from_db_observations(obs, self._TZ, min_local_hour=6)
        assert result is not None
        high_f, _ = result
        assert high_f == pytest.approx(85.0), (
            "Overnight reading should be excluded; high should be the daytime reading"
        )

    def test_returns_none_when_no_valid_observations(self):
        """Returns None when all observations are outside today's window."""
        # All before min_local_hour
        obs = [self._make_obs(3, 0, 99.0)]
        result = compute_daily_high_from_db_observations(obs, self._TZ, min_local_hour=6)
        assert result is None

    def test_returns_none_for_empty_list(self):
        """Returns None for an empty observation list."""
        result = compute_daily_high_from_db_observations([], self._TZ)
        assert result is None

    def test_skips_rows_with_missing_fields(self):
        """Rows missing ts or temp_f are ignored gracefully."""
        obs = [
            {"ts": None, "temp_f": 99.0},
            {"ts": self._make_obs(10, 0, 0)["ts"], "temp_f": None},
            self._make_obs(11, 0, 85.0),  # valid
        ]
        result = compute_daily_high_from_db_observations(obs, self._TZ, min_local_hour=6)
        assert result is not None
        high_f, _ = result
        assert high_f == pytest.approx(85.0)


# ---------------------------------------------------------------------------
# 4. Peak detection — MSS intra-30-min spike beats METAR
# ---------------------------------------------------------------------------

class TestMssIntraMinutePeakDetection:
    """MSS 1-min feed catches peaks that 30-min METAR misses.

    Scenario (Singapore, SGT = UTC+8):
    - METAR reads 30.0°C (86.0°F) at 13:00 SGT and 30.1°C (86.18°F) at 13:30 SGT.
    - MSS records a 34.0°C (93.2°F) spike at 13:15 SGT (between the two METARs).
    - compute_daily_high from METAR alone: 86.18°F.
    - compute_daily_high_from_db_observations from union ["Singapore","WSSS"]: 93.2°F.
    """

    _TZ = "Asia/Singapore"
    _SGT = timezone(timedelta(hours=8))

    def _ts(self, hour: int, minute: int) -> str:
        now = datetime.now(self._SGT).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        return now.isoformat()

    def _metar_obs(self, hour: int, minute: int, temp_c: float) -> dict:
        """DB row as METAR would persist it (station='WSSS')."""
        return {
            "ts": self._ts(hour, minute),
            "station": "WSSS",
            "temp_f": temp_c * 9 / 5 + 32,
            "source": "metar",
        }

    def _mss_obs(self, hour: int, minute: int, temp_c: float) -> dict:
        """DB row as MSS would persist it (station='Singapore')."""
        return {
            "ts": self._ts(hour, minute),
            "station": "Singapore",
            "temp_f": temp_c * 9 / 5 + 32,
            "source": "mss",
        }

    def test_metar_alone_misses_peak(self):
        """METAR-only dataset does NOT see the 34°C spike."""
        metar_rows = [
            self._metar_obs(13, 0, 30.0),   # 86.0°F
            self._metar_obs(13, 30, 30.1),  # 86.18°F
        ]
        result = compute_daily_high_from_db_observations(
            metar_rows, self._TZ, min_local_hour=6
        )
        assert result is not None
        high_f, _ = result
        assert high_f == pytest.approx(86.18, rel=1e-4), (
            f"Expected METAR-only high ~86.18°F, got {high_f:.2f}°F"
        )

    def test_union_of_feeds_catches_peak(self):
        """Union of MSS + METAR rows catches the 34°C intra-30-min MSS spike."""
        union_rows = [
            self._metar_obs(13, 0, 30.0),   # 86.0°F  (WSSS)
            self._mss_obs(13, 15, 34.0),    # 93.2°F  (Singapore MSS) ← peak
            self._metar_obs(13, 30, 30.1),  # 86.18°F (WSSS)
        ]
        result = compute_daily_high_from_db_observations(
            union_rows, self._TZ, min_local_hour=6
        )
        assert result is not None
        high_f, _ = result
        assert high_f == pytest.approx(93.2, rel=1e-4), (
            f"Expected MSS-peak high 93.2°F, got {high_f:.2f}°F"
        )

    def test_peak_is_higher_than_metar_only(self):
        """Explicitly assert union high > METAR-only high."""
        metar_only = [
            self._metar_obs(13, 0, 30.0),
            self._metar_obs(13, 30, 30.1),
        ]
        union_rows = metar_only + [self._mss_obs(13, 15, 34.0)]

        metar_result = compute_daily_high_from_db_observations(
            metar_only, self._TZ, min_local_hour=6
        )
        union_result = compute_daily_high_from_db_observations(
            union_rows, self._TZ, min_local_hour=6
        )
        assert metar_result is not None
        assert union_result is not None
        metar_high, _ = metar_result
        union_high, _ = union_result
        assert union_high > metar_high, (
            f"Union high ({union_high:.2f}°F) should exceed METAR-only high ({metar_high:.2f}°F)"
        )

    def test_no_peak_upgrade_when_metar_is_highest(self):
        """When METAR has the highest reading, the union still returns it correctly."""
        rows = [
            self._metar_obs(14, 0, 36.0),   # 96.8°F (highest)
            self._mss_obs(13, 30, 34.0),    # 93.2°F
        ]
        result = compute_daily_high_from_db_observations(
            rows, self._TZ, min_local_hour=6
        )
        assert result is not None
        high_f, _ = result
        assert high_f == pytest.approx(96.8, rel=1e-4), (
            "METAR reading is highest; union should not lower the peak"
        )


# ---------------------------------------------------------------------------
# 5. get_observations_multi_station — boundary / edge cases
# ---------------------------------------------------------------------------

class TestMultiStationEdgeCases:

    def test_single_station_list_behaves_like_get_observations(self):
        """Single-element list should return same rows as the scalar helper."""
        db = _db()
        ts = _utc(10, 0)
        db.insert_observation(
            ts=ts, station="KORD", temp_f=75.0, temp_native=23.9,
            unit="C", source="metar", cadence_min=30, is_official=1,
        )
        multi = db.get_observations_multi_station(["KORD"], since=_utc(0, 0))
        single = db.get_observations("KORD", since=_utc(0, 0))
        assert len(multi) == len(single)
        assert multi[0]["temp_f"] == pytest.approx(single[0]["temp_f"])

    def test_duplicate_timestamps_across_sources_are_kept(self):
        """If MSS and METAR happen to share a timestamp, both rows are returned."""
        db = _db()
        ts = _utc(10, 0)
        db.insert_observation(
            ts=ts, station="Singapore", temp_f=86.0, temp_native=30.0,
            unit="C", source="mss", cadence_min=1, is_official=1,
        )
        db.insert_observation(
            ts=ts, station="WSSS", temp_f=84.0, temp_native=28.9,
            unit="C", source="metar", cadence_min=30, is_official=1,
        )
        rows = db.get_observations_multi_station(
            ["Singapore", "WSSS"], since=_utc(0, 0)
        )
        # Both rows are kept; peak logic will pick the higher one
        assert len(rows) == 2

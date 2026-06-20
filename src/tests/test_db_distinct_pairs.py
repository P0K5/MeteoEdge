"""Unit tests for Database.get_distinct_pairs() method."""
import pytest

from src.data.db import Database


def _db() -> Database:
    """Return a fresh in-memory Database instance."""
    return Database(":memory:")


class TestGetDistinctPairs:
    """Test Database.get_distinct_pairs() method."""

    def test_returns_distinct_pairs(self):
        """get_distinct_pairs should return distinct (station, source) tuples."""
        db = _db()
        db.upsert_intraday_correction(
            city="NYC",
            station="KORD",
            source="metar",
            date="2026-01-01",
            obs_time="2026-01-01T08:00:00+00:00",
            obs_temp_f=50.0,
            model_temp_f=48.0,
            delta_f=1.0,
            corrected_mu_f=82.0,
            decay_factor=0.9,
        )
        db.upsert_intraday_correction(
            city="NYC",
            station="KORD",
            source="metar",
            date="2026-01-02",
            obs_time="2026-01-02T08:00:00+00:00",
            obs_temp_f=52.0,
            model_temp_f=50.0,
            delta_f=2.0,
            corrected_mu_f=83.0,
            decay_factor=0.9,
        )
        db.upsert_intraday_correction(
            city="NYC",
            station="KLGA",
            source="taf",
            date="2026-01-01",
            obs_time="2026-01-01T09:00:00+00:00",
            obs_temp_f=49.0,
            model_temp_f=48.5,
            delta_f=0.5,
            corrected_mu_f=81.5,
            decay_factor=0.9,
        )
        result = db.get_distinct_pairs("NYC", "2026-01-01")
        assert len(result) == 2
        assert ("KORD", "metar") in result
        assert ("KLGA", "taf") in result

    def test_filters_by_city_and_date(self):
        """get_distinct_pairs should filter by city and date."""
        db = _db()
        db.upsert_intraday_correction(
            city="NYC",
            station="KORD",
            source="metar",
            date="2026-01-01",
            obs_time="2026-01-01T08:00:00+00:00",
            obs_temp_f=50.0,
            model_temp_f=48.0,
            delta_f=1.0,
            corrected_mu_f=82.0,
            decay_factor=0.9,
        )
        db.upsert_intraday_correction(
            city="LAX",
            station="KLAX",
            source="metar",
            date="2026-01-01",
            obs_time="2026-01-01T08:00:00+00:00",
            obs_temp_f=75.0,
            model_temp_f=73.0,
            delta_f=1.0,
            corrected_mu_f=88.0,
            decay_factor=0.9,
        )
        result = db.get_distinct_pairs("NYC", "2026-01-01")
        assert result == [("KORD", "metar")]

    def test_returns_empty_when_no_rows(self):
        """get_distinct_pairs should return empty list when no rows match."""
        db = _db()
        result = db.get_distinct_pairs("NYC", "2026-01-01")
        assert result == []

    def test_filters_by_since_date(self):
        """get_distinct_pairs should filter by since_date (inclusive)."""
        db = _db()
        db.upsert_intraday_correction(
            city="NYC",
            station="KORD",
            source="metar",
            date="2026-01-01",
            obs_time="2026-01-01T08:00:00+00:00",
            obs_temp_f=50.0,
            model_temp_f=48.0,
            delta_f=1.0,
            corrected_mu_f=82.0,
            decay_factor=0.9,
        )
        db.upsert_intraday_correction(
            city="NYC",
            station="KLGA",
            source="taf",
            date="2026-01-03",
            obs_time="2026-01-03T08:00:00+00:00",
            obs_temp_f=52.0,
            model_temp_f=50.0,
            delta_f=2.0,
            corrected_mu_f=83.0,
            decay_factor=0.9,
        )
        result_before = db.get_distinct_pairs("NYC", "2026-01-02")
        assert result_before == [("KLGA", "taf")]
        result_all = db.get_distinct_pairs("NYC", "2026-01-01")
        assert len(result_all) == 2
        assert ("KORD", "metar") in result_all
        assert ("KLGA", "taf") in result_all

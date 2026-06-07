"""Tests for freshness monitor (Issue #110)."""
from datetime import datetime, timedelta, timezone

import pytest

from src.data.db import Database
from src.data.freshness_monitor import FreshnessMonitor


class TestFreshnessMonitorCheck:
    """Test FreshnessMonitor.check() method."""

    def test_check_fresh_observation(self):
        """Fresh observation should return True."""
        db = Database(":memory:")
        monitor = FreshnessMonitor()

        # Insert a recent observation (now - 5 minutes)
        now = datetime.now(timezone.utc)
        recent_ts = (now - timedelta(minutes=5)).isoformat()
        db.insert_observation(
            ts=recent_ts,
            station="Tokyo",
            temp_f=72.0,
            temp_native=22.2,
            unit="C",
            source="jma_ameidas",
        )

        # Check with cadence_min=10 (stale threshold = 20 minutes)
        # Since obs is 5 minutes old, it's fresh
        result = monitor.check(db, "jma_ameidas", "Tokyo", cadence_min=10)
        assert result is True

    def test_check_stale_observation(self, caplog):
        """Stale observation should return False and log CRITICAL."""
        db = Database(":memory:")
        monitor = FreshnessMonitor()

        # Insert an old observation (now - 30 minutes)
        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(minutes=30)).isoformat()
        db.insert_observation(
            ts=old_ts,
            station="Seoul",
            temp_f=68.0,
            temp_native=20.0,
            unit="C",
            source="amos",
        )

        # Check with cadence_min=10 (stale threshold = 20 minutes)
        # Since obs is 30 minutes old, it's stale
        result = monitor.check(db, "amos", "Seoul", cadence_min=10)
        assert result is False

        # Verify CRITICAL log was generated
        assert any("Stale observation" in record.message for record in caplog.records)

    def test_check_no_data(self, caplog):
        """No data should return False and log CRITICAL."""
        db = Database(":memory:")
        monitor = FreshnessMonitor()

        # Check with no data
        result = monitor.check(db, "nonexistent", "station", cadence_min=10)
        assert result is False

        # Verify CRITICAL log was generated
        assert any("No observation data" in record.message for record in caplog.records)

    def test_check_at_stale_boundary(self):
        """Observation exactly at stale threshold should return False."""
        db = Database(":memory:")
        monitor = FreshnessMonitor()

        # Insert observation exactly 20 minutes old (at 2*cadence_min boundary)
        now = datetime.now(timezone.utc)
        boundary_ts = (now - timedelta(minutes=20)).isoformat()
        db.insert_observation(
            ts=boundary_ts,
            station="Busan",
            temp_f=65.0,
            temp_native=18.0,
            unit="C",
            source="amos",
        )

        # Check with cadence_min=10 (stale threshold = 20 minutes)
        # Since age >= stale_threshold, should be stale (False)
        result = monitor.check(db, "amos", "Busan", cadence_min=10)
        assert result is False


class TestFreshnessMonitorCheckAll:
    """Test FreshnessMonitor.check_all() method."""

    def test_check_all_mixed_freshness(self):
        """check_all should return dict with mixed results."""
        db = Database(":memory:")
        monitor = FreshnessMonitor()

        now = datetime.now(timezone.utc)

        # Fresh observation
        fresh_ts = (now - timedelta(minutes=5)).isoformat()
        db.insert_observation(
            ts=fresh_ts,
            station="Tokyo",
            temp_f=72.0,
            temp_native=22.2,
            unit="C",
            source="jma_ameidas",
        )

        # Stale observation
        stale_ts = (now - timedelta(minutes=30)).isoformat()
        db.insert_observation(
            ts=stale_ts,
            station="Seoul",
            temp_f=68.0,
            temp_native=20.0,
            unit="C",
            source="amos",
        )

        sources = [
            {"source": "jma_ameidas", "station": "Tokyo", "cadence_min": 10},
            {"source": "amos", "station": "Seoul", "cadence_min": 10},
            {"source": "mss", "station": "Singapore", "cadence_min": 20},
        ]

        result = monitor.check_all(db, sources)

        assert result["jma_ameidas/Tokyo"] is True  # fresh
        assert result["amos/Seoul"] is False  # stale
        assert result["mss/Singapore"] is False  # no data

    def test_check_all_empty_list(self):
        """check_all with empty sources list should return empty dict."""
        db = Database(":memory:")
        monitor = FreshnessMonitor()

        result = monitor.check_all(db, [])
        assert result == {}

    def test_check_all_result_keys_format(self):
        """Result keys should be formatted as 'source/station'."""
        db = Database(":memory:")
        monitor = FreshnessMonitor()

        now = datetime.now(timezone.utc)
        ts = now.isoformat()
        db.insert_observation(
            ts=ts,
            station="TestStation",
            temp_f=70.0,
            temp_native=21.0,
            unit="C",
            source="test_source",
        )

        sources = [
            {"source": "test_source", "station": "TestStation", "cadence_min": 10},
        ]

        result = monitor.check_all(db, sources)

        assert "test_source/TestStation" in result

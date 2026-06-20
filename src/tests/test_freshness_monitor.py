"""Tests for src/data/freshness_monitor.py.

Covers:
- 3-tier log levels (silent, WARNING, CRITICAL)
- Source-specific thresholds from config
- De-duplication: at most one log per (source, station) per 15 minutes
- Recovery INFO when data returns to fresh after being stale
- No data scenarios (CRITICAL log, no de-duplication)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.data.freshness_monitor import FreshnessMonitor, _last_log_time


class TestFreshnessMonitor:
    """Test suite for FreshnessMonitor.check()."""

    def setup_method(self):
        """Clear de-duplication state before each test."""
        _last_log_time.clear()

    def _make_obs(self, age_seconds: int) -> dict:
        """Create a mock observation with given age in seconds."""
        ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        return {"ts": ts.isoformat().replace("+00:00", "Z")}

    def _make_db(self, obs: dict | None) -> MagicMock:
        """Create a mock Database that returns the given observation."""
        db = MagicMock()
        db.get_latest_observation.return_value = obs
        return db

    # -----------------------------------------------------------------------
    # Fresh observations (within threshold) — silent
    # -----------------------------------------------------------------------

    def test_fresh_within_threshold_silent(self, caplog):
        """Observation within threshold → no log, returns True."""
        monitor = FreshnessMonitor()
        db = self._make_db(self._make_obs(age_seconds=60))  # 1 minute, threshold is 180s

        with caplog.at_level("DEBUG"):
            result = monitor.check(db, "metar", "KORD", cadence_min=5)

        assert result is True
        assert not any(
            "WARNING" in record.levelname or "CRITICAL" in record.levelname
            for record in caplog.records
        )

    def test_fresh_with_amos_source(self, caplog):
        """AMOS source (90s threshold) within limit → silent."""
        monitor = FreshnessMonitor()
        db = self._make_db(self._make_obs(age_seconds=60))  # 60s < 90s threshold

        result = monitor.check(db, "amos", "Singapore", cadence_min=10)

        assert result is True
        assert not any(
            record.levelname in ("WARNING", "CRITICAL")
            for record in caplog.records
        )

    # -----------------------------------------------------------------------
    # Stale observations (1x–3x threshold) → WARNING
    # -----------------------------------------------------------------------

    def test_stale_within_3x_threshold_warning(self, caplog):
        """Stale 1x–3x threshold → WARNING log."""
        monitor = FreshnessMonitor()
        # METAR threshold: 180 seconds (3 minutes)
        # Stale at 300 seconds = 1.67x threshold
        db = self._make_db(self._make_obs(age_seconds=300))

        result = monitor.check(db, "metar", "KORD", cadence_min=5)

        assert result is False
        assert any(
            record.levelname == "WARNING"
            and "metar/KORD" in record.message
            for record in caplog.records
        )

    def test_stale_with_mss_source_warning(self, caplog):
        """MSS source (15s threshold) at 30s stale (2x) → WARNING."""
        monitor = FreshnessMonitor()
        # MSS threshold: 15 seconds
        # Stale at 30 seconds = 2x threshold
        db = self._make_db(self._make_obs(age_seconds=30))

        result = monitor.check(db, "mss", "Station1", cadence_min=10)

        assert result is False
        assert any(
            record.levelname == "WARNING" and "mss/Station1" in record.message
            for record in caplog.records
        )

    # -----------------------------------------------------------------------
    # Very stale observations (>3x threshold) → CRITICAL
    # -----------------------------------------------------------------------

    def test_stale_beyond_3x_threshold_critical(self, caplog):
        """Stale >3x threshold → CRITICAL log."""
        monitor = FreshnessMonitor()
        # METAR threshold: 180 seconds (3 minutes)
        # Stale at 600 seconds = 3.33x threshold
        db = self._make_db(self._make_obs(age_seconds=600))

        result = monitor.check(db, "metar", "KMIA", cadence_min=5)

        assert result is False
        assert any(
            record.levelname == "CRITICAL"
            and "metar/KMIA" in record.message
            and "3x" in record.message
            for record in caplog.records
        )

    # -----------------------------------------------------------------------
    # Silent for >24h → CRITICAL
    # -----------------------------------------------------------------------

    def test_silent_beyond_24h_critical(self, caplog):
        """Observation stale for >24h → CRITICAL log."""
        monitor = FreshnessMonitor()
        # Stale for 25 hours = 90000 seconds
        db = self._make_db(self._make_obs(age_seconds=90000))

        result = monitor.check(db, "metar", "KATL", cadence_min=5)

        assert result is False
        assert any(
            record.levelname == "CRITICAL"
            and "metar/KATL" in record.message
            and "24h" in record.message
            for record in caplog.records
        )

    # -----------------------------------------------------------------------
    # De-duplication: at most one log per 15 minutes
    # -----------------------------------------------------------------------

    def test_dedup_same_staleness_suppressed(self, caplog):
        """Same staleness logged twice → second is suppressed by dedup."""
        monitor = FreshnessMonitor()
        db = self._make_db(self._make_obs(age_seconds=300))  # 5 minutes stale

        # First check — should log
        monitor.check(db, "metar", "KORD", cadence_min=5)
        logs_first = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(logs_first) == 1

        caplog.clear()

        # Immediately after, same observation — should NOT log (dedup window)
        # Manipulate _last_log_time to fake 5 minutes passing
        import src.data.freshness_monitor as fm
        key = "metar/KORD"
        old_last = fm._last_log_time[key]
        # Set it to 5 minutes ago (within the 15-min dedup window)
        fm._last_log_time[key] = old_last - timedelta(minutes=5)

        monitor.check(db, "metar", "KORD", cadence_min=5)
        logs_second = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(logs_second) == 0, "Dedup should suppress the second log"

    def test_dedup_independent_per_source_station(self, caplog):
        """De-duplication is per (source, station) → different pairs log independently."""
        monitor = FreshnessMonitor()
        db = self._make_db(self._make_obs(age_seconds=300))

        # First check for metar/KORD
        monitor.check(db, "metar", "KORD", cadence_min=5)
        assert any(
            record.levelname == "WARNING"
            and "metar/KORD" in record.message
            for record in caplog.records
        )

        caplog.clear()

        # Second check for metar/KMIA (different station) — should log independently
        monitor.check(db, "metar", "KMIA", cadence_min=5)
        assert any(
            record.levelname == "WARNING"
            and "metar/KMIA" in record.message
            for record in caplog.records
        )

    # -----------------------------------------------------------------------
    # Recovery: INFO log when returning to fresh after WARNING/CRITICAL
    # -----------------------------------------------------------------------

    def test_recovery_after_warning(self, caplog):
        """Stale then fresh → INFO recovery log emitted."""
        monitor = FreshnessMonitor()

        # First: stale (300 seconds = 5 minutes)
        db_stale = self._make_db(self._make_obs(age_seconds=300))
        monitor.check(db_stale, "metar", "KORD", cadence_min=5)

        caplog.clear()

        # Second: fresh (60 seconds = 1 minute)
        db_fresh = self._make_db(self._make_obs(age_seconds=60))
        with caplog.at_level("INFO"):
            result = monitor.check(db_fresh, "metar", "KORD", cadence_min=5)

        assert result is True
        assert any(
            record.levelname == "INFO"
            and "recovered" in record.message
            and "metar/KORD" in record.message
            for record in caplog.records
        )

    def test_recovery_resets_dedup_state(self, caplog):
        """After recovery, de-dup state resets → next stale logs immediately."""
        monitor = FreshnessMonitor()

        # First: stale
        db_stale = self._make_db(self._make_obs(age_seconds=300))
        monitor.check(db_stale, "metar", "KORD", cadence_min=5)

        caplog.clear()

        # Second: fresh (triggers recovery)
        db_fresh = self._make_db(self._make_obs(age_seconds=60))
        monitor.check(db_fresh, "metar", "KORD", cadence_min=5)

        caplog.clear()

        # Third: stale again — should log immediately (de-dup state was cleared by recovery)
        monitor.check(db_stale, "metar", "KORD", cadence_min=5)
        assert any(
            record.levelname == "WARNING"
            for record in caplog.records
        )

    # -----------------------------------------------------------------------
    # No data (None observation)
    # -----------------------------------------------------------------------

    def test_no_data_critical_log(self, caplog):
        """No observation → CRITICAL log, returns False."""
        monitor = FreshnessMonitor()
        db = self._make_db(None)

        result = monitor.check(db, "metar", "KORD", cadence_min=5)

        assert result is False
        assert any(
            record.levelname == "CRITICAL"
            and "No observation data" in record.message
            for record in caplog.records
        )

    def test_no_data_clears_dedup_state(self, caplog):
        """No data → de-dup state cleared for that key."""
        monitor = FreshnessMonitor()

        # First: stale (to populate de-dup state)
        db_stale = self._make_db(self._make_obs(age_seconds=300))
        monitor.check(db_stale, "metar", "KORD", cadence_min=5)

        caplog.clear()

        # Second: no data
        db_none = self._make_db(None)
        monitor.check(db_none, "metar", "KORD", cadence_min=5)

        # Verify the key is removed from _last_log_time
        assert "metar/KORD" not in _last_log_time

    # -----------------------------------------------------------------------
    # Default threshold for unlisted sources
    # -----------------------------------------------------------------------

    def test_unlisted_source_uses_default_threshold(self, caplog):
        """Source not in FRESHNESS_THRESHOLDS_MIN uses default (180s)."""
        monitor = FreshnessMonitor()
        # "unknown_source" not in config.FRESHNESS_THRESHOLDS_MIN
        # Default threshold is 180 seconds
        # Stale at 300 seconds = 1.67x default threshold → WARNING
        db = self._make_db(self._make_obs(age_seconds=300))

        result = monitor.check(db, "unknown_source", "Station", cadence_min=5)

        assert result is False
        assert any(
            record.levelname == "WARNING"
            and "unknown_source/Station" in record.message
            for record in caplog.records
        )

    # -----------------------------------------------------------------------
    # check_all still works
    # -----------------------------------------------------------------------

    def test_check_all_aggregates_results(self):
        """check_all returns dict of results for multiple sources."""
        monitor = FreshnessMonitor()
        db = MagicMock()

        # Configure db to return different ages for different sources
        obs_fresh = self._make_obs(age_seconds=60)   # fresh
        obs_stale = self._make_obs(age_seconds=300)  # stale

        def side_effect(source, station):
            if source == "metar" and station == "KORD":
                return obs_fresh
            elif source == "amos" and station == "Singapore":
                return obs_stale
            return None

        db.get_latest_observation.side_effect = side_effect

        sources = [
            {"source": "metar", "station": "KORD", "cadence_min": 5},
            {"source": "amos", "station": "Singapore", "cadence_min": 5},
        ]

        results = monitor.check_all(db, sources)

        assert results["metar/KORD"] is True  # fresh
        assert results["amos/Singapore"] is False  # stale

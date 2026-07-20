"""Tests for src/data/freshness_monitor.py (issue #745, #744).

Covers:
- cadence-derived staleness thresholds (not a global constant)
- one CRITICAL per outage (fresh->stale transition), DEBUG heartbeat after,
  INFO recovery on stale->fresh
- missing-data outages de-duplicated the same way
- check_all skips chronically-dead METAR stations (METAR_SKIP_STATIONS, e.g. ZSJN)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src import config
from src.data.freshness_monitor import (
    FreshnessMonitor,
    cadence_staleness_threshold_min,
    _outage_since,
)


def _obs(age_min: float) -> dict:
    ts = datetime.now(timezone.utc) - timedelta(minutes=age_min)
    return {"ts": ts.isoformat().replace("+00:00", "Z")}


def _db(obs):
    db = MagicMock()
    db.get_latest_observation.return_value = obs
    return db


class TestThresholdDerivation:
    def test_slow_metar_feed_uses_multiplicative(self):
        # 30-min cadence -> max(90, 45) = 90
        assert cadence_staleness_threshold_min(30) == 90.0

    def test_fast_feed_uses_additive_floor(self):
        # 1-min cadence -> max(3, 16) = 16
        assert cadence_staleness_threshold_min(1) == 16.0

    def test_mid_feed(self):
        # 10-min cadence -> max(30, 25) = 30
        assert cadence_staleness_threshold_min(10) == 30.0

    def test_zero_or_negative_cadence_floored(self):
        assert cadence_staleness_threshold_min(0) == 16.0


class TestOutageStateMachine:
    def setup_method(self):
        _outage_since.clear()

    def test_fresh_within_cadence_threshold_silent(self, caplog):
        """The exact #745 spam case: a 30-min METAR feed at 14min age is fresh."""
        monitor = FreshnessMonitor()
        with caplog.at_level("DEBUG"):
            result = monitor.check(_db(_obs(14.0)), "metar", "WSSS", cadence_min=30)
        assert result is True
        assert not any(r.levelname == "CRITICAL" for r in caplog.records)

    def test_stale_logs_one_critical_then_debug(self, caplog):
        monitor = FreshnessMonitor()
        db = _db(_obs(120.0))  # 120min > 90min threshold for cadence 30

        with caplog.at_level("DEBUG"):
            monitor.check(db, "metar", "WSSS", cadence_min=30)
        criticals = [r for r in caplog.records if r.levelname == "CRITICAL"]
        assert len(criticals) == 1 and "metar/WSSS" in criticals[0].message

        caplog.clear()
        with caplog.at_level("DEBUG"):
            monitor.check(db, "metar", "WSSS", cadence_min=30)  # outage continues
        assert not any(r.levelname == "CRITICAL" for r in caplog.records)
        assert any(r.levelname == "DEBUG" for r in caplog.records)

    def test_recovery_emits_info_and_clears_state(self, caplog):
        monitor = FreshnessMonitor()
        monitor.check(_db(_obs(120.0)), "metar", "WSSS", cadence_min=30)
        assert "metar/WSSS" in _outage_since

        caplog.clear()
        with caplog.at_level("INFO"):
            result = monitor.check(_db(_obs(5.0)), "metar", "WSSS", cadence_min=30)
        assert result is True
        assert any(r.levelname == "INFO" and "recovered" in r.message for r in caplog.records)
        assert "metar/WSSS" not in _outage_since

    def test_second_outage_after_recovery_logs_critical_again(self, caplog):
        monitor = FreshnessMonitor()
        monitor.check(_db(_obs(120.0)), "metar", "WSSS", cadence_min=30)   # outage 1
        monitor.check(_db(_obs(5.0)), "metar", "WSSS", cadence_min=30)     # recover
        caplog.clear()
        with caplog.at_level("DEBUG"):
            monitor.check(_db(_obs(120.0)), "metar", "WSSS", cadence_min=30)  # outage 2
        assert any(r.levelname == "CRITICAL" for r in caplog.records)

    def test_missing_data_outage_deduped(self, caplog):
        monitor = FreshnessMonitor()
        with caplog.at_level("DEBUG"):
            monitor.check(_db(None), "jma_ameidas", "Tokyo", cadence_min=10)
        assert sum(r.levelname == "CRITICAL" for r in caplog.records) == 1
        caplog.clear()
        with caplog.at_level("DEBUG"):
            monitor.check(_db(None), "jma_ameidas", "Tokyo", cadence_min=10)
        assert not any(r.levelname == "CRITICAL" for r in caplog.records)

    def test_outage_state_independent_per_key(self, caplog):
        monitor = FreshnessMonitor()
        with caplog.at_level("DEBUG"):
            monitor.check(_db(_obs(120.0)), "metar", "KORD", cadence_min=30)
            monitor.check(_db(_obs(120.0)), "metar", "KMIA", cadence_min=30)
        criticals = [r for r in caplog.records if r.levelname == "CRITICAL"]
        assert len(criticals) == 2


class TestCheckAllSkipsDeadStations:
    """Issue #744: chronically-dead METAR stations (ZSJN) are not monitored."""

    def setup_method(self):
        _outage_since.clear()

    def test_zsjn_excluded_from_check_all(self, caplog):
        assert "ZSJN" in config.METAR_SKIP_STATIONS  # delisted by #736
        monitor = FreshnessMonitor()
        db = _db(None)  # ZSJN has no data; would CRITICAL every tick if checked
        sources = [
            {"source": "metar", "station": "ZSJN", "cadence_min": 30},
            {"source": "metar", "station": "KORD", "cadence_min": 30},
        ]
        with caplog.at_level("DEBUG"):
            results = monitor.check_all(db, sources)
        assert "metar/ZSJN" not in results      # never checked
        assert "metar/KORD" in results
        assert not any("ZSJN" in r.message for r in caplog.records)  # no spam

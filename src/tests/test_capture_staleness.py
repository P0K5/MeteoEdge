"""Tests for the forecast-capture staleness watchdog (issue #717).

Covers:
- get_capture_health(): stale/fresh classification against the configurable
  threshold, and the "no rows at all" case.
- check_capture_staleness(): fires an ERROR log line when stale, stays silent
  when fresh, and logs a recovery INFO line after a stale period ends.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.data.db import Database
from src.monitoring import capture_staleness
from src.monitoring.capture_staleness import check_capture_staleness, get_capture_health


@pytest.fixture(autouse=True)
def reset_watchdog_state():
    """Reset the module-level alert dedup state before/after each test."""
    capture_staleness._last_alert_time = None
    yield
    capture_staleness._last_alert_time = None


def _db() -> Database:
    """In-memory Database for testing."""
    return Database(":memory:")


def _insert_capture_row(db: Database, logged_at: str) -> None:
    """Insert a single model_forecast_log row with a specific logged_at timestamp."""
    with db._lock:
        db._conn.execute(
            "INSERT INTO model_forecast_log"
            "(station,model,date,forecast_high_f,logged_at,lead_hours,issued_at,sigma_f)"
            " VALUES(?,?,?,?,?,?,?,?)",
            ("KORD", "nws", "2026-07-14", 72.0, logged_at, 24, logged_at, None),
        )
        db._conn.commit()


class TestGetCaptureHealth:
    """get_capture_health() -- pure read-only classification."""

    def test_fresh_timestamp_is_not_stale(self):
        db = _db()
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        fresh_ts = (now - timedelta(hours=1)).isoformat()
        _insert_capture_row(db, fresh_ts)

        health = get_capture_health(db, now=now)

        assert health["stale"] is False
        assert health["last_logged_at"] == fresh_ts
        assert health["age_hours"] == pytest.approx(1.0, abs=0.01)
        assert health["threshold_hours"] == pytest.approx(10.0)

    def test_stale_timestamp_older_than_threshold(self):
        db = _db()
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        # 12h age is unambiguously stale against the 10h default (issue #726:
        # kept clear of the designed ~8.3h overnight capture gap, which must NOT
        # be treated as stale).
        stale_ts = (now - timedelta(hours=12)).isoformat()
        _insert_capture_row(db, stale_ts)

        health = get_capture_health(db, now=now)

        assert health["stale"] is True
        assert health["age_hours"] == pytest.approx(12.0, abs=0.01)

    def test_no_rows_is_stale(self):
        """Empty model_forecast_log (or missing table) -- treat as stale, never silently OK."""
        db = _db()
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)

        health = get_capture_health(db, now=now)

        assert health["stale"] is True
        assert health["last_logged_at"] is None
        assert health["age_hours"] is None

    def test_none_db_is_stale(self):
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        health = get_capture_health(None, now=now)
        assert health["stale"] is True
        assert health["last_logged_at"] is None

    def test_custom_threshold_from_config(self):
        """A DB-configured threshold overrides the hardcoded default."""
        db = _db()
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        ts = (now - timedelta(hours=3)).isoformat()
        _insert_capture_row(db, ts)
        db.set_config("FORECAST_CAPTURE_STALENESS_THRESHOLD_HOURS", "2.0")

        health = get_capture_health(db, now=now)

        assert health["threshold_hours"] == pytest.approx(2.0)
        assert health["stale"] is True  # 3h age > 2h threshold

    def test_exactly_at_threshold_is_not_stale(self):
        """Boundary: age == threshold should not be flagged (strictly greater-than)."""
        db = _db()
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        ts = (now - timedelta(hours=10)).isoformat()
        _insert_capture_row(db, ts)

        health = get_capture_health(db, now=now)

        assert health["stale"] is False

    def test_designed_overnight_gap_is_not_stale(self):
        """Regression for issue #726: the ~8.3h gap between the last evening
        capture (~21:46 UTC) and the next morning run (~06:05 UTC) is by design
        and must NOT alert. The old 6h default false-fired here every night."""
        db = _db()
        now = datetime(2026, 7, 15, 6, 5, 0, tzinfo=timezone.utc)
        overnight_ts = (now - timedelta(hours=8, minutes=20)).isoformat()
        _insert_capture_row(db, overnight_ts)

        health = get_capture_health(db, now=now)

        assert health["stale"] is False


class TestCheckCaptureStaleness:
    """check_capture_staleness() -- logging side effects on top of get_capture_health()."""

    def test_fires_error_when_stale(self, caplog):
        db = MagicMock()
        db.get_config.return_value = None  # use hardcoded default (10h)
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        stale_ts = (now - timedelta(hours=144)).isoformat()  # 6 days, per the #717 incident
        db.get_last_forecast_capture_ts.return_value = stale_ts

        with caplog.at_level(logging.ERROR):
            health = check_capture_staleness(db, now=now)

        assert health["stale"] is True
        assert "[capture-staleness] ALERT" in caplog.text
        assert "forecast capture stale" in caplog.text

    def test_does_not_fire_when_fresh(self, caplog):
        db = MagicMock()
        db.get_config.return_value = None
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        fresh_ts = (now - timedelta(minutes=30)).isoformat()
        db.get_last_forecast_capture_ts.return_value = fresh_ts

        with caplog.at_level(logging.DEBUG):
            health = check_capture_staleness(db, now=now)

        assert health["stale"] is False
        assert "[capture-staleness] ALERT" not in caplog.text

    def test_fires_when_no_rows_at_all(self, caplog):
        db = MagicMock()
        db.get_config.return_value = None
        db.get_last_forecast_capture_ts.return_value = None
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)

        with caplog.at_level(logging.ERROR):
            health = check_capture_staleness(db, now=now)

        assert health["stale"] is True
        assert "[capture-staleness] ALERT" in caplog.text
        assert "no readable model_forecast_log rows" in caplog.text

    def test_dedup_suppresses_repeat_alert_within_window(self, caplog):
        """A second check within the dedup window (1h) should not re-log."""
        db = MagicMock()
        db.get_config.return_value = None
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        stale_ts = (now - timedelta(hours=12)).isoformat()  # clearly > 10h default
        db.get_last_forecast_capture_ts.return_value = stale_ts

        with caplog.at_level(logging.ERROR):
            check_capture_staleness(db, now=now)
        first_alert_count = caplog.text.count("[capture-staleness] ALERT")
        assert first_alert_count == 1

        # 10 minutes later, still stale -- within the 1h dedup window
        with caplog.at_level(logging.ERROR):
            check_capture_staleness(db, now=now + timedelta(minutes=10))
        assert caplog.text.count("[capture-staleness] ALERT") == 1  # unchanged

    def test_recovery_logs_info_after_stale_period(self, caplog):
        db = MagicMock()
        db.get_config.return_value = None
        now = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        stale_ts = (now - timedelta(hours=12)).isoformat()  # clearly > 10h default
        db.get_last_forecast_capture_ts.return_value = stale_ts

        with caplog.at_level(logging.ERROR):
            check_capture_staleness(db, now=now)

        # Capture resumes: fresh timestamp on the next check
        fresh_ts = (now - timedelta(minutes=5)).isoformat()
        db.get_last_forecast_capture_ts.return_value = fresh_ts
        caplog.clear()

        with caplog.at_level(logging.INFO):
            health = check_capture_staleness(db, now=now + timedelta(hours=1))

        assert health["stale"] is False
        assert "forecast capture recovered" in caplog.text

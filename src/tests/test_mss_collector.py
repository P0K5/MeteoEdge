"""Unit tests for src/data/collectors/mss.py.

All HTTP calls are mocked via patch on src.data.collectors.mss.fetch.
All DB writes use an in-memory Database instance.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.data.collectors.mss import MssCollector, _SGT
from src.data.db import Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db() -> Database:
    """Fresh in-memory Database with cadence_min/is_official columns applied.

    These columns are added by the #106 schema migration. We apply them here
    so tests pass before that PR is merged (idempotent — ALTER TABLE is a no-op
    if columns already exist after #106 merges).
    """
    db = Database(":memory:")
    for col, defn in [("cadence_min", "INTEGER"), ("is_official", "INTEGER DEFAULT 1")]:
        try:
            db._conn.execute(f"ALTER TABLE observations ADD COLUMN {col} {defn}")
            db._conn.commit()
        except Exception:
            pass  # column already exists
    return db


def _make_response(status_code: int = 200, json_data: dict | None = None) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    if json_data is not None:
        mock_resp.json.return_value = json_data
    else:
        mock_resp.json.side_effect = ValueError("no JSON")
    return mock_resp


def _mss_response(
    stations: dict[str, float] | None = None,
    timestamp: str = "2026-06-07T10:30:00+08:00",
) -> dict:
    """Build a mock MSS API response.

    Args:
        stations: mapping of station_id → temperature value. Defaults to
                  both S24 (29.4) and S108 (28.9).
        timestamp: ISO timestamp string (SGT with offset).
    """
    if stations is None:
        stations = {"S24": 29.4, "S108": 28.9}
    readings = [{"station_id": sid, "value": val} for sid, val in stations.items()]
    return {"items": [{"timestamp": timestamp, "readings": readings}]}


# ---------------------------------------------------------------------------
# Basic insertion
# ---------------------------------------------------------------------------

class TestMssPoll:
    def test_inserts_observation_on_success(self):
        """A successful MSS fetch inserts one row for Singapore."""
        db = _db()
        collector = MssCollector(db)

        with patch("src.data.collectors.mss.fetch", return_value=_make_response(200, _mss_response())):
            result = collector.poll()

        assert result is True
        rows = db.get_observations("Singapore", since="2000-01-01")
        assert len(rows) == 1
        r = rows[0]
        assert r["station"] == "Singapore"
        assert r["source"] == "mss"
        assert r["unit"] == "C"
        assert r["cadence_min"] == 1
        assert r["is_official"] == 1
        assert r["temp_native"] == pytest.approx(29.4)
        # S24 value: 29.4°C → 84.92°F
        assert r["temp_f"] == pytest.approx(84.92, rel=1e-4)

    def test_raw_json_stored(self):
        """raw_json field contains the station and value information."""
        db = _db()
        collector = MssCollector(db)

        with patch("src.data.collectors.mss.fetch", return_value=_make_response(200, _mss_response())):
            collector.poll()

        rows = db.get_observations("Singapore", since="2000-01-01")
        raw = json.loads(rows[0]["raw_json"])
        assert raw["station_id"] == "S24"
        assert raw["value"] == pytest.approx(29.4)

    def test_cadence_min_is_one_by_default(self):
        """cadence_min defaults to 1."""
        db = _db()
        collector = MssCollector(db)
        assert collector._cadence_min == 1

    def test_cadence_env_var(self, monkeypatch):
        """MSS_CADENCE_MINUTES env var is respected."""
        monkeypatch.setenv("MSS_CADENCE_MINUTES", "2")
        db = _db()
        collector = MssCollector(db)
        assert collector._cadence_min == 2

        with patch("src.data.collectors.mss.fetch", return_value=_make_response(200, _mss_response())):
            collector.poll()

        rows = db.get_observations("Singapore", since="2000-01-01")
        assert rows[0]["cadence_min"] == 2


# ---------------------------------------------------------------------------
# SGT → UTC conversion
# ---------------------------------------------------------------------------

class TestTimezoneConversion:
    def test_sgt_to_utc(self):
        """SGT 10:30 → UTC 02:30 is stored correctly."""
        db = _db()
        collector = MssCollector(db)

        # SGT 10:30 = UTC 02:30 (UTC+8)
        resp = _mss_response(timestamp="2026-06-07T10:30:00+08:00")
        with patch("src.data.collectors.mss.fetch", return_value=_make_response(200, resp)):
            collector.poll()

        rows = db.get_observations("Singapore", since="2000-01-01")
        assert len(rows) == 1
        stored_ts = rows[0]["ts"]
        parsed = datetime.fromisoformat(stored_ts)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        utc = parsed.astimezone(timezone.utc)
        assert utc.hour == 2
        assert utc.minute == 30

    def test_midnight_sgt_is_previous_day_utc(self):
        """SGT 00:00 (midnight) = UTC -8h = previous day 16:00 UTC."""
        db = _db()
        collector = MssCollector(db)

        resp = _mss_response(timestamp="2026-06-08T00:00:00+08:00")
        with patch("src.data.collectors.mss.fetch", return_value=_make_response(200, resp)):
            collector.poll()

        rows = db.get_observations("Singapore", since="2000-01-01")
        parsed = datetime.fromisoformat(rows[0]["ts"])
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        utc = parsed.astimezone(timezone.utc)
        assert utc.hour == 16
        assert utc.day == 7  # previous day


# ---------------------------------------------------------------------------
# Station priority / fallback
# ---------------------------------------------------------------------------

class TestStationPriority:
    def test_prefers_s24_when_both_available(self):
        """S24 is used when both S24 and S108 are in the response."""
        db = _db()
        collector = MssCollector(db)

        resp = _mss_response(stations={"S24": 30.0, "S108": 28.0})
        with patch("src.data.collectors.mss.fetch", return_value=_make_response(200, resp)):
            collector.poll()

        rows = db.get_observations("Singapore", since="2000-01-01")
        assert rows[0]["temp_native"] == pytest.approx(30.0)  # S24 value

    def test_falls_back_to_s108_when_s24_missing(self, caplog):
        """When S24 is absent, S108 is used and a warning is logged."""
        db = _db()
        collector = MssCollector(db)

        resp = _mss_response(stations={"S108": 27.5})  # no S24
        with caplog.at_level(logging.WARNING, logger="src.data.collectors.mss"):
            with patch("src.data.collectors.mss.fetch", return_value=_make_response(200, resp)):
                result = collector.poll()

        assert result is True
        rows = db.get_observations("Singapore", since="2000-01-01")
        assert rows[0]["temp_native"] == pytest.approx(27.5)
        # Warning about S24 not being found
        assert any("S24" in r.message for r in caplog.records)

    def test_no_row_when_both_stations_missing(self):
        """If neither S24 nor S108 is in the response, no row is inserted."""
        db = _db()
        collector = MssCollector(db)

        resp = _mss_response(stations={"S999": 25.0})  # unknown station
        with patch("src.data.collectors.mss.fetch", return_value=_make_response(200, resp)):
            result = collector.poll()

        assert result is False
        rows = db.get_observations("Singapore", since="2000-01-01")
        assert len(rows) == 0


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_network_error_does_not_crash(self):
        """fetch() raising an exception is caught gracefully."""
        db = _db()
        collector = MssCollector(db)

        with patch("src.data.collectors.mss.fetch", side_effect=Exception("network down")):
            result = collector.poll()

        assert result is False

    def test_empty_items_returns_false(self):
        """Empty 'items' list in response is handled gracefully."""
        db = _db()
        collector = MssCollector(db)

        resp = {"items": []}
        with patch("src.data.collectors.mss.fetch", return_value=_make_response(200, resp)):
            result = collector.poll()

        assert result is False

    def test_bad_json_response_does_not_crash(self):
        """Non-JSON response from fetch() is handled gracefully."""
        db = _db()
        collector = MssCollector(db)

        mock_resp = MagicMock()
        mock_resp.json.side_effect = ValueError("not JSON")
        with patch("src.data.collectors.mss.fetch", return_value=mock_resp):
            result = collector.poll()

        assert result is False


# ---------------------------------------------------------------------------
# Staleness check
# ---------------------------------------------------------------------------

class TestStaleness:
    def test_critical_logged_when_stale(self, caplog):
        """CRITICAL logged when no data for > 2× cadence_min minutes."""
        db = _db()
        collector = MssCollector(db)
        collector._cadence_min = 1  # default
        # Simulate last obs 5 minutes ago (> 2 * 1 = 2 min threshold)
        collector._last_obs_ts = datetime.now(timezone.utc) - timedelta(minutes=5)

        with caplog.at_level(logging.CRITICAL, logger="src.data.collectors.mss"):
            collector._check_staleness()

        assert any(r.levelname == "CRITICAL" for r in caplog.records)
        assert any("CRITICAL" in r.message for r in caplog.records)

    def test_no_critical_when_fresh(self, caplog):
        """No CRITICAL when data arrived within 2× cadence_min."""
        db = _db()
        collector = MssCollector(db)
        collector._cadence_min = 1
        collector._last_obs_ts = datetime.now(timezone.utc) - timedelta(seconds=30)

        with caplog.at_level(logging.CRITICAL, logger="src.data.collectors.mss"):
            collector._check_staleness()

        assert not any(r.levelname == "CRITICAL" for r in caplog.records)

    def test_no_critical_on_first_poll(self, caplog):
        """No CRITICAL on first poll (last_obs_ts is None)."""
        db = _db()
        collector = MssCollector(db)
        assert collector._last_obs_ts is None

        with caplog.at_level(logging.CRITICAL, logger="src.data.collectors.mss"):
            collector._check_staleness()

        assert not any(r.levelname == "CRITICAL" for r in caplog.records)

    def test_poll_updates_last_obs_ts(self):
        """After a successful poll, _last_obs_ts is updated."""
        db = _db()
        collector = MssCollector(db)
        assert collector._last_obs_ts is None

        with patch("src.data.collectors.mss.fetch", return_value=_make_response(200, _mss_response())):
            collector.poll()

        assert collector._last_obs_ts is not None
        assert isinstance(collector._last_obs_ts, datetime)
        assert collector._last_obs_ts.tzinfo is not None

    def test_staleness_not_triggered_right_after_poll(self, caplog):
        """Staleness check is not triggered immediately after a successful poll."""
        db = _db()
        collector = MssCollector(db)
        collector._cadence_min = 1

        # Use a current timestamp so the stored obs is recent
        now_sgt = datetime.now(timezone(timedelta(hours=8)))
        ts_str = now_sgt.isoformat()
        resp = _mss_response(timestamp=ts_str)
        with patch("src.data.collectors.mss.fetch", return_value=_make_response(200, resp)):
            collector.poll()

        # Force staleness check; since we just polled, it should not trigger
        with caplog.at_level(logging.CRITICAL, logger="src.data.collectors.mss"):
            collector._check_staleness()

        assert not any(r.levelname == "CRITICAL" for r in caplog.records)

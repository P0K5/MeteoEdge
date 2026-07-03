"""Unit tests for src/data/collectors/jma_ameidas.py.

All HTTP calls are mocked via patch on src.data.collectors.jma_ameidas.fetch.
All DB writes use an in-memory Database instance.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.data.collectors.jma_ameidas import JmaAmedasCollector, _JST
from src.data.db import Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db() -> Database:
    """Fresh in-memory Database with cadence_min/is_official columns applied."""
    db = Database(":memory:")
    for col, defn in [("cadence_min", "INTEGER"), ("is_official", "INTEGER DEFAULT 1")]:
        try:
            db._conn.execute(f"ALTER TABLE observations ADD COLUMN {col} {defn}")
            db._conn.commit()
        except Exception:
            pass
    return db


def _make_response(status_code: int = 200, json_data: dict | None = None) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    if json_data is not None:
        mock_resp.json.return_value = json_data
    else:
        mock_resp.json.side_effect = ValueError("no JSON")
    return mock_resp


def _jma_response(temp_c: float, time_key: str = "090000") -> dict:
    """Build a minimal JMA AMeDAS JSON response for one time slot."""
    return {
        time_key: {
            "temp": [temp_c, 0],   # [value, quality_flag=0 (good)]
            "wind": [3.2, 0],
            "windDirection": [5, 0],
        }
    }


def _open_meteo_response(temp_c: float) -> dict:
    now_utc = datetime.now(timezone.utc)
    past_hour = now_utc.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    return {
        "hourly": {
            "time": [past_hour.isoformat()],
            "temperature_2m": [temp_c],
        }
    }


# ---------------------------------------------------------------------------
# Happy path — JMA primary source
# ---------------------------------------------------------------------------

class TestJmaHappyPath:
    def test_inserts_row_on_success(self):
        """Valid JMA response inserts one observation row for Tokyo."""
        db = _db()
        collector = JmaAmedasCollector(db)

        resp = _make_response(200, _jma_response(22.4))
        with patch("src.data.collectors.jma_ameidas.fetch", return_value=resp):
            result = collector.poll()

        assert result is True
        rows = db.get_observations("Tokyo", since="2000-01-01")
        assert len(rows) == 1
        r = rows[0]
        assert r["station"] == "Tokyo"
        assert r["source"] == "jma_ameidas"
        assert r["unit"] == "C"
        assert r["temp_native"] == pytest.approx(22.4)
        assert r["cadence_min"] == 10
        assert r["is_official"] == 1

    def test_temp_f_conversion(self):
        """Temperature is converted correctly: 0°C = 32°F."""
        db = _db()
        collector = JmaAmedasCollector(db)

        resp = _make_response(200, _jma_response(0.0))
        with patch("src.data.collectors.jma_ameidas.fetch", return_value=resp):
            collector.poll()

        rows = db.get_observations("Tokyo", since="2000-01-01")
        assert rows[0]["temp_f"] == pytest.approx(32.0, rel=1e-4)

    def test_latest_time_slot_used_when_multiple(self):
        """When multiple time slots exist, the lexicographically last is used."""
        db = _db()
        collector = JmaAmedasCollector(db)

        data = {
            "090000": {"temp": [20.0, 0], "wind": [1.0, 0]},
            "091000": {"temp": [21.0, 0], "wind": [1.0, 0]},
            "092000": {"temp": [22.5, 0], "wind": [1.0, 0]},
        }
        resp = _make_response(200, data)
        with patch("src.data.collectors.jma_ameidas.fetch", return_value=resp):
            collector.poll()

        rows = db.get_observations("Tokyo", since="2000-01-01")
        assert rows[0]["temp_native"] == pytest.approx(22.5)

    def test_bad_quality_slot_skipped(self):
        """Slots with quality_flag != 0 are skipped."""
        db = _db()
        collector = JmaAmedasCollector(db)

        data = {
            "090000": {"temp": [99.9, 8], "wind": [1.0, 0]},  # quality=8, bad
            "091000": {"temp": [21.0, 0], "wind": [1.0, 0]},  # quality=0, good
        }
        resp = _make_response(200, data)
        with patch("src.data.collectors.jma_ameidas.fetch", return_value=resp):
            collector.poll()

        rows = db.get_observations("Tokyo", since="2000-01-01")
        assert len(rows) == 1
        assert rows[0]["temp_native"] == pytest.approx(21.0)

    def test_raw_json_stored(self):
        """raw_json field contains the time_key and slot data."""
        db = _db()
        collector = JmaAmedasCollector(db)

        resp = _make_response(200, _jma_response(23.0, time_key="091000"))
        with patch("src.data.collectors.jma_ameidas.fetch", return_value=resp):
            collector.poll()

        rows = db.get_observations("Tokyo", since="2000-01-01")
        raw = json.loads(rows[0]["raw_json"])
        assert raw["time_key"] == "091000"


# ---------------------------------------------------------------------------
# JST → UTC conversion
# ---------------------------------------------------------------------------

class TestJstToUtcConversion:
    def test_jst_9am_stored_as_utc_midnight(self):
        """JST 09:00 converts to UTC 00:00."""
        db = _db()
        collector = JmaAmedasCollector(db)

        # The time_key "090000" = 09:00 JST. Our _fetch_jma replaces now_jst's
        # hour/minute with those from the time_key. We patch datetime.now indirectly
        # by providing a fixed now_jst through the response: the collector builds
        # obs_jst = now_jst.replace(hour=9, minute=0) for key "090000".
        # We can verify the stored ts is 9 hours behind the JST key.
        resp = _make_response(200, _jma_response(25.0, time_key="090000"))
        with patch("src.data.collectors.jma_ameidas.fetch", return_value=resp):
            collector.poll()

        rows = db.get_observations("Tokyo", since="2000-01-01")
        stored_ts = rows[0]["ts"]
        parsed = datetime.fromisoformat(stored_ts)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        utc = parsed.astimezone(timezone.utc)
        # The stored hour should be 9 - 9 = 0 (UTC)
        assert utc.hour == 0
        assert utc.minute == 0


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_404_falls_back_to_open_meteo(self):
        """HTTP 404 triggers Open-Meteo fallback, not a crash."""
        db = _db()
        collector = JmaAmedasCollector(db)

        jma_404 = _make_response(404)
        om_resp = _make_response(200, _open_meteo_response(20.0))

        # 404 on current slot triggers previous-slot then previous-hour retry before Open-Meteo
        with patch("src.data.collectors.jma_ameidas.fetch", side_effect=[jma_404, jma_404, jma_404, om_resp]):
            result = collector.poll()

        assert result is True
        rows = db.get_observations("Tokyo", since="2000-01-01")
        assert len(rows) == 1
        assert rows[0]["source"] == "jma_ameidas"

    def test_network_error_falls_back_to_open_meteo(self):
        """Network exception triggers Open-Meteo fallback."""
        db = _db()
        collector = JmaAmedasCollector(db)

        om_resp = _make_response(200, _open_meteo_response(19.0))

        with patch(
            "src.data.collectors.jma_ameidas.fetch",
            side_effect=[Exception("connection reset"), Exception("connection reset"), Exception("connection reset"), om_resp],
        ):
            result = collector.poll()

        assert result is True
        rows = db.get_observations("Tokyo", since="2000-01-01")
        assert len(rows) == 1

    def test_both_sources_fail_returns_false(self):
        """When both JMA and Open-Meteo fail, poll() returns False without crashing."""
        db = _db()
        collector = JmaAmedasCollector(db)

        with patch(
            "src.data.collectors.jma_ameidas.fetch",
            side_effect=[Exception("JMA down"), Exception("JMA down"), Exception("JMA down"), Exception("OM down")],
        ):
            result = collector.poll()

        assert result is False
        rows = db.get_observations("Tokyo", since="2000-01-01")
        assert len(rows) == 0

    def test_empty_data_falls_back_to_open_meteo(self):
        """Empty JMA response triggers fallback."""
        db = _db()
        collector = JmaAmedasCollector(db)

        jma_empty = _make_response(200, {})
        om_resp = _make_response(200, _open_meteo_response(18.0))

        with patch("src.data.collectors.jma_ameidas.fetch", side_effect=[jma_empty, jma_empty, jma_empty, om_resp]):
            result = collector.poll()

        assert result is True


# ---------------------------------------------------------------------------
# Staleness check
# ---------------------------------------------------------------------------

class TestStaleness:
    def test_critical_logged_when_stale(self, caplog):
        """CRITICAL logged when last observation is older than 2× cadence_min."""
        db = _db()
        collector = JmaAmedasCollector(db)
        collector._cadence_min = 10
        collector._last_obs_ts = datetime.now(timezone.utc) - timedelta(minutes=25)

        with caplog.at_level(logging.CRITICAL, logger="src.data.collectors.jma_ameidas"):
            collector._check_staleness()

        assert any(r.levelname == "CRITICAL" for r in caplog.records)
        assert any("CRITICAL" in r.message for r in caplog.records)

    def test_no_critical_when_fresh(self, caplog):
        """No CRITICAL logged when data is within 2× cadence_min."""
        db = _db()
        collector = JmaAmedasCollector(db)
        collector._cadence_min = 10
        collector._last_obs_ts = datetime.now(timezone.utc) - timedelta(minutes=5)

        with caplog.at_level(logging.CRITICAL, logger="src.data.collectors.jma_ameidas"):
            collector._check_staleness()

        assert not any(r.levelname == "CRITICAL" for r in caplog.records)

    def test_no_critical_on_first_poll(self, caplog):
        """No CRITICAL on the very first poll (last_obs_ts is None)."""
        db = _db()
        collector = JmaAmedasCollector(db)
        assert collector._last_obs_ts is None

        with caplog.at_level(logging.CRITICAL, logger="src.data.collectors.jma_ameidas"):
            collector._check_staleness()

        assert not any(r.levelname == "CRITICAL" for r in caplog.records)


# ---------------------------------------------------------------------------
# Cadence env var
# ---------------------------------------------------------------------------

class TestCadenceEnvVar:
    def test_cadence_from_env(self, monkeypatch):
        """JMA_CADENCE_MINUTES env var is applied."""
        monkeypatch.setenv("JMA_CADENCE_MINUTES", "5")
        db = _db()
        collector = JmaAmedasCollector(db)
        assert collector._cadence_min == 5

        resp = _make_response(200, _jma_response(20.0))
        with patch("src.data.collectors.jma_ameidas.fetch", return_value=resp):
            collector.poll()

        rows = db.get_observations("Tokyo", since="2000-01-01")
        assert rows[0]["cadence_min"] == 5


# ---------------------------------------------------------------------------
# Publication-lag clamp — no future-timestamp requests (issue #560)
# ---------------------------------------------------------------------------

class TestPublicationLagAndFutureTimestamps:
    """The collector must never request a JMA slot that hasn't been published
    yet. These tests freeze the clock at awkward boundaries (just before/after
    a 10-min mark, exactly on the mark, and both directions of JST midnight)
    and call the REAL `_fetch_jma()` / `poll()` path, asserting on the actual
    URL(s) requested via the mocked `fetch`. The fallback chain (current →
    previous 10-min slot → previous hour) must remain intact — only the
    starting anchor shifts by the publication lag.
    """

    @patch("src.data.collectors.jma_ameidas.datetime")
    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_no_future_timestamp_just_after_10min_mark(
        self, mock_fetch: MagicMock, mock_datetime: MagicMock
    ):
        """At 09:10:05 JST, the primary request must target 09:00, not 09:10
        (09:10 just opened 5 seconds ago and cannot possibly be published yet)."""
        db = _db()
        collector = JmaAmedasCollector(db)

        now_jst = datetime(2026, 6, 20, 9, 10, 5, tzinfo=_JST)
        mock_datetime.now.return_value = now_jst
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        mock_fetch.return_value = _make_response(200, _jma_response(22.4))

        collector.poll()

        url = mock_fetch.call_args_list[0][0][0]
        assert "20260620090000" in url
        assert "20260620091000" not in url

    @patch("src.data.collectors.jma_ameidas.datetime")
    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_no_future_timestamp_just_before_10min_mark(
        self, mock_fetch: MagicMock, mock_datetime: MagicMock
    ):
        """At 09:08:30 JST, lag-adjustment and grid-snap both land on 09:00."""
        db = _db()
        collector = JmaAmedasCollector(db)

        now_jst = datetime(2026, 6, 20, 9, 8, 30, tzinfo=_JST)
        mock_datetime.now.return_value = now_jst
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        mock_fetch.return_value = _make_response(200, _jma_response(21.0))

        collector.poll()

        url = mock_fetch.call_args_list[0][0][0]
        assert "20260620090000" in url

    @patch("src.data.collectors.jma_ameidas.datetime")
    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_no_future_timestamp_at_10min_boundary_exactly(
        self, mock_fetch: MagicMock, mock_datetime: MagicMock
    ):
        """At 09:10:00 JST exactly, the just-opened slot must not be requested."""
        db = _db()
        collector = JmaAmedasCollector(db)

        now_jst = datetime(2026, 6, 20, 9, 10, 0, tzinfo=_JST)
        mock_datetime.now.return_value = now_jst
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        mock_fetch.return_value = _make_response(200, _jma_response(22.5))

        collector.poll()

        url = mock_fetch.call_args_list[0][0][0]
        assert "20260620090000" in url
        assert "20260620091000" not in url

    @patch("src.data.collectors.jma_ameidas.datetime")
    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_no_future_timestamp_around_jst_midnight_before(
        self, mock_fetch: MagicMock, mock_datetime: MagicMock
    ):
        """At 23:58:00 JST, the request stays on the same calendar day (23:50),
        never rolling into the next day."""
        db = _db()
        collector = JmaAmedasCollector(db)

        now_jst = datetime(2026, 6, 20, 23, 58, 0, tzinfo=_JST)
        mock_datetime.now.return_value = now_jst
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        mock_fetch.return_value = _make_response(200, _jma_response(18.5))

        collector.poll()

        url = mock_fetch.call_args_list[0][0][0]
        assert "20260620235000" in url
        assert "20260621" not in url

    @patch("src.data.collectors.jma_ameidas.datetime")
    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_no_future_timestamp_around_jst_midnight_after(
        self, mock_fetch: MagicMock, mock_datetime: MagicMock
    ):
        """At 00:01:00 JST (just after midnight), lag-adjustment rolls the
        anchor back into the *previous* calendar day (23:59) before snapping,
        landing on the previous day's 23:50 slot — never a future timestamp."""
        db = _db()
        collector = JmaAmedasCollector(db)

        now_jst = datetime(2026, 6, 21, 0, 1, 0, tzinfo=_JST)
        mock_datetime.now.return_value = now_jst
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        mock_fetch.return_value = _make_response(200, _jma_response(17.0))

        collector.poll()

        url = mock_fetch.call_args_list[0][0][0]
        assert "20260620235000" in url

    @patch("src.data.collectors.jma_ameidas.datetime")
    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_retry_previous_slot_for_just_published_boundary(
        self, mock_fetch: MagicMock, mock_datetime: MagicMock
    ):
        """When the lag-adjusted primary slot still 404s, fall back to exactly
        one previous 10-minute slot — the fallback chain depth is preserved,
        it does not hammer the future slot again."""
        db = _db()
        collector = JmaAmedasCollector(db)

        now_jst = datetime(2026, 6, 20, 9, 12, 0, tzinfo=_JST)
        mock_datetime.now.return_value = now_jst
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        mock_fetch.side_effect = [
            _make_response(404),
            _make_response(200, _jma_response(20.5)),
        ]

        collector.poll()

        assert mock_fetch.call_count == 2
        first_call = mock_fetch.call_args_list[0][0][0]
        second_call = mock_fetch.call_args_list[1][0][0]
        assert "20260620091000" in first_call
        assert "20260620090000" in second_call

    @patch("src.data.collectors.jma_ameidas.datetime")
    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_previous_hour_tier_still_reachable(
        self, mock_fetch: MagicMock, mock_datetime: MagicMock
    ):
        """The third fallback tier (previous hour) must still be reachable —
        the lag clamp must not collapse the chain from 3 attempts to 2."""
        db = _db()
        collector = JmaAmedasCollector(db)

        now_jst = datetime(2026, 6, 20, 9, 12, 0, tzinfo=_JST)
        mock_datetime.now.return_value = now_jst
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        mock_fetch.side_effect = [
            _make_response(404),  # 09:10 (anchor)
            _make_response(404),  # 09:00 (previous slot)
            _make_response(200, _jma_response(19.5)),  # 08:10 (previous hour)
        ]

        result = collector.poll()

        assert result is True
        assert mock_fetch.call_count == 3
        third_call = mock_fetch.call_args_list[2][0][0]
        assert "20260620081000" in third_call

    @patch("src.data.collectors.jma_ameidas.datetime")
    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_tokyo_cadence_unchanged(
        self, mock_fetch: MagicMock, mock_datetime: MagicMock
    ):
        """Tokyo observation cadence is unchanged (still 10 minutes) after the fix."""
        db = _db()
        collector = JmaAmedasCollector(db)
        assert collector._cadence_min == 10

        now_jst = datetime(2026, 6, 20, 14, 30, 0, tzinfo=_JST)
        mock_datetime.now.return_value = now_jst
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        mock_fetch.return_value = _make_response(200, _jma_response(25.0))

        result = collector.poll()

        assert result is True
        rows = db.get_observations("Tokyo", since="2000-01-01")
        assert rows[0]["cadence_min"] == 10

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


def _jma_response(temp_c: float, time_key: str = "20260620090000") -> dict:
    """Build a minimal JMA AMeDAS JSON response for one time slot.

    Post-#739 the JMA point file is a 3-hour bucket keyed by full
    ``YYYYMMDDHHmmss`` slot timestamps (not the old 6-digit ``HHMMss``).
    """
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
            "20260620090000": {"temp": [20.0, 0], "wind": [1.0, 0]},
            "20260620091000": {"temp": [21.0, 0], "wind": [1.0, 0]},
            "20260620092000": {"temp": [22.5, 0], "wind": [1.0, 0]},
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
            "20260620090000": {"temp": [99.9, 8], "wind": [1.0, 0]},  # quality=8, bad
            "20260620091000": {"temp": [21.0, 0], "wind": [1.0, 0]},  # quality=0, good
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

        resp = _make_response(200, _jma_response(23.0, time_key="20260620091000"))
        with patch("src.data.collectors.jma_ameidas.fetch", return_value=resp):
            collector.poll()

        rows = db.get_observations("Tokyo", since="2000-01-01")
        raw = json.loads(rows[0]["raw_json"])
        assert raw["time_key"] == "20260620091000"


# ---------------------------------------------------------------------------
# JST → UTC conversion
# ---------------------------------------------------------------------------

class TestJstToUtcConversion:
    def test_jst_9am_stored_as_utc_midnight(self):
        """JST 09:00 converts to UTC 00:00."""
        db = _db()
        collector = JmaAmedasCollector(db)

        # The slot key "20260620090000" = 09:00 JST. Post-#739 the parser reads
        # the full YYYYMMDDHHmmss timestamp directly from the key and converts it
        # JST->UTC, so 09:00 JST must be stored as 00:00 UTC (9 hours earlier).
        resp = _make_response(200, _jma_response(25.0, time_key="20260620090000"))
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
# Publication-lag clamp — 3-hour bucket requests, no future timestamps (#560/#739)
# ---------------------------------------------------------------------------

class TestPublicationLagAndFutureTimestamps:
    """The collector must never request an unpublished JMA file nor store a
    future timestamp. Post-#739 the point feed is a 3-hour bucket file
    (``YYYYMMDD_HH.json``); the publication-lag anchor (issue #560) still
    shifts the request off a just-opened bucket, and the fallback chain
    (current bucket -> previous slot offset -> previous hour) must stay intact.

    These tests freeze the clock and drive the REAL ``poll()`` path, asserting
    on the bucket URL(s) requested and the timestamp actually stored. The
    per-slot URL/grid-snap details are covered separately in
    ``test_jma_ameidas.py`` (``TestUrlFormat``/``TestFallbackChain``); here we
    keep the poll-level, clock-frozen integration coverage.
    """

    @patch("src.data.collectors.jma_ameidas.datetime")
    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_primary_request_targets_current_3h_bucket(
        self, mock_fetch: MagicMock, mock_datetime: MagicMock
    ):
        """Mid-bucket (09:17 JST) the primary request targets the 09:00 bucket
        file (``20260620_09.json``)."""
        db = _db()
        collector = JmaAmedasCollector(db)

        now_jst = datetime(2026, 6, 20, 9, 17, 0, tzinfo=_JST)
        mock_datetime.now.return_value = now_jst
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        mock_fetch.return_value = _make_response(200, _jma_response(22.4))

        collector.poll()

        url = mock_fetch.call_args_list[0][0][0]
        assert "20260620_09.json" in url

    @patch("src.data.collectors.jma_ameidas.datetime")
    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_publication_lag_avoids_just_opened_bucket(
        self, mock_fetch: MagicMock, mock_datetime: MagicMock
    ):
        """At 09:01 JST the 09:00 bucket only just opened; the 2-min lag anchor
        (08:59) keeps the primary request on the already-published 06:00 bucket
        rather than the just-opened 09:00 one."""
        db = _db()
        collector = JmaAmedasCollector(db)

        now_jst = datetime(2026, 6, 20, 9, 1, 0, tzinfo=_JST)
        mock_datetime.now.return_value = now_jst
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        mock_fetch.return_value = _make_response(200, _jma_response(21.0, time_key="20260620065000"))

        collector.poll()

        url = mock_fetch.call_args_list[0][0][0]
        assert "20260620_06.json" in url
        assert "20260620_09.json" not in url

    @patch("src.data.collectors.jma_ameidas.datetime")
    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_previous_bucket_reachable_on_404(
        self, mock_fetch: MagicMock, mock_datetime: MagicMock
    ):
        """If the current bucket 404s, the fallback chain must still reach the
        previous 3-hour bucket (06:00) — the chain does not collapse."""
        db = _db()
        collector = JmaAmedasCollector(db)

        now_jst = datetime(2026, 6, 20, 9, 17, 0, tzinfo=_JST)
        mock_datetime.now.return_value = now_jst
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        # current bucket (09) 404s on the anchor and the -10min retry (still 09),
        # the -1h retry lands on the previous bucket (06) and succeeds.
        mock_fetch.side_effect = [
            _make_response(404),
            _make_response(404),
            _make_response(200, _jma_response(19.5, time_key="20260620081000")),
        ]

        result = collector.poll()

        assert result is True
        assert mock_fetch.call_count == 3
        assert "20260620_06.json" in mock_fetch.call_args_list[2][0][0]

    @patch("src.data.collectors.jma_ameidas.datetime")
    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_stored_timestamp_not_in_future(
        self, mock_fetch: MagicMock, mock_datetime: MagicMock
    ):
        """The stored observation timestamp is the newest published slot in the
        bucket and is never ahead of 'now'."""
        db = _db()
        collector = JmaAmedasCollector(db)

        now_jst = datetime(2026, 6, 20, 9, 17, 0, tzinfo=_JST)
        mock_datetime.now.return_value = now_jst
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        # Newest good slot is 09:10 JST (already published at 09:17); no future slot.
        data = {
            "20260620090000": {"temp": [22.0, 0], "wind": [1.0, 0]},
            "20260620091000": {"temp": [22.3, 0], "wind": [1.0, 0]},
        }
        mock_fetch.return_value = _make_response(200, data)

        assert collector.poll() is True

        rows = db.get_observations("Tokyo", since="2000-01-01")
        stored = datetime.fromisoformat(rows[0]["ts"])
        if stored.tzinfo is None:
            stored = stored.replace(tzinfo=timezone.utc)
        now_utc = now_jst.astimezone(timezone.utc)
        assert stored <= now_utc
        # 09:10 JST -> 00:10 UTC
        assert stored.astimezone(timezone.utc).hour == 0
        assert stored.astimezone(timezone.utc).minute == 10

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

        mock_fetch.return_value = _make_response(200, _jma_response(25.0, time_key="20260620143000"))

        result = collector.poll()

        assert result is True
        rows = db.get_observations("Tokyo", since="2000-01-01")
        assert rows[0]["cadence_min"] == 10

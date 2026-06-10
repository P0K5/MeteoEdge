"""Tests for src/data/taf_collector.py — TafCollector class.

Uses a real in-memory Database and mocks fetch() to avoid HTTP calls.

Covers:
- fetch_one returns correct window count
- stale window deletion occurs before insert
- errors on one ICAO do not abort the loop for subsequent ICAOs
- DB rows match parser output
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, call, patch

import pytest

from src.data.db import Database
from src.data.taf_collector import TafCollector

# ---------------------------------------------------------------------------
# Sample TAF JSON payload (aviationweather.gov format)
# ---------------------------------------------------------------------------

SAMPLE_TAF = """\
TAF
RJTT 071700Z 0718/0824 12010KT 9999 FEW020
  TEMPO 0718/0722 4000 TSRA FEW010 BKN020CB
  FM072200 15015KT 9999 SCT030
  BECMG 0800/0802 VRB03KT 9999 FEW030
  PROB30 TEMPO 0806/0812 2000 FG
"""

SAMPLE_JSON = json.dumps([{"rawTAF": SAMPLE_TAF}])


def _mock_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.text = text
    return resp


@pytest.fixture
def db() -> Database:
    return Database(":memory:")


@pytest.fixture
def collector(db: Database) -> TafCollector:
    return TafCollector(db, cadence_min=30, airport_icaos=[("RJTT", "Tokyo")])


# ---------------------------------------------------------------------------
# fetch_one behaviour
# ---------------------------------------------------------------------------

class TestFetchOne:
    def test_returns_window_count(self, collector):
        with patch("src.data.taf_collector.fetch", return_value=_mock_response(SAMPLE_JSON)):
            n = collector.fetch_one("RJTT", "Tokyo")
        assert n == 5  # base + TEMPO + FM + BECMG + PROB30 TEMPO

    def test_inserts_windows_to_db(self, collector, db):
        with patch("src.data.taf_collector.fetch", return_value=_mock_response(SAMPLE_JSON)):
            collector.fetch_one("RJTT", "Tokyo")
        # Pull windows out of DB — use a wide range to cover any day-of-month in SAMPLE_TAF
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        from_ts = (now - timedelta(days=30)).isoformat().replace("+00:00", "Z")
        to_ts   = (now + timedelta(days=30)).isoformat().replace("+00:00", "Z")
        windows = db.get_taf_windows("Tokyo", from_ts, to_ts)
        assert len(windows) == 5

    def test_city_set_correctly_in_db(self, collector, db):
        with patch("src.data.taf_collector.fetch", return_value=_mock_response(SAMPLE_JSON)):
            collector.fetch_one("RJTT", "Tokyo")
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        from_ts = (now - timedelta(days=30)).isoformat().replace("+00:00", "Z")
        to_ts   = (now + timedelta(days=30)).isoformat().replace("+00:00", "Z")
        windows = db.get_taf_windows("Tokyo", from_ts, to_ts)
        assert all(w["city"] == "Tokyo" for w in windows)

    def test_returns_zero_for_empty_taf(self, collector):
        with patch("src.data.taf_collector.fetch", return_value=_mock_response("[]")):
            n = collector.fetch_one("RJTT", "Tokyo")
        assert n == 0

    def test_fetch_called_with_correct_url(self, collector):
        with patch("src.data.taf_collector.fetch", return_value=_mock_response(SAMPLE_JSON)) as mock_fetch:
            collector.fetch_one("RJTT", "Tokyo")
        assert mock_fetch.call_count == 1
        url = mock_fetch.call_args[0][0]
        assert "RJTT" in url
        assert "format=json" in url


# ---------------------------------------------------------------------------
# Stale window deletion
# ---------------------------------------------------------------------------

class TestStaleWindowDeletion:
    def test_stale_windows_deleted_before_insert(self, collector, db):
        """Inserting a second time for the same issued_at should not duplicate rows."""
        with patch("src.data.taf_collector.fetch", return_value=_mock_response(SAMPLE_JSON)):
            collector.fetch_one("RJTT", "Tokyo")
        # Fetch again — should delete previous windows then insert fresh ones
        with patch("src.data.taf_collector.fetch", return_value=_mock_response(SAMPLE_JSON)):
            collector.fetch_one("RJTT", "Tokyo")
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        from_ts = (now - timedelta(days=30)).isoformat().replace("+00:00", "Z")
        to_ts   = (now + timedelta(days=30)).isoformat().replace("+00:00", "Z")
        windows = db.get_taf_windows("Tokyo", from_ts, to_ts)
        # Should still be exactly 5, not 10 (stale rows were deleted first)
        assert len(windows) == 5

    def test_delete_stale_called_with_correct_args(self, collector):
        with patch("src.data.taf_collector.fetch", return_value=_mock_response(SAMPLE_JSON)):
            with patch.object(collector._db, "delete_stale_taf_windows", wraps=collector._db.delete_stale_taf_windows) as mock_delete:
                with patch.object(collector._db, "insert_taf_window", wraps=collector._db.insert_taf_window):
                    collector.fetch_one("RJTT", "Tokyo")
        mock_delete.assert_called_once()
        args = mock_delete.call_args
        assert args[0][0] == "Tokyo"   # city
        # Second arg is issued_at — just check it's a non-empty string
        assert isinstance(args[0][1], str) and args[0][1]


# ---------------------------------------------------------------------------
# Error isolation in run_loop
# ---------------------------------------------------------------------------

class TestRunLoopErrorIsolation:
    def test_error_on_one_icao_does_not_abort_others(self, db):
        """If fetch fails for one ICAO, subsequent ICAOs should still be processed."""
        icaos = [("RJTT", "Tokyo"), ("RKSI", "Seoul"), ("WSSS", "Singapore")]
        collector = TafCollector(db, cadence_min=30, airport_icaos=icaos)

        call_count = 0
        def fake_fetch(url, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if "RJTT" in url:
                raise RuntimeError("simulated network error")
            return _mock_response(SAMPLE_JSON)

        # Patch sleep so the loop runs exactly once then raises SystemExit
        sleep_calls = []
        def fake_sleep(secs):
            sleep_calls.append(secs)
            raise SystemExit("stop after one iteration")

        with patch("src.data.taf_collector.fetch", side_effect=fake_fetch):
            with patch("src.data.taf_collector.time.sleep", side_effect=fake_sleep):
                with pytest.raises(SystemExit):
                    collector.run_loop()

        # fetch was called for all 3 ICAOs despite the error on RJTT
        assert call_count == 3
        # sleep was called once (end of loop iteration)
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 30 * 60

    def test_error_icao_produces_no_db_rows(self, db):
        """A failed ICAO should leave no rows in the DB."""
        icaos = [("RJTT", "Tokyo")]
        collector = TafCollector(db, cadence_min=30, airport_icaos=icaos)

        def fake_fetch(url, *args, **kwargs):
            raise RuntimeError("network down")

        def fake_sleep(secs):
            raise SystemExit("stop")

        with patch("src.data.taf_collector.fetch", side_effect=fake_fetch):
            with patch("src.data.taf_collector.time.sleep", side_effect=fake_sleep):
                with pytest.raises(SystemExit):
                    collector.run_loop()

        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        from_ts = (now - timedelta(days=30)).isoformat().replace("+00:00", "Z")
        to_ts   = (now + timedelta(days=30)).isoformat().replace("+00:00", "Z")
        windows = db.get_taf_windows("Tokyo", from_ts, to_ts)
        assert windows == []

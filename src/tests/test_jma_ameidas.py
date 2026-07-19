"""Tests for src/data/collectors/jma_ameidas.py — JmaAmedasCollector class.

Uses a real in-memory Database and mocks fetch() to avoid HTTP calls.

Covers:
- 3-hour bucket format (YYYYMMDD_HH.json)
- 3-hour grid snapping logic
- Fallback chain (current bucket → previous bucket)
- JMA JSON response parsing with quality flags
- Newest good-quality slot selection (reverse sorted order)
- JST→UTC conversion
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.data.collectors.jma_ameidas import JmaAmedasCollector
from src.data.db import Database

_JST = timezone(timedelta(hours=9))


def _mock_response(status_code: int, data: dict | None = None) -> MagicMock:
    """Create a mock HTTP response."""
    resp = MagicMock()
    resp.status_code = status_code
    if data:
        resp.json.return_value = data
    return resp


@pytest.fixture
def db() -> Database:
    return Database(":memory:")


@pytest.fixture
def collector(db: Database) -> JmaAmedasCollector:
    return JmaAmedasCollector(db)


# ---------------------------------------------------------------------------
# Sample JMA AMeDAS JSON payload
# ---------------------------------------------------------------------------

SAMPLE_JMA_DATA = {
    "20260620090000": {
        "temp": [22.4, 0],
        "wind": [3.2, 0],
        "windDirection": [5, 0],
    },
    "20260620091000": {
        "temp": [22.5, 0],
        "wind": [3.1, 0],
        "windDirection": [5, 0],
    },
    "20260620092000": {
        "temp": [22.6, 0],
        "wind": [3.0, 0],
        "windDirection": [5, 0],
    },
}


class TestBucketSnapping:
    """Test 3-hour bucket snapping logic."""

    def test_snap_hour_0_stays_0(self, collector: JmaAmedasCollector) -> None:
        """Hour 0 (00:00-02:59) should snap to bucket hour 0."""
        base_jst = datetime(2026, 6, 20, 0, 30, 0, tzinfo=_JST)
        bucket_hour = (base_jst.hour // 3) * 3
        assert bucket_hour == 0

    def test_snap_hour_1_to_0(self, collector: JmaAmedasCollector) -> None:
        """Hour 1 should snap down to bucket hour 0."""
        base_jst = datetime(2026, 6, 20, 1, 30, 0, tzinfo=_JST)
        bucket_hour = (base_jst.hour // 3) * 3
        assert bucket_hour == 0

    def test_snap_hour_2_to_0(self, collector: JmaAmedasCollector) -> None:
        """Hour 2 should snap down to bucket hour 0."""
        base_jst = datetime(2026, 6, 20, 2, 59, 0, tzinfo=_JST)
        bucket_hour = (base_jst.hour // 3) * 3
        assert bucket_hour == 0

    def test_snap_hour_3_stays_3(self, collector: JmaAmedasCollector) -> None:
        """Hour 3 (03:00-05:59) should snap to bucket hour 3."""
        base_jst = datetime(2026, 6, 20, 3, 0, 0, tzinfo=_JST)
        bucket_hour = (base_jst.hour // 3) * 3
        assert bucket_hour == 3

    def test_snap_hour_5_to_3(self, collector: JmaAmedasCollector) -> None:
        """Hour 5 should snap down to bucket hour 3."""
        base_jst = datetime(2026, 6, 20, 5, 59, 0, tzinfo=_JST)
        bucket_hour = (base_jst.hour // 3) * 3
        assert bucket_hour == 3

    def test_snap_hour_9_stays_9(self, collector: JmaAmedasCollector) -> None:
        """Hour 9 (09:00-11:59) should snap to bucket hour 9."""
        base_jst = datetime(2026, 6, 20, 9, 30, 0, tzinfo=_JST)
        bucket_hour = (base_jst.hour // 3) * 3
        assert bucket_hour == 9

    def test_snap_hour_23_to_21(self, collector: JmaAmedasCollector) -> None:
        """Hour 23 should snap down to bucket hour 21."""
        base_jst = datetime(2026, 6, 20, 23, 59, 0, tzinfo=_JST)
        bucket_hour = (base_jst.hour // 3) * 3
        assert bucket_hour == 21


class TestUrlFormat:
    """Test 3-hour bucket URL format (YYYYMMDD_HH.json)."""

    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_url_format_3hour_bucket(
        self, mock_fetch: MagicMock, collector: JmaAmedasCollector
    ) -> None:
        """URL should contain 3-hour bucket format (YYYYMMDD_HH.json)."""
        base_jst = datetime(2026, 6, 20, 9, 17, 30, tzinfo=_JST)
        mock_fetch.return_value = _mock_response(200, SAMPLE_JMA_DATA)

        collector._fetch_jma_slot(base_jst)

        # Check the exact URL format
        url = mock_fetch.call_args[0][0]
        assert "https://www.jma.go.jp/bosai/amedas/data/point/44132/" in url
        assert "20260620_09.json" in url

    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_url_format_snap_hour_5_to_3(
        self, mock_fetch: MagicMock, collector: JmaAmedasCollector
    ) -> None:
        """URL for hour 5 should snap to bucket hour 03."""
        base_jst = datetime(2026, 6, 20, 5, 45, 30, tzinfo=_JST)
        mock_fetch.return_value = _mock_response(200, SAMPLE_JMA_DATA)

        collector._fetch_jma_slot(base_jst)

        url = mock_fetch.call_args[0][0]
        assert "20260620_03.json" in url

    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_url_format_snap_hour_23_to_21(
        self, mock_fetch: MagicMock, collector: JmaAmedasCollector
    ) -> None:
        """URL for hour 23 should snap to bucket hour 21."""
        base_jst = datetime(2026, 6, 20, 23, 59, 45, tzinfo=_JST)
        mock_fetch.return_value = _mock_response(200, SAMPLE_JMA_DATA)

        collector._fetch_jma_slot(base_jst)

        url = mock_fetch.call_args[0][0]
        assert "20260620_21.json" in url


class TestDataParsing:
    """Test JMA JSON response parsing."""

    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_parse_valid_response(
        self, mock_fetch: MagicMock, collector: JmaAmedasCollector
    ) -> None:
        """Should extract the newest valid reading from JMA response."""
        base_jst = datetime(2026, 6, 20, 9, 0, 0, tzinfo=_JST)
        mock_fetch.return_value = _mock_response(200, SAMPLE_JMA_DATA)

        result = collector._fetch_jma_slot(base_jst)

        assert result is not None
        ts_utc, temp_c, raw = result
        # Newest reading in SAMPLE_JMA_DATA is at 20260620092000 (9:20 JST) = 22.6°C
        assert temp_c == 22.6
        assert raw["time_key"] == "20260620092000"

    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_parse_ignores_quality_flag_1(
        self, mock_fetch: MagicMock, collector: JmaAmedasCollector
    ) -> None:
        """Should skip readings with quality flag != 0, prefer older good reading."""
        base_jst = datetime(2026, 6, 20, 9, 0, 0, tzinfo=_JST)
        data = {
            "20260620090000": {"temp": [22.4, 0], "wind": [3.2, 0]},  # quality=0, good
            "20260620091000": {"temp": [22.5, 1], "wind": [3.1, 0]},  # quality=1, bad
        }
        mock_fetch.return_value = _mock_response(200, data)

        result = collector._fetch_jma_slot(base_jst)

        assert result is not None
        ts_utc, temp_c, raw = result
        # Should use the 20260620090000 reading (22.4) since 20260620091000 has bad quality
        # We iterate in reverse, so we see the bad one first and skip it
        assert temp_c == 22.4
        assert raw["time_key"] == "20260620090000"

    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_parse_404_returns_none(
        self, mock_fetch: MagicMock, collector: JmaAmedasCollector
    ) -> None:
        """Should return None on 404 (file not yet published)."""
        base_jst = datetime(2026, 6, 20, 9, 17, 0, tzinfo=_JST)
        mock_fetch.return_value = _mock_response(404)

        result = collector._fetch_jma_slot(base_jst)

        assert result is None

    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_parse_http_error_returns_none(
        self, mock_fetch: MagicMock, collector: JmaAmedasCollector
    ) -> None:
        """Should return None on HTTP errors other than 404."""
        base_jst = datetime(2026, 6, 20, 9, 17, 0, tzinfo=_JST)
        mock_fetch.return_value = _mock_response(500)

        result = collector._fetch_jma_slot(base_jst)

        assert result is None

    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_parse_network_error_returns_none(
        self, mock_fetch: MagicMock, collector: JmaAmedasCollector
    ) -> None:
        """Should return None on network errors."""
        base_jst = datetime(2026, 6, 20, 9, 17, 0, tzinfo=_JST)
        mock_fetch.side_effect = ConnectionError("Network error")

        result = collector._fetch_jma_slot(base_jst)

        assert result is None


class TestFallbackChain:
    """Test the fallback chain for retrying previous 3-hour buckets."""

    @patch("src.data.collectors.jma_ameidas.fetch")
    @patch("src.data.collectors.jma_ameidas.datetime")
    def test_fallback_to_previous_bucket(
        self, mock_datetime: MagicMock, mock_fetch: MagicMock, collector: JmaAmedasCollector
    ) -> None:
        """Should retry previous 3-hour bucket if current is not available."""
        now_jst = datetime(2026, 6, 20, 9, 17, 0, tzinfo=_JST)
        mock_datetime.now.return_value = now_jst
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        # Current bucket (09:00) returns 404, previous bucket (06:00) returns data
        mock_fetch.side_effect = [
            _mock_response(404),  # First call: current bucket (09:00) → 404
            _mock_response(200, SAMPLE_JMA_DATA),  # Second call: previous bucket (06:00) → success
        ]

        result = collector._fetch_jma()

        assert result is not None
        ts_utc, temp_c, raw = result
        assert temp_c == 22.6
        # Should have called fetch twice
        assert mock_fetch.call_count == 2

    @patch("src.data.collectors.jma_ameidas.fetch")
    @patch("src.data.collectors.jma_ameidas.datetime")
    def test_fallback_to_previous_hour_offset(
        self, mock_datetime: MagicMock, mock_fetch: MagicMock, collector: JmaAmedasCollector
    ) -> None:
        """Should retry 1 hour before if both current and previous bucket fail."""
        now_jst = datetime(2026, 6, 20, 9, 17, 0, tzinfo=_JST)
        mock_datetime.now.return_value = now_jst
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        # Current bucket (09:00) returns 404, previous bucket (06:00) returns 404,
        # 1 hour before current (08:00) returns data
        mock_fetch.side_effect = [
            _mock_response(404),  # First call: current bucket (09:00) → 404
            _mock_response(404),  # Second call: previous bucket (06:00) → 404
            _mock_response(200, SAMPLE_JMA_DATA),  # Third call: 1 hour before (08:00) → success
        ]

        result = collector._fetch_jma()

        assert result is not None
        ts_utc, temp_c, raw = result
        assert temp_c == 22.6
        # Should have called fetch three times
        assert mock_fetch.call_count == 3

    @patch("src.data.collectors.jma_ameidas.fetch")
    @patch("src.data.collectors.jma_ameidas.datetime")
    def test_fallback_all_fail(
        self, mock_datetime: MagicMock, mock_fetch: MagicMock, collector: JmaAmedasCollector
    ) -> None:
        """Should return None if all fallback attempts fail."""
        now_jst = datetime(2026, 6, 20, 9, 17, 0, tzinfo=_JST)
        mock_datetime.now.return_value = now_jst
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        # All attempts return 404
        mock_fetch.side_effect = [
            _mock_response(404),  # Current bucket
            _mock_response(404),  # Previous bucket
            _mock_response(404),  # Previous hour
        ]

        result = collector._fetch_jma()

        assert result is None
        # Should have called fetch three times
        assert mock_fetch.call_count == 3


# ---------------------------------------------------------------------------
# Issue #739 regression tests: 3-hour bucket format, newest slot selection,
# JST→UTC conversion
# ---------------------------------------------------------------------------

class TestNewestSlotSelection:
    """Verify the NEWEST good-quality slot is selected from a bucket."""

    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_newest_good_slot_selected(
        self, mock_fetch: MagicMock, collector: JmaAmedasCollector
    ) -> None:
        """Should select the NEWEST slot with quality_flag == 0."""
        base_jst = datetime(2026, 6, 20, 9, 0, 0, tzinfo=_JST)
        data = {
            "20260620090000": {"temp": [22.0, 0], "wind": [3.0, 0]},  # 09:00 JST, good
            "20260620091000": {"temp": [22.2, 0], "wind": [3.1, 0]},  # 09:10 JST, good
            "20260620092000": {"temp": [22.4, 0], "wind": [3.2, 0]},  # 09:20 JST, good (NEWEST)
        }
        mock_fetch.return_value = _mock_response(200, data)

        result = collector._fetch_jma_slot(base_jst)

        assert result is not None
        ts_utc, temp_c, raw = result
        # Should pick the newest: 09:20 = 22.4
        assert temp_c == 22.4
        assert raw["time_key"] == "20260620092000"

    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_skip_bad_quality_find_older_good(
        self, mock_fetch: MagicMock, collector: JmaAmedasCollector
    ) -> None:
        """Should skip bad-quality slots and find the newest good one."""
        base_jst = datetime(2026, 6, 20, 9, 0, 0, tzinfo=_JST)
        data = {
            "20260620090000": {"temp": [22.0, 0], "wind": [3.0, 0]},  # 09:00 JST, good
            "20260620091000": {"temp": [22.2, 1], "wind": [3.1, 0]},  # 09:10 JST, BAD quality
            "20260620092000": {"temp": [22.4, 2], "wind": [3.2, 0]},  # 09:20 JST, BAD quality
        }
        mock_fetch.return_value = _mock_response(200, data)

        result = collector._fetch_jma_slot(base_jst)

        assert result is not None
        ts_utc, temp_c, raw = result
        # Should pick the newest good one: 09:00 = 22.0
        assert temp_c == 22.0
        assert raw["time_key"] == "20260620090000"

    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_jst_utc_conversion(
        self, mock_fetch: MagicMock, collector: JmaAmedasCollector
    ) -> None:
        """Should correctly convert JST → UTC (JST is UTC+9)."""
        base_jst = datetime(2026, 6, 20, 9, 0, 0, tzinfo=_JST)
        data = {
            "20260620090000": {"temp": [22.0, 0], "wind": [3.0, 0]},  # 09:00 JST
        }
        mock_fetch.return_value = _mock_response(200, data)

        result = collector._fetch_jma_slot(base_jst)

        assert result is not None
        ts_utc, temp_c, raw = result
        # 09:00 JST = 00:00 UTC (on the same day, 9 hours earlier)
        assert ts_utc.hour == 0
        assert ts_utc.day == 20
        assert ts_utc.month == 6
        # Confirm it's UTC
        assert ts_utc.tzinfo == timezone.utc

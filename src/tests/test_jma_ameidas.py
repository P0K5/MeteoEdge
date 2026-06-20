"""Tests for src/data/collectors/jma_ameidas.py — JmaAmedasCollector class.

Uses a real in-memory Database and mocks fetch() to avoid HTTP calls.

Covers:
- 14-digit timestamp format (YYYYMMDDHHMMSS)
- 10-minute grid snapping logic
- Fallback chain (current slot → previous slot → previous hour)
- JMA JSON response parsing with quality flags
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
    "090000": {
        "temp": [22.4, 0],
        "wind": [3.2, 0],
        "windDirection": [5, 0],
    },
    "091000": {
        "temp": [22.5, 0],
        "wind": [3.1, 0],
        "windDirection": [5, 0],
    },
    "092000": {
        "temp": [22.6, 0],
        "wind": [3.0, 0],
        "windDirection": [5, 0],
    },
}


class TestGridSnapping:
    """Test 10-minute grid snapping logic."""

    def test_snap_minute_0_stays_0(self, collector: JmaAmedasCollector) -> None:
        """Minute 0 should snap to 0."""
        base_jst = datetime(2026, 6, 20, 9, 0, 0, tzinfo=_JST)
        snapped = base_jst.replace(
            minute=base_jst.minute // 10 * 10, second=0, microsecond=0
        )
        assert snapped.minute == 0

    def test_snap_minute_5_to_0(self, collector: JmaAmedasCollector) -> None:
        """Minute 5 should snap down to 0."""
        base_jst = datetime(2026, 6, 20, 9, 5, 0, tzinfo=_JST)
        snapped = base_jst.replace(
            minute=base_jst.minute // 10 * 10, second=0, microsecond=0
        )
        assert snapped.minute == 0

    def test_snap_minute_10_stays_10(self, collector: JmaAmedasCollector) -> None:
        """Minute 10 should snap to 10."""
        base_jst = datetime(2026, 6, 20, 9, 10, 0, tzinfo=_JST)
        snapped = base_jst.replace(
            minute=base_jst.minute // 10 * 10, second=0, microsecond=0
        )
        assert snapped.minute == 10

    def test_snap_minute_17_to_10(self, collector: JmaAmedasCollector) -> None:
        """Minute 17 should snap down to 10."""
        base_jst = datetime(2026, 6, 20, 9, 17, 0, tzinfo=_JST)
        snapped = base_jst.replace(
            minute=base_jst.minute // 10 * 10, second=0, microsecond=0
        )
        assert snapped.minute == 10

    def test_snap_minute_50_stays_50(self, collector: JmaAmedasCollector) -> None:
        """Minute 50 should snap to 50."""
        base_jst = datetime(2026, 6, 20, 9, 50, 0, tzinfo=_JST)
        snapped = base_jst.replace(
            minute=base_jst.minute // 10 * 10, second=0, microsecond=0
        )
        assert snapped.minute == 50

    def test_snap_minute_59_to_50(self, collector: JmaAmedasCollector) -> None:
        """Minute 59 should snap down to 50."""
        base_jst = datetime(2026, 6, 20, 9, 59, 0, tzinfo=_JST)
        snapped = base_jst.replace(
            minute=base_jst.minute // 10 * 10, second=0, microsecond=0
        )
        assert snapped.minute == 50


class TestUrlFormat:
    """Test 14-digit timestamp URL format."""

    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_url_format_14digit(
        self, mock_fetch: MagicMock, collector: JmaAmedasCollector
    ) -> None:
        """URL should contain 14-digit timestamp (YYYYMMDDHHMMSS)."""
        base_jst = datetime(2026, 6, 20, 9, 17, 30, tzinfo=_JST)
        mock_fetch.return_value = _mock_response(200, SAMPLE_JMA_DATA)

        with patch("src.data.collectors.jma_ameidas.datetime") as mock_datetime:
            mock_datetime.now.return_value = base_jst
            mock_datetime.side_effect = lambda *args, **kwargs: datetime(
                *args, **kwargs
            )
            collector._fetch_jma_slot(base_jst)

        # Capture the URL that was called
        call_args = mock_fetch.call_args
        url = call_args[0][0] if call_args[0] else call_args[1].get("url")

        # URL should contain 14-digit timestamp snapped to 9:10:00
        assert "20260620091000" in url

    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_url_format_snap_17_to_10(
        self, mock_fetch: MagicMock, collector: JmaAmedasCollector
    ) -> None:
        """URL for minute 17 should snap to 10 (14-digit: ...091000)."""
        base_jst = datetime(2026, 6, 20, 9, 17, 30, tzinfo=_JST)
        mock_fetch.return_value = _mock_response(200, SAMPLE_JMA_DATA)

        collector._fetch_jma_slot(base_jst)

        # Check the exact URL format
        url = mock_fetch.call_args[0][0]
        assert "https://www.jma.go.jp/bosai/amedas/data/point/44132/" in url
        assert "20260620091000.json" in url

    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_url_format_snap_59_to_50(
        self, mock_fetch: MagicMock, collector: JmaAmedasCollector
    ) -> None:
        """URL for minute 59 should snap to 50 (14-digit: ...095000)."""
        base_jst = datetime(2026, 6, 20, 9, 59, 45, tzinfo=_JST)
        mock_fetch.return_value = _mock_response(200, SAMPLE_JMA_DATA)

        collector._fetch_jma_slot(base_jst)

        url = mock_fetch.call_args[0][0]
        assert "20260620095000.json" in url


class TestDataParsing:
    """Test JMA JSON response parsing."""

    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_parse_valid_response(
        self, mock_fetch: MagicMock, collector: JmaAmedasCollector
    ) -> None:
        """Should extract the most recent valid reading from JMA response."""
        base_jst = datetime(2026, 6, 20, 9, 0, 0, tzinfo=_JST)
        mock_fetch.return_value = _mock_response(200, SAMPLE_JMA_DATA)

        result = collector._fetch_jma_slot(base_jst)

        assert result is not None
        ts_utc, temp_c, raw = result
        # Last reading in SAMPLE_JMA_DATA is at 092000 (9:20 JST) = 22.6°C
        assert temp_c == 22.6
        assert raw["time_key"] == "092000"

    @patch("src.data.collectors.jma_ameidas.fetch")
    def test_parse_ignores_quality_flag_1(
        self, mock_fetch: MagicMock, collector: JmaAmedasCollector
    ) -> None:
        """Should skip readings with quality flag != 0."""
        base_jst = datetime(2026, 6, 20, 9, 0, 0, tzinfo=_JST)
        data = {
            "090000": {"temp": [22.4, 1], "wind": [3.2, 0]},  # quality=1, bad
            "091000": {"temp": [22.5, 0], "wind": [3.1, 0]},  # quality=0, good
        }
        mock_fetch.return_value = _mock_response(200, data)

        result = collector._fetch_jma_slot(base_jst)

        assert result is not None
        ts_utc, temp_c, raw = result
        # Should use the 091000 reading (22.5) since 090000 has bad quality
        assert temp_c == 22.5

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
    """Test the fallback chain for retrying previous slots."""

    @patch("src.data.collectors.jma_ameidas.fetch")
    @patch("src.data.collectors.jma_ameidas.datetime")
    def test_fallback_to_previous_slot(
        self, mock_datetime: MagicMock, mock_fetch: MagicMock, collector: JmaAmedasCollector
    ) -> None:
        """Should retry previous 10-minute slot if current is not available."""
        now_jst = datetime(2026, 6, 20, 9, 17, 0, tzinfo=_JST)
        mock_datetime.now.return_value = now_jst
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        # Current slot (09:10) returns 404, previous slot (09:00) returns data
        mock_fetch.side_effect = [
            _mock_response(404),  # First call: current slot → 404
            _mock_response(200, SAMPLE_JMA_DATA),  # Second call: previous slot → success
        ]

        result = collector._fetch_jma()

        assert result is not None
        ts_utc, temp_c, raw = result
        assert temp_c == 22.6
        # Should have called fetch twice
        assert mock_fetch.call_count == 2

    @patch("src.data.collectors.jma_ameidas.fetch")
    @patch("src.data.collectors.jma_ameidas.datetime")
    def test_fallback_to_previous_hour(
        self, mock_datetime: MagicMock, mock_fetch: MagicMock, collector: JmaAmedasCollector
    ) -> None:
        """Should retry previous hour if both current and previous slot fail."""
        now_jst = datetime(2026, 6, 20, 9, 17, 0, tzinfo=_JST)
        mock_datetime.now.return_value = now_jst
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        # Current slot (09:10) returns 404, previous slot (09:00) returns 404,
        # previous hour (08:xx) returns data
        mock_fetch.side_effect = [
            _mock_response(404),  # First call: current slot → 404
            _mock_response(404),  # Second call: previous slot → 404
            _mock_response(200, SAMPLE_JMA_DATA),  # Third call: previous hour → success
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
            _mock_response(404),  # Current slot
            _mock_response(404),  # Previous slot
            _mock_response(404),  # Previous hour
        ]

        result = collector._fetch_jma()

        assert result is None
        # Should have called fetch three times
        assert mock_fetch.call_count == 3

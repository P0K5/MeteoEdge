"""Unit tests for src/data/collectors/amos.py.

All HTTP calls are mocked via patch on src.data.collectors.amos.fetch.
All DB writes use an in-memory Database instance.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.data.collectors.amos import AmosCollector, _KST
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


# Sample KMA API response structure
def _kma_response(temp_c: float, station_id: str = "112") -> dict:
    return {
        "response": {
            "body": {
                "items": {
                    "item": [
                        {
                            "tm": "2026-06-07 09:00",
                            "stnId": station_id,
                            "ta": str(temp_c),
                        }
                    ]
                }
            }
        }
    }


# Open-Meteo fallback response
def _open_meteo_response(temp_c: float) -> dict:
    now_utc = datetime.now(timezone.utc)
    past_hour = now_utc.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    return {
        "hourly": {
            "time": [past_hour.isoformat()],
            "temperature_2m": [temp_c],
        }
    }


# KMA ASOS "previous-day only" response (issue #694) — HTTP 200 with resultCode=99
def _kma_result_code_response(code: str = "99", msg: str = "전날 자료까지 제공됩니다.") -> dict:
    return {
        "response": {
            "header": {
                "resultCode": code,
                "resultMsg": msg,
            }
        }
    }


# ---------------------------------------------------------------------------
# KMA API path
# ---------------------------------------------------------------------------

class TestKmaPath:
    def test_inserts_seoul_row_via_kma(self, monkeypatch):
        """With KMA_API_KEY set, fetches Seoul data and inserts a row."""
        monkeypatch.setenv("KMA_API_KEY", "test-key-123")
        db = _db()
        collector = AmosCollector(db)

        seoul_resp = _make_response(200, _kma_response(25.0, "112"))
        busan_resp = _make_response(200, _kma_response(23.0, "159"))

        with patch("src.data.collectors.amos.fetch", side_effect=[seoul_resp, busan_resp]):
            results = collector.poll()

        assert results["Seoul"] is True
        assert results["Busan"] is True

        rows = db.get_observations("Seoul", since="2000-01-01")
        assert len(rows) == 1
        r = rows[0]
        assert r["source"] == "amos"
        assert r["station"] == "Seoul"
        assert r["unit"] == "C"
        assert r["temp_native"] == pytest.approx(25.0)
        assert r["is_official"] == 1
        assert r["cadence_min"] == 10

    def test_inserts_busan_row_via_kma(self, monkeypatch):
        """KMA path stores Busan row with correct station name."""
        monkeypatch.setenv("KMA_API_KEY", "test-key-123")
        db = _db()
        collector = AmosCollector(db)

        seoul_resp = _make_response(200, _kma_response(25.0, "112"))
        busan_resp = _make_response(200, _kma_response(22.5, "159"))

        with patch("src.data.collectors.amos.fetch", side_effect=[seoul_resp, busan_resp]):
            collector.poll()

        rows = db.get_observations("Busan", since="2000-01-01")
        assert len(rows) == 1
        assert rows[0]["temp_native"] == pytest.approx(22.5)
        assert rows[0]["source"] == "amos"

    def test_temp_f_conversion_via_kma(self, monkeypatch):
        """°C→°F conversion is correct for KMA path."""
        monkeypatch.setenv("KMA_API_KEY", "test-key-123")
        db = _db()
        collector = AmosCollector(db)

        # 20°C = 68°F
        seoul_resp = _make_response(200, _kma_response(20.0, "112"))
        busan_resp = _make_response(200, _kma_response(20.0, "159"))

        with patch("src.data.collectors.amos.fetch", side_effect=[seoul_resp, busan_resp]):
            collector.poll()

        rows = db.get_observations("Seoul", since="2000-01-01")
        assert rows[0]["temp_f"] == pytest.approx(68.0, rel=1e-4)


# ---------------------------------------------------------------------------
# Open-Meteo fallback (no KMA_API_KEY)
# ---------------------------------------------------------------------------

class TestOpenMeteoFallback:
    def test_fallback_when_no_api_key(self, monkeypatch):
        """Without KMA_API_KEY, falls back to Open-Meteo for both stations."""
        monkeypatch.delenv("KMA_API_KEY", raising=False)
        db = _db()
        collector = AmosCollector(db)
        assert collector._api_key is None

        seoul_om = _make_response(200, _open_meteo_response(26.0))
        busan_om = _make_response(200, _open_meteo_response(24.0))

        with patch("src.data.collectors.amos.fetch", side_effect=[seoul_om, busan_om]):
            results = collector.poll()

        assert results["Seoul"] is True
        assert results["Busan"] is True

        rows_s = db.get_observations("Seoul", since="2000-01-01")
        rows_b = db.get_observations("Busan", since="2000-01-01")
        assert len(rows_s) == 1
        assert len(rows_b) == 1
        assert rows_s[0]["source"] == "amos"
        assert rows_b[0]["source"] == "amos"

    def test_fallback_when_kma_fails(self, monkeypatch):
        """When KMA errors, Open-Meteo fallback is used."""
        monkeypatch.setenv("KMA_API_KEY", "test-key")
        db = _db()
        collector = AmosCollector(db)

        kma_error = Exception("KMA down")
        seoul_om = _make_response(200, _open_meteo_response(27.0))
        busan_om = _make_response(200, _open_meteo_response(25.0))

        with patch("src.data.collectors.amos.fetch", side_effect=[kma_error, seoul_om, kma_error, busan_om]):
            results = collector.poll()

        assert results["Seoul"] is True
        assert results["Busan"] is True


# ---------------------------------------------------------------------------
# KMA ASOS "previous-day only" response (issue #694)
# ---------------------------------------------------------------------------

class TestKmaHistoricalOnlyResponse:
    """AsosHourlyInfoService always returns resultCode=99 for same-day requests.

    This is expected, permanent behavior (the archive-only endpoint), not an
    unexpected parse failure -- it must be handled explicitly and quietly
    (DEBUG, not ERROR), and must still fall back to Open-Meteo.
    """

    def test_falls_back_to_open_meteo_on_resultcode_99(self, monkeypatch):
        """resultCode=99 is treated as an expected non-error and falls back cleanly."""
        monkeypatch.setenv("KMA_API_KEY", "test-key")
        db = _db()
        collector = AmosCollector(db)

        kma_seoul = _make_response(200, _kma_result_code_response("99"))
        kma_busan = _make_response(200, _kma_result_code_response("99"))
        seoul_om = _make_response(200, _open_meteo_response(25.5))
        busan_om = _make_response(200, _open_meteo_response(23.5))

        with patch(
            "src.data.collectors.amos.fetch",
            side_effect=[kma_seoul, seoul_om, kma_busan, busan_om],
        ):
            results = collector.poll()

        assert results["Seoul"] is True
        assert results["Busan"] is True

        rows_s = db.get_observations("Seoul", since="2000-01-01")
        assert len(rows_s) == 1
        assert rows_s[0]["is_official"] == 0  # Open-Meteo fallback, not official AMOS

    def test_resultcode_99_does_not_log_error(self, monkeypatch, caplog):
        """resultCode=99 must NOT produce an ERROR-level log (issue #694 — was a KeyError/ERROR before)."""
        monkeypatch.setenv("KMA_API_KEY", "test-key")
        db = _db()
        collector = AmosCollector(db)

        kma_resp = _make_response(200, _kma_result_code_response("99"))

        with caplog.at_level(logging.DEBUG, logger="src.data.collectors.amos"):
            with patch("src.data.collectors.amos.fetch", return_value=kma_resp):
                reading = collector._fetch_kma("Seoul")

        assert reading is None
        assert not any(r.levelname == "ERROR" for r in caplog.records)
        assert any(
            "historical-only" in r.message or "resultCode=99" in r.message
            for r in caplog.records
        )

    def test_unexpected_result_code_still_logs_warning(self, monkeypatch, caplog):
        """A non-99, non-00 resultCode is a genuinely new failure mode and should stay visible."""
        monkeypatch.setenv("KMA_API_KEY", "test-key")
        db = _db()
        collector = AmosCollector(db)

        kma_resp = _make_response(200, _kma_result_code_response("30", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"))

        with caplog.at_level(logging.WARNING, logger="src.data.collectors.amos"):
            with patch("src.data.collectors.amos.fetch", return_value=kma_resp):
                reading = collector._fetch_kma("Seoul")

        assert reading is None
        assert any(r.levelname == "WARNING" for r in caplog.records)


# ---------------------------------------------------------------------------
# Station independence
# ---------------------------------------------------------------------------

class TestStationIndependence:
    def test_busan_processed_even_if_seoul_fails(self, monkeypatch):
        """Error in Seoul does NOT prevent Busan from being processed."""
        monkeypatch.delenv("KMA_API_KEY", raising=False)
        db = _db()
        collector = AmosCollector(db)

        # Seoul Open-Meteo fails, Busan Open-Meteo succeeds
        seoul_err = Exception("Seoul unavailable")
        busan_ok = _make_response(200, _open_meteo_response(21.0))

        with patch("src.data.collectors.amos.fetch", side_effect=[seoul_err, busan_ok]):
            results = collector.poll()

        assert results["Seoul"] is False
        assert results["Busan"] is True

        busan_rows = db.get_observations("Busan", since="2000-01-01")
        assert len(busan_rows) == 1

    def test_seoul_processed_even_if_busan_fails(self, monkeypatch):
        """Error in Busan does NOT prevent Seoul from being processed."""
        monkeypatch.delenv("KMA_API_KEY", raising=False)
        db = _db()
        collector = AmosCollector(db)

        seoul_ok = _make_response(200, _open_meteo_response(28.0))
        busan_err = Exception("Busan unavailable")

        with patch("src.data.collectors.amos.fetch", side_effect=[seoul_ok, busan_err]):
            results = collector.poll()

        assert results["Seoul"] is True
        assert results["Busan"] is False

        seoul_rows = db.get_observations("Seoul", since="2000-01-01")
        assert len(seoul_rows) == 1


# ---------------------------------------------------------------------------
# Staleness check
# ---------------------------------------------------------------------------

class TestStaleness:
    def test_critical_logged_when_stale(self, caplog):
        """CRITICAL logged when station data is older than 2× cadence_min."""
        db = _db()
        collector = AmosCollector(db)
        collector._cadence_min = 10
        collector._last_obs_ts["Seoul"] = datetime.now(timezone.utc) - timedelta(minutes=25)

        with caplog.at_level(logging.CRITICAL, logger="src.data.collectors.amos"):
            collector._check_staleness("Seoul")

        assert any(r.levelname == "CRITICAL" for r in caplog.records)
        assert any("Seoul" in r.message for r in caplog.records)

    def test_no_critical_when_fresh(self, caplog):
        """No CRITICAL when data is within 2× cadence_min."""
        db = _db()
        collector = AmosCollector(db)
        collector._cadence_min = 10
        collector._last_obs_ts["Busan"] = datetime.now(timezone.utc) - timedelta(minutes=5)

        with caplog.at_level(logging.CRITICAL, logger="src.data.collectors.amos"):
            collector._check_staleness("Busan")

        assert not any(r.levelname == "CRITICAL" for r in caplog.records)

    def test_no_critical_on_first_poll(self, caplog):
        """No CRITICAL when last_obs_ts is None (first poll)."""
        db = _db()
        collector = AmosCollector(db)

        with caplog.at_level(logging.CRITICAL, logger="src.data.collectors.amos"):
            collector._check_staleness("Seoul")

        assert not any(r.levelname == "CRITICAL" for r in caplog.records)


# ---------------------------------------------------------------------------
# Cadence env var
# ---------------------------------------------------------------------------

class TestCadenceEnvVar:
    def test_cadence_from_env(self, monkeypatch):
        """AMOS_CADENCE_MINUTES env var is applied."""
        monkeypatch.setenv("AMOS_CADENCE_MINUTES", "5")
        monkeypatch.delenv("KMA_API_KEY", raising=False)
        db = _db()
        collector = AmosCollector(db)
        assert collector._cadence_min == 5

        om_resp = _make_response(200, _open_meteo_response(20.0))
        with patch("src.data.collectors.amos.fetch", side_effect=[om_resp, om_resp]):
            collector.poll()

        rows = db.get_observations("Seoul", since="2000-01-01")
        assert rows[0]["cadence_min"] == 5

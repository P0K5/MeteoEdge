"""Integration tests for observation adapters (Issue #112).

Verifies that each adapter (jma_ameidas, amos, mss) correctly:
1. Fetches and stores observations with the required schema
2. Integrates with FreshnessMonitor for staleness detection
3. Matches the source priority configuration
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.config import get_source_priority
from src.data.collectors.jma_ameidas import JmaAmedasCollector
from src.data.collectors.amos import AmosCollector
from src.data.collectors.mss import MssCollector
from src.data.db import Database
from src.data.freshness_monitor import FreshnessMonitor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db(tmp_path) -> Database:
    """Database with cadence_min/is_official migration applied."""
    db = Database(str(tmp_path / "test.db"))
    return db


def _make_resp(status_code: int = 200, json_data: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    if json_data is not None:
        r.json.return_value = json_data
    else:
        r.json.side_effect = ValueError("no JSON")
    return r


# ---------------------------------------------------------------------------
# JMA AMeDAS integration
# ---------------------------------------------------------------------------

class TestJmaAmedasAdapterIntegration:

    def test_jma_stores_observation_with_correct_schema(self, tmp_path):
        """JMA adapter stores observations with all required columns."""
        db = _db(tmp_path)
        collector = JmaAmedasCollector(db)

        jma_resp = _make_resp(200, {"090000": {"temp": [22.5, 0], "wind": [3.2, 0]}})
        with patch("src.data.collectors.jma_ameidas.fetch", return_value=jma_resp):
            result = collector.poll()

        assert result is True
        obs = db.get_observations("Tokyo", since="2000-01-01")
        assert len(obs) > 0

        row = obs[-1]
        assert row["source"] == "jma_ameidas"
        assert row["station"] == "Tokyo"
        assert row["unit"] == "C"
        assert row["is_official"] == 1
        assert row["cadence_min"] == 10
        assert isinstance(row["temp_f"], float)
        datetime.fromisoformat(row["ts"])

    def test_jma_cadence_configured(self):
        """JMA appears in source priority config with correct cadence."""
        sources = get_source_priority("Tokyo")
        jma = next((s for s in sources if s["source"] == "jma_ameidas"), None)
        assert jma is not None
        assert jma["station"] == "Tokyo"
        assert jma["cadence_min"] == 10
        assert jma["is_official"] is True

    def test_jma_freshness_integration(self, tmp_path):
        """Fresh JMA observation passes freshness check."""
        db = _db(tmp_path)
        monitor = FreshnessMonitor()

        now = datetime.now(timezone.utc)
        db.insert_observation(
            ts=now.isoformat(),
            station="Tokyo",
            temp_f=72.0,
            temp_native=22.2,
            unit="C",
            source="jma_ameidas",
        )

        assert monitor.check(db, "jma_ameidas", "Tokyo", cadence_min=10) is True


# ---------------------------------------------------------------------------
# AMOS integration
# ---------------------------------------------------------------------------

class TestAmosAdapterIntegration:

    def test_amos_stores_observation_with_correct_schema(self, tmp_path, monkeypatch):
        """AMOS adapter stores observations with all required columns."""
        monkeypatch.delenv("KMA_API_KEY", raising=False)
        db = _db(tmp_path)
        collector = AmosCollector(db)

        # Open-Meteo fallback response (no KMA key)
        now_utc = datetime.now(timezone.utc)
        past_hour = now_utc.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        om_resp = _make_resp(200, {
            "hourly": {
                "time": [past_hour.isoformat()],
                "temperature_2m": [20.5],
            }
        })

        with patch("src.data.collectors.amos.fetch", side_effect=[om_resp, om_resp]):
            result = collector.poll()

        # poll() returns {station: bool} dict
        assert result.get("Seoul") is True or result.get("Busan") is True

        obs = db.get_observations("Seoul", since="2000-01-01")
        assert len(obs) > 0

        row = obs[-1]
        assert row["source"] == "amos"
        assert row["station"] == "Seoul"
        assert row["unit"] == "C"
        assert row["is_official"] == 1
        assert isinstance(row["temp_f"], float)
        datetime.fromisoformat(row["ts"])

    def test_amos_cadence_configured(self):
        """AMOS appears in source priority config with correct cadence."""
        sources = get_source_priority("Seoul")
        amos = next((s for s in sources if s["source"] == "amos"), None)
        assert amos is not None
        assert amos["station"] == "Seoul"
        assert amos["cadence_min"] == 15
        assert amos["is_official"] is True

    def test_amos_freshness_integration_stale(self, tmp_path):
        """Stale AMOS observation fails freshness check."""
        db = _db(tmp_path)
        monitor = FreshnessMonitor()

        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=35)).isoformat()
        db.insert_observation(
            ts=old_ts,
            station="Seoul",
            temp_f=68.0,
            temp_native=20.0,
            unit="C",
            source="amos",
        )

        # cadence_min=15, threshold=30 min — 35 min old is stale
        assert monitor.check(db, "amos", "Seoul", cadence_min=15) is False


# ---------------------------------------------------------------------------
# MSS integration
# ---------------------------------------------------------------------------

class TestMssAdapterIntegration:

    def test_mss_stores_observation_with_correct_schema(self, tmp_path):
        """MSS adapter stores observations with all required columns."""
        db = _db(tmp_path)
        collector = MssCollector(db)

        mss_resp = _make_resp(200, {
            "items": [{
                "timestamp": "2026-06-07T10:30:00+08:00",
                "readings": [{"station_id": "S24", "value": 27.5}],
            }]
        })

        with patch("src.data.collectors.mss.fetch", return_value=mss_resp):
            result = collector.poll()

        assert result is True
        obs = db.get_observations("Singapore", since="2000-01-01")
        assert len(obs) > 0

        row = obs[-1]
        assert row["source"] == "mss"
        assert row["station"] == "Singapore"
        assert row["unit"] == "C"
        assert row["is_official"] == 1
        assert row["cadence_min"] == 1
        assert isinstance(row["temp_f"], float)
        datetime.fromisoformat(row["ts"])

    def test_mss_cadence_configured(self):
        """MSS appears in source priority config with cadence_min=1."""
        sources = get_source_priority("Singapore")
        mss = next((s for s in sources if s["source"] == "mss"), None)
        assert mss is not None
        assert mss["station"] == "Singapore"
        assert mss["cadence_min"] == 1
        assert mss["is_official"] is True

    def test_mss_freshness_integration_fresh(self, tmp_path):
        """Fresh MSS observation passes freshness check."""
        db = _db(tmp_path)
        monitor = FreshnessMonitor()

        now = datetime.now(timezone.utc)
        db.insert_observation(
            ts=now.isoformat(),
            station="Singapore",
            temp_f=81.5,
            temp_native=27.5,
            unit="C",
            source="mss",
        )

        # cadence_min=1, threshold=2 min — just inserted, so fresh
        assert monitor.check(db, "mss", "Singapore", cadence_min=1) is True


# ---------------------------------------------------------------------------
# Source priority integration
# ---------------------------------------------------------------------------

class TestSourcePriorityIntegration:

    def test_all_cities_have_source_priority(self):
        """All required cities have at least one source configured."""
        for city in ["Tokyo", "Seoul", "Busan", "Singapore"]:
            sources = get_source_priority(city)
            assert len(sources) > 0, f"No sources for {city}"

    def test_tokyo_jma_is_first(self):
        assert get_source_priority("Tokyo")[0]["source"] == "jma_ameidas"

    def test_seoul_amos_is_first(self):
        assert get_source_priority("Seoul")[0]["source"] == "amos"

    def test_singapore_mss_is_first(self):
        assert get_source_priority("Singapore")[0]["source"] == "mss"

    def test_all_sources_have_required_fields(self):
        for city in ["Tokyo", "Seoul", "Busan", "Singapore"]:
            for source in get_source_priority(city):
                assert "source" in source
                assert "station" in source
                assert "cadence_min" in source
                assert "is_official" in source
                assert isinstance(source["cadence_min"], int)
                assert source["cadence_min"] > 0


# ---------------------------------------------------------------------------
# FreshnessMonitor cross-adapter integration
# ---------------------------------------------------------------------------

class TestFreshnessMonitorWithAdapters:

    def test_check_all_with_multiple_sources(self, tmp_path):
        """check_all reports freshness correctly for multiple sources."""
        db = _db(tmp_path)
        monitor = FreshnessMonitor()

        now = datetime.now(timezone.utc)

        db.insert_observation(
            ts=(now - timedelta(minutes=5)).isoformat(),
            station="Tokyo",
            temp_f=72.0,
            temp_native=22.2,
            unit="C",
            source="jma_ameidas",
        )

        db.insert_observation(
            ts=(now - timedelta(minutes=35)).isoformat(),
            station="Seoul",
            temp_f=68.0,
            temp_native=20.0,
            unit="C",
            source="amos",
        )

        sources = get_source_priority("Tokyo")
        result = monitor.check_all(db, sources)

        assert result.get("jma_ameidas/Tokyo") is True

    def test_stale_source_reported_false(self, tmp_path):
        """Source with data older than 2× cadence is reported False."""
        db = _db(tmp_path)
        monitor = FreshnessMonitor()

        old_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        db.insert_observation(
            ts=old_ts,
            station="Tokyo",
            temp_f=70.0,
            temp_native=21.1,
            unit="C",
            source="jma_ameidas",
        )

        result = monitor.check(db, "jma_ameidas", "Tokyo", cadence_min=10)
        assert result is False

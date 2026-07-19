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

        jma_resp = _make_resp(200, {"20260620090000": {"temp": [22.5, 0], "wind": [3.2, 0]}})
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
# AMOS integration -- retired, issue #740
# ---------------------------------------------------------------------------

class TestAmosAdapterIntegration:
    """Issue #740: AmosCollector is retired (no-op). METAR (RKSI/RKPK) is now
    the sole, primary Korea observation source in source_priority.yaml."""

    def test_amos_collector_is_a_noop(self, tmp_path, monkeypatch):
        """poll() must not write any observation rows -- no HTTP calls, no
        DB writes, regardless of KMA_API_KEY."""
        monkeypatch.delenv("KMA_API_KEY", raising=False)
        db = _db(tmp_path)
        collector = AmosCollector(db)

        result = collector.poll()

        assert result == {"Seoul": False, "Busan": False}
        assert db.get_observations("Seoul", since="2000-01-01") == []
        assert db.get_observations("Busan", since="2000-01-01") == []

    def test_amos_no_longer_in_source_priority(self):
        """amos must not appear in source_priority.yaml for Seoul or Busan --
        METAR is now the sole, primary source (issue #740)."""
        for city in ("Seoul", "Busan"):
            sources = get_source_priority(city)
            amos = next((s for s in sources if s["source"] == "amos"), None)
            assert amos is None, f"{city} still has an amos source_priority entry"

    def test_metar_is_primary_korea_source(self):
        """METAR (RKSI/RKPK) is configured as the (sole) official source for
        Seoul/Busan, which also keeps the intraday-correction entry alive."""
        seoul = get_source_priority("Seoul")
        assert any(
            s["source"] == "metar" and s["station"] == "RKSI" and s["is_official"] is True
            for s in seoul
        )
        busan = get_source_priority("Busan")
        assert any(
            s["source"] == "metar" and s["station"] == "RKPK" and s["is_official"] is True
            for s in busan
        )

    def test_amos_freshness_check_no_longer_expected(self, tmp_path):
        """With no amos entry in source_priority.yaml, run.py's freshness loop
        (which iterates get_source_priority(city)) no longer checks amos/Seoul
        or amos/Busan at all -- confirmed here indirectly: an amos observation
        that WOULD be stale is simply absent from the active_sources list a
        caller would build from get_source_priority("Seoul")."""
        sources = get_source_priority("Seoul")
        assert all(s["source"] != "amos" for s in sources)


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

    def test_seoul_metar_is_first(self):
        """Issue #740: amos is retired -- METAR (RKSI) is Seoul's sole source."""
        assert get_source_priority("Seoul")[0]["source"] == "metar"

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

        # jma_ameidas threshold: 60 seconds, fresh at 30 seconds
        db.insert_observation(
            ts=(now - timedelta(seconds=30)).isoformat(),
            station="Tokyo",
            temp_f=72.0,
            temp_native=22.2,
            unit="C",
            source="jma_ameidas",
        )

        # amos threshold: 90 seconds, stale at 180 seconds
        db.insert_observation(
            ts=(now - timedelta(seconds=180)).isoformat(),
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


# ---------------------------------------------------------------------------
# Fallback labeling and retry behavior
# ---------------------------------------------------------------------------

class TestFallbackLabeling:

    def test_jma_fallback_stores_is_official_zero(self, tmp_path):
        """When JMA 404s (both hours) and Open-Meteo is used, row is is_official=0."""
        db = _db(tmp_path)
        collector = JmaAmedasCollector(db)

        now_utc = datetime.now(timezone.utc)
        past_hour = now_utc.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        resp_404 = _make_resp(404, {})
        om_resp = _make_resp(200, {
            "hourly": {
                "time": [past_hour.isoformat()],
                "temperature_2m": [21.0],
            }
        })

        with patch(
            "src.data.collectors.jma_ameidas.fetch",
            side_effect=[resp_404, resp_404, resp_404, om_resp],
        ):
            result = collector.poll()

        assert result is True
        obs = db.get_observations("Tokyo", since="2000-01-01")
        row = obs[-1]
        assert row["source"] == "jma_ameidas"
        assert row["is_official"] == 0

    def test_jma_official_path_stores_is_official_one(self, tmp_path):
        """When the AMeDAS file is available, row keeps is_official=1."""
        db = _db(tmp_path)
        collector = JmaAmedasCollector(db)

        jma_resp = _make_resp(200, {"20260620090000": {"temp": [22.5, 0]}})
        with patch("src.data.collectors.jma_ameidas.fetch", return_value=jma_resp):
            result = collector.poll()

        assert result is True
        obs = db.get_observations("Tokyo", since="2000-01-01")
        assert obs[-1]["is_official"] == 1


class TestJmaPreviousHourRetry:

    @patch("src.data.collectors.jma_ameidas.datetime")
    def test_404_on_current_bucket_retries_previous_bucket(self, mock_datetime, tmp_path):
        """A 404 on the current 3-hour bucket file (post-#739) triggers a retry
        that reaches a different, earlier bucket. Clock is frozen near a bucket
        boundary (09:05 JST) so the retry deterministically crosses from the
        09:00 bucket to the 06:00 bucket."""
        db = _db(tmp_path)
        collector = JmaAmedasCollector(db)

        now_jst = datetime(2026, 6, 20, 9, 5, 0, tzinfo=timezone(timedelta(hours=9)))
        mock_datetime.now.return_value = now_jst
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        resp_404 = _make_resp(404, {})
        jma_resp = _make_resp(200, {"20260620065000": {"temp": [19.5, 0]}})

        fetched_urls: list[str] = []

        def _fake_fetch(url, **kwargs):
            fetched_urls.append(url)
            return resp_404 if "20260620_09.json" in url else jma_resp

        with patch("src.data.collectors.jma_ameidas.fetch", side_effect=_fake_fetch):
            result = collector.poll()

        assert result is True
        # current bucket (09) 404s; the fallback chain reaches the earlier 06 bucket
        assert any("20260620_09.json" in u for u in fetched_urls)
        assert any("20260620_06.json" in u for u in fetched_urls)
        obs = db.get_observations("Tokyo", since="2000-01-01")
        assert obs[-1]["is_official"] == 1


class TestMssWarnOnStateChange:

    def _resp_without_preferred(self):
        return _make_resp(200, {
            "items": [{
                "timestamp": "2026-06-11T15:30:00+08:00",
                "readings": [{"station_id": "S107", "value": 28.0}],
            }]
        })

    def test_missing_station_warned_only_once(self, tmp_path, caplog):
        """Repeated polls with the same missing stations warn only once."""
        import logging as _logging
        db = _db(tmp_path)
        collector = MssCollector(db)

        with patch("src.data.collectors.mss.fetch", return_value=self._resp_without_preferred()):
            with caplog.at_level(_logging.WARNING, logger="src.data.collectors.mss"):
                collector.poll()
                collector.poll()
                collector.poll()

        s24_warnings = [r for r in caplog.records if "S24 not in readings" in r.message]
        assert len(s24_warnings) == 1

    def test_recovery_logged_when_station_returns(self, tmp_path, caplog):
        """When a previously missing station reappears, an info recovery log fires."""
        import logging as _logging
        db = _db(tmp_path)
        collector = MssCollector(db)

        resp_with_s24 = _make_resp(200, {
            "items": [{
                "timestamp": "2026-06-11T15:31:00+08:00",
                "readings": [{"station_id": "S24", "value": 29.0}],
            }]
        })

        with caplog.at_level(_logging.INFO, logger="src.data.collectors.mss"):
            with patch("src.data.collectors.mss.fetch", return_value=self._resp_without_preferred()):
                collector.poll()
            with patch("src.data.collectors.mss.fetch", return_value=resp_with_s24):
                collector.poll()

        assert any("S24 back in readings" in r.message for r in caplog.records)

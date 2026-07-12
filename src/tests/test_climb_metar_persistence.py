"""Tests for issue #669: decouple climb-table METAR persistence from STATION_ACTIVE_HOURS.

Critical invariants:
1. persist_metar_for_climb_stations() must persist METAR regardless of local hour
   or STATION_ACTIVE_HOURS -- it has no active-hours gate by design.
2. It must be idempotent: an unchanged METAR timestamp does not insert a duplicate row.
3. One station's fetch/parse failure must not prevent other stations from being persisted
   (same isolation guarantee used elsewhere in this codebase, e.g. AmosCollector.poll()).
4. It must default to config.CLIMB_BUILDER_24H_METAR_STATIONS when no explicit
   station list is given, and must no-op cleanly without a db.
"""
from datetime import datetime, timezone
from unittest.mock import patch

from src.data.db import Database


_RKSI_METAR = [{"temp": 22.0, "reportTime": "2026-06-24T02:00:00+00:00"}]
_ZGSZ_METAR = [{"temp": 26.0, "reportTime": "2026-06-24T01:30:00+00:00"}]


def _db() -> Database:
    return Database(":memory:")


class TestPersistMetarForClimbStationsNoGate:
    def test_persists_regardless_of_active_hours(self):
        """RKSI's active window starts at local 11:00 -- this must still persist
        a 02:00 UTC (~11:00 KST, still pre-window on some days) METAR."""
        from src.weather.builder import persist_metar_for_climb_stations

        db = _db()
        station_tuple = ("RKSI", 37.4602, 126.4407, "Seoul")

        with patch("src.weather.builder.fetch_all_metars_today", return_value=_RKSI_METAR):
            results = persist_metar_for_climb_stations(stations=[station_tuple], db=db)

        assert results["RKSI"] is True
        rows = db.get_observations("RKSI", since="2000-01-01")
        assert len(rows) == 1
        assert rows[0]["source"] == "metar"
        assert abs(rows[0]["temp_f"] - 71.6) < 0.1  # 22C -> 71.6F

    def test_does_not_import_or_check_station_active_hours(self):
        """Sanity check the function signature has no active-hours dependency:
        calling it with a station tuple that has no STATION_ACTIVE_HOURS entry
        at all must not raise."""
        from src.weather.builder import persist_metar_for_climb_stations

        db = _db()
        unknown_station = ("ZZZZ", 0.0, 0.0, "Nowhere")

        with patch("src.weather.builder.fetch_all_metars_today", return_value=_RKSI_METAR):
            results = persist_metar_for_climb_stations(stations=[unknown_station], db=db)

        assert results["ZZZZ"] is True


class TestPersistMetarForClimbStationsIdempotent:
    def test_duplicate_timestamp_does_not_insert_again(self):
        from src.weather.builder import persist_metar_for_climb_stations

        db = _db()
        station_tuple = ("RKSI", 37.4602, 126.4407, "Seoul")

        with patch("src.weather.builder.fetch_all_metars_today", return_value=_RKSI_METAR):
            first = persist_metar_for_climb_stations(stations=[station_tuple], db=db)
            second = persist_metar_for_climb_stations(stations=[station_tuple], db=db)

        assert first["RKSI"] is True
        assert second["RKSI"] is False
        rows = db.get_observations("RKSI", since="2000-01-01")
        assert len(rows) == 1


class TestPersistMetarForClimbStationsIsolation:
    def test_one_station_failure_does_not_block_others(self):
        from src.weather.builder import persist_metar_for_climb_stations

        db = _db()
        rksi = ("RKSI", 37.4602, 126.4407, "Seoul")
        zgsz = ("ZGSZ", 22.6393, 113.8108, "Shenzhen")

        def _fake_fetch(station):
            if station == "RKSI":
                raise RuntimeError("aviationweather.gov timeout")
            return _ZGSZ_METAR

        with patch("src.weather.builder.fetch_all_metars_today", side_effect=_fake_fetch):
            results = persist_metar_for_climb_stations(stations=[rksi, zgsz], db=db)

        assert results["RKSI"] is False
        assert results["ZGSZ"] is True
        assert len(db.get_observations("ZGSZ", since="2000-01-01")) == 1

    def test_missing_metar_data_returns_false_not_raise(self):
        from src.weather.builder import persist_metar_for_climb_stations

        db = _db()
        station_tuple = ("RKSI", 37.4602, 126.4407, "Seoul")

        with patch("src.weather.builder.fetch_all_metars_today", return_value=[]):
            results = persist_metar_for_climb_stations(stations=[station_tuple], db=db)

        assert results["RKSI"] is False


class TestPersistMetarForClimbStationsDefaults:
    def test_returns_empty_dict_without_db(self):
        from src.weather.builder import persist_metar_for_climb_stations

        assert persist_metar_for_climb_stations(stations=[("RKSI", 0, 0, "Seoul")], db=None) == {}

    def test_defaults_to_climb_builder_24h_metar_stations(self):
        from src.config import CLIMB_BUILDER_24H_METAR_STATIONS
        from src.weather.builder import persist_metar_for_climb_stations

        db = _db()
        with patch("src.weather.builder.fetch_all_metars_today", return_value=_RKSI_METAR):
            results = persist_metar_for_climb_stations(db=db)

        assert set(results.keys()) == CLIMB_BUILDER_24H_METAR_STATIONS
        assert CLIMB_BUILDER_24H_METAR_STATIONS == {
            "RKSI", "RKPK", "ZGSZ", "ZGGG", "ZHHH", "ZHCC", "ZSPD",
        }


class TestPersistMetarForClimbStationsSharesCache:
    def test_reuses_metars_cache_no_double_fetch(self):
        """When metars_cache already has the station (fetched by the scanner
        builder earlier this poll), persist_metar_for_climb_stations must not
        issue a second HTTP fetch (issue #582 sharing pattern)."""
        from src.weather.builder import persist_metar_for_climb_stations

        db = _db()
        station_tuple = ("RKSI", 37.4602, 126.4407, "Seoul")
        shared_cache = {"RKSI": _RKSI_METAR}

        with patch("src.weather.builder.fetch_all_metars_today") as mock_fetch:
            results = persist_metar_for_climb_stations(
                stations=[station_tuple], db=db, metars_cache=shared_cache,
            )

        mock_fetch.assert_not_called()
        assert results["RKSI"] is True

"""Tests for the METAR fetch skip-list (issue #732).

Chronically-dead upstream feeds (e.g. ZSJN/Jinan on aviationweather.gov) must
short-circuit fetch_metar()/fetch_all_metars_today() so the bot stops making
wasted HTTP calls and logging repetitive JSON-parse errors. Skipping returns
the same empty outcome the previous error path produced, minus the noise and
the network round-trip.
"""
from unittest.mock import patch

from src.config import METAR_SKIP_STATIONS
from src.data import metar


class TestMetarSkipStations:
    def test_zsjn_in_default_skip_set(self):
        """ZSJN ships in the default skip set (issue #732)."""
        assert "ZSJN" in METAR_SKIP_STATIONS

    def test_fetch_metar_skips_without_http_call(self):
        """A skip-listed station returns None and never hits the network."""
        with patch("src.data.metar.fetch") as mock_fetch, \
                patch.object(metar, "METAR_SKIP_STATIONS", frozenset({"ZSJN"})):
            result = metar.fetch_metar("ZSJN")
        assert result is None
        mock_fetch.assert_not_called()

    def test_fetch_all_metars_today_skips_without_http_call(self):
        """A skip-listed station returns [] and never hits the network."""
        with patch("src.data.metar.fetch") as mock_fetch, \
                patch.object(metar, "METAR_SKIP_STATIONS", frozenset({"ZSJN"})):
            result = metar.fetch_all_metars_today("ZSJN")
        assert result == []
        mock_fetch.assert_not_called()

    def test_non_skip_station_still_fetches(self):
        """A station not in the skip set must still perform the HTTP fetch."""
        class _Resp:
            def json(self):
                return [{"temp": 25.0}]

        with patch("src.data.metar.fetch", return_value=_Resp()) as mock_fetch, \
                patch.object(metar, "METAR_SKIP_STATIONS", frozenset({"ZSJN"})):
            result = metar.fetch_all_metars_today("KORD")
        assert result == [{"temp": 25.0}]
        mock_fetch.assert_called_once()

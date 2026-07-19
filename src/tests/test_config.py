"""Tests for src/config.py source priority configuration."""
import pytest

from src.config import get_source_priority, is_training_eligible


class TestGetSourcePriority:
    """Test source priority config loader."""

    def test_get_source_priority_tokyo(self):
        """Tokyo should have jma_ameidas as first priority."""
        sources = get_source_priority("Tokyo")
        assert len(sources) > 0
        assert sources[0]["source"] == "jma_ameidas"
        assert sources[0]["station"] == "Tokyo"
        assert sources[0]["cadence_min"] == 10
        assert sources[0]["is_official"] is True

    def test_get_source_priority_seoul(self):
        """Issue #740: amos is retired -- Seoul's sole source is METAR (RKSI)."""
        sources = get_source_priority("Seoul")
        assert len(sources) > 0
        assert sources[0]["source"] == "metar"
        assert sources[0]["station"] == "RKSI"
        assert sources[0]["cadence_min"] == 30
        assert all(s["source"] != "amos" for s in sources)

    def test_get_source_priority_busan(self):
        """Issue #740: amos is retired -- Busan's sole source is METAR (RKPK)."""
        sources = get_source_priority("Busan")
        assert len(sources) > 0
        assert sources[0]["source"] == "metar"
        assert sources[0]["station"] == "RKPK"
        assert all(s["source"] != "amos" for s in sources)

    def test_get_source_priority_singapore(self):
        """Singapore should have mss as first priority."""
        sources = get_source_priority("Singapore")
        assert len(sources) > 0
        assert sources[0]["source"] == "mss"
        assert sources[0]["station"] == "Singapore"
        assert sources[0]["cadence_min"] == 1
        assert sources[0]["is_official"] is True

    def test_get_source_priority_returns_list(self):
        """Each city should return a non-empty list of dicts."""
        for city in ["Tokyo", "Seoul", "Busan", "Singapore"]:
            sources = get_source_priority(city)
            assert isinstance(sources, list)
            assert len(sources) > 0
            for source in sources:
                assert isinstance(source, dict)
                assert "source" in source
                assert "station" in source
                assert "cadence_min" in source
                assert "is_official" in source

    def test_get_source_priority_nonexistent_city(self):
        """Nonexistent cities should return an empty list."""
        sources = get_source_priority("NonExistentCity")
        assert sources == []

    def test_get_source_priority_caching(self):
        """Function should be cached (LRU cache)."""
        # Call twice and verify cache is working
        sources1 = get_source_priority("Tokyo")
        sources2 = get_source_priority("Tokyo")
        # Should be the same object (cached)
        assert sources1 is sources2


class TestIsTrainingEligible:
    """Tests for issue #558: is_training_eligible() per-city accessor."""

    EXCLUDED_CITIES = ["Jinan", "Shenzhen", "Wuhan", "Zhengzhou"]

    def test_excluded_cities_are_ineligible(self):
        """The 4 cities flagged in the 2026-07-01 audit must be ineligible."""
        for city in self.EXCLUDED_CITIES:
            assert is_training_eligible(city) is False, (
                f"{city} should be training_eligible=False"
            )

    def test_other_cities_are_eligible(self):
        """Cities without an explicit training_eligible: false flag default to True."""
        for city in ["Tokyo", "Seoul", "Busan", "Singapore", "Chicago", "Miami"]:
            assert is_training_eligible(city) is True, (
                f"{city} should default to training_eligible=True"
            )

    def test_city_with_no_source_priority_entry_is_eligible(self):
        """Cities absent from source_priority.yaml entirely default to eligible."""
        assert is_training_eligible("NonExistentCity") is True


class TestArchiveShadowStations:
    """Tests for issue #274: archive shadow candidates ported to src/config.py."""

    ARCHIVE_ICAOS = [
        # Europe
        "EGLC", "LFPB", "LIMC", "EFHK", "EPWA", "LTFM", "LTAC",
        # Asia / Pacific
        "RJTT", "RCSS", "ZSPD", "ZGGG", "ZHHH", "ZSJN", "ZHCC", "RPLL",
        # MENA
        "LLBG", "OEJN",
        # Latin America
        "SBGR",
        # Oceania
        "NZWN",
    ]

    def _station_icaos(self):
        from src.config import STATIONS
        return [s[0] for s in STATIONS]

    def test_all_archive_icaos_present_in_stations(self):
        """Every archive candidate ICAO must be present in STATIONS."""
        from src.config import STATIONS
        icaos = {s[0] for s in STATIONS}
        missing = [code for code in self.ARCHIVE_ICAOS if code not in icaos]
        assert missing == [], f"Archive ICAOs missing from STATIONS: {missing}"

    def test_no_duplicate_icaos_in_stations(self):
        """STATIONS must not contain duplicate ICAO codes."""
        from src.config import STATIONS
        icaos = [s[0] for s in STATIONS]
        dups = [code for code in set(icaos) if icaos.count(code) > 1]
        assert dups == [], f"Duplicate ICAO codes found in STATIONS: {dups}"

    def test_all_archive_icaos_have_active_hours(self):
        """Every archive ICAO must have an entry in STATION_ACTIVE_HOURS."""
        from src.config import STATION_ACTIVE_HOURS
        missing = [
            code for code in self.ARCHIVE_ICAOS
            if code not in STATION_ACTIVE_HOURS
        ]
        assert missing == [], (
            f"Archive ICAOs missing from STATION_ACTIVE_HOURS: {missing}"
        )

    def test_all_archive_icaos_have_timezone(self):
        """Every archive ICAO must have an entry in STATION_TZ."""
        from src.config import STATION_TZ
        missing = [
            code for code in self.ARCHIVE_ICAOS
            if code not in STATION_TZ
        ]
        assert missing == [], (
            f"Archive ICAOs missing from STATION_TZ: {missing}"
        )

    def test_station_tuples_are_7_elements(self):
        """All STATIONS entries must be 7-tuples (icao, lat, lon, name, res_stn, unit, tz)."""
        from src.config import STATIONS
        bad = [(s[0], len(s)) for s in STATIONS if len(s) != 7]
        assert bad == [], f"Stations with wrong tuple length (expected 7): {bad}"

    def test_archive_icaos_in_shadow_stations_archive(self):
        """SHADOW_STATIONS_ARCHIVE must contain exactly the 19 archive ICAOs."""
        from src.config import SHADOW_STATIONS_ARCHIVE
        expected = frozenset(self.ARCHIVE_ICAOS)
        assert SHADOW_STATIONS_ARCHIVE == expected, (
            f"Mismatch: extra={SHADOW_STATIONS_ARCHIVE - expected}, "
            f"missing={expected - SHADOW_STATIONS_ARCHIVE}"
        )

    def test_active_hours_are_valid_tuples(self):
        """STATION_ACTIVE_HOURS entries for archive stations must be (start, end) with 0<=start<end<=24."""
        from src.config import STATION_ACTIVE_HOURS
        for code in self.ARCHIVE_ICAOS:
            hours = STATION_ACTIVE_HOURS[code]
            assert isinstance(hours, tuple) and len(hours) == 2, (
                f"{code}: expected (start, end) tuple, got {hours!r}"
            )
            start, end = hours
            assert 0 <= start < end <= 24, (
                f"{code}: invalid active hours {hours!r}"
            )

    def test_archive_non_us_units_are_celsius(self):
        """All archive station tuples must use unit='C' (non-US cities)."""
        from src.config import STATIONS
        archive_set = set(self.ARCHIVE_ICAOS)
        bad = [s for s in STATIONS if s[0] in archive_set and s[5] != "C"]
        assert bad == [], f"Archive stations with non-C unit: {bad}"

    def test_archive_tz_matches_expected(self):
        """Spot-check IANA timezones for a selection of archive cities."""
        from src.config import STATION_TZ
        expected_tzs = {
            "EGLC": "Europe/London",
            "RJTT": "Asia/Tokyo",
            "LLBG": "Asia/Jerusalem",
            "NZWN": "Pacific/Auckland",
            "SBGR": "America/Sao_Paulo",
            "LTAC": "Europe/Istanbul",
        }
        for code, tz in expected_tzs.items():
            assert STATION_TZ[code] == tz, (
                f"{code}: expected timezone {tz!r}, got {STATION_TZ[code]!r}"
            )

    def test_existing_stations_unchanged(self):
        """Pre-existing stations must still be present and unmodified."""
        from src.config import STATIONS
        existing = {s[0]: s for s in STATIONS}
        pre_existing = {
            "KORD": ("KORD", 41.9742, -87.9073, "Chicago", "KORD", "F", "America/Chicago"),
            "KMIA": ("KMIA", 25.7953, -80.2901, "Miami", "KMIA", "F", "America/New_York"),
            "WSSS": ("WSSS", 1.3644, 103.9915, "Singapore", "WSSS", "C", "Asia/Singapore"),
            "MPMG": ("MPMG", 8.9734, -79.5556, "Panama City", "MPMG", "C", "America/Panama"),
        }
        for code, expected_tuple in pre_existing.items():
            assert code in existing, f"Pre-existing station {code} was removed!"
            actual = existing[code]
            assert actual == expected_tuple, (
                f"Pre-existing station {code} was modified: {actual!r}"
            )

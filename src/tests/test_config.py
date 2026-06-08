"""Tests for src/config.py source priority configuration."""
import pytest

from src.config import get_source_priority


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
        """Seoul should have amos as first priority."""
        sources = get_source_priority("Seoul")
        assert len(sources) > 0
        assert sources[0]["source"] == "amos"
        assert sources[0]["station"] == "Seoul"
        assert sources[0]["cadence_min"] == 15

    def test_get_source_priority_busan(self):
        """Busan should have amos as first priority."""
        sources = get_source_priority("Busan")
        assert len(sources) > 0
        assert sources[0]["source"] == "amos"
        assert sources[0]["station"] == "Busan"

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

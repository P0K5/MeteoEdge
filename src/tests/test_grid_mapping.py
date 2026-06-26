"""Unit tests for src/data/grid_mapping.py."""

import pytest

from src.data.grid_mapping import nearest_grid, build_station_grid_cache


class TestNearestGrid:
    def test_kord_0_25_resolution(self):
        """KORD at 41.9742, -87.9073 should snap to 42.0, -88.0 at 0.25° resolution."""
        grid_lat, grid_lon = nearest_grid(41.9742, -87.9073, 0.25)
        assert grid_lat == 42.0
        assert grid_lon == -88.0

    def test_0_0625_resolution(self):
        """Test with ICON 0.0625° resolution."""
        grid_lat, grid_lon = nearest_grid(41.9742, -87.9073, 0.0625)
        assert grid_lat == round(41.9742 / 0.0625) * 0.0625
        assert grid_lon == round(-87.9073 / 0.0625) * 0.0625

    def test_rounding_to_4_decimals(self):
        """Result should be rounded to 4 decimal places to eliminate float noise."""
        grid_lat, grid_lon = nearest_grid(45.123456, -120.987654, 0.25)
        assert grid_lat == round(grid_lat, 4)
        assert grid_lon == round(grid_lon, 4)

    def test_exact_grid_point(self):
        """If input is already a grid point, output should be unchanged."""
        grid_lat, grid_lon = nearest_grid(42.0, -88.0, 0.25)
        assert grid_lat == 42.0
        assert grid_lon == -88.0

    def test_half_resolution_snap_up(self):
        """Point exactly between two grid cells should snap to nearest."""
        grid_lat, grid_lon = nearest_grid(42.125, -88.125, 0.25)
        assert grid_lat == 42.0 or grid_lat == 42.25
        assert grid_lon == -88.0 or grid_lon == -88.25

    def test_negative_coordinates(self):
        """Test with southern and western hemisphere."""
        grid_lat, grid_lon = nearest_grid(-23.435, -46.473, 0.25)
        assert isinstance(grid_lat, float)
        assert isinstance(grid_lon, float)
        assert -90 <= grid_lat <= 90
        assert -180 <= grid_lon <= 180

    def test_equator_and_prime_meridian(self):
        """Test near equator and prime meridian."""
        grid_lat, grid_lon = nearest_grid(0.05, 0.05, 0.25)
        assert grid_lat == 0.0
        assert grid_lon == 0.0


class TestBuildStationGridCache:
    def test_returns_dict(self):
        """build_station_grid_cache should return a dict."""
        cache = build_station_grid_cache(0.25)
        assert isinstance(cache, dict)

    def test_all_stations_present(self):
        """Cache should contain all stations from STATIONS."""
        cache = build_station_grid_cache(0.25)
        from src.config import STATIONS
        for station_row in STATIONS:
            station_code = station_row[0]
            assert station_code in cache

    def test_values_are_tuples(self):
        """Each cache value should be a 2-tuple of floats."""
        cache = build_station_grid_cache(0.25)
        for code, grid_point in cache.items():
            assert isinstance(grid_point, tuple)
            assert len(grid_point) == 2
            assert isinstance(grid_point[0], float)
            assert isinstance(grid_point[1], float)

    def test_kord_in_cache_0_25(self):
        """KORD should map to (42.0, -88.0) at 0.25° resolution."""
        cache = build_station_grid_cache(0.25)
        assert "KORD" in cache
        assert cache["KORD"] == (42.0, -88.0)

    def test_different_resolution_gives_different_results(self):
        """Different resolutions should produce different cache results."""
        cache_025 = build_station_grid_cache(0.25)
        cache_00625 = build_station_grid_cache(0.0625)
        kord_025 = cache_025.get("KORD")
        kord_00625 = cache_00625.get("KORD")
        assert kord_025 != kord_00625

    def test_cache_not_empty(self):
        """Cache should not be empty."""
        cache = build_station_grid_cache(0.25)
        assert len(cache) > 0

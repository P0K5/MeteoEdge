"""Utility for mapping station coordinates to nearest GRIB grid cells.

Maps a station's lat/lon to the nearest grid point at a given resolution.
Supports ECMWF (0.25°) and ICON (0.0625°) resolutions, and any other regular grid.
"""

from src.config import STATIONS


def nearest_grid(lat: float, lon: float, resolution_deg: float) -> tuple[float, float]:
    """Snap latitude and longitude to the nearest grid point.

    Rounds to the nearest multiple of resolution_deg, then to 4 decimal places
    to eliminate floating-point precision noise.

    Args:
        lat: Latitude in degrees (range: -90 to 90).
        lon: Longitude in degrees (range: -180 to 180).
        resolution_deg: Grid resolution in degrees (e.g. 0.25 for ECMWF).

    Returns:
        Tuple of (grid_lat, grid_lon) rounded to 4 decimal places.
    """
    grid_lat = round(lat / resolution_deg) * resolution_deg
    grid_lon = round(lon / resolution_deg) * resolution_deg

    grid_lat = round(grid_lat, 4)
    grid_lon = round(grid_lon, 4)

    return (grid_lat, grid_lon)


def build_station_grid_cache(resolution_deg: float) -> dict[str, tuple[float, float]]:
    """Build a lookup table mapping station codes to their nearest grid points.

    Iterates through STATIONS, applies nearest_grid to each station's coordinates,
    and returns a dict keyed by station code.

    Args:
        resolution_deg: Grid resolution in degrees (e.g. 0.25 for ECMWF).

    Returns:
        Dict mapping station code (str) -> (grid_lat, grid_lon).
    """
    cache = {}
    for station_row in STATIONS:
        station_code = station_row[0]
        lat = station_row[1]
        lon = station_row[2]
        grid_point = nearest_grid(lat, lon, resolution_deg)
        cache[station_code] = grid_point
    return cache

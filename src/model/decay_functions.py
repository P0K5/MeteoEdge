"""Decay function library for intraday bias correction.

Pure functions — no side effects, no DB access, no I/O.
All decay values are in [0.0, 1.0]. Returns 0.0 inside or past the peak window.
"""
from datetime import datetime, timezone
from math import exp

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

# Peak window configuration: city → (peak_start_hour_local, peak_end_hour_local)
# Correction influence is always 0.0 inside or past the peak window.
PEAK_WINDOWS: dict[str, tuple[int, int]] = {
    "Tokyo":     (13, 17),
    "Seoul":     (13, 17),
    "Busan":     (13, 17),
    "Singapore": (13, 16),
}
DEFAULT_PEAK_WINDOW = (13, 17)
MARKET_OPEN_HOUR = 0  # Local hour when trading day starts

# Explicit timezone overrides for cities that appear in PEAK_WINDOWS but are
# not necessarily represented in STATIONS (e.g. Tokyo has no active METAR
# station in the current config).  Values are IANA timezone strings.
_CITY_TZ_OVERRIDES: dict[str, str] = {
    "Tokyo":     "Asia/Tokyo",
    "Seoul":     "Asia/Seoul",
    "Busan":     "Asia/Seoul",
    "Singapore": "Asia/Singapore",
}


def _city_tz(city: str) -> str:
    """Return IANA timezone string for city, falling back to UTC."""
    # 1. Check explicit overrides first (covers PEAK_WINDOWS cities).
    if city in _CITY_TZ_OVERRIDES:
        return _CITY_TZ_OVERRIDES[city]

    # 2. Try STATIONS list — keyed by station code, but has city name at index 3.
    # STATIONS tuple: (station_code, lat, lon, city_name, metar_station, unit, tz)
    try:
        from src.config import STATIONS
        for row in STATIONS:
            if row[3] == city:
                return row[6]
    except Exception:
        pass

    return "UTC"


def _peak_window(city: str) -> tuple[int, int]:
    return PEAK_WINDOWS.get(city, DEFAULT_PEAK_WINDOW)


def _progress(city: str, obs_time: datetime) -> float:
    """Return fraction of elapsed time from market open to peak start.

    Returns 0.0 at market open, 1.0 at peak start, >1.0 inside/past peak.
    obs_time must be tz-aware UTC.
    """
    tz_str = _city_tz(city)
    try:
        local_dt = obs_time.astimezone(ZoneInfo(tz_str))
    except Exception:
        local_dt = obs_time.astimezone(timezone.utc)

    peak_start, _ = _peak_window(city)
    total_hours = peak_start - MARKET_OPEN_HOUR  # hours from open to peak
    if total_hours <= 0:
        return 1.0

    elapsed_hours = local_dt.hour + local_dt.minute / 60.0 + local_dt.second / 3600.0
    return elapsed_hours / total_hours


def linear_decay(city: str, obs_time: datetime) -> float:
    """Return linear decay factor: 1.0 at market open, 0.0 at peak start.

    Always 0.0 inside or past the peak window.
    obs_time must be tz-aware UTC datetime.
    """
    progress = _progress(city, obs_time)
    return max(0.0, 1.0 - progress)


def exponential_decay(city: str, obs_time: datetime, k: float = 3.0) -> float:
    """Return exponential decay factor in [0.0, 1.0].

    Decays faster than linear. k controls steepness (higher k = faster decay).
    Always 0.0 inside or past the peak window.
    obs_time must be tz-aware UTC datetime.
    """
    progress = _progress(city, obs_time)
    if progress >= 1.0:
        return 0.0
    return max(0.0, exp(-k * progress))


def get_decay_factor(city: str, obs_time: datetime, decay_type: str = "linear") -> float:
    """Dispatch to the correct decay function.

    Args:
        city: City name (e.g. "Tokyo")
        obs_time: tz-aware UTC datetime of the observation
        decay_type: "linear" or "exponential"

    Returns:
        Decay factor in [0.0, 1.0].
    """
    if decay_type == "exponential":
        return exponential_decay(city, obs_time)
    return linear_decay(city, obs_time)

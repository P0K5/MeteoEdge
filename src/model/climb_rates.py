"""Per-station per-month climb rate tables (°F of additional rise possible from each hour of day).

Keys: month number (1=Jan ... 12=Dec)
Values: dict mapping hour-of-day (0–23) to p95 additional rise in °F.

The primary lookup is CLIMB_LOOKUP from src/data/climb_lookup.py, which contains
per-station per-month per-hour p95 values derived from historical data.
DEFAULT_CLIMB_LOOKUP from config.py is preserved as a fallback for stations
not present in CLIMB_LOOKUP.

Historical note: prior to this change, only May (month 5) was calibrated.
All months used May values as a conservative placeholder.
"""
from datetime import datetime

from src.config import DEFAULT_CLIMB_LOOKUP
from src.data.climb_lookup import CLIMB_LOOKUP

_MAY: dict[int, float] = {
    0: 22.0, 1: 22.0, 2: 21.0, 3: 20.0, 4: 19.0, 5: 18.0,
    6: 16.0, 7: 14.0, 8: 12.0, 9: 10.0, 10: 8.0, 11: 7.0,
    12: 6.0, 13: 5.0, 14: 4.5, 15: 3.5, 16: 2.5, 17: 1.5,
    18: 0.5, 19: 0.0, 20: 0.0, 21: 0.0, 22: 0.0, 23: 0.0,
}

CLIMB_BY_MONTH: dict[int, dict[int, float]] = {m: _MAY for m in range(1, 13)}


def expected_additional_rise(now_local: datetime, station: str | None = None) -> float:
    """Return p95 additional °F rise from now_local.hour to end-of-day.

    Args:
        now_local: datetime in the station's local timezone
        station:   ICAO station code (e.g. "KORD"). When provided, the per-station
                   per-month lookup table (CLIMB_LOOKUP) is used. Falls back to
                   DEFAULT_CLIMB_LOOKUP (month-agnostic) if the station is not found,
                   then to the old CLIMB_BY_MONTH table if DEFAULT_CLIMB_LOOKUP also
                   has no entry for the hour.

    Returns:
        Expected additional rise in °F (0.0 if no further rise expected)
    """
    month = now_local.month
    hour = now_local.hour

    if station is not None:
        station_months = CLIMB_LOOKUP.get(station)
        if station_months:
            month_table = station_months.get(month)
            if month_table:
                return month_table.get(hour, 0.0)
        # Station not in CLIMB_LOOKUP — fall back to DEFAULT_CLIMB_LOOKUP
        return DEFAULT_CLIMB_LOOKUP.get(hour, 0.0)

    # Legacy path: no station provided — use CLIMB_BY_MONTH
    table = CLIMB_BY_MONTH.get(month, _MAY)
    return table.get(hour, 0.0)

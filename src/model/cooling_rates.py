"""Per-station per-month cooling rate tables (°F of additional drop possible from each hour of day).

Keys: month number (1=Jan ... 12=Dec)
Values: dict mapping hour-of-day (0–23) to p95 additional drop in °F.

The primary lookup is COOLING_LOOKUP from src/data/cooling_lookup.py, which contains
per-station per-month per-hour p95 values derived from historical data.
_DEFAULT_COOLING_LOOKUP is preserved as a fallback for stations not present
in COOLING_LOOKUP.

Sub-window semantics:
  Pre-midnight (19–23): fastest remaining drop — cooling accelerates after sunset.
  Post-midnight (00–04): slower — radiative cooling continues at reduced rate.
  Dawn (05–08): asymptotic — approaching daily low.
  Daytime (09–18): 0.0 — daily low has already occurred overnight.
"""
from datetime import datetime

from src.data.cooling_lookup import COOLING_LOOKUP

# Default fallback table (month-agnostic) for stations not in COOLING_LOOKUP.
# Values represent a mid-latitude continental mid-season approximation.
_DEFAULT_COOLING_LOOKUP: dict[int, float] = {
    0: 5.0, 1: 3.5, 2: 2.0, 3: 1.2, 4: 0.5,
    5: 1.0, 6: 0.7, 7: 0.4, 8: 0.2,
    9: 0.0, 10: 0.0, 11: 0.0, 12: 0.0, 13: 0.0,
    14: 0.0, 15: 0.0, 16: 0.0, 17: 0.0, 18: 0.0,
    19: 10.0, 20: 8.5, 21: 7.0, 22: 5.5, 23: 4.5,
}

_DEFAULT_MONTH: dict[int, float] = {
    0: 5.0, 1: 3.5, 2: 2.0, 3: 1.2, 4: 0.5,
    5: 1.0, 6: 0.7, 7: 0.4, 8: 0.2,
    9: 0.0, 10: 0.0, 11: 0.0, 12: 0.0, 13: 0.0,
    14: 0.0, 15: 0.0, 16: 0.0, 17: 0.0, 18: 0.0,
    19: 10.0, 20: 8.5, 21: 7.0, 22: 5.5, 23: 4.5,
}

COOLING_BY_MONTH: dict[int, dict[int, float]] = {m: _DEFAULT_MONTH for m in range(1, 13)}


def expected_additional_drop(now_local: datetime, station: str | None = None) -> float:
    """Return p95 additional °F drop from now_local.hour to overnight daily low.

    Args:
        now_local: datetime in the station's local timezone
        station:   ICAO station code (e.g. "KORD"). When provided, the per-station
                   per-month lookup table (COOLING_LOOKUP) is used. Falls back to
                   _DEFAULT_COOLING_LOOKUP (month-agnostic) if the station is not found,
                   then to the old COOLING_BY_MONTH table if _DEFAULT_COOLING_LOOKUP also
                   has no entry for the hour.

    Returns:
        Expected additional drop in °F (0.0 if daily low already reached)
    """
    month = now_local.month
    hour = now_local.hour

    if station is not None:
        station_months = COOLING_LOOKUP.get(station)
        if station_months:
            month_table = station_months.get(month)
            if month_table:
                return month_table.get(hour, 0.0)
        # Station not in COOLING_LOOKUP — fall back to _DEFAULT_COOLING_LOOKUP
        return _DEFAULT_COOLING_LOOKUP.get(hour, 0.0)

    # Legacy path: no station provided — use COOLING_BY_MONTH
    table = COOLING_BY_MONTH.get(month, _DEFAULT_MONTH)
    return table.get(hour, 0.0)

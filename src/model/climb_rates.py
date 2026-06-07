"""Per-month climb rate tables (°F of additional rise possible from each hour of day).

Keys: month number (1=Jan ... 12=Dec)
Values: dict mapping hour-of-day (0–23) to p95 additional rise in °F.

Currently only May (month 5) is calibrated from historical data.
All other months use May as a conservative placeholder.
Epic 5, issue E5-3 will derive accurate per-city values from NOAA METAR archives.
"""

_MAY: dict[int, float] = {
    0: 22.0, 1: 22.0, 2: 21.0, 3: 20.0, 4: 19.0, 5: 18.0,
    6: 16.0, 7: 14.0, 8: 12.0, 9: 10.0, 10: 8.0, 11: 7.0,
    12: 6.0, 13: 5.0, 14: 4.5, 15: 3.5, 16: 2.5, 17: 1.5,
    18: 0.5, 19: 0.0, 20: 0.0, 21: 0.0, 22: 0.0, 23: 0.0,
}

CLIMB_BY_MONTH: dict[int, dict[int, float]] = {m: _MAY for m in range(1, 13)}


def expected_additional_rise(now_local) -> float:
    """Return p95 additional °F rise from now_local.hour to end-of-day.

    Args:
        now_local: datetime in the station's local timezone
    Returns:
        Expected additional rise in °F (0.0 if no further rise expected)
    """
    month = now_local.month
    hour = now_local.hour
    table = CLIMB_BY_MONTH.get(month, _MAY)
    return table.get(hour, 0.0)

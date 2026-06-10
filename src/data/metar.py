"""METAR data fetcher — aviation weather observations for each station.

All HTTP calls go through http_client for rate limiting, retry, and caching.
STATION_TZ is imported from src.config to avoid duplication.
"""
from datetime import datetime
from dateutil import parser as dtparse
from datetime import timezone
import pytz
from astral import LocationInfo
from astral.sun import sun

import logging

from src.config import STATION_TZ, HTTP_TIMEOUT_SECONDS
from src.http_client import fetch

log = logging.getLogger(__name__)


def fetch_metar(station: str) -> dict | None:
    """Fetch the latest METAR observation for a station.

    Returns the most recent METAR report as a dict with keys like 'temp',
    'reportTime', 'obsTime', etc. Returns None on error.
    """
    url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json&hours=2"
    try:
        r = fetch(url, timeout=HTTP_TIMEOUT_SECONDS)
        data = r.json()
        if not data:
            return None
        return data[0]
    except Exception as e:
        log.warning("[%s] metar error: %s", station, e)
        return None


def fetch_all_metars_today(station: str) -> list[dict]:
    """Fetch all METAR observations for a station in the last 24 hours.

    Returns a list of METAR reports as dicts. Returns empty list on error.
    """
    url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json&hours=24"
    try:
        r = fetch(url, timeout=HTTP_TIMEOUT_SECONDS)
        return r.json() or []
    except Exception as e:
        log.warning("[%s] metar-day error: %s", station, e)
        return []


def now_local(station: str) -> datetime:
    """Return the current time in the local timezone of the given station."""
    return datetime.now(pytz.timezone(STATION_TZ[station]))


def sunset_local(station: str, lat: float, lon: float) -> datetime:
    """Return sunset time (as a datetime) in the local timezone of the given station."""
    local_tz = pytz.timezone(STATION_TZ[station])
    loc = LocationInfo(station, "US", STATION_TZ[station], lat, lon)
    s = sun(loc.observer, date=datetime.now(local_tz).date(), tzinfo=local_tz)
    return s["sunset"]


def compute_daily_high(
    metars: list[dict], tz_name: str, min_local_hour: int = 6
) -> tuple[float, datetime] | None:
    """Compute today's daily high temperature from a list of METAR observations.

    Args:
        metars: List of METAR report dicts (each containing 'temp', 'reportTime'/'obsTime', etc.)
        tz_name: Timezone name (e.g., 'America/New_York')
        min_local_hour: Skip METARs before this local hour (default 6, ~sunrise).
            Overnight observations after local midnight carry the previous day's
            heat tail; including them anchors today's "running high" to yesterday's
            decay curve.  Filtering to post-sunrise prevents the May 27 KHOU bug
            where a 78.98°F reading from 03:43 CDT (≈ yesterday's tail) became
            today's frozen daily high.

    Returns:
        Tuple of (high_temp_f, obs_time_local) for today's high, or None if no
        valid post-sunrise observations exist yet.
    """
    tz = pytz.timezone(tz_name)
    today_local_date = datetime.now(tz).date()
    best_temp, best_time = None, None
    for m in metars:
        temp_c = m.get("temp")
        obs_time_str = m.get("reportTime") or m.get("obsTime")
        if temp_c is None or obs_time_str is None:
            continue
        try:
            obs_time = dtparse.parse(obs_time_str)
            if obs_time.tzinfo is None:
                obs_time = obs_time.replace(tzinfo=timezone.utc)
            obs_local = obs_time.astimezone(tz)
            if obs_local.date() != today_local_date:
                continue
            if obs_local.hour < min_local_hour:
                continue  # Skip overnight readings — likely yesterday's heat tail
            temp_f = (float(temp_c) * 9 / 5) + 32
            if best_temp is None or temp_f > best_temp:
                best_temp, best_time = temp_f, obs_local
        except Exception:
            continue
    if best_temp is None:
        return None
    return best_temp, best_time

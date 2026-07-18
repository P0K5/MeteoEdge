"""METAR data fetcher — aviation weather observations for each station.

All HTTP calls go through http_client for rate limiting, retry, and caching.
STATION_TZ is imported from src.config to avoid duplication.
"""
from datetime import datetime, timedelta
from dateutil import parser as dtparse
from datetime import timezone
import pytz
from astral import LocationInfo
from astral.sun import sun

import logging

from src.config import STATION_TZ, HTTP_TIMEOUT_SECONDS, METAR_SKIP_STATIONS
from src.http_client import fetch

log = logging.getLogger(__name__)


def fetch_metar(station: str) -> dict | None:
    """Fetch the latest METAR observation for a station.

    Returns the most recent METAR report as a dict with keys like 'temp',
    'reportTime', 'obsTime', etc. Returns None on error.
    """
    if station in METAR_SKIP_STATIONS:
        # Issue #732: chronically-dead upstream feed — skip the fetch to avoid
        # wasted HTTP calls and repetitive parse-error spam. Same outcome (no
        # reading) as the previous error path, minus the noise.
        return None
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
    if station in METAR_SKIP_STATIONS:
        # Issue #732: see fetch_metar() — skip the known-dead upstream feed.
        return []
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


def low_window_bounds(station: str, lat: float, lon: float) -> "tuple[datetime, datetime]":
    """Return (window_start, window_end) for the current overnight-low observation window.

    Lowest-temperature markets settle against the minimum observed roughly
    between local sunset and the following local sunrise (~12:00 UTC
    settlement). This returns:
      - window_start: the most recent local sunset that has already occurred
      - window_end:   the local sunrise that follows window_start

    If ``now`` is before today's sunrise (i.e. still inside last night's
    window), window_end is still in the future. If ``now`` is after today's
    sunrise but before today's sunset (broad daytime), window_end is in the
    past -- the overnight window has already closed and a new one hasn't
    started. Callers should treat a past window_end as "no live low market
    open for this window" -- scan_markets already skips markets whose
    settlement date/time don't match via its own wrong_date/outside_window
    checks, so no extra gating is required here.
    """
    local_tz = pytz.timezone(STATION_TZ[station])
    loc = LocationInfo(station, "US", STATION_TZ[station], lat, lon)
    now = datetime.now(local_tz)

    s_today = sun(loc.observer, date=now.date(), tzinfo=local_tz)
    if now >= s_today["sunset"]:
        window_start = s_today["sunset"]
        s_next = sun(loc.observer, date=now.date() + timedelta(days=1), tzinfo=local_tz)
        window_end = s_next["sunrise"]
    else:
        s_prev = sun(loc.observer, date=now.date() - timedelta(days=1), tzinfo=local_tz)
        window_start = s_prev["sunset"]
        window_end = s_today["sunrise"]
    return window_start, window_end


def compute_daily_low_window(
    metars: list[dict], tz_name: str, window_start_local: datetime
) -> "tuple[float, datetime] | None":
    """Compute the running minimum temperature observed since *window_start_local*.

    Analogous to :func:`compute_daily_high` but for the overnight-low
    observation window (previous sunset -> next sunrise) instead of the
    calendar-day-since-sunrise window used for daily highs.

    Args:
        metars: List of METAR report dicts (each containing 'temp', 'reportTime'/'obsTime', etc.)
        tz_name: Timezone name (e.g., 'America/Chicago')
        window_start_local: Start of the observation window (tz-aware, local time) --
            typically the value returned by ``low_window_bounds()[0]``.

    Returns:
        Tuple of (low_temp_f, obs_time_local) for the running low, or None if
        no observations fall within the window yet.
    """
    tz = pytz.timezone(tz_name)
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
            if obs_local < window_start_local:
                continue  # before the overnight-low observation window
            temp_f = (float(temp_c) * 9 / 5) + 32
            if best_temp is None or temp_f < best_temp:
                best_temp, best_time = temp_f, obs_local
        except Exception:
            continue
    if best_temp is None:
        return None
    return best_temp, best_time


def compute_daily_high_from_db_observations(
    observations: list[dict], tz_name: str, min_local_hour: int = 6
) -> "tuple[float, datetime] | None":
    """Compute today's daily high temperature from DB observation rows.

    Like :func:`compute_daily_high` but accepts the ``observations`` table row
    format (dicts with ``ts`` and ``temp_f`` keys) instead of the raw METAR API
    response format.  This allows computing the daily high from a union of
    city-keyed high-cadence feeds and ICAO-keyed METAR rows.

    Args:
        observations: List of DB observation dicts, each containing at least
            ``ts`` (ISO timestamp string) and ``temp_f`` (float, already in °F).
        tz_name: Timezone name (e.g. ``'Asia/Singapore'``).
        min_local_hour: Skip observations before this local hour (default 6).

    Returns:
        Tuple of (high_temp_f, obs_time_local) or None if no valid observations.
    """
    tz = pytz.timezone(tz_name)
    today_local_date = datetime.now(tz).date()
    best_temp, best_time = None, None
    for row in observations:
        temp_f = row.get("temp_f")
        ts_str = row.get("ts")
        if temp_f is None or ts_str is None:
            continue
        try:
            obs_time = dtparse.parse(ts_str)
            if obs_time.tzinfo is None:
                obs_time = obs_time.replace(tzinfo=timezone.utc)
            obs_local = obs_time.astimezone(tz)
            if obs_local.date() != today_local_date:
                continue
            if obs_local.hour < min_local_hour:
                continue
            if best_temp is None or float(temp_f) > best_temp:
                best_temp, best_time = float(temp_f), obs_local
        except Exception:
            continue
    if best_temp is None:
        return None
    return best_temp, best_time


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

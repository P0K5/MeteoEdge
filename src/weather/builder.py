"""Stateless. Assembles WeatherState from NWP sources. No side effects."""
import json
import logging
from datetime import datetime, timezone, timedelta

import pytz
from dateutil import parser as dtparse

from src.config import (
    STATIONS, STATION_TZ, STATION_ACTIVE_HOURS,
    get_source_priority, get_canonical_station_feeds,
)
from src.data.metar import (
    fetch_all_metars_today, compute_daily_high,
    compute_daily_high_from_db_observations,
    now_local, sunset_local,
    low_window_bounds, compute_daily_low_window,
)
from src.data.nws import fetch_nws_forecast_high
from src.data.open_meteo import fetch_secondary_forecast, fetch_hourly_temp_now, fetch_gfs_forecast_high
from src.model.deb_weighting import refresh_weights, get_weights
from src.model.deb_hourly_consensus import compute_deb_mu_f
from src.model.intraday_correction import compute_correction
from src.model.residual_correction import apply_residual_correction
from src.model.envelope import WeatherState
from src.model.envelope_low import WeatherStateLow
from src.data.obs_consensus import compute_consensus_high

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Delta-logging state: suppress per-poll weather log lines when values are
# stable. Only log when any value changes >0.5°F or once per hour (heartbeat).
# Trade/order/flag events are NOT deduped — this only applies to weather lines.
# ---------------------------------------------------------------------------

_WEATHER_LOG_THRESHOLD_F: float = 0.5        # °F change that forces a log
_WEATHER_HEARTBEAT_INTERVAL_S: float = 3600  # 1 hour

# Per-station: {"high_f": float, "latest_f": float, "nws": float|None, "last_logged": datetime}
_weather_log_state: dict[str, dict] = {}


def _should_log_weather(station: str, high_f: float, latest_temp_f: float,
                         forecast_nws: float | None) -> bool:
    """Return True if this poll's weather should be logged at INFO level.

    Logs when:
    - Station has never been logged before.
    - Any tracked value changed by more than _WEATHER_LOG_THRESHOLD_F.
    - More than _WEATHER_HEARTBEAT_INTERVAL_S seconds have passed since last log.
    """
    now = datetime.now(timezone.utc)
    prev = _weather_log_state.get(station)
    if prev is None:
        _weather_log_state[station] = {
            "high_f": high_f,
            "latest_f": latest_temp_f,
            "nws": forecast_nws,
            "last_logged": now,
        }
        return True

    # Heartbeat: always log once per hour
    elapsed = (now - prev["last_logged"]).total_seconds()
    if elapsed >= _WEATHER_HEARTBEAT_INTERVAL_S:
        _weather_log_state[station].update(
            high_f=high_f, latest_f=latest_temp_f, nws=forecast_nws, last_logged=now
        )
        return True

    # Delta check: log if any value changed beyond threshold
    def _nws_changed() -> bool:
        old_nws = prev["nws"]
        if old_nws is None and forecast_nws is None:
            return False
        if old_nws is None or forecast_nws is None:
            return True
        return abs(forecast_nws - old_nws) > _WEATHER_LOG_THRESHOLD_F

    if (
        abs(high_f - prev["high_f"]) > _WEATHER_LOG_THRESHOLD_F
        or abs(latest_temp_f - prev["latest_f"]) > _WEATHER_LOG_THRESHOLD_F
        or _nws_changed()
    ):
        _weather_log_state[station].update(
            high_f=high_f, latest_f=latest_temp_f, nws=forecast_nws, last_logged=now
        )
        return True

    return False


def _station_in_active_window(station: str) -> bool:
    if station not in STATION_TZ:
        return True
    now_local_dt = datetime.now(pytz.timezone(STATION_TZ[station]))
    active_start, active_end = STATION_ACTIVE_HOURS.get(station, (6, 23))
    return active_start <= now_local_dt.hour < active_end


# European ICAO prefixes (E* = North/Central Europe, L* = South Europe / MENA border).
# Used to route EU stations to the "eu" DEB region so ICON weight is included.
_EU_ICAO_PREFIXES = frozenset(("E", "L"))


def _deb_station_region(station: str, unit: str) -> str:
    """Map a station to its DEB registry region ("us", "eu", or "global").

    - unit="F" → "us"  (NWS/HRRR/NBM are applicable)
    - unit="C" + European ICAO prefix → "eu"  (ICON-EU is applicable)
    - unit="C" otherwise → "global"  (Open-Meteo + GFS + ECMWF only)
    """
    if unit == "F":
        return "us"
    if station[:1] in _EU_ICAO_PREFIXES:
        return "eu"
    return "global"


def _build_one_station(
    station: str,
    lat: float,
    lon: float,
    city: str,
    unit: str = "F",
    db=None,
    health_out: "list | None" = None,
) -> "WeatherState | None":
    """Build a WeatherState for a single station.

    This is a shared implementation helper called by both
    ``build_weather_for_scanning`` and ``build_weather_for_pricing``.  It does
    NOT apply the active-hours gate — callers are responsible for that decision.

    Returns the WeatherState on success, or None when data is unavailable
    (no METAR, parse error, etc.).  A ``health_out`` entry is appended on
    failure (and on success when health_out is provided).
    """
    def _degraded(reason: str) -> None:
        if health_out is not None:
            health_out.append({"station": station, "status": "degraded", "reason": reason})

    metars = fetch_all_metars_today(station)
    if not metars:
        log.info("[%s] no METAR data, skipping", station)
        _degraded("no METAR data")
        return None

    active_start, _ = STATION_ACTIVE_HOURS.get(station, (6, 23))
    result = compute_daily_high(metars, STATION_TZ[station], min_local_hour=active_start)
    if not result:
        log.info("[%s] could not compute daily high, skipping", station)
        _degraded("could not compute daily high")
        return None
    high_f, high_time = result

    # Upgrade daily high from densest feed with cross-source outlier rejection.
    # High-cadence feeds (MSS 1-min, AMOS/JMA 10-min) can catch intra-30-min
    # peaks that 30-min METAR would miss, but bad ticks are rejected when they
    # exceed the METAR running high by more than CONSENSUS_OUTLIER_SIGMA_F.
    if db is not None:
        feed_keys = get_canonical_station_feeds(station)
        if len(feed_keys) > 1:
            since_today = datetime.now(
                pytz.timezone(STATION_TZ[station])
            ).date().isoformat() + "T00:00:00+00:00"
            db_obs = db.get_observations_multi_station(feed_keys, since=since_today)
            for o in db_obs:
                o.setdefault("station", station)
            consensus_high = compute_consensus_high(db_obs)
            if consensus_high is not None and consensus_high > high_f:
                log.debug(
                    "[%s] daily high upgraded by obs consensus: %.1fF → %.1fF",
                    station, high_f, consensus_high,
                )
                high_f = consensus_high

    latest = metars[0]
    latest_temp_c = latest.get("temp")
    if latest_temp_c is None:
        log.info("[%s] latest METAR missing temp, skipping", station)
        _degraded("latest METAR missing temperature")
        return None

    obs_str = latest.get("reportTime") or latest.get("obsTime")
    if not obs_str:
        log.info("[%s] latest METAR missing time, skipping", station)
        _degraded("latest METAR missing timestamp")
        return None

    try:
        latest_temp_f = (float(latest_temp_c) * 9 / 5) + 32
        latest_time = dtparse.parse(obs_str)
        if latest_time.tzinfo is None:
            latest_time = latest_time.replace(tzinfo=timezone.utc)
    except Exception as e:
        log.warning("[%s] METAR parse error: %s, skipping", station, e)
        _degraded(f"METAR parse error: {e}")
        return None

    # Persist the latest METAR so the freshness monitor and intraday
    # correction can use it as a fallback observation source.
    if db is not None:
        metar_ts = latest_time.astimezone(timezone.utc).isoformat()
        prev_metar = db.get_latest_observation("metar", station)
        if prev_metar is None or prev_metar["ts"] != metar_ts:
            db.insert_observation(
                ts=metar_ts,
                station=station,
                temp_f=latest_temp_f,
                temp_native=float(latest_temp_c),
                unit="C",
                source="metar",
                cadence_min=30,
                is_official=1,
                raw_json=json.dumps(latest),
            )

    # Attempt to upgrade latest_temp_f from a fresher high-freq obs
    obs_bias_offset_f = None
    if db is not None:
        for src_cfg in get_source_priority(city):
            if src_cfg["source"] == "metar":
                continue
            obs = db.get_latest_observation(src_cfg["source"], src_cfg["station"])
            if obs is None:
                continue
            try:
                obs_ts = dtparse.parse(obs["ts"])
                if obs_ts.tzinfo is None:
                    obs_ts = obs_ts.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            age_min = (datetime.now(timezone.utc) - obs_ts).total_seconds() / 60
            if age_min > 2 * src_cfg["cadence_min"]:
                continue  # stale — skip
            latest_temp_f = float(obs["temp_f"])
            latest_time = obs_ts
            break  # highest-priority fresh source wins
        hourly_model_f = fetch_hourly_temp_now(lat, lon)
        if hourly_model_f is not None:
            obs_bias_offset_f = latest_temp_f - hourly_model_f

    forecast_nws = fetch_nws_forecast_high(lat, lon)
    forecast_secondary = fetch_secondary_forecast(lat, lon)
    forecast_gfs = fetch_gfs_forecast_high(lat, lon)

    if db is not None:
        # NOTE: forecast logging to model_forecast_log was removed from the scan loop.
        # The cron capture worker (src/scripts/capture_forecasts.py) is the sole
        # writer to model_forecast_log, logging at fixed lead-time bins so EMOS
        # trains on genuine ahead-of-event forecasts rather than nowcast snapshots.
        # See issues #422/#423.
        station_region = _deb_station_region(station, unit)
        refresh_weights(db, station, city, station_region=station_region)
    weights = get_weights(db, city, station_region=_deb_station_region(station, unit)) if db is not None else {"nws": 0.5, "open_meteo": 0.5, "gfs": 0.0}
    deb_mu_f = compute_deb_mu_f(forecast_nws, forecast_secondary, weights, forecast_gfs=forecast_gfs)
    if deb_mu_f is not None:
        log.debug(
            "[%s] deb_mu_f=%.1fF weights=nws:%.2f/om:%.2f/gfs:%.2f",
            station, deb_mu_f,
            weights.get('nws', 0), weights.get('open_meteo', 0), weights.get('gfs', 0),
        )

    state = WeatherState(
        station=station,
        now_local=now_local(station),
        sunset_local=sunset_local(station, lat, lon),
        current_high_f=high_f,
        current_high_time=high_time,
        latest_temp_f=latest_temp_f,
        latest_temp_time=latest_time,
        forecast_high_f=forecast_nws,
        secondary_forecast_f=forecast_secondary,
        obs_bias_offset_f=obs_bias_offset_f,
        deb_mu_f=deb_mu_f,
    )
    if db is not None:
        corrected = compute_correction(city, state, db)
        if corrected is not None:
            state.corrected_mu_f = corrected
            log.debug("[%s] corrected_mu_f=%.1fF (delta=%+.1fF)", station, corrected, corrected - deb_mu_f)
        # Apply per-city rolling residual bias correction (issue #307)
        base_mu = state.corrected_mu_f if state.corrected_mu_f is not None else deb_mu_f
        if base_mu is not None:
            residual_mu, _res_stats = apply_residual_correction(city, base_mu, db)
            if residual_mu != base_mu:
                state.corrected_mu_f = residual_mu
                log.debug(
                    "[%s] residual_correction applied: %.1fF → %.1fF",
                    station, base_mu, residual_mu,
                )
                db.log_guardrail_event(
                    datetime.now(timezone.utc).isoformat(),
                    station, "correction_applied", base_mu, residual_mu,
                )
    if _should_log_weather(station, high_f, latest_temp_f, forecast_nws):
        log.info("[%s] high=%.1fF latest=%.1fF nws=%s", station, high_f, latest_temp_f, forecast_nws)
    else:
        log.debug("[%s] high=%.1fF latest=%.1fF nws=%s (no change)", station, high_f, latest_temp_f, forecast_nws)
    if health_out is not None:
        health_out.append({"station": station, "status": "ok", "reason": ""})
    return state


def build_weather_for_scanning(stations=None, db=None, health_out=None) -> dict:
    """Assemble per-station WeatherState for the SCANNER path.

    Applies the active-hours gate: stations outside their configured local
    window are excluded.  This gate is intentional — opening new entries when
    overnight carryover can fool bracket logic caused the KHOU 2026-05-27
    incident and must not be regressed.

    Args:
        stations: iterable of (station, lat, lon, city, ...) tuples, or None
            to use the full STATIONS list from config.
        db: optional Database instance.
        health_out: optional list; one ``{"station", "status", "reason"}``
            entry is appended per station for dashboard health reporting.

    Returns dict mapping station code → WeatherState (only in-window stations).
    """
    def _degraded(station: str, reason: str) -> None:
        if health_out is not None:
            health_out.append({"station": station, "status": "degraded", "reason": reason})

    station_list = stations if stations is not None else STATIONS
    weather: dict[str, WeatherState] = {}
    for station, lat, lon, city, *rest in station_list:
        unit = rest[1] if len(rest) >= 2 else "F"
        now_local_dt = datetime.now(pytz.timezone(STATION_TZ[station]))
        active_start, active_end = STATION_ACTIVE_HOURS.get(station, (6, 23))
        if not (active_start <= now_local_dt.hour < active_end):
            log.info(
                "[%s] local %s outside active window %02d:00-%02d:00 -- skipping",
                station, now_local_dt.strftime('%H:%M'), active_start, active_end,
            )
            _degraded(station, f"outside active window {active_start:02d}:00-{active_end:02d}:00 (local {now_local_dt.strftime('%H:%M')})")
            continue

        state = _build_one_station(station, lat, lon, city, unit=unit, db=db, health_out=health_out)
        if state is not None:
            weather[station] = state
    return weather


def build_weather_for_pricing(stations, db=None) -> dict:
    """Assemble per-station WeatherState for the POSITION RE-PRICER path.

    Unlike ``build_weather_for_scanning``, this function has NO active-hours
    gate.  METARs flow 24 hours a day and the data is valid even at 03:00
    local; the active-hours gate exists only to prevent the scanner from
    opening new entries during overnight windows.  Re-pricing means continuous
    fair-value updates for already-held positions — it does NOT open new
    positions.

    Callers MUST pass only the stations with open positions (typically 1-3).
    Do NOT pass the full 30+ station list; that wastes upstream API calls.

    Args:
        stations: iterable of (station, lat, lon, city, ...) tuples for open
            position stations only.
        db: optional Database instance.

    Returns dict mapping station code → WeatherState for every station that
    has current METAR data (regardless of local hour).
    """
    weather: dict[str, WeatherState] = {}
    for station, lat, lon, city, *rest in stations:
        unit = rest[1] if len(rest) >= 2 else "F"
        state = _build_one_station(station, lat, lon, city, unit=unit, db=db, health_out=None)
        if state is not None:
            weather[station] = state
    return weather


def _build_one_station_low(station: str, lat: float, lon: float, db=None) -> "WeatherStateLow | None":
    """Build a WeatherStateLow for a single station's overnight-low window.

    Root cause of issue #554: this function (and build_weather_low_for_scanning
    below) never existed, so scan_markets() was never given a `weather_low`
    dict and its entire low-side shadow block (scanner.py ~660-740) was
    unreachable dead code in production, even though it was fully unit-tested
    in isolation.

    Deliberately minimal for v1: forecast_low_f/secondary_forecast_low_f are
    left as None (true_probability_low_in_bracket falls back to the envelope
    midpoint when no forecast is available -- see envelope_low.py). Wiring a
    real low-temperature forecast source is left to a follow-up issue.
    """
    metars = fetch_all_metars_today(station)
    if not metars:
        log.debug("[%s] no METAR data, skipping low-side build", station)
        return None

    window_start, window_end = low_window_bounds(station, lat, lon)
    result = compute_daily_low_window(metars, STATION_TZ[station], window_start)
    if not result:
        log.debug("[%s] no observations in overnight-low window yet, skipping", station)
        return None
    low_f, low_time = result

    latest = metars[0]
    latest_temp_c = latest.get("temp")
    if latest_temp_c is None:
        log.debug("[%s] latest METAR missing temp, skipping low-side build", station)
        return None
    obs_str = latest.get("reportTime") or latest.get("obsTime")
    if not obs_str:
        log.debug("[%s] latest METAR missing time, skipping low-side build", station)
        return None

    try:
        latest_temp_f = (float(latest_temp_c) * 9 / 5) + 32
        latest_time = dtparse.parse(obs_str)
        if latest_time.tzinfo is None:
            latest_time = latest_time.replace(tzinfo=timezone.utc)
    except Exception as e:
        log.warning("[%s] METAR parse error (low-side): %s, skipping", station, e)
        return None

    return WeatherStateLow(
        station=station,
        now_local=now_local(station),
        sunrise_local=window_end,
        current_low_f=low_f,
        current_low_time=low_time,
        latest_temp_f=latest_temp_f,
        latest_temp_time=latest_time,
        forecast_low_f=None,
        secondary_forecast_low_f=None,
    )


def build_weather_low_for_scanning(stations=None, db=None) -> dict:
    """Assemble per-station WeatherStateLow for the SCANNER low-side path.

    Epic-C low-side shadow rollout (issue #457); this is the low-side
    counterpart of build_weather_for_scanning(). Unlike the high-side builder,
    there is no active-hours gate here: the overnight-low observation window
    (previous sunset -> next sunrise) is self-limiting -- compute_daily_low_window
    returns None until the window has at least one observation -- and
    scan_markets' own mins_left/wrong_date checks skip markets outside the
    live low market's settlement window. See low_window_bounds() docstring.

    Only stations with a known 'lowest temperature in <city>' Polymarket
    mapping are built (POLYMARKET_CITY_TO_STATION_LOW in strategy/scanner.py);
    imported lazily to avoid a module-load-order dependency between
    src.weather.builder and src.strategy.scanner.

    Returns dict mapping station code -> WeatherStateLow (only stations with
    at least one observation in their current overnight-low window).
    """
    from src.strategy.scanner import POLYMARKET_CITY_TO_STATION_LOW
    low_stations = set(POLYMARKET_CITY_TO_STATION_LOW.values())

    station_list = stations if stations is not None else STATIONS
    weather_low: "dict[str, WeatherStateLow]" = {}
    for station, lat, lon, city, *rest in station_list:
        if station not in low_stations:
            continue
        state = _build_one_station_low(station, lat, lon, db=db)
        if state is not None:
            weather_low[station] = state
    return weather_low


def _build_weather(db=None, health_out=None) -> dict:
    """Assemble per-station WeatherState.

    .. deprecated::
        Use ``build_weather_for_scanning`` (scanner path) or
        ``build_weather_for_pricing`` (position re-pricer path) instead.
        This wrapper delegates to ``build_weather_for_scanning`` and is kept
        for backward compatibility with existing call sites and tests.

    When *health_out* is a list, one ``{"station", "status", "reason"}`` entry
    is appended per station so callers (the dashboard) can surface *what* is
    failing and *why* -- e.g. outside active window, no METAR, parse error.
    ``status`` is ``"ok"`` for stations that produced a WeatherState.
    """
    return build_weather_for_scanning(db=db, health_out=health_out)

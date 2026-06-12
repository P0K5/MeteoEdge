"""Stateless. Assembles WeatherState from NWP sources. No side effects."""
import json
import logging
from datetime import datetime, timezone

import pytz
from dateutil import parser as dtparse

from src.config import (
    STATIONS, STATION_TZ, STATION_ACTIVE_HOURS,
    get_source_priority,
)
from src.data.metar import fetch_all_metars_today, compute_daily_high, now_local, sunset_local
from src.data.nws import fetch_nws_forecast_high
from src.data.open_meteo import fetch_secondary_forecast, fetch_hourly_temp_now
from src.model.deb_weighting import log_forecast, refresh_weights, get_weights
from src.model.deb_hourly_consensus import compute_deb_mu_f
from src.model.intraday_correction import compute_correction
from src.model.envelope import WeatherState

log = logging.getLogger(__name__)


def _station_in_active_window(station: str) -> bool:
    if station not in STATION_TZ:
        return True
    now_local_dt = datetime.now(pytz.timezone(STATION_TZ[station]))
    active_start, active_end = STATION_ACTIVE_HOURS.get(station, (6, 23))
    return active_start <= now_local_dt.hour < active_end


def _build_weather(db=None) -> dict:
    weather: dict[str, WeatherState] = {}
    for station, lat, lon, city, *_ in STATIONS:
        now_local_dt = datetime.now(pytz.timezone(STATION_TZ[station]))
        active_start, active_end = STATION_ACTIVE_HOURS.get(station, (6, 23))
        if not (active_start <= now_local_dt.hour < active_end):
            log.info(
                "[%s] local %s outside active window %02d:00-%02d:00 -- skipping",
                station, now_local_dt.strftime('%H:%M'), active_start, active_end,
            )
            continue

        metars = fetch_all_metars_today(station)
        if not metars:
            log.info("[%s] no METAR data, skipping", station)
            continue

        result = compute_daily_high(metars, STATION_TZ[station], min_local_hour=active_start)
        if not result:
            log.info("[%s] could not compute daily high, skipping", station)
            continue
        high_f, high_time = result

        latest = metars[0]
        latest_temp_c = latest.get("temp")
        if latest_temp_c is None:
            log.info("[%s] latest METAR missing temp, skipping", station)
            continue

        obs_str = latest.get("reportTime") or latest.get("obsTime")
        if not obs_str:
            log.info("[%s] latest METAR missing time, skipping", station)
            continue

        try:
            latest_temp_f = (float(latest_temp_c) * 9 / 5) + 32
            latest_time = dtparse.parse(obs_str)
            if latest_time.tzinfo is None:
                latest_time = latest_time.replace(tzinfo=timezone.utc)
        except Exception as e:
            log.warning("[%s] METAR parse error: %s, skipping", station, e)
            continue

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

        today_str = datetime.now(timezone.utc).date().isoformat()
        if db is not None:
            if forecast_nws is not None:
                log_forecast(db, station, "nws", today_str, forecast_nws)
            if forecast_secondary is not None:
                log_forecast(db, station, "open_meteo", today_str, forecast_secondary)
            refresh_weights(db, station, city)
        weights = get_weights(db, city) if db is not None else {"nws": 0.5, "open_meteo": 0.5}
        deb_mu_f = compute_deb_mu_f(forecast_nws, forecast_secondary, weights)
        if deb_mu_f is not None:
            log.debug("[%s] deb_mu_f=%.1fF weights=nws:%.2f/om:%.2f", station, deb_mu_f, weights['nws'], weights['open_meteo'])

        weather[station] = WeatherState(
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
            corrected = compute_correction(city, weather[station], db)
            if corrected is not None:
                weather[station].corrected_mu_f = corrected
                log.debug("[%s] corrected_mu_f=%.1fF (delta=%+.1fF)", station, corrected, corrected - deb_mu_f)
        log.info("[%s] high=%.1fF latest=%.1fF nws=%s", station, high_f, latest_temp_f, forecast_nws)
    return weather

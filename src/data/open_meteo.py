"""Open-Meteo secondary forecast source.

Used as a weighted secondary input (40%) alongside NWS (60%) in the ensemble.
Cached 30 min — Open-Meteo updates hourly and the free tier caps at 10,000 req/day.

Attribution: This module uses the Open-Meteo API (https://open-meteo.com/).
Open-Meteo provides free weather forecast data. See docs/OPERATIONS.md for
commercial use policy and terms of service details.
"""
from datetime import datetime, timezone

from src.http_client import cached_fetch_json


def _fetch_open_meteo_hourly(lat: float, lon: float) -> dict | None:
    """Fetch raw Open-Meteo hourly payload for the given coordinates.

    Cached 30 min per coordinate pair.
    Returns None if unavailable.
    """
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m,weather_code"
        f"&temperature_unit=fahrenheit&timezone=auto"
    )
    return cached_fetch_json(url, ttl_minutes=30)


def fetch_secondary_forecast(lat: float, lon: float) -> float | None:
    """Fetch forecast daily high (°F) from Open-Meteo for the given coordinates.

    Cached 30 min per coordinate pair.
    Returns None if unavailable.
    """
    data = _fetch_open_meteo_hourly(lat, lon)
    if not data:
        return None
    try:
        temps = data["hourly"]["temperature_2m"][:24]
        return max(temps) if temps else None
    except Exception as e:
        print(f"[open-meteo] parse error for ({lat},{lon}): {e}")
        return None


def fetch_hourly_temp_now(lat: float, lon: float) -> float | None:
    """Return Open-Meteo hourly temperature (°F) for the nearest past UTC hour.

    Returns None if unavailable.
    """
    data = _fetch_open_meteo_hourly(lat, lon)
    if not data:
        return None
    try:
        times = data["hourly"]["time"]
        temps = data["hourly"]["temperature_2m"]
        now_utc = datetime.now(timezone.utc)
        best_temp = None
        for t_str, t_val in zip(times, temps):
            t_dt = datetime.fromisoformat(t_str)
            if t_dt.tzinfo is None:
                t_dt = t_dt.replace(tzinfo=timezone.utc)
            if t_dt <= now_utc and t_val is not None:
                best_temp = float(t_val)
        return best_temp
    except Exception as e:
        print(f"[open-meteo] hourly parse error for ({lat},{lon}): {e}")
        return None

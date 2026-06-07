"""
Forecast adapter. Routes US stations to NWS (matches live spike's source
so shadow predictions for US cohort are directly comparable to live).
Routes non-US stations to Open-Meteo (global, free, no auth, returns
either Celsius or Fahrenheit on request).
"""
import httpx

from config import HTTP_TIMEOUT_SECONDS, USER_AGENT


def fetch_forecast_high(
    lat: float, lon: float, unit: str, source: str, timezone: str = "auto"
) -> float | None:
    """
    Return today's expected daily-high temperature in the requested unit,
    or None on any failure (caller falls back to envelope midpoint).
    """
    if source == "nws":
        return _fetch_nws(lat, lon, unit)
    if source == "open-meteo":
        return _fetch_open_meteo(lat, lon, unit, timezone)
    print(f"[forecast] unknown source: {source!r}")
    return None


def _fetch_nws(lat: float, lon: float, unit: str) -> float | None:
    """NWS is US-only. Returns Fahrenheit; converts to C if requested."""
    try:
        points_url = f"https://api.weather.gov/points/{lat},{lon}"
        r = httpx.get(points_url, headers={"User-Agent": USER_AGENT},
                      timeout=HTTP_TIMEOUT_SECONDS)
        r.raise_for_status()
        forecast_url = r.json()["properties"]["forecastHourly"]
        r2 = httpx.get(forecast_url, headers={"User-Agent": USER_AGENT},
                       timeout=HTTP_TIMEOUT_SECONDS)
        r2.raise_for_status()
        periods = r2.json()["properties"]["periods"]
        highs_f = [p["temperature"] for p in periods[:18]
                   if p.get("temperatureUnit") == "F"]
        if not highs_f:
            return None
        high_f = max(highs_f)
        return high_f if unit == "F" else (high_f - 32) * 5 / 9
    except Exception as e:
        print(f"[nws] {lat},{lon} error: {e}")
        return None


def _fetch_open_meteo(
    lat: float, lon: float, unit: str, timezone: str
) -> float | None:
    """
    Open-Meteo daily forecast. Pulls today's `temperature_2m_max`.
    `timezone` is the station's IANA zone so 'today' aligns with
    Polymarket's local-day resolution.
    """
    try:
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max",
            "timezone": timezone,
            "forecast_days": 1,
        }
        if unit == "F":
            params["temperature_unit"] = "fahrenheit"
        r = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        r.raise_for_status()
        daily = r.json().get("daily", {})
        highs = daily.get("temperature_2m_max") or []
        if not highs:
            return None
        return float(highs[0])
    except Exception as e:
        print(f"[open-meteo] {lat},{lon} error: {e}")
        return None

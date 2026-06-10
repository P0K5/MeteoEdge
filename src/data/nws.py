"""NWS forecast fetcher — uses http_client's NWS /points permanent cache
and 30-min TTL for forecast data.
"""
import logging

from src.http_client import get_nws_forecast_url, cached_fetch_json

log = logging.getLogger(__name__)


def fetch_nws_forecast_high(lat: float, lon: float) -> float | None:
    """Return today's forecast high (°F) for lat/lon using NWS hourly forecast.

    Uses http_client's permanent /points cache and 30-min forecast cache.
    Returns None if the forecast is unavailable.
    """
    forecast_url = get_nws_forecast_url(lat, lon)
    if not forecast_url:
        return None

    data = cached_fetch_json(forecast_url, ttl_minutes=30)
    if not data:
        return None

    try:
        periods = data["properties"]["periods"]
        highs = [p["temperature"] for p in periods[:18] if p.get("temperatureUnit") == "F"]
        return max(highs) if highs else None
    except Exception as e:
        log.warning("[nws] parse error for (%s,%s): %s", lat, lon, e)
        return None

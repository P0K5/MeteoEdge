"""NWS forecast fetcher — uses http_client's NWS /points permanent cache
and 30-min TTL for forecast data.
"""
import logging

from src.http_client import get_nws_forecast_url, cached_fetch_json

log = logging.getLogger(__name__)


# Climatological NWS forecast uncertainty by lead time (°F).
# Source: approximate NOAA/NDFD historical verification statistics.
# NWS does not publish ensemble spread via its public API; these values are
# derived from climatological MAE/RMSE ratios at each lead-time bin.
# Known limitation: values are global averages; station-specific error may differ.
# Linked to issue #423 — replace with station-specific values when available.
_NWS_SIGMA_BY_LEAD: dict[int, float] = {
    3:  1.0,
    6:  1.5,
    12: 2.0,
    18: 2.5,
    24: 3.0,
}
_NWS_SIGMA_DEFAULT: float = 3.0  # fallback for lead times not in the table


def _nws_sigma_for_lead(lead_hours: int) -> float:
    """Return climatological NWS forecast σ (°F) for a given lead time.

    Uses exact matches first, then the nearest tabulated lead time.
    """
    if lead_hours in _NWS_SIGMA_BY_LEAD:
        return _NWS_SIGMA_BY_LEAD[lead_hours]
    # Nearest-neighbour lookup
    best = min(_NWS_SIGMA_BY_LEAD.keys(), key=lambda k: abs(k - lead_hours))
    return _NWS_SIGMA_BY_LEAD[best]


def fetch_nws_with_spread(
    lat: float, lon: float, lead_hours: int
) -> "tuple[float, float] | None":
    """Fetch NWS forecast high (°F) and climatological σ for the given lead time.

    NWS does not publish ensemble spread via its public /gridpoints API.  The
    returned sigma_f is a climatological value keyed on lead_hours (see
    _NWS_SIGMA_BY_LEAD).  This is a known limitation — replace with
    station-specific ensemble spread when the NWS Probabilistic Guidance
    API becomes publicly accessible (linked: issue #423).

    Args:
        lat:        Latitude.
        lon:        Longitude.
        lead_hours: Lead time in hours; determines climatological σ lookup.

    Returns:
        (mu_f, sigma_f) where sigma_f is the climatological spread for this
        lead time, or None if the NWS forecast is unavailable.
    """
    mu_f = fetch_nws_forecast_high(lat, lon)
    if mu_f is None:
        return None
    sigma_f = _nws_sigma_for_lead(lead_hours)
    return float(mu_f), float(sigma_f)


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


def fetch_nws_forecast_low(lat: float, lon: float) -> float | None:
    """Return the upcoming overnight low forecast (°F) for lat/lon using NWS hourly forecast.

    Mirrors ``fetch_nws_forecast_high`` -- same /points permanent cache and
    30-min forecast cache, same forecast window -- but takes the minimum
    instead of the maximum. Gives the low-side builder
    (``build_weather_low_for_scanning`` in ``src/weather/builder.py``) a
    forecast signal analogous to how ``forecast_high_f`` is sourced for the
    high-side builder (issue #583).

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
        lows = [p["temperature"] for p in periods[:18] if p.get("temperatureUnit") == "F"]
        return min(lows) if lows else None
    except Exception as e:
        log.warning("[nws] low-forecast parse error for (%s,%s): %s", lat, lon, e)
        return None

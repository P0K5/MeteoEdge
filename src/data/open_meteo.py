"""Open-Meteo secondary forecast source.

Used as a weighted secondary input (40%) alongside NWS (60%) in the ensemble.
Cached 30 min — Open-Meteo updates hourly and the free tier caps at 10,000 req/day.
"""
from src.http_client import cached_fetch_json


def fetch_secondary_forecast(lat: float, lon: float) -> float | None:
    """Fetch forecast daily high (°F) from Open-Meteo for the given coordinates.

    Cached 30 min per coordinate pair.
    Returns None if unavailable.
    """
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m,weather_code"
        f"&temperature_unit=fahrenheit&timezone=auto"
    )
    data = cached_fetch_json(url, ttl_minutes=30)
    if not data:
        return None
    try:
        temps = data["hourly"]["temperature_2m"][:24]
        return max(temps) if temps else None
    except Exception as e:
        print(f"[open-meteo] parse error for ({lat},{lon}): {e}")
        return None

"""DEB hourly consensus temperature path builder."""
import os
from datetime import datetime, timezone
from src.data.open_meteo import _fetch_open_meteo_hourly


def build_consensus(
    lat: float, lon: float, weights: dict[str, float]
) -> list[tuple[str, float]] | None:
    """Return DEB-weighted hourly temperature path for today (UTC).

    Since only Open-Meteo provides an hourly path, the consensus is the
    Open-Meteo path scaled by its DEB weight. NWS weight is ignored here
    (NWS has no hourly resolution).

    Returns list of (iso_time_str, temp_f) for current-day slots, or None.
    """
    data = _fetch_open_meteo_hourly(lat, lon)
    if not data:
        return None
    try:
        times = data["hourly"]["time"]
        temps = data["hourly"]["temperature_2m"]
        om_weight = weights.get("open_meteo", 0.5)
        today = datetime.now(timezone.utc).date().isoformat()
        result = []
        for t_str, t_val in zip(times, temps):
            if t_val is None:
                continue
            if t_str[:10] == today:
                result.append((t_str, float(t_val) * om_weight))
        return result if result else None
    except Exception:
        return None


def compute_deb_mu_f(
    forecast_nws: float | None,
    forecast_open_meteo: float | None,
    weights: dict[str, float],
) -> float | None:
    """Return DEB-weighted forecast high (°F).

    Replaces the static 60/40 ensemble_forecast() blend when DEB_ENABLED=true.
    Returns None if either input forecast is None.
    """
    if forecast_nws is None or forecast_open_meteo is None:
        return None
    w_nws = weights.get("nws", 0.5)
    w_om = weights.get("open_meteo", 0.5)
    return w_nws * forecast_nws + w_om * forecast_open_meteo

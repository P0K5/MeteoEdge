"""EMOS (Ensemble Model Output Statistics) linear calibration.

Fits Gaussian EMOS parameters (a, b, c, d) by minimising mean CRPS
over historical forecast-vs-actual pairs using scipy.optimize.minimize.

Reference: Gneiting et al. (2005) doi:10.1175/MWR2904.1

The calibration fits a linear post-processing of the ensemble mean (mu) and
spread (sigma):

    mu_cal   = a + b * mu_raw
    sigma_cal = c + d * sigma_raw

Parameters a, b shift/scale the mean; c, d shift/scale the spread.
Bounds enforce sigma_cal > 0 at all times (c > 1e-3, d > 1e-3).

Output coefficients are always written as model_mode='emos_shadow' with
ready_for_promotion=0 — promotion to active is a deliberate manual step.
"""
from __future__ import annotations

from datetime import datetime, timezone

from scipy.optimize import minimize

from src.config import FORECAST_STDDEV_F, STATIONS
from src.model.crps_score import crps_gaussian


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------

class InsufficientDataError(ValueError):
    """Raised when the training dataset has fewer than min_samples triples.

    This typically occurs in early deployments when the model_forecast_log
    and/or observations tables have not accumulated enough settled days yet.
    Production callers should catch this and skip calibration rather than
    crashing.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _city_to_station(city: str) -> str | None:
    """Return the METAR station code for a city name (matches STATIONS config)."""
    for station, _lat, _lon, cfg_city, _res_station, _unit, _tz in STATIONS:
        if cfg_city.lower() == city.lower():
            return station
    return None


def _crps_loss(params: list[float], data: list[tuple[float, float, float]]) -> float:
    """Mean CRPS loss for EMOS parameters over training data.

    Args:
        params: [a, b, c, d] — EMOS linear transform coefficients.
        data:   List of (mu_raw, sigma_raw, y) triples.

    Returns:
        Mean CRPS, or a large penalty (1e9) if any calibrated sigma <= 0.
    """
    a, b, c, d = params
    total = 0.0
    for mu_raw, sigma_raw, y in data:
        mu_cal = a + b * mu_raw
        sigma_cal = c + d * sigma_raw
        if sigma_cal <= 0:
            return 1e9  # penalty — should not occur given bounds, but guard anyway
        total += crps_gaussian(mu_cal, sigma_cal, y)
    return total / len(data)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_training_data(
    city: str,
    db,
    min_samples: int = 60,
    lead_hours: int = 24,
    forecast_source: str | None = None,
) -> list[tuple[float, float, float]]:
    """Build (mu_ensemble, sigma_ensemble, actual_high_f) triples for a city.

    Strategy:
    1. Query model_forecast_log for rows matching *station* and *lead_hours*.
       Each row is a forecast captured at a fixed lead time by the cron worker
       (src/scripts/capture_forecasts.py).
    2. For each date, average all model forecasts (nws/open_meteo/gfs) to form
       the ensemble mean mu_f.  sigma_f is taken from the persisted ``sigma_f``
       column when available; otherwise falls back to ``FORECAST_STDDEV_F``.
    3. For each date, look up the settled daily high from the observations table
       (source='metar', MAX(temp_f) for that date).
    4. Return the joined list; raise InsufficientDataError if fewer than
       min_samples triples are found.

    NOTE: In early deployments (or after the #422 migration) model_forecast_log
    will have zero rows at lead_hours=24.  The function will raise
    InsufficientDataError immediately.  Production callers should catch that and
    skip calibration until enough lead-time rows accumulate.

    Args:
        city:            City name (must match a STATIONS entry, e.g. "Chicago").
        db:              A src.data.db.Database instance.
        min_samples:     Minimum joined (forecast, actual) pairs required. Default 60.
        lead_hours:      Lead-time bin to train on (hours). Default 24 for backward compat.
        forecast_source: Optional forecast stack identifier (e.g. "hrrr_nbm"). When
                         provided, only model_forecast_log rows whose ``model`` column
                         matches this value are included. When None, all models are used
                         (legacy behaviour).

    Returns:
        List of (mu_f, sigma_f, actual_high_f) float triples.

    Raises:
        InsufficientDataError: If fewer than min_samples triples are found.
    """
    station = _city_to_station(city)
    if station is None:
        raise InsufficientDataError(
            f"City '{city}' not found in STATIONS config; cannot fetch training data."
        )

    # Fetch forecast rows filtered to the requested lead-time bin.
    # get_forecast_log_by_lead() is the v2 method; fall back to get_forecast_log()
    # for DB instances that don't yet have the method (e.g. test doubles).
    if hasattr(db, "get_forecast_log_by_lead"):
        forecast_rows = db.get_forecast_log_by_lead(
            station, since_date="2000-01-01", lead_hours=lead_hours
        )
    else:
        # Legacy fallback: no lead_hours filter
        forecast_rows = db.get_forecast_log(station, since_date="2000-01-01")

    # Filter by forecast_source when requested — only use rows whose model column
    # matches the requested stack identifier (e.g. "hrrr_nbm").
    if forecast_source is not None:
        forecast_rows = [r for r in forecast_rows if r.get("model") == forecast_source]

    if not forecast_rows:
        source_info = f", forecast_source='{forecast_source}'" if forecast_source else ""
        raise InsufficientDataError(
            f"model_forecast_log is empty for station '{station}' (city='{city}') "
            f"at lead_hours={lead_hours}{source_info}. "
            f"Need {min_samples} settled days; have 0."
        )

    # Build a date → (mu_f, sigma_f) lookup.
    # Average mu across all models logged for that date; use first non-NULL sigma_f.
    default_sigma = float(FORECAST_STDDEV_F)
    date_mu_accum: dict[str, list[float]] = {}
    date_sigma: dict[str, "float | None"] = {}
    for row in forecast_rows:
        d = row["date"]
        mu = float(row["forecast_high_f"])
        date_mu_accum.setdefault(d, []).append(mu)
        # Use the first non-NULL sigma_f encountered for a given date
        if date_sigma.get(d) is None:
            raw_sigma = row.get("sigma_f")
            date_sigma[d] = float(raw_sigma) if raw_sigma is not None else None

    result: list[tuple[float, float, float]] = []
    for date_str, mu_list in date_mu_accum.items():
        mu_f = sum(mu_list) / len(mu_list)
        sigma_f = date_sigma.get(date_str)
        if sigma_f is None:
            sigma_f = default_sigma  # fallback for rows without persisted spread
        # daily high = MAX(temp_f) for this station on this date
        obs_high = db.get_daily_obs_high(station, date_str)
        if obs_high is None:
            continue  # no observation for this date — skip
        result.append((mu_f, sigma_f, obs_high))

    if len(result) < min_samples:
        raise InsufficientDataError(
            f"Only {len(result)} joined (forecast, actual) pairs found for city='{city}' "
            f"(station='{station}') at lead_hours={lead_hours}; need at least {min_samples}."
        )

    return result


def fit_emos(
    training_data: list[tuple[float, float, float]],
) -> tuple[float, float, float, float]:
    """Fit EMOS linear calibration parameters by minimising mean CRPS.

    Solves:
        (a*, b*, c*, d*) = argmin_θ mean_CRPS(θ; training_data)

    where:
        mu_cal(θ)    = a + b * mu_raw
        sigma_cal(θ) = c + d * sigma_raw

    Bounds: c > 1e-3, d > 1e-3 (ensures sigma_cal > 0).
    Method: L-BFGS-B (handles box constraints efficiently).
    Initial point: identity transform (a=0, b=1, c=0.5, d=1).

    Args:
        training_data: List of (mu_raw, sigma_raw, actual_high_f) triples.

    Returns:
        Tuple (a, b, c, d) of fitted float coefficients.
    """
    x0 = [0.0, 1.0, 0.5, 1.0]
    bounds = [
        (None, None),   # a — unconstrained intercept
        (None, None),   # b — unconstrained slope
        (1e-3, None),   # c > 0 — sigma intercept must be positive
        (1e-3, None),   # d > 0 — sigma slope must be positive
    ]
    result = minimize(
        _crps_loss,
        x0,
        args=(training_data,),
        method="L-BFGS-B",
        bounds=bounds,
    )
    a, b, c, d = result.x
    return float(a), float(b), float(c), float(d)


def save_coefficients(
    city: str,
    a: float,
    b: float,
    c: float,
    d: float,
    crps_score: float,
    db,
    forecast_source: str = "nws_open_meteo",
) -> None:
    """Persist EMOS coefficients to the emos_calibration table.

    Always writes model_mode='emos_shadow' and ready_for_promotion=0.
    Promotion to active use is a deliberate manual step — this function
    never sets ready_for_promotion=1.

    Coefficients for different forecast_source values are stored independently
    — saving for "hrrr_nbm" never overwrites the legacy "nws_open_meteo" row.

    Args:
        city:            City name (e.g. "Chicago").
        a:               EMOS mu intercept.
        b:               EMOS mu slope.
        c:               EMOS sigma intercept (must be > 0).
        d:               EMOS sigma slope (must be > 0).
        crps_score:      Mean CRPS on the training set after fitting.
        db:              A src.data.db.Database instance.
        forecast_source: Forecast stack identifier (default "nws_open_meteo").
    """
    db.upsert_emos_coefficients(
        city=city,
        model_mode="emos_shadow",
        a=a,
        b=b,
        c=c,
        d=d,
        crps_score=crps_score,
        trained_at=datetime.now(timezone.utc).isoformat(),
        ready_for_promotion=0,  # always — manual promotion only
        forecast_source=forecast_source,
    )


def check_ready_for_promotion(
    db,
    forecast_source: str,
    cities: list[str],
) -> bool:
    """Return True only if every city has ready_for_promotion=1 for this source.

    This is the promotion gate: FORECAST_STACK must not be switched to live
    until every city in scope has validated EMOS coefficients retrained on
    the new source. Callers should check this before any FORECAST_STACK change.

    Args:
        db:              A src.data.db.Database instance.
        forecast_source: The new forecast stack identifier (e.g. "hrrr_nbm").
        cities:          List of city names that must all be ready.

    Returns:
        True iff all cities have a row with ready_for_promotion=1 for this source.
    """
    if not cities:
        return False
    rows = db.get_all_emos_calibration()
    promoted = {
        r["city"]
        for r in rows
        if r.get("forecast_source") == forecast_source and r.get("ready_for_promotion") == 1
    }
    return all(city in promoted for city in cities)

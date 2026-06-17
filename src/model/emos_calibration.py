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
) -> list[tuple[float, float, float]]:
    """Build (mu_ensemble, sigma_ensemble, actual_high_f) triples for a city.

    Strategy:
    1. Query model_forecast_log for (date, forecast_high_f) rows for the
       station that corresponds to *city*.
    2. For each date, look up the settled daily high from the observations
       table (source='metar', MAX(temp_f) for that date).
    3. Use FORECAST_STDDEV_F as the constant sigma_ensemble (the ensemble
       spread is a fixed config constant in the current model).
    4. Return the joined list; raise InsufficientDataError if fewer than
       min_samples triples are found.

    NOTE: In early deployments where model_forecast_log is empty, this
    function will raise InsufficientDataError immediately.  Production
    callers should catch that exception and skip calibration until the
    deployment has enough history.

    Args:
        city:        City name (must match a STATIONS entry, e.g. "Chicago").
        db:          A src.data.db.Database instance.
        min_samples: Minimum number of joined (forecast, actual) pairs
                     required before fitting.  Defaults to 60.

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

    # Fetch all forecast log entries for this station (from the beginning)
    forecast_rows = db.get_forecast_log(station, since_date="2000-01-01")
    if not forecast_rows:
        raise InsufficientDataError(
            f"model_forecast_log is empty for station '{station}' (city='{city}'). "
            f"Need {min_samples} settled days; have 0."
        )

    # Build a date → mu_f lookup (latest model entry per date wins via INSERT OR REPLACE)
    date_to_mu: dict[str, float] = {}
    for row in forecast_rows:
        date_to_mu[row["date"]] = float(row["forecast_high_f"])

    # For each forecast date, find the settled daily high from METAR observations
    sigma = float(FORECAST_STDDEV_F)
    result: list[tuple[float, float, float]] = []

    for date_str, mu_f in date_to_mu.items():
        # daily high = MAX(temp_f) for this station on this date
        obs_high = db.get_daily_obs_high(station, date_str)
        if obs_high is None:
            continue  # no observation for this date — skip
        result.append((mu_f, sigma, obs_high))

    if len(result) < min_samples:
        raise InsufficientDataError(
            f"Only {len(result)} joined (forecast, actual) pairs found for city='{city}' "
            f"(station='{station}'); need at least {min_samples}."
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
) -> None:
    """Persist EMOS coefficients to the emos_calibration table.

    Always writes model_mode='emos_shadow' and ready_for_promotion=0.
    Promotion to active use is a deliberate manual step — this function
    never sets ready_for_promotion=1.

    Calling this function twice for the same (city, 'emos_shadow') pair
    will overwrite the first record (INSERT OR REPLACE semantics in db).

    Args:
        city:       City name (e.g. "Chicago").
        a:          EMOS mu intercept.
        b:          EMOS mu slope.
        c:          EMOS sigma intercept (must be > 0).
        d:          EMOS sigma slope (must be > 0).
        crps_score: Mean CRPS on the training set after fitting.
        db:         A src.data.db.Database instance.
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
    )

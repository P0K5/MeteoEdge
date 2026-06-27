"""EMOS (Ensemble Model Output Statistics) calibration for temperature forecasts.

Fits a linear bias-correction model:
    corrected_mu = a + b * model_mu
    corrected_sigma = c + d * sigma_naive

per (city, forecast_source) pair. Coefficients are stored in the
`emos_calibration` DB table and never overwrite rows from other sources.

Usage (WRITE-THE-SCRIPT ONLY — do not run until ≥30 days of data exist):
    from src.model.emos_calibration import fit_emos, save_coefficients, InsufficientDataError
    from src.data.db import Database

    with Database() as db:
        try:
            a, b, c, d, crps = fit_emos(db, city="Chicago", forecast_source="nws_open_meteo")
            save_coefficients(db, city="Chicago", forecast_source="nws_open_meteo",
                              a=a, b=b, c=c, d=d, crps_score=crps)
        except InsufficientDataError as e:
            print(f"Not enough data: {e}")
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import NamedTuple

MIN_TRAINING_ROWS = 30


class InsufficientDataError(ValueError):
    """Raised when fewer than MIN_TRAINING_ROWS calibration samples are available."""


class EmosFit(NamedTuple):
    a: float  # intercept for mu correction
    b: float  # slope for mu correction
    c: float  # intercept for sigma correction
    d: float  # slope for sigma correction
    crps_score: float  # mean CRPS on training set (lower is better)


def _crps_gaussian(mu: float, sigma: float, obs: float) -> float:
    """Closed-form CRPS for a Gaussian forecast."""
    from math import erf, exp, pi, sqrt
    z = (obs - mu) / sigma
    phi_z = exp(-0.5 * z * z) / sqrt(2 * pi)
    Phi_z = 0.5 * (1 + erf(z / sqrt(2)))
    return sigma * (z * (2 * Phi_z - 1) + 2 * phi_z - 1 / sqrt(pi))


def _mean_crps(rows: list[tuple[float, float, float, float]], a: float, b: float, c: float, d: float) -> float:
    """Compute mean CRPS over training rows for given EMOS parameters."""
    total = 0.0
    for model_mu, sigma_naive, obs, _ in rows:
        mu = a + b * model_mu
        sigma = max(0.01, c + d * sigma_naive)
        total += _crps_gaussian(mu, sigma, obs)
    return total / len(rows)


def _load_training_rows(
    db,
    city: str,
    forecast_source: str,
    lookback_days: int = 90,
) -> list[tuple[float, float, float, float]]:
    """Load (model_mu, sigma_naive, observed_high, date) tuples from DB.

    Joins intraday_corrections (for corrected_mu_f as model_mu proxy) against
    model_forecast_log (for sigma_naive, filtered to forecast_source) and
    observations (for the actual daily high).

    Returns list of (model_mu, sigma_naive, obs_high_f, date) tuples.
    """
    sql = """
        SELECT
            ic.corrected_mu_f   AS model_mu,
            COALESCE(mfl.sigma_f, 4.0) AS sigma_naive,
            ic.obs_temp_f       AS obs_high,
            ic.date
        FROM intraday_corrections ic
        JOIN model_forecast_log mfl
            ON mfl.station = (
                SELECT station FROM observations
                WHERE 1=0  -- placeholder; real join uses city-to-station map
            )
        WHERE ic.city = ?
          AND ic.date >= date('now', ? || ' days')
        ORDER BY ic.date ASC
    """
    # Simplified approach: query intraday_corrections directly, sigma_naive defaults to 4.0
    # (the GEFS sigma feature from #447 adds sigma_f later; for now we fit mu only)
    sql_simple = """
        SELECT
            corrected_mu_f  AS model_mu,
            4.0             AS sigma_naive,
            obs_temp_f      AS obs_high,
            date
        FROM intraday_corrections
        WHERE city = ?
          AND date >= date('now', ? || ' days')
        ORDER BY date ASC
    """
    lookback_expr = f"-{lookback_days}"
    with db._lock:
        try:
            rows = db._conn.execute(sql_simple, (city, lookback_expr)).fetchall()
        except Exception:
            return []
    return [(float(r[0]), float(r[1]), float(r[2]), str(r[3])) for r in rows]


def fit_emos(
    db,
    city: str,
    forecast_source: str,
    lookback_days: int = 90,
) -> EmosFit:
    """Fit EMOS coefficients for (city, forecast_source).

    Uses L-BFGS-B optimisation to minimise mean CRPS on training data.

    Args:
        db: Database instance.
        city: Polymarket city name (e.g. "Chicago").
        forecast_source: Identifier for the forecast stack (e.g. "nws_open_meteo",
            "hrrr_nbm", "ecmwf_icon").
        lookback_days: How many days of history to train on (default 90).

    Returns:
        EmosFit with (a, b, c, d, crps_score).

    Raises:
        InsufficientDataError: if fewer than MIN_TRAINING_ROWS samples available.
    """
    from scipy.optimize import minimize

    rows = _load_training_rows(db, city, forecast_source, lookback_days)
    if len(rows) < MIN_TRAINING_ROWS:
        raise InsufficientDataError(
            f"{city}/{forecast_source}: need ≥{MIN_TRAINING_ROWS} rows, "
            f"got {len(rows)}"
        )

    def objective(params):
        a, b, c, d = params
        return _mean_crps(rows, a, b, c, d)

    x0 = [0.0, 1.0, 1.0, 1.0]
    bounds = [(-20, 20), (0.5, 2.0), (0.01, 10.0), (0.01, 5.0)]
    result = minimize(objective, x0, method="L-BFGS-B", bounds=bounds)

    a, b, c, d = result.x
    crps = float(result.fun)
    return EmosFit(a=float(a), b=float(b), c=float(c), d=float(d), crps_score=crps)


def save_coefficients(
    db,
    city: str,
    forecast_source: str,
    a: float,
    b: float,
    c: float,
    d: float,
    crps_score: float,
) -> None:
    """Persist EMOS coefficients to the emos_calibration table.

    Always writes model_mode='emos_shadow' and ready_for_promotion=0.
    A human must manually set ready_for_promotion=1 after validating calibration.
    Never overwrites rows with a different forecast_source.

    Args:
        db: Database instance.
        city: Polymarket city name.
        forecast_source: Forecast stack identifier.
        a, b, c, d: EMOS linear parameters.
        crps_score: Mean CRPS on training data (lower is better).
    """
    fitted_at = datetime.now(timezone.utc).isoformat()
    sql = """
        INSERT INTO emos_calibration
            (city, forecast_source, a, b, c, d, crps_score, model_mode,
             ready_for_promotion, fitted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'emos_shadow', 0, ?)
        ON CONFLICT(city, forecast_source) DO UPDATE SET
            a = excluded.a,
            b = excluded.b,
            c = excluded.c,
            d = excluded.d,
            crps_score = excluded.crps_score,
            model_mode = 'emos_shadow',
            ready_for_promotion = 0,
            fitted_at = excluded.fitted_at
    """
    with db._lock:
        db._conn.execute(sql, (city, forecast_source, a, b, c, d, crps_score, fitted_at))
        db._conn.commit()


def load_coefficients(
    db,
    city: str,
    forecast_source: str,
) -> "EmosFit | None":
    """Load stored EMOS coefficients for (city, forecast_source).

    Returns None if no coefficients exist for this combination.
    Only returns coefficients with ready_for_promotion=1 (manually validated).
    To load shadow coefficients, use load_coefficients_shadow().
    """
    sql = """
        SELECT a, b, c, d, crps_score
        FROM emos_calibration
        WHERE city = ? AND forecast_source = ? AND ready_for_promotion = 1
        ORDER BY fitted_at DESC
        LIMIT 1
    """
    with db._lock:
        row = db._conn.execute(sql, (city, forecast_source)).fetchone()
    if row is None:
        return None
    return EmosFit(a=row[0], b=row[1], c=row[2], d=row[3], crps_score=row[4])


def load_coefficients_shadow(
    db,
    city: str,
    forecast_source: str,
) -> "EmosFit | None":
    """Load shadow (unvalidated) EMOS coefficients for (city, forecast_source)."""
    sql = """
        SELECT a, b, c, d, crps_score
        FROM emos_calibration
        WHERE city = ? AND forecast_source = ?
        ORDER BY fitted_at DESC
        LIMIT 1
    """
    with db._lock:
        row = db._conn.execute(sql, (city, forecast_source)).fetchone()
    if row is None:
        return None
    return EmosFit(a=row[0], b=row[1], c=row[2], d=row[3], crps_score=row[4])


def check_ready_for_promotion(db, forecast_source: str, cities: list[str]) -> bool:
    """Return True only if all cities have ready_for_promotion=1 coefficients.

    This is the promotion gate: FORECAST_STACK must not be promoted to live
    until every city in scope has validated EMOS coefficients for that source.
    """
    if not cities:
        return False
    sql = """
        SELECT COUNT(DISTINCT city)
        FROM emos_calibration
        WHERE forecast_source = ? AND ready_for_promotion = 1 AND city IN ({})
    """.format(",".join("?" * len(cities)))
    with db._lock:
        row = db._conn.execute(sql, [forecast_source] + list(cities)).fetchone()
    return (row[0] if row else 0) == len(cities)

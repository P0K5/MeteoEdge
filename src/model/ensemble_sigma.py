"""Per-station conditional sigma estimator from GEFS ensemble spread.

Given the 30 GEFS member daily-high values for a station, returns a
calibrated σ estimate in °F for use in downstream probability models.

Two estimators are available:
- **Naive**: standard deviation of the member values (σ_naive).
- **Calibrated**: simple linear regression of σ_naive against realised
  absolute error |actual - mu_raw| per station, fitted from historical
  model_forecast_log + settlements pairs.

The calibrated estimator is selected automatically when history_db is
provided AND at least MIN_CALIBRATION_SAMPLES historical rows are
available for the station.  When history is insufficient the naive
estimator is used as a fallback.

σ floor: 1.0 °F — never return a pathologically narrow distribution.

Capture vs. consumption split (issue #555)
--------------------------------------------
Prior to #555, ``compute_ensemble_sigma()`` was called directly by the
capture worker (``src/scripts/capture_forecasts.py``) and its floored
return value was persisted verbatim to ``model_forecast_log.sigma_f``.
That destroyed the real ensemble-spread signal at logging time: 240/345
GEFS rows sat at exactly the 1.00°F floor, starving EMOS's spread
coefficient ``d`` (``σ_calibrated = c + d·σ_ensemble``) of any signal to
learn from.

The floor now applies **only at consumption time**, not at capture time:

- ``raw_member_sigma()`` — capture-time.  Returns the *unfloored* sample
  stdev of the raw ensemble members (or ``None`` when it cannot be
  computed).  This is what ``src/scripts/capture_forecasts.py`` persists
  to ``model_forecast_log.sigma_f`` for the ``gefs`` channel.  A genuine
  near-zero spread is logged as a genuine near-zero value — never
  silently clamped up.
- ``compute_ensemble_sigma()`` — consumption-time.  Unchanged behaviour:
  still applies ``SIGMA_FLOOR_F`` (and the historical-calibration
  regression when enough history is available).  This is the sanctioned
  entry point for anything that needs a ready-to-use, floor-safe σ for
  live probability/trading computations (envelope, EMOS "shadow"
  training self-calibration, etc.) — its output is unchanged by #555 so
  no existing or future caller sees a behaviour change.

This module is compute-only.  It does NOT:
- Write any rows to the database (that is tracked in #448, Week 3).
- Import from src/trading/, src/strategy/, or src/model/envelope.py.
- Modify EMOS coefficients or the emos_calibration table.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

from scipy.stats import linregress

SIGMA_FLOOR_F: float = 1.0
MIN_CALIBRATION_SAMPLES: int = 30


def _naive_sigma(members: list[float]) -> float:
    """Return max(stdev(members), SIGMA_FLOOR_F).

    Uses population stdev with ddof=1 (sample stdev) when len(members) > 1.
    Returns SIGMA_FLOOR_F when all members are identical or fewer than 2 are given.
    """
    if len(members) < 2:
        return SIGMA_FLOOR_F
    try:
        s = statistics.stdev(members)
    except statistics.StatisticsError:
        return SIGMA_FLOOR_F
    return max(s, SIGMA_FLOOR_F)


def raw_member_sigma(members: list[float]) -> "float | None":
    """Return the UNFLOORED sample stdev of ensemble members (capture-time).

    This is the raw member-spread signal EMOS needs as its σ input (#555):
    ``σ_calibrated = c + d·σ_ensemble`` cannot learn ``d`` from a σ_ensemble
    that has already been clamped to a floor before it was logged.

    Unlike ``compute_ensemble_sigma()``, this function performs NO
    calibration-regression and applies NO ``SIGMA_FLOOR_F`` clamp — the true
    (possibly near-zero) spread is returned as-is. Callers that need a
    floor-safe σ for live consumption (envelope/probability computation)
    must go through ``compute_ensemble_sigma()`` instead; the floor must
    never be baked into what gets persisted to ``model_forecast_log``.

    Args:
        members: List of per-member daily-high values in °F.

    Returns:
        Sample stdev (ddof=1) of ``members``, or ``None`` when fewer than 2
        members are given (stdev is undefined) — callers should persist
        ``NULL`` in that case rather than inventing a placeholder number.
    """
    if len(members) < 2:
        return None
    try:
        return statistics.stdev(members)
    except statistics.StatisticsError:
        return None


def _load_calibration_pairs(
    station: str,
    history_db,
    lookback_days: int = 90,
) -> list[tuple[float, float]]:
    """Return (sigma_naive, abs_error) pairs from historical data.

    Pairs are built by joining model_forecast_log rows with settlement rows
    on (station, date).  Only rows where both forecast and settlement exist
    are included.

    Returns an empty list if the DB has no relevant rows or if the DB does
    not support the required query.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    try:
        # model_forecast_log rows with sigma_f: only the migrated schema has it.
        # Fall back gracefully to an empty list if the column is absent.
        forecast_rows = history_db._conn.execute(
            "SELECT date, forecast_high_f, sigma_f FROM model_forecast_log "
            "WHERE station=? AND date>=? AND sigma_f IS NOT NULL ORDER BY date ASC",
            (station, since),
        ).fetchall()
    except Exception:
        return []

    if not forecast_rows:
        return []

    # Build a lookup from date → (forecast_high_f, sigma_f)
    forecast_by_date: dict[str, tuple[float, float]] = {
        row[0]: (float(row[1]), float(row[2])) for row in forecast_rows
    }

    try:
        settlement_rows = history_db._conn.execute(
            "SELECT DATE(ts) as settle_date, actual_high_f "
            "FROM settlements WHERE station=? AND ts>=? ORDER BY ts ASC",
            (station, since + "T00:00:00Z"),
        ).fetchall()
    except Exception:
        return []

    pairs: list[tuple[float, float]] = []
    for row in settlement_rows:
        date_str = row[0]
        actual = float(row[1])
        if date_str in forecast_by_date:
            mu_raw, sigma_naive = forecast_by_date[date_str]
            abs_err = abs(actual - mu_raw)
            pairs.append((sigma_naive, abs_err))

    return pairs


def _calibrated_sigma(sigma_naive: float, pairs: list[tuple[float, float]]) -> float:
    """Apply linear regression calibration: sigma_cal = slope * sigma_naive + intercept.

    Uses scipy.stats.linregress on historical (sigma_naive, abs_error) pairs.
    Falls back to sigma_naive when regression is degenerate (zero variance in x).
    """
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]

    if len(set(xs)) < 2:
        # All x-values identical — regression is undefined; return naive
        return max(sigma_naive, SIGMA_FLOOR_F)

    result = linregress(xs, ys)
    sigma_cal = result.slope * sigma_naive + result.intercept
    return max(sigma_cal, SIGMA_FLOOR_F)


def compute_ensemble_sigma(
    members: list[float],
    station: str,
    history_db=None,
) -> float:
    """Return calibrated σ in °F from GEFS ensemble member spread.

    Args:
        members:    List of per-member daily-high values in °F (typically 30 values
                    from GEFS gec00 + gep01…gep30).  Must have at least 1 element.
        station:    METAR station code (e.g. 'KORD').  Used to select historical
                    calibration data from history_db.
        history_db: Optional src.data.db.Database instance.  When provided and
                    >= MIN_CALIBRATION_SAMPLES (30) historical pairs are available,
                    the calibrated estimator is used.  When None or insufficient
                    history, falls back to the naive estimator.

    Returns:
        σ estimate in °F, always >= SIGMA_FLOOR_F (1.0 °F).

    Notes:
        - DO NOT import this module from src/trading/, src/strategy/, or
          src/model/envelope.py.  Integration is tracked in #448 (Week 3).
        - This function does not write to the database.
    """
    sigma_naive = _naive_sigma(members)

    if history_db is None:
        return sigma_naive

    pairs = _load_calibration_pairs(station, history_db)
    if len(pairs) < MIN_CALIBRATION_SAMPLES:
        return sigma_naive

    return _calibrated_sigma(sigma_naive, pairs)

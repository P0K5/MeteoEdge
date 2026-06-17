"""DEB (Dynamic Error Balancing) weight computation module.

Computes per-model forecast weights based on exponential-decay RMSE over a
rolling window of forecast-vs-actual pairs.  Three models are tracked:
  - "nws"        : NWS daily-high forecast (US stations only)
  - "open_meteo" : Open-Meteo best-match daily-high forecast (global)
  - "gfs"        : Open-Meteo GFS seamless daily-high forecast (global)

NWS is unavailable for international stations (those stations only accumulate
"open_meteo" and "gfs" rows in model_forecast_log).  The phantom-model guard
below excludes models with zero rows from the blend, so international stations
naturally receive a two-model (open_meteo + gfs) ensemble once enough data
accumulates.

Phantom-model guard: a model that has zero model_forecast_log rows in the
trailing window (e.g. NWS at international stations) is excluded from the
blend entirely.  Remaining weights are renormalised to sum to 1.0.  A
WARNING is logged for each excluded phantom contributor.

When DEB_ENABLED is false (default) or when fewer than MIN_SAMPLES pairs
exist for any model, equal weights (1/3 each) are returned silently.
"""
import math
import os
import logging
from datetime import date as date_cls, timedelta

log = logging.getLogger(__name__)

_MIN_SAMPLES = int(os.getenv("DEB_MIN_SAMPLES", "10"))
_REFRESH_CADENCE_H = float(os.getenv("DEB_REFRESH_CADENCE_HOURS", "24"))
_DECAY_RATE = 0.05  # per day; e^(-0.05*k) weights errors k days ago

MODELS: tuple[str, ...] = ("nws", "open_meteo", "gfs")
EQUAL_WEIGHTS: dict[str, float] = {m: round(1.0 / len(MODELS), 10) for m in MODELS}
MIN_SAMPLES: int = _MIN_SAMPLES


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _decay_weight(days_ago: int) -> float:
    """Return the exponential decay weight for an error that is *days_ago* days old."""
    return math.exp(-_DECAY_RATE * days_ago)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def log_forecast(db, station: str, model: str, date: str, forecast_high_f: float) -> None:
    """Write a forecast to model_forecast_log via db.upsert_forecast_log().

    No-op when *db* is None (e.g. in tests or when the DB is unavailable).
    """
    if db is None:
        return
    db.upsert_forecast_log(
        station=station,
        model=model,
        date=date,
        forecast_high_f=forecast_high_f,
    )


def compute_weights(
    db, station: str, city: str, window_days: int = 30,
) -> tuple[dict[str, float], dict[str, float]]:
    """Compute inverse-error weights for each model over the last *window_days* days.

    Joins model_forecast_log with settlements on (station, date) to obtain
    forecast-vs-actual pairs.  RMSE is computed with exponential time-decay so
    that recent errors matter more than older ones.

    Returns:
        (weights, rmse) where both are dicts keyed by model name.
        weights sums to 1.0.  rmse values are 0.0 on equal-weights fallback.

    Phantom-model guard: models with zero log rows in the window are excluded
    before any weight computation.  A WARNING is emitted per phantom model.
    If only one model has rows, it receives weight=1.0.
    Falls back to EQUAL_WEIGHTS (logged at DEBUG) when all candidate models
    have fewer than _MIN_SAMPLES valid pairs.
    """
    since_date = (date_cls.today() - timedelta(days=window_days)).isoformat()
    log_rows = db.get_forecast_log(station, since_date)
    settlements = db.get_settlements(station, since_date + "T00:00:00")

    # Build actual_high lookup: date_str -> actual_high_f
    actuals: dict[str, float] = {}
    for row in settlements:
        d = row["ts"][:10]
        actuals[d] = row["actual_high_f"]

    # Count raw log rows per model (before filtering against actuals).
    # A model with zero rows in the window is a phantom contributor and must
    # be excluded from the blend entirely -- regardless of MIN_SAMPLES.
    raw_row_counts: dict[str, int] = {m: 0 for m in MODELS}
    for row in log_rows:
        m = row.get("model")
        if m in raw_row_counts:
            raw_row_counts[m] += 1

    # Drop phantom models (zero log rows) and warn.
    active_models = []
    for m in MODELS:
        if raw_row_counts[m] == 0:
            log.warning(
                "[deb] city=%s model=%s has no forecast rows — excluded from blend",
                city, m,
            )
        else:
            active_models.append(m)

    _zero_rmse = {m: 0.0 for m in MODELS}

    # If no models have any rows at all, fall back to equal weights.
    if not active_models:
        log.debug(
            "[deb] no forecast rows for %s in trailing %d-day window -- using equal weights",
            city, window_days,
        )
        return dict(EQUAL_WEIGHTS), _zero_rmse

    # Group (days_ago, abs_error) pairs by model -- only for active models.
    errors: dict[str, list[tuple[int, float]]] = {m: [] for m in active_models}
    today = date_cls.today()
    for row in log_rows:
        m = row.get("model")
        if m not in errors:
            continue
        d = row["date"]
        if d not in actuals:
            continue
        days_ago = (today - date_cls.fromisoformat(d)).days
        err = abs(row["forecast_high_f"] - actuals[d])
        errors[m].append((days_ago, err))

    # Check minimum samples for each active model; fall back to equal weights
    # if any active model is below the threshold.
    for m in active_models:
        if len(errors[m]) < _MIN_SAMPLES:
            log.debug(
                "[deb] insufficient samples for %s/%s (%d < %d) -- using equal weights",
                city, m, len(errors[m]), _MIN_SAMPLES,
            )
            return dict(EQUAL_WEIGHTS), _zero_rmse

    # Compute decay-weighted RMSE per active model.
    rmse: dict[str, float] = {}
    for m in active_models:
        total_w = sum(_decay_weight(k) for k, _ in errors[m])
        weighted_mse = sum(_decay_weight(k) * e ** 2 for k, e in errors[m]) / total_w
        rmse[m] = math.sqrt(weighted_mse)

    # Inverse-error weights, normalised to sum to 1.0.
    # Only active models receive weight; phantom models get 0.0.
    raw = {m: 1.0 / rmse[m] for m in active_models}
    total = sum(raw.values())
    weights = {m: 0.0 for m in MODELS}
    for m in active_models:
        weights[m] = raw[m] / total
    return weights, rmse


def refresh_weights(db, station: str, city: str) -> None:
    """Recompute and persist model weights for *city* / *station*.

    Skips when:
    - DEB_ENABLED env var is not "true"  (default: false)
    - weights were already refreshed today (checked via model_weights table)

    When weights are written, each model row is upserted with today's date.
    The RMSE is stored as 0.0 when the computation fell back to equal weights
    (no data or insufficient samples).
    """
    if os.getenv("DEB_ENABLED", "false").lower() != "true":
        return

    today = date_cls.today().isoformat()

    # Skip if we already refreshed today for this city
    existing = db.get_model_weights(city)
    if existing and existing[0]["date"] == today:
        return

    weights, rmse = compute_weights(db, station, city)
    for model, weight in weights.items():
        db.upsert_model_weight(
            city=city,
            model=model,
            date=today,
            weight=weight,
            rmse=rmse.get(model, 0.0),
        )
    log.info("[deb] refreshed weights for %s: %s  rmse=%s", city, weights, rmse)


def get_weights(db, city: str) -> dict[str, float]:
    """Return the most-recent persisted weights for *city*.

    Returns EQUAL_WEIGHTS when:
    - DEB_ENABLED is not "true"
    - No rows exist in model_weights for *city*
    """
    if os.getenv("DEB_ENABLED", "false").lower() != "true":
        return dict(EQUAL_WEIGHTS)

    rows = db.get_model_weights(city)
    if not rows:
        return dict(EQUAL_WEIGHTS)

    # Most recent date first (db.get_model_weights orders by date DESC).
    # Read the latest weight for each tracked model.
    latest: dict[str, float] = {}
    for row in rows:
        m = row["model"]
        if m not in latest:
            latest[m] = row["weight"]
        if len(latest) == len(MODELS):
            break

    # If any tracked model is missing from the table, fall back to equal weights.
    if any(m not in latest for m in MODELS):
        return dict(EQUAL_WEIGHTS)

    return latest


def check_weight_quality(db, station: str, city: str, window_days: int = 30) -> list[str]:
    """Data-quality check: return a list of violation strings for *city*/*station*.

    A violation is raised when a model carries non-zero weight in model_weights
    but has zero model_forecast_log rows in the trailing *window_days* window.
    Returns an empty list when everything is consistent.

    Intended to be called on startup after DEB weights have been refreshed.
    Each violation is also logged at WARNING level.
    """
    violations: list[str] = []

    rows = db.get_model_weights(city)
    if not rows:
        return violations

    # Build the latest weight per model from persisted model_weights.
    latest_weight: dict[str, float] = {}
    for row in rows:
        m = row["model"]
        if m not in latest_weight:
            latest_weight[m] = row["weight"]
        if len(latest_weight) == len(MODELS):
            break

    since_date = (date_cls.today() - timedelta(days=window_days)).isoformat()
    log_rows = db.get_forecast_log(station, since_date)

    # Count log rows per model in the trailing window.
    row_counts: dict[str, int] = {m: 0 for m in MODELS}
    for row in log_rows:
        m = row.get("model")
        if m in row_counts:
            row_counts[m] += 1

    for m, weight in latest_weight.items():
        if weight > 0.0 and row_counts.get(m, 0) == 0:
            msg = (
                f"[deb] city={city} model={m} carries weight={weight:.4f} "
                f"but has no forecast rows in the trailing {window_days}-day window"
            )
            log.warning(msg)
            violations.append(msg)

    return violations

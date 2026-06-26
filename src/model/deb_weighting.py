"""DEB (Dynamic Error Balancing) weight computation module.

Computes per-model forecast weights based on exponential-decay RMSE over a
rolling window of forecast-vs-actual pairs.

Models are managed through a registry (register_model) that supports adding new
forecast channels declaratively without touching compute logic.

Pre-registered channels:
  - "nws"        : NWS daily-high forecast  (US, 24h cadence)
  - "open_meteo" : Open-Meteo daily-high    (global, 24h cadence)
  - "gfs"        : GFS daily-high forecast  (global, 6h cadence)

When DEB_ENABLED is false (default) or when fewer than MIN_SAMPLES pairs exist,
equal weights are returned silently.

Tunable parameters (all overridable via env vars):
  DEB_MIN_SAMPLES          – minimum pairs before DEB activates (default 10)
  DEB_REFRESH_CADENCE_HOURS – weight refresh cadence in hours (default 24)
  DEB_BASE_DECAY_RATE      – base exponential decay rate per day (default 0.05)
  DEB_GROUP_WEIGHT_CAP     – max combined weight for any group_id (default 0.7)
"""
import math
import os
import logging
from dataclasses import dataclass, field
from datetime import date as date_cls, timedelta
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config params (env-var overridable, live-read at call time)
# ---------------------------------------------------------------------------

_MIN_SAMPLES = int(os.getenv("DEB_MIN_SAMPLES", "10"))
_REFRESH_CADENCE_H = float(os.getenv("DEB_REFRESH_CADENCE_HOURS", "24"))

# BASE_DECAY_RATE: per-day decay for a 24h-cadence model.
# Cadence-aware rate per model = BASE_DECAY_RATE * (cadence_h / 24).
BASE_DECAY_RATE: float = float(os.getenv("DEB_BASE_DECAY_RATE", "0.05"))

# GROUP_WEIGHT_CAP: max total weight allowed for any single group_id.
# Models without a group_id are uncapped.
GROUP_WEIGHT_CAP: float = float(os.getenv("DEB_GROUP_WEIGHT_CAP", "0.7"))

MIN_SAMPLES: int = _MIN_SAMPLES


# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

@dataclass
class _ModelEntry:
    name: str
    region: str                   # "us", "eu", or "global"
    expected_cadence_h: float     # hours between forecasts
    group_id: Optional[str]       # correlated-channel group (None = uncapped)
    cold_start_fraction: float    # weight multiplier during cold-start (< MIN_SAMPLES)


_REGISTRY: dict[str, _ModelEntry] = {}


def register_model(
    name: str,
    region: str,
    expected_cadence_h: float,
    group_id: Optional[str] = None,
    cold_start_fraction: float = 0.5,
) -> None:
    """Register a forecast model in the DEB registry.

    Args:
        name: string key (e.g. "nws", "open_meteo", "hrrr")
        region: one of "us", "eu", "global"
        expected_cadence_h: hours between forecast updates
        group_id: optional string grouping correlated channels
        cold_start_fraction: weight multiplier for this model during cold-start
                             (fewer than MIN_SAMPLES pairs). Default 0.5x.
    """
    _REGISTRY[name] = _ModelEntry(
        name=name,
        region=region,
        expected_cadence_h=expected_cadence_h,
        group_id=group_id,
        cold_start_fraction=cold_start_fraction,
    )


# Pre-register the 3 legacy channels
register_model("nws",        region="us",     expected_cadence_h=24.0, group_id="noaa_us")
register_model("open_meteo", region="global", expected_cadence_h=24.0, group_id=None)
register_model("gfs",        region="global", expected_cadence_h=6.0,  group_id=None)


def _models_for_region(station_region: str) -> list[_ModelEntry]:
    """Return registry entries applicable to *station_region*.

    A model is applicable when its region equals the station_region OR is "global".
    """
    return [
        m for m in _REGISTRY.values()
        if m.region == station_region or m.region == "global"
    ]


def _model_names_for_region(station_region: str) -> tuple[str, ...]:
    return tuple(m.name for m in _models_for_region(station_region))


# ---------------------------------------------------------------------------
# Module-level convenience exports (backward compat)
# ---------------------------------------------------------------------------

# MODELS: tuple of model names for the default "us" region.
# Computed from the registry so callers that iterate over it still work.
# Order: nws, open_meteo, gfs  (same as before; registry insertion order preserved)
MODELS: tuple[str, ...] = _model_names_for_region("us")

# EQUAL_WEIGHTS: uniform weight dict for the default "us" region.
# Recomputed if registry changes via register_model after module load.
def _equal_weights_for(station_region: str = "us") -> dict[str, float]:
    names = _model_names_for_region(station_region)
    w = 1.0 / len(names) if names else 1.0
    return {n: w for n in names}


EQUAL_WEIGHTS: dict[str, float] = _equal_weights_for("us")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _decay_weight(days_ago: int, decay_rate: float) -> float:
    """Return the exponential decay weight for an error *days_ago* days old."""
    return math.exp(-decay_rate * days_ago)


def _cadence_decay_rate(entry: _ModelEntry) -> float:
    """Per-model decay rate scaled by forecast cadence.

    A 24h-cadence model gets BASE_DECAY_RATE unchanged.
    A 6h-cadence model (GFS) gets BASE_DECAY_RATE * (6/24) = 0.0125 at default,
    preventing over-discounting older observations that arrive more frequently.
    """
    return BASE_DECAY_RATE * (entry.expected_cadence_h / 24.0)


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
    db,
    station: str,
    city: str,
    window_days: int = 30,
    station_region: str = "us",
) -> dict[str, float]:
    """Compute inverse-error weights for each model over the last *window_days* days.

    Joins model_forecast_log with settlements on (station, date) to obtain
    forecast-vs-actual pairs.  RMSE is computed with exponential time-decay
    (cadence-aware per model) so that recent errors matter more than older ones.
    Returns a dict that sums to 1.0.

    New behaviours vs legacy code:
    - Regional applicability: models whose region doesn't match station_region
      (and isn't "global") are excluded before any weight computation.
    - Cadence-aware decay: models with shorter cadences get proportionally lower
      decay rates, preventing over-discounting of high-frequency observations.
    - Cold-start policy: models below MIN_SAMPLES get cold_start_fraction * (1/N)
      weight; remaining weight is distributed proportionally to calibrated models.
      Falls back to full equal weights only when ALL models are in cold-start.
    - Group weight cap: total weight of any group_id is capped at GROUP_WEIGHT_CAP.
      Models with no group_id are uncapped.

    Backward compat: for legacy 3-channel US stations the result is identical to
    the old 2-model code once GFS data accumulates; during cold-start the new
    policy blends more gracefully than the hard equal-weight fallback.
    """
    applicable = _models_for_region(station_region)
    if not applicable:
        return _equal_weights_for(station_region)

    applicable_names = [m.name for m in applicable]
    equal_w = _equal_weights_for(station_region)

    since_date = (date_cls.today() - timedelta(days=window_days)).isoformat()
    # Use lead-hours-filtered log when available (introduced in #422) to avoid
    # mixing nowcast snapshots with genuine 24h-ahead forecasts.
    if hasattr(db, "get_forecast_log_by_lead"):
        log_rows = db.get_forecast_log_by_lead(station, since_date, lead_hours=24)
    else:
        log_rows = db.get_forecast_log(station, since_date)
    settlements = db.get_settlements(station, since_date + "T00:00:00")

    # Build actual_high lookup: date_str -> actual_high_f
    actuals: dict[str, float] = {}
    for row in settlements:
        d = row["ts"][:10]
        actuals[d] = row["actual_high_f"]

    # Group (days_ago, abs_error) pairs by model
    errors: dict[str, list[tuple[int, float]]] = {m: [] for m in applicable_names}
    today = date_cls.today()
    for row in log_rows:
        model_name = row["model"]
        if model_name not in errors:
            continue  # model not applicable for this station_region
        d = row["date"]
        if d not in actuals:
            continue
        days_ago = (today - date_cls.fromisoformat(d)).days
        err = abs(row["forecast_high_f"] - actuals[d])
        errors[model_name].append((days_ago, err))

    # Classify models: calibrated vs cold-start
    cold_start: list[str] = []
    calibrated: list[str] = []
    for m in applicable_names:
        if len(errors[m]) < _MIN_SAMPLES:
            cold_start.append(m)
        else:
            calibrated.append(m)

    # All models in cold-start: fall back to full equal weights
    if len(cold_start) == len(applicable_names):
        for m in cold_start:
            log.debug(
                "[deb] insufficient samples for %s/%s (%d < %d) — using equal weights",
                city, m, len(errors[m]), _MIN_SAMPLES,
            )
        return dict(equal_w)

    n_models = len(applicable_names)

    # Compute decay-weighted RMSE for calibrated models
    rmse: dict[str, float] = {}
    for m in calibrated:
        entry = _REGISTRY[m]
        decay_rate = _cadence_decay_rate(entry)
        total_w = sum(_decay_weight(k, decay_rate) for k, _ in errors[m])
        weighted_mse = sum(_decay_weight(k, decay_rate) * e ** 2 for k, e in errors[m]) / total_w
        rmse[m] = math.sqrt(weighted_mse)

    # Inverse-error raw weights for calibrated models
    raw_calibrated: dict[str, float] = {m: 1.0 / rmse[m] for m in calibrated}
    total_calibrated = sum(raw_calibrated.values())

    # Cold-start models get cold_start_fraction * (1/N_models) each
    cold_start_reserved = sum(
        _REGISTRY[m].cold_start_fraction / n_models for m in cold_start
    )
    calibrated_budget = 1.0 - cold_start_reserved

    # Distribute calibrated_budget proportionally among calibrated models
    weights: dict[str, float] = {}
    for m in calibrated:
        weights[m] = raw_calibrated[m] / total_calibrated * calibrated_budget
    for m in cold_start:
        weights[m] = _REGISTRY[m].cold_start_fraction / n_models
        log.debug(
            "[deb] cold-start for %s/%s (%d < %d) — assigned %.4f weight",
            city, m, len(errors[m]), _MIN_SAMPLES, weights[m],
        )

    # Apply group weight cap
    weights = _apply_group_cap(weights, applicable)

    return weights


def _apply_group_cap(
    weights: dict[str, float],
    applicable: list[_ModelEntry],
) -> dict[str, float]:
    """Cap the total weight of any group_id to GROUP_WEIGHT_CAP.

    Models without a group_id are uncapped. When a group exceeds the cap, its
    members are scaled down proportionally and the freed weight is redistributed
    to uncapped models proportionally to their current weights.
    """
    # Collect group totals
    group_models: dict[str, list[str]] = {}
    ungrouped: list[str] = []
    for m in applicable:
        if m.group_id is not None:
            group_models.setdefault(m.group_id, []).append(m.name)
        else:
            ungrouped.append(m.name)

    result = dict(weights)
    freed = 0.0

    for gid, members in group_models.items():
        group_total = sum(result.get(m, 0.0) for m in members)
        if group_total > GROUP_WEIGHT_CAP and group_total > 0:
            scale = GROUP_WEIGHT_CAP / group_total
            excess = group_total - GROUP_WEIGHT_CAP
            freed += excess
            for m in members:
                result[m] = result.get(m, 0.0) * scale

    # Redistribute freed weight to uncapped models proportionally
    if freed > 0 and ungrouped:
        uncapped_total = sum(result.get(m, 0.0) for m in ungrouped)
        if uncapped_total > 0:
            for m in ungrouped:
                result[m] = result.get(m, 0.0) + freed * (result.get(m, 0.0) / uncapped_total)
        else:
            # All uncapped models have zero weight — distribute equally
            per_model = freed / len(ungrouped)
            for m in ungrouped:
                result[m] = result.get(m, 0.0) + per_model

    return result


def refresh_weights(db, station: str, city: str) -> None:
    """Recompute and persist model weights for *city* / *station*.

    Skips when:
    - DEB_ENABLED env var is not "true"  (default: false)
    - weights were already refreshed today (checked via model_weights table)

    When weights are written, each model row is upserted with today's date.
    """
    if os.getenv("DEB_ENABLED", "false").lower() != "true":
        return

    today = date_cls.today().isoformat()

    # Skip if we already refreshed today for this city
    existing = db.get_model_weights(city)
    if existing and existing[0]["date"] == today:
        return

    weights = compute_weights(db, station, city)
    for model, weight in weights.items():
        db.upsert_model_weight(
            city=city,
            model=model,
            date=today,
            weight=weight,
            rmse=0.0,
        )
    log.info("[deb] refreshed weights for %s: %s", city, weights)


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


def check_weight_quality(
    db,
    station: str,
    city: str,
    window_days: int = 30,
) -> dict[str, object]:
    """Return a quality report for DEB weight inputs.

    Returns a dict with keys:
      - "sample_counts": {model: count} for each model
      - "has_min_samples": bool — True when all models have >= MIN_SAMPLES
      - "models": list of model names checked
    """
    since_date = (date_cls.today() - timedelta(days=window_days)).isoformat()
    log_rows = db.get_forecast_log(station, since_date)
    settlements = db.get_settlements(station, since_date + "T00:00:00")

    actuals: dict[str, float] = {}
    for row in settlements:
        d = row["ts"][:10]
        actuals[d] = row["actual_high_f"]

    counts: dict[str, int] = {m: 0 for m in MODELS}
    for row in log_rows:
        m = row["model"]
        if m in counts and row["date"] in actuals:
            counts[m] += 1

    return {
        "sample_counts": counts,
        "has_min_samples": all(v >= MIN_SAMPLES for v in counts.values()),
        "models": list(MODELS),
    }

"""Ensemble distribution exposure for the Edge tab (issue #511).

Builds a read-only summary of the per-model ensemble forecast for a given
(station, date), sourced exclusively from ``model_forecast_log``. This is the
foundational data contract for the Edge tab's KPI strip and distribution
chart (epic #510).

CRITICAL constraint: this module must never call a live forecast fetcher.
``capture_forecasts.py`` is the sole writer to ``model_forecast_log`` — any
second writer corrupts the lead-time bins that EMOS trains on. All data here
comes from DB reads on ``model_forecast_log``, ``deb_weight_log``,
``emos_calibration``, and ``bot_config``. This function performs no writes.

Distribution bucketing: ``int(math.floor(forecast_high_f))`` — e.g. 55.7°F
falls into bucket 55.

FORECAST_STACK alignment: ``ensemble_mean`` and ``bias_corrected`` reflect
only the models in the currently active FORECAST_STACK (see
``src.config.FORECAST_STACK_MODELS``), weighted by the most recent
``deb_weight_log`` snapshot for the city (falling back to equal weights).
The ``distribution`` / ``member_count`` / ``range`` fields cover *all*
models present in ``model_forecast_log`` for the date, regardless of stack,
to give operators full visibility. ``active_stack_models`` surfaces which
subset fed the mean.
"""
from __future__ import annotations

import json
import logging
import math

from src.config import CONFIG_DEFAULTS, FORECAST_STACK_MODELS, STATIONS

log = logging.getLogger(__name__)

_DEFAULT_STACK = frozenset({"nws", "open_meteo"})


def _station_to_city(station: str) -> "str | None":
    """Return the city name configured for a METAR station code."""
    for cfg_station, _lat, _lon, city, _res_station, _unit, _tz in STATIONS:
        if cfg_station == station:
            return city
    return None


def _lowest_lead_per_model(station: str, date: str, db) -> dict[str, float]:
    """Return {model: forecast_high_f} using the lowest lead_hours row per model.

    Queries model_forecast_log directly for *station* and *date*, grouping by
    model and keeping the row with the smallest lead_hours (closest-to-valid
    capture), consistent with the backtest scripts.
    """
    if hasattr(db, "get_forecast_log_for_date"):
        rows = db.get_forecast_log_for_date(station, date)
    else:
        # Fall back to the broader log call and filter locally.
        rows = [r for r in db.get_forecast_log(station, date) if r.get("date") == date]

    best_lead: dict[str, float] = {}
    best_value: dict[str, float] = {}
    for row in rows:
        model = row.get("model")
        if model is None:
            continue
        lead_hours = row.get("lead_hours")
        lead_hours = float(lead_hours) if lead_hours is not None else math.inf
        if model not in best_lead or lead_hours < best_lead[model]:
            best_lead[model] = lead_hours
            best_value[model] = float(row["forecast_high_f"])
    return best_value


def _active_stack(db) -> frozenset:
    """Resolve the active FORECAST_STACK model set from bot_config (read-only)."""
    stack_value = db.get_config("FORECAST_STACK")
    if stack_value is None:
        stack_value = CONFIG_DEFAULTS.get("FORECAST_STACK", "baseline")
    return FORECAST_STACK_MODELS.get(stack_value, _DEFAULT_STACK)


def _deb_weights_for_city(city: str, allowed: frozenset, db) -> dict[str, float]:
    """Return normalized DEB weights restricted to *allowed* models.

    Looks up the most recent deb_weight_log row for *city*. Falls back to
    equal weights across *allowed* when no row exists, or when none of the
    weighted models intersect *allowed*.
    """
    weights_json = None
    if hasattr(db, "get_latest_deb_weights"):
        weights_json = db.get_latest_deb_weights(city)
    elif hasattr(db, "get_emos_shadow_city_status"):
        weights_json = db.get_emos_shadow_city_status(city).get("deb_weights_snapshot")

    raw_weights: dict[str, float] = {}
    if weights_json:
        try:
            raw_weights = json.loads(weights_json) if isinstance(weights_json, str) else dict(weights_json)
        except (ValueError, TypeError):
            raw_weights = {}

    filtered = {m: w for m, w in raw_weights.items() if m in allowed}
    if not filtered:
        log.debug(
            "[ensemble_distribution] no deb_weight_log row for city=%s (or no overlap "
            "with active stack) — using equal weights",
            city,
        )
        return {m: 1.0 / len(allowed) for m in allowed} if allowed else {}

    total = sum(filtered.values())
    if total <= 0:
        return {m: 1.0 / len(filtered) for m in filtered}
    return {m: w / total for m, w in filtered.items()}


def _emos_bias_correct(city: str, ensemble_mean: float, db) -> float:
    """Apply EMOS (a, b) coefficients to *ensemble_mean*; fall back to raw mean."""
    calibration_rows = db.get_all_emos_calibration() if hasattr(db, "get_all_emos_calibration") else []
    row = next(
        (
            r for r in calibration_rows
            if r.get("city") == city and r.get("forecast_source") == "nws_open_meteo"
        ),
        None,
    )
    if row is None:
        log.warning(
            "[ensemble_distribution] no emos_calibration row for city=%s "
            "(forecast_source='nws_open_meteo') — falling back to raw ensemble_mean",
            city,
        )
        return ensemble_mean
    a = float(row["a"])
    b = float(row["b"])
    return a + b * ensemble_mean


def get_ensemble_distribution(station: str, date: str, db) -> "dict | None":
    """Return the ensemble distribution summary for (station, date).

    Reads exclusively from model_forecast_log (lowest lead_hours per model),
    bot_config (FORECAST_STACK), deb_weight_log, and emos_calibration. Never
    calls a live forecast fetcher and never writes to the database.

    Args:
        station: METAR station code (e.g. "KORD").
        date:    Forecast date, "YYYY-MM-DD".
        db:      A src.data.db.Database instance.

    Returns:
        None if no model_forecast_log rows exist for (station, date). Otherwise
        a dict with keys: ensemble_mean, bias_corrected, member_count, range,
        distribution, active_stack_models.
    """
    per_model = _lowest_lead_per_model(station, date, db)
    if not per_model:
        return None

    values = list(per_model.values())
    member_count = len(values)
    value_range = (min(values), max(values))

    distribution: dict[int, int] = {}
    for v in values:
        bucket = int(math.floor(v))
        distribution[bucket] = distribution.get(bucket, 0) + 1

    city = _station_to_city(station)

    allowed = _active_stack(db)
    stack_models = {m: v for m, v in per_model.items() if m in allowed}
    active_stack_models = sorted(stack_models.keys())

    if stack_models and city is not None:
        weights = _deb_weights_for_city(city, frozenset(stack_models.keys()), db)
        ensemble_mean = sum(stack_models[m] * weights.get(m, 0.0) for m in stack_models)
    elif stack_models:
        # No city mapping available — fall back to equal weights across stack models.
        ensemble_mean = sum(stack_models.values()) / len(stack_models)
    else:
        # No models from the active stack present for this date — fall back to
        # the full available set so the KPI strip still has a sensible value.
        log.debug(
            "[ensemble_distribution] no active-stack models present for station=%s "
            "date=%s — falling back to full-set mean",
            station, date,
        )
        ensemble_mean = sum(values) / len(values)

    if city is not None:
        bias_corrected = _emos_bias_correct(city, ensemble_mean, db)
    else:
        log.warning(
            "[ensemble_distribution] station=%s has no city mapping in STATIONS — "
            "cannot look up emos_calibration; falling back to raw ensemble_mean",
            station,
        )
        bias_corrected = ensemble_mean

    return {
        "ensemble_mean": ensemble_mean,
        "bias_corrected": bias_corrected,
        "member_count": member_count,
        "range": value_range,
        "distribution": distribution,
        "active_stack_models": active_stack_models,
    }

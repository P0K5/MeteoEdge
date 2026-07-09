"""Ensemble distribution exposure for the Edge tab (issue #511).

Builds a read-only summary of the per-model ensemble forecast for a given
(station, date), sourced exclusively from ``model_forecast_log``. This is the
foundational data contract for the Edge tab's KPI strip and distribution
chart (epic #510).

CRITICAL constraint: this module must never call a live forecast fetcher.
``capture_forecasts.py`` is the sole writer to ``model_forecast_log`` — any
second writer corrupts the lead-time bins that EMOS trains on. All data here
comes from DB reads on ``model_forecast_log``, ``model_weights``,
``emos_calibration``, and ``bot_config``. This function performs no writes.

Distribution bucketing: ``int(math.floor(forecast_high_f))`` — e.g. 55.7°F
falls into bucket 55.

FORECAST_STACK alignment: ``ensemble_mean`` and ``bias_corrected`` reflect
only the models in the currently active FORECAST_STACK (see
``src.config.FORECAST_STACK_MODELS``), weighted by the most recent
``model_weights`` row for the city (falling back to equal weights). This is
the same table ``deb_weighting.get_weights()`` reads for live trading
decisions (issue #552) — using it here rather than the separate
``deb_weight_log`` snapshot table keeps a single source of truth and avoids
the Edge tab silently drifting out of sync if one of the two write paths
stalls. A staleness guard falls back to equal weights (with a warning) when
the freshest row is more than ``EDGE_DEB_WEIGHT_STALENESS_DAYS`` days old.
The ``distribution`` / ``member_count`` / ``range`` fields cover *all*
models present in ``model_forecast_log`` for the date, regardless of stack,
to give operators full visibility. ``active_stack_models`` surfaces which
subset fed the mean.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import date as date_cls

from src.config import CONFIG_DEFAULTS, FORECAST_STACK_MODELS, STATIONS

log = logging.getLogger(__name__)

_DEFAULT_STACK = frozenset({"nws", "open_meteo"})

# Freshest model_weights row for a city may be at most this many days old
# before ensemble_distribution treats it as stale and falls back to equal
# weights (issue #552).
_STALENESS_DAYS = int(os.getenv("EDGE_DEB_WEIGHT_STALENESS_DAYS", "1"))


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


def _latest_model_weights(city: str, db) -> "tuple[dict[str, float], str | None]":
    """Return ({model: weight}, latest_date) for *city* from ``model_weights``.

    ``model_weights`` is written daily by ``deb_weighting.refresh_weights()``
    (called from the live scan/pricing loop) and is the same table
    ``deb_weighting.get_weights()`` reads for trading decisions. Only rows
    from the single freshest date are returned so weights from different
    refresh cycles are never blended together. Returns ``({}, None)`` when
    the DB has no rows for *city* (or doesn't support the lookup).
    """
    if not hasattr(db, "get_model_weights"):
        return {}, None
    rows = db.get_model_weights(city)
    if not rows:
        return {}, None
    latest_date = rows[0]["date"]  # get_model_weights() orders by date DESC
    return {r["model"]: r["weight"] for r in rows if r["date"] == latest_date}, latest_date


def _weights_are_stale(latest_date: "str | None") -> bool:
    """Return True when *latest_date* (YYYY-MM-DD) is more than the staleness
    threshold old, unparseable, or missing entirely."""
    if latest_date is None:
        return True
    try:
        parsed = date_cls.fromisoformat(latest_date)
    except (ValueError, TypeError):
        return True
    return (date_cls.today() - parsed).days > _STALENESS_DAYS


def _deb_weights_for_city(city: str, allowed: frozenset, db) -> dict[str, float]:
    """Return normalized DEB weights restricted to *allowed* models.

    Looks up the most recent model_weights row(s) for *city*. Falls back to
    equal weights across *allowed* when no row exists, when the freshest row
    is stale (older than EDGE_DEB_WEIGHT_STALENESS_DAYS, default 1 — logged
    as a warning so a silent write stall surfaces immediately), or when none
    of the weighted models intersect *allowed*.
    """
    raw_weights, latest_date = _latest_model_weights(city, db)

    if raw_weights and _weights_are_stale(latest_date):
        log.warning(
            "[ensemble_distribution] model_weights for city=%s is stale "
            "(freshest row dated %s, allowed max age %d day(s)) — falling back "
            "to equal weights",
            city, latest_date, _STALENESS_DAYS,
        )
        raw_weights = {}

    filtered = {m: w for m, w in raw_weights.items() if m in allowed}
    if not filtered:
        log.debug(
            "[ensemble_distribution] no fresh model_weights row for city=%s (or no "
            "overlap with active stack) — using equal weights",
            city,
        )
        return {m: 1.0 / len(allowed) for m in allowed} if allowed else {}

    total = sum(filtered.values())
    if total <= 0:
        return {m: 1.0 / len(filtered) for m in filtered}
    return {m: w / total for m, w in filtered.items()}


def _emos_bias_correct(city: str, ensemble_mean: float, db) -> float:
    """Apply EMOS (a, b) coefficients to *ensemble_mean*; fall back to raw mean."""
    # Key on the ACTIVE stack — the same forecast_source the shadow runner
    # saves under (#659). The old hardcoded 'nws_open_meteo' could never
    # match rows written per stack name.
    active_source = "baseline"
    if hasattr(db, "get_config"):
        active_source = db.get_config("FORECAST_STACK") or "baseline"
    calibration_rows = db.get_all_emos_calibration() if hasattr(db, "get_all_emos_calibration") else []
    row = next(
        (
            r for r in calibration_rows
            if r.get("city") == city and r.get("forecast_source") == active_source
        ),
        None,
    )
    if row is None:
        log.warning(
            "[ensemble_distribution] no emos_calibration row for city=%s "
            "(forecast_source=%r) — falling back to raw ensemble_mean",
            city, active_source,
        )
        return ensemble_mean
    a = float(row["a"])
    b = float(row["b"])
    return a + b * ensemble_mean


def get_ensemble_distribution(station: str, date: str, db) -> "dict | None":
    """Return the ensemble distribution summary for (station, date).

    Reads exclusively from model_forecast_log (lowest lead_hours per model),
    bot_config (FORECAST_STACK), model_weights, and emos_calibration. Never
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

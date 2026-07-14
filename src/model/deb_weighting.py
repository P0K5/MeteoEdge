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

from src.config import get_live_config, CONFIG_DEFAULTS, FORECAST_STACK_MODELS, is_training_eligible

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

# GFS_DATA_VALID_FROM: cutoff date (ISO "YYYY-MM-DD") before which "gfs" rows
# in model_forecast_log are known byte-identical duplicates of "open_meteo"
# rows (issue #548 — fetch_gfs_with_spread() previously just returned
# fetch_open_meteo_with_spread() verbatim, so every pre-fix "gfs" row is a
# copy of the corresponding "open_meteo" row, not an independent signal).
# compute_weights() excludes "gfs" matched pairs dated strictly before this
# constant so DEB never calibrates on the duplicated period.
#
# Set to DEPLOYMENT DATE + 1 (deployed 2026-07-01), NOT the merge date: the
# old duplicated code kept writing "gfs" rows throughout deployment day, so
# rows dated 2026-07-01 are still contaminated — 2026-07-02 is the first date
# guaranteed fully clean. Scoped narrowly to the "gfs" model only — other
# channels' historical pairs are unaffected.
GFS_DATA_VALID_FROM: str = "2026-07-02"

# Tracks (city, date) pairs already logged this process lifetime.
# Used by external callers that want once-per-day log semantics.
_logged_today: set = set()


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
#
# group_id honesty (issue #550): "open_meteo" and "gfs" are NOT independent
# signals — both ultimately derive from Open-Meteo's API, and open_meteo's
# multi-model mean literally includes a gfs_seamless constituent (see
# fetch_open_meteo_with_spread() / fetch_gfs_with_spread() in
# src/data/open_meteo.py). Before #548, "gfs" was in fact a byte-identical
# duplicate of "open_meteo" — the single strongest piece of provenance
# evidence for how tightly these two channels are coupled. Both are grouped
# under "gfs_family" alongside "gefs" (NOAA's GFS ensemble) once that channel
# is wired into DEB (tracked separately by issue #448 — "gefs" ingestion
# exists in src/data/gefs.py but is not yet registered here).
#
# open_meteo's other two constituents (ecmwf_ifs04, jma_seamless) are NOT
# captured by this single group_id — the registry only supports one group per
# channel. This is a deliberate, documented trade-off (see docs/OPERATIONS.md
# "Channel raw-model provenance" table): open_meteo's GFS overlap is the
# tightest and best-evidenced correlation, so it anchors the choice, but the
# residual ECMWF/JMA correlation against "ecmwf_intl" remains uncapped. A
# follow-up could split open_meteo into per-constituent channels or extend the
# registry to support multiple group memberships; out of scope for #550.
register_model("nws",        region="us",     expected_cadence_h=24.0, group_id="noaa_us")
register_model("open_meteo", region="global", expected_cadence_h=24.0, group_id="gfs_family")
register_model("gfs",        region="global", expected_cadence_h=6.0,  group_id="gfs_family")

# HRRR and NBM: CONUS-only NOAA sources; correlated with NWS via noaa_us group cap.
# cold_start_fraction is live-read from env so seed_config / dashboard overrides take effect.
_HRRR_COLD_START = float(os.getenv("DEB_HRRR_COLD_START_FRACTION", "0.4"))
_NBM_COLD_START = float(os.getenv("DEB_NBM_COLD_START_FRACTION", "0.4"))
register_model("hrrr", region="us", expected_cadence_h=1.0, group_id="noaa_us", cold_start_fraction=_HRRR_COLD_START)
register_model("nbm",  region="us", expected_cadence_h=6.0, group_id="noaa_us", cold_start_fraction=_NBM_COLD_START)

# ECMWF and ICON: international sources; correlated via ecmwf_intl group cap.
# ECMWF is global; ICON is EU-only (ICON-EU domain).
#
# group_id reassessment (issue #550): ICON-EU is DWD's own independently
# developed model, not literally derived from ECMWF's IFS — so this grouping
# is NOT a "shared raw model" pairing the way gfs_family is. It is kept
# unchanged because (a) both are non-NOAA-US, high-resolution deterministic
# NWP sources whose errors over the European domain correlate in practice
# (overlapping synoptic-scale obs/boundary conditions), and (b) no evidence
# was found to justify decoupling them — see docs/OPERATIONS.md for the
# raw-model provenance table this decision is based on.
# cold_start_fraction is live-read from env so seed_config / dashboard overrides take effect.
_ECMWF_COLD_START = float(os.getenv("DEB_ECMWF_COLD_START_FRACTION", "0.5"))
_ICON_COLD_START = float(os.getenv("DEB_ICON_COLD_START_FRACTION", "0.5"))
register_model("ecmwf", region="global", expected_cadence_h=12.0, group_id="ecmwf_intl", cold_start_fraction=_ECMWF_COLD_START)
register_model("icon",  region="eu",     expected_cadence_h=6.0,  group_id="ecmwf_intl", cold_start_fraction=_ICON_COLD_START)


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


def _compute_weights_with_metadata(
    db,
    station: str,
    city: str,
    window_days: int = 30,
    station_region: str = "us",
) -> tuple[dict[str, float], dict[str, float], dict[str, int]]:
    """Compute weights alongside per-model RMSE and sample counts.

    Returns a tuple of (weights, rmse_dict, sample_counts_dict) where:
    - weights: dict[str, float] normalized to sum to 1.0
    - rmse_dict: dict[str, float] per-model RMSE for calibrated models; 0.0 for cold-start
    - sample_counts_dict: dict[str, int] number of matched pairs per model

    This is an internal function used by refresh_weights() to persist calibrated
    RMSE values and distinguish cold-start models (sample_count < MIN_SAMPLES)
    from calibrated ones.
    """
    applicable = _models_for_region(station_region)
    if not applicable:
        equal_w = _equal_weights_for(station_region)
        empty_rmse = {m: 0.0 for m in equal_w.keys()}
        empty_counts = {m: 0 for m in equal_w.keys()}
        return equal_w, empty_rmse, empty_counts

    applicable_names = [m.name for m in applicable]
    equal_w = _equal_weights_for(station_region)

    since_date = (date_cls.today() - timedelta(days=window_days)).isoformat()
    # Use lead-hours-filtered log when available (introduced in #422) to avoid
    # mixing nowcast snapshots with genuine 24h-ahead forecasts.
    if hasattr(db, "get_forecast_log_by_lead"):
        log_rows = db.get_forecast_log_by_lead(station, since_date, lead_hours=24)
    else:
        log_rows = db.get_forecast_log(station, since_date)

    # Build actual_high lookup: date_str -> actual_high_f.
    # Use observed METAR highs (get_obs_highs_range) rather than settled trade
    # outcomes so DEB activates even before any live trades resolve.  The
    # settlements table is only populated by live trade resolution and would
    # leave DEB permanently in cold-start during shadow-mode operation.
    if hasattr(db, "get_obs_highs_range"):
        actuals: dict[str, float] = db.get_obs_highs_range(station, since_date)
    else:
        # Fallback: legacy path via settlements (pre-get_obs_highs_range deployments)
        settlements = db.get_settlements(station, since_date + "T00:00:00")
        actuals = {row["ts"][:10]: row["actual_high_f"] for row in settlements}

    # Group (days_ago, abs_error) pairs by model
    errors: dict[str, list[tuple[int, float]]] = {m: [] for m in applicable_names}
    today = date_cls.today()
    for row in log_rows:
        model_name = row["model"]
        if model_name not in errors:
            continue  # model not applicable for this station_region
        d = row["date"]
        if model_name == "gfs" and d < GFS_DATA_VALID_FROM:
            # Duplicate-era row (issue #548) — exclude from DEB training so
            # calibration doesn't learn from the open_meteo-duplicated period.
            continue
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
        equal_weights_ret = dict(equal_w)
        rmse_ret = {m: 0.0 for m in applicable_names}
        sample_counts_ret = {m: len(errors[m]) for m in applicable_names}
        return equal_weights_ret, rmse_ret, sample_counts_ret

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

    # Build return values for RMSE and sample counts:
    # - Calibrated models: real RMSE and sample count >= MIN_SAMPLES
    # - Cold-start models: 0.0 RMSE and sample count < MIN_SAMPLES
    rmse_ret: dict[str, float] = {}
    sample_counts_ret: dict[str, int] = {}
    for m in applicable_names:
        sample_counts_ret[m] = len(errors[m])
        if m in calibrated:
            rmse_ret[m] = rmse[m]
        else:
            rmse_ret[m] = 0.0

    return weights, rmse_ret, sample_counts_ret


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
    weights, _, _ = _compute_weights_with_metadata(db, station, city, window_days, station_region)
    return weights


def _apply_group_cap(
    weights: dict[str, float],
    applicable: list[_ModelEntry],
) -> dict[str, float]:
    """Cap the total weight of any group_id to GROUP_WEIGHT_CAP.

    Models without a group_id are uncapped. When a group exceeds the cap, its
    members are scaled down proportionally and the freed weight is
    redistributed proportionally to every model that is NOT a member of an
    over-cap group — i.e. ungrouped models plus members of any other group
    that stayed within its cap.

    Note (issue #550): earlier versions of this function only redistributed
    freed weight to explicitly ungrouped (group_id=None) models. Once every
    registered channel for a region has a real group_id — which is now the
    case, e.g. for EU/global stations where open_meteo/gfs (group "gfs_family")
    and ecmwf/icon (group "ecmwf_intl") are the only applicable models and none
    is left ungrouped — that fallback had nowhere to send freed weight, and it
    was silently dropped, so the returned weights no longer summed to 1.0.
    Redistributing to "everyone outside the over-cap group(s)" fixes that
    while still enforcing the same GROUP_WEIGHT_CAP for the offending group.
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
    capped_members: set[str] = set()

    for gid, members in group_models.items():
        group_total = sum(result.get(m, 0.0) for m in members)
        if group_total > GROUP_WEIGHT_CAP and group_total > 0:
            scale = GROUP_WEIGHT_CAP / group_total
            excess = group_total - GROUP_WEIGHT_CAP
            freed += excess
            for m in members:
                result[m] = result.get(m, 0.0) * scale
            capped_members.update(members)

    # Redistribute freed weight proportionally to every model outside the
    # over-cap group(s): ungrouped models plus members of any group that
    # stayed within cap.
    if freed > 0:
        receivers = [m.name for m in applicable if m.name not in capped_members]
        if receivers:
            receiver_total = sum(result.get(m, 0.0) for m in receivers)
            if receiver_total > 0:
                for m in receivers:
                    result[m] = result.get(m, 0.0) + freed * (result.get(m, 0.0) / receiver_total)
            else:
                # All receiver models have zero weight — distribute equally
                per_model = freed / len(receivers)
                for m in receivers:
                    result[m] = result.get(m, 0.0) + per_model
        # else: every applicable model is inside an over-cap group. This can
        # only happen with a misconfigured GROUP_WEIGHT_CAP well below 0.5 and
        # 3+ groups (mathematically, two groups partitioning a normalized
        # weight sum of 1.0 cannot both exceed a 0.7 cap simultaneously);
        # freed weight has no valid receiver and is left uncapped-back rather
        # than silently discarded or fabricated.

    return result


def refresh_weights(db, station: str, city: str, station_region: str = "us") -> None:
    """Recompute and persist model weights for *city* / *station*.

    Skips when:
    - DEB_ENABLED env var is not "true"  (default: false)
    - weights were already refreshed today (checked via model_weights table)

    When weights are written, each model row is upserted with today's date.
    Real per-model RMSE is computed and persisted; cold-start models
    (sample_count < MIN_SAMPLES) are marked with rmse=0.0 and sample_count < MIN_SAMPLES.

    Args:
        station_region: DEB registry region for this station — "us", "eu", or
            "global". Determines which model set is applicable. Defaults to "us"
            for backward compat; callers should pass the correct value so that
            US-only models (NWS, HRRR, NBM) are not attributed cold-start weight
            at international stations where they will never have data.
    """
    if not get_live_config(db).get("DEB_ENABLED", CONFIG_DEFAULTS["DEB_ENABLED"]):
        return

    # Skip weight training for excluded cities (issue #558, #718)
    if not is_training_eligible(city):
        return

    today = date_cls.today().isoformat()

    # Skip if we already refreshed today for this city
    existing = db.get_model_weights(city)
    if existing and existing[0]["date"] == today:
        return

    weights, rmse_dict, sample_counts_dict = _compute_weights_with_metadata(
        db, station, city, station_region=station_region
    )
    for model, weight in weights.items():
        db.upsert_model_weight(
            city=city,
            model=model,
            date=today,
            weight=weight,
            rmse=rmse_dict[model],
            sample_count=sample_counts_dict[model],
        )
    log.info("[deb] refreshed weights for %s (%s): %s", city, station_region, weights)


def get_weights(db, city: str, station_region: str = "us") -> dict[str, float]:
    """Return the most-recent persisted weights for *city*.

    Returns region-appropriate equal weights when:
    - DEB_ENABLED is not "true"
    - No rows exist in model_weights for *city*
    - Any tracked model for the region is missing from the table

    Args:
        station_region: DEB registry region — "us", "eu", or "global". Must
            match the value used when refresh_weights was last called for this
            city, otherwise the tracked-model completeness check may fall back
            to equal weights incorrectly.
    """
    equal_w = _equal_weights_for(station_region)

    if not get_live_config(db).get("DEB_ENABLED", CONFIG_DEFAULTS["DEB_ENABLED"]):
        return dict(equal_w)

    rows = db.get_model_weights(city)
    if not rows:
        return dict(equal_w)

    # Most recent date first (db.get_model_weights orders by date DESC).
    # Read the latest weight for each tracked model applicable to this region.
    tracked_models = _model_names_for_region(station_region)
    latest: dict[str, float] = {}
    for row in rows:
        m = row["model"]
        if m not in latest:
            latest[m] = row["weight"]
        if len(latest) == len(tracked_models):
            break

    # If any tracked model is missing from the table, fall back to equal weights.
    if any(m not in latest for m in tracked_models):
        return dict(equal_w)

    # Filter to only models in the active FORECAST_STACK so the weights returned
    # always sum to 1.0 over the models actually used in the live blend.
    # Out-of-stack models retain their computed rows in model_weights for future
    # promotion; they just don't participate in the current ensemble.
    live_cfg = get_live_config(db)
    active_stack = live_cfg.get("FORECAST_STACK", CONFIG_DEFAULTS["FORECAST_STACK"])
    stack_models = FORECAST_STACK_MODELS.get(active_stack, frozenset())
    if stack_models:
        stack_latest = {m: w for m, w in latest.items() if m in stack_models}
        if stack_latest:
            total = sum(stack_latest.values())
            if total > 0:
                return {m: w / total for m, w in stack_latest.items()}
        # No stack models have weights yet — return equal weight for stack models
        stack_equal = {m: 1.0 / len(stack_models) for m in stack_models}
        return stack_equal

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

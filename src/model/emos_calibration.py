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

import math
from datetime import datetime, timezone

from scipy.optimize import minimize

from src.config import FORECAST_STDDEV_F, STATIONS
from src.model.crps_score import crps_gaussian


# GFS_DATA_VALID_FROM: cutoff date (ISO "YYYY-MM-DD") before which "gfs" rows
# in model_forecast_log are known byte-identical duplicates of "open_meteo"
# rows (issue #548 — fetch_gfs_with_spread() previously just returned
# fetch_open_meteo_with_spread() verbatim, so every pre-fix "gfs" row is a
# copy of the corresponding "open_meteo" row, not an independent signal).
# fetch_training_data() excludes "gfs" matched pairs dated strictly before this
# constant so EMOS never calibrates on the duplicated period.
#
# Set to DEPLOYMENT DATE + 1 (deployed 2026-07-01), NOT the merge date: the
# old duplicated code kept writing "gfs" rows throughout deployment day, so
# rows dated 2026-07-01 are still contaminated — 2026-07-02 is the first date
# guaranteed fully clean. Scoped narrowly to the "gfs" model only — other
# channels' historical pairs are unaffected.
GFS_DATA_VALID_FROM: str = "2026-07-02"

# Valid sigma_source values for fetch_training_data / save_coefficients (issue #449).
# "ensemble" is fetch_training_data's historical, unparametrised behaviour:
# prefer the persisted per-row model_forecast_log.sigma_f, falling back to the
# constant FORECAST_STDDEV_F only when a row has none. "fixed" is new: it
# ignores sigma_f entirely and always trains against the constant, matching
# what live serving does when USE_ENSEMBLE_SIGMA is off (resolve_sigma_raw
# returns fallback_sigma unconditionally). Training under "fixed" produces a
# coefficient set paired with that legacy serving path; training under
# "ensemble" pairs with USE_ENSEMBLE_SIGMA=True serving (state.ensemble_sigma_f).
SIGMA_SOURCES: frozenset[str] = frozenset({"fixed", "ensemble"})


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
    regime: "frozenset[str] | set[str] | None" = None,
    sigma_source: str = "ensemble",
) -> list[tuple[float, float, float]]:
    """Build (mu_ensemble, sigma_ensemble, actual_high_f) triples for a city.

    Strategy:
    1. Query model_forecast_log for rows matching *station* and *lead_hours*.
       Each row is a forecast captured at a fixed lead time by the cron worker
       (src/scripts/capture_forecasts.py).
    2. For each date, average all model forecasts (nws/open_meteo/gfs) to form
       the ensemble mean mu_f.  sigma_f is resolved per ``sigma_source`` (see
       below).
    3. For each date, look up the settled daily high from the observations table
       (source='metar', MAX(temp_f) for that date).
    4. Drop any triple containing a NaN/inf value (issue #449) -- a corrupt
       sensor read or persisted sigma_f must not silently poison the fit.
    5. Return the joined list; raise InsufficientDataError if fewer than
       min_samples triples are found (counted AFTER the NaN/inf drop).

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
                         provided (and ``regime`` is None), only model_forecast_log rows
                         whose ``model`` column matches this value are included. When both
                         are None, a ValueError is raised.
        regime:          Optional set of model tags (e.g. frozenset({"nws", "open_meteo"}))
                         to include in the ensemble μ. When provided, takes precedence over
                         ``forecast_source`` for row filtering. When both ``regime`` and
                         ``forecast_source`` are None, raises ValueError.
        sigma_source:    "ensemble" (default, back-compat with pre-#449 behaviour) uses
                         the persisted per-row ``sigma_f`` when a date has one, falling
                         back to ``FORECAST_STDDEV_F`` only for dates with none.
                         "fixed" ignores ``sigma_f`` entirely and always uses
                         ``FORECAST_STDDEV_F`` — the constant every triple was trained
                         against before per-row sigma existed, and what live serving
                         uses when USE_ENSEMBLE_SIGMA is off. Must be one of
                         SIGMA_SOURCES; raises ValueError otherwise (issue #449).
                         IMPORTANT (issue #799): this parameter's OWN default
                         ("ensemble") is independent of the live USE_ENSEMBLE_SIGMA
                         flag -- a retrain caller (run_emos_shadow.py, auto_retrain_
                         probability_calibration.py) must explicitly resolve
                         sigma_source from USE_ENSEMBLE_SIGMA (the same flag
                         Database._active_sigma_source() derives the coefficient-row
                         key from) and pass it here explicitly, matching what it
                         passes to save_coefficients()/upsert_emos_coefficients() for
                         the SAME call. Relying on this function's bare default while
                         a different sigma_source gets resolved at save time is
                         exactly the train/serve decoupling that reproduces the #658
                         skew.

    Returns:
        List of (mu_f, sigma_f, actual_high_f) float triples.

    Raises:
        InsufficientDataError: If fewer than min_samples triples are found.
        ValueError: If both ``regime`` and ``forecast_source`` are None, or if
            ``sigma_source`` is not one of SIGMA_SOURCES.
    """
    if regime is None and forecast_source is None:
        raise ValueError(
            "fetch_training_data requires either 'regime' or 'forecast_source'"
        )
    if sigma_source not in SIGMA_SOURCES:
        raise ValueError(
            f"sigma_source must be one of {sorted(SIGMA_SOURCES)}, got {sigma_source!r}"
        )

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

    # Filter rows by regime (set of model tags) or forecast_source (exact match).
    # regime takes precedence when both are provided.
    if regime is not None:
        forecast_rows = [r for r in forecast_rows if r.get("model") in regime]
    elif forecast_source is not None:
        forecast_rows = [r for r in forecast_rows if r.get("model") == forecast_source]

    # Filter out "gfs" rows dated before GFS_DATA_VALID_FROM (issue #548 duplicate-era exclusion)
    # to avoid training on rows that are byte-identical copies of "open_meteo" rows.
    filtered_rows = []
    for r in forecast_rows:
        if r.get("model") == "gfs" and r.get("date", "") < GFS_DATA_VALID_FROM:
            continue  # Skip duplicate-era GFS rows
        filtered_rows.append(r)
    forecast_rows = filtered_rows

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
        if sigma_source == "fixed":
            # #449: ignore any persisted sigma_f entirely — every triple trains
            # against the same constant, matching legacy (pre-sigma_f) behaviour
            # and USE_ENSEMBLE_SIGMA=False serving.
            continue
        # sigma_source == "ensemble": use the first non-NULL sigma_f encountered
        # for a given date (unchanged pre-#449 behaviour).
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
        # Issue #449: a NaN/inf anywhere in the triple (bad sensor read, a
        # corrupt persisted sigma_f, etc.) must not silently corrupt the CRPS
        # fit -- scipy.optimize.minimize propagates NaN through the whole loss
        # without raising, so an unfiltered NaN row can poison every
        # coefficient for the city. Drop just that date rather than failing
        # the whole fetch; min_samples below still guards against too few
        # clean triples remaining.
        if any(math.isnan(v) or math.isinf(v) for v in (mu_f, sigma_f, obs_high)):
            continue
        result.append((mu_f, sigma_f, obs_high))

    if len(result) < min_samples:
        raise InsufficientDataError(
            f"Only {len(result)} joined (forecast, actual) pairs found for city='{city}' "
            f"(station='{station}') at lead_hours={lead_hours}; need at least {min_samples}."
        )

    return result


def pooling_group(city: str) -> str | None:
    """Return the cross-station pooling group for *city* (issue #659).

    Per-station EMOS needs min_samples=60 settled days per lead bin — months
    away for a fresh deployment. Pooling stations with broadly similar
    forecast-error structure lets a shared (a, b, c, d) fit start producing
    emos_shadow rows (and CRPS promotion evidence) immediately; per-station
    fits automatically take precedence once a station individually clears
    min_samples (see run_emos_shadow).

    Groups (deliberately coarse — a shared fit learns the group's AVERAGE
    bias, which is the shrinkage trade-off, not a defect):
      - "us_f":      unit=F stations (US, °F brackets, NWS-covered)
      - "tropics_c": unit=C stations within the tropics (|lat| < 23.5) —
                     low day-to-day variance, narrow diurnal range
      - "midlat_c":  every other unit=C station (Europe/Asia/Oceania
                     mid-latitudes — synoptic-driven variance)

    Returns None for cities not present in STATIONS.
    """
    for station_cfg in STATIONS:
        _station, lat, _lon, cfg_city, _res, unit, _tz = station_cfg
        if cfg_city.lower() != city.lower():
            continue
        if unit == "F":
            return "us_f"
        if abs(lat) < 23.5:
            return "tropics_c"
        return "midlat_c"
    return None


def fetch_training_data_pooled(
    cities: list[str],
    db,
    min_samples: int = 60,
    lead_hours: int = 24,
    forecast_source: str | None = None,
    regime: "frozenset[str] | set[str] | None" = None,
    sigma_source: str = "ensemble",
) -> tuple[list[tuple[float, float, float]], dict[str, int]]:
    """Pool per-city training triples across *cities* (issue #659).

    Calls fetch_training_data() per city with min_samples=1 (a city
    contributes whatever it has; cities with zero joined pairs are skipped)
    and concatenates the triples.

    Args:
        sigma_source: Forwarded to fetch_training_data() for every city — see
            that function's docstring (issue #449). Default "ensemble" matches
            pre-#449 pooled-fit behaviour exactly.

    Returns:
        (pooled_triples, per_city_counts) — per_city_counts maps each city
        to the number of triples it contributed (0-contributors omitted).

    Raises:
        InsufficientDataError: if the POOLED total is below min_samples.
    """
    pooled: list[tuple[float, float, float]] = []
    per_city: dict[str, int] = {}
    for c in cities:
        try:
            triples = fetch_training_data(
                c, db, min_samples=1, lead_hours=lead_hours,
                forecast_source=forecast_source, regime=regime,
                sigma_source=sigma_source,
            )
        except InsufficientDataError:
            continue
        pooled.extend(triples)
        per_city[c] = len(triples)

    if len(pooled) < min_samples:
        raise InsufficientDataError(
            f"Pooled training data across {len(cities)} cities has only "
            f"{len(pooled)} triples at lead_hours={lead_hours}; need at "
            f"least {min_samples}."
        )
    return pooled, per_city


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
    forecast_source: "str | None" = None,
    sample_count: int | None = None,
    sigma_source: "str | None" = None,
    lead_hours: int = 24,
) -> None:
    """Persist EMOS coefficients to the emos_calibration table.

    Always writes model_mode='emos_shadow' and ready_for_promotion=0.
    Promotion to active use remains a deliberate manual step (the dashboard's
    mark-ready endpoint / Database.toggle_emos_ready_for_promotion) — this
    function never sets ready_for_promotion=1 itself, regardless of
    sample_count or CRPS. The <60-sample guardrail from issue #556 is
    therefore structural: there is no code path here that could promote a
    reduced-sample shadow fit, not merely a threshold check that could later
    be bypassed.

    Coefficients for different forecast_source, sigma_source, or lead_hours
    values are stored independently — saving for "hrrr_nbm" (or
    sigma_source="ensemble", or lead_hours=6) never overwrites the legacy
    "nws_open_meteo"/"fixed"/lead_hours=24 row (issues #659, #449, #665).

    Args:
        city:            City name (e.g. "Chicago").
        a:               EMOS mu intercept.
        b:               EMOS mu slope.
        c:               EMOS sigma intercept (must be > 0).
        d:               EMOS sigma slope (must be > 0).
        crps_score:      Mean CRPS on the training set after fitting.
        db:              A src.data.db.Database instance.
        forecast_source: Forecast stack identifier. None resolves to the
                         active FORECAST_STACK inside the Database layer, so
                         writers and readers key on the same source (#659).
        sample_count:    Number of training samples used to fit the coefficients.
                         Recorded for caller/log context only — does not affect
                         ready_for_promotion, which is always 0 here (see above).
        sigma_source:    "fixed" | "ensemble" | None. None resolves to
                         Database._active_sigma_source(), which derives from the
                         active USE_ENSEMBLE_SIGMA bot_config flag (#449, #799), so
                         writers and readers key on the same track. Pass the SAME
                         value fetch_training_data() was called with for this fit
                         -- do not rely on the two independently defaulting to the
                         same thing (see fetch_training_data's sigma_source
                         docstring note, issue #799).
        lead_hours:      Lead-time bin these coefficients were fitted at.
                         Default 24, matching the only bin fetch_training_data()
                         used before per-lead-bin fitting existed (#665).
    """
    ready_for_promotion = 0

    db.upsert_emos_coefficients(
        city=city,
        model_mode="emos_shadow",
        a=a,
        b=b,
        c=c,
        d=d,
        crps_score=crps_score,
        trained_at=datetime.now(timezone.utc).isoformat(),
        ready_for_promotion=ready_for_promotion,
        forecast_source=forecast_source,
        sigma_source=sigma_source,
        lead_hours=lead_hours,
    )


def check_ready_for_promotion(
    db,
    forecast_source: str,
    cities: list[str],
    sigma_source: "str | None" = None,
) -> bool:
    """Return True only if every city has ready_for_promotion=1 for this source.

    This is the promotion gate: FORECAST_STACK must not be switched to live
    until every city in scope has validated EMOS coefficients retrained on
    the new source. Callers should check this before any FORECAST_STACK change.

    Args:
        db:              A src.data.db.Database instance.
        forecast_source: The new forecast stack identifier (e.g. "hrrr_nbm").
        cities:          List of city names that must all be ready.
        sigma_source:    Optional "fixed" | "ensemble" filter (issue #449).
                         None (default) does not filter on sigma_source at
                         all — every row for the requested forecast_source
                         counts regardless of which sigma track it was
                         trained on, identical to pre-#449 behaviour. Pass a
                         value explicitly to gate promotion on a specific
                         sigma-source retrain (e.g. require an "ensemble" fit
                         before enabling USE_ENSEMBLE_SIGMA in production).

    Returns:
        True iff all cities have a row with ready_for_promotion=1 for this
        source (and, when given, this sigma_source).
    """
    if not cities:
        return False
    rows = db.get_all_emos_calibration()
    promoted = {
        r["city"]
        for r in rows
        if r.get("forecast_source") == forecast_source
        and r.get("ready_for_promotion") == 1
        and (sigma_source is None or r.get("sigma_source") == sigma_source)
    }
    return all(city in promoted for city in cities)

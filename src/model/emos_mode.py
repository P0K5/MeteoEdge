"""EMOS deployment mode helpers.

Controls whether each city uses legacy Gaussian, EMOS shadow, or EMOS primary mode.
"""
import logging
import os

from src.config import CONFIG_DEFAULTS, get_live_config

log = logging.getLogger(__name__)


def _emos_min_samples(db) -> int:
    """Return the CRPS-logged shadow-day minimum for promotion to emos_primary.

    Default is 60 per epic #70: "retraining should use minimum 60 samples per city
    before promoting". Although the units differ (CRPS-logged shadow days vs. training
    triples), by the time #667 lands every fitted city gets exactly one legacy-comparable
    CRPS entry per day, so 60 days of shadow evidence before serving emos_primary
    is the conservative, defensible reading of the epic's intent. This threshold is
    operator-tunable via the dashboard (CONFIG_DEFAULTS + bot_config DB table).
    """
    return int(get_live_config(db).get(
        "EMOS_MIN_SAMPLES_PROMOTION",
        CONFIG_DEFAULTS["EMOS_MIN_SAMPLES_PROMOTION"]
    ))


def _default_mode(db=None) -> str:
    """Resolve the EMOS fallback mode for cities with no calibration rows.

    Reads the dashboard-editable ``EMOS_DEFAULT_MODE`` from bot_config when a
    db handle is available (live-read, same pattern as ``_emos_min_samples``);
    without a db the env var keeps its historical role (issue #680).
    """
    if db is not None:
        return str(get_live_config(db).get(
            "EMOS_DEFAULT_MODE",
            CONFIG_DEFAULTS["EMOS_DEFAULT_MODE"]
        ))
    return os.environ.get("EMOS_DEFAULT_MODE", "legacy")


def _shadow_or_default(city: str, db) -> str:
    """Fall back to emos_shadow when a shadow row exists, else EMOS_DEFAULT_MODE."""
    if db.get_emos_coefficients(city, "emos_shadow"):
        return "emos_shadow"
    return _default_mode(db)


def _primary_allowed(city: str, db) -> bool:
    """Return True only if a primary row exists and the CRPS sample guard passes.

    ``get_emos_crps_count(city)`` (forecast_source unset) resolves to the
    active FORECAST_STACK via ``db._active_forecast_source`` — same
    resolution the calibration reads use — so the promotion guard counts
    CRPS evidence for the currently-served stack only, never pooling samples
    accrued under a different forecast_source across a stack switch
    (issue #759).
    """
    if db.get_emos_coefficients(city, "emos_primary") is None:
        return False
    n = db.get_emos_crps_count(city)
    min_samples = _emos_min_samples(db)
    if n < min_samples:
        log.info("[emos] city=%s: %d/%d samples, primary blocked", city, n, min_samples)
        return False
    return True


def get_city_mode(city: str, db=None) -> str:
    """Return the deployment mode for a city: 'legacy', 'emos_shadow', or 'emos_primary'.

    Resolution order:

    1. **Operator override** — the effective mode written by the dashboard
       promote/demote endpoints (``emos_mode_override`` table) is authoritative.
       The promote endpoint already enforces shadow readiness, so an explicit
       override is treated as the operator's deliberate decision. ``emos_primary``
       is still subject to the CRPS sample guard below; ``demote`` (legacy) is
       honoured unconditionally.
    2. **Calibration rows** — with no override, derive the mode from the
       ``emos_calibration`` rows: a primary row flagged ``ready_for_promotion=1``
       (typically written by the offline retrain) promotes once the sample guard
       passes; otherwise an existing shadow row serves ``emos_shadow``.

    Falls back to ``EMOS_DEFAULT_MODE`` — read live from bot_config when a db
    is available, else from the env var (default 'legacy').

    Promotion guard: a city needs at least ``EMOS_MIN_SAMPLES`` CRPS log entries
    before it may serve ``emos_primary``, regardless of which path requested it.
    """
    if db is None:
        return _default_mode()

    # 1. Operator override (dashboard promote/demote) is authoritative.
    override = db.get_emos_effective_mode(city)
    if override is not None:
        if override == "emos_primary":
            return "emos_primary" if _primary_allowed(city, db) else _shadow_or_default(city, db)
        if override == "emos_shadow":
            return _shadow_or_default(city, db)
        # 'legacy' (or any explicit rollback) is honoured unconditionally.
        return "legacy"

    # 2. No override — derive from calibration rows. Fetch each row once.
    shadow = db.get_emos_coefficients(city, "emos_shadow")
    primary = db.get_emos_coefficients(city, "emos_primary")
    if shadow is None and primary is None:
        return _default_mode(db)
    if primary and primary.get("ready_for_promotion") == 1 and _primary_allowed(city, db):
        return "emos_primary"
    if shadow:
        return "emos_shadow"
    return _default_mode(db)


# Stack members whose forecasts are available on WeatherState at scan time.
# Training (fetch_training_data) averages model_forecast_log rows for the
# stack regime with EQUAL weights; serving must feed apply_emos the same
# equal-weight mean of the same feeds — never corrected_mu_f/deb_mu_f, which
# embed DEB weighting + intraday + residual corrections the coefficients were
# not fitted against (train/serve parity, issue #666). Until scan-time state
# carries HRRR/ECMWF/etc. values, non-baseline stacks serve on the two
# always-available members; test_serving_members_parity_guard ensures every
# model in the active FORECAST_STACK has a corresponding scan-time attribute.
_SERVING_MEMBERS = ("forecast_high_f", "secondary_forecast_f")


def resolve_sigma_raw(state, use_ensemble_sigma: "bool | None", fallback_sigma: float) -> float:
    """Return the sigma_raw EMOS serving should feed into apply_emos (issue #448).

    Picks state.ensemble_sigma_f (per-station GEFS ensemble spread) when
    use_ensemble_sigma resolves True AND the state carries a value; otherwise
    returns fallback_sigma (FORECAST_STDDEV_F) unchanged -- the default,
    behaviour-preserving path while USE_ENSEMBLE_SIGMA stays off.

    Args:
        state: WeatherState for the station being scored.
        use_ensemble_sigma: Resolved USE_ENSEMBLE_SIGMA flag. Callers with DB
            access should pass the live-config value (resolved once per scan,
            same pattern as DEB_ENABLED). When None, falls back to the
            USE_ENSEMBLE_SIGMA env var (backward compatibility, mirrors
            true_probability_yes's use_ensemble_sigma param).
        fallback_sigma: sigma to use when ensemble_sigma_f is unavailable or
            the flag is off (typically FORECAST_STDDEV_F).
    """
    if use_ensemble_sigma is None:
        use_ensemble_sigma = os.getenv("USE_ENSEMBLE_SIGMA", "false").lower() == "true"
    if use_ensemble_sigma and getattr(state, "ensemble_sigma_f", None) is not None:
        return state.ensemble_sigma_f
    return fallback_sigma


def _nearest_lead_hours(lead_hours: float, available: "list[int]") -> int:
    """Return the entry of *available* nearest to *lead_hours* (issue #665).

    Same nearest-match idiom as src/data/nws.py:_nws_sigma_for_lead. Ties
    (e.g. lead_hours=9 with available=[6, 12]) resolve to whichever bin
    appears first in *available* — deterministic given a stable iteration
    order, matching Python's min() tie-breaking.

    Args:
        lead_hours: Target lead time in hours (fractional; e.g.
            minutes_to_settlement / 60).
        available:  Non-empty list of fitted lead_hours bin values.
    """
    return min(available, key=lambda k: abs(k - lead_hours))


def _select_emos_row(city: str, db, minutes_to_settlement: "float | None") -> "dict | None":
    """Return the emos_calibration row apply_emos should use for *city*.

    emos_primary is preferred over emos_shadow — unchanged precedence from
    before #665. Within whichever mode wins:

    - minutes_to_settlement given: picks the row for the lead bin nearest to
      it among every lead_hours value fitted for that mode (issue #665). A
      city that has only ever been fit at the legacy lead_hours=24 default
      always resolves to that single row regardless of minutes_to_settlement,
      so this is a no-op until a city is retrained at more than one bin.
    - minutes_to_settlement is None: the exact pre-#665 lookup (the single
      row at the default lead_hours=24).

    Returns None (missing-bin fallback) when the winning precedence has no
    row in either mode — callers fall back to (mu_raw, sigma_raw) unchanged,
    identical to today.
    """
    for mode in ("emos_primary", "emos_shadow"):
        if minutes_to_settlement is not None:
            by_lead = db.get_emos_coefficients_by_lead(city, mode)
            if by_lead:
                nearest = _nearest_lead_hours(
                    minutes_to_settlement / 60.0, list(by_lead.keys())
                )
                return by_lead[nearest]
        else:
            row = db.get_emos_coefficients(city, mode)
            if row is not None:
                return row
    return None


def emos_serving_mu(
    state, city: str, db, sigma_raw: float,
    minutes_to_settlement: "float | None" = None,
) -> "tuple[float, float] | None":
    """Return (mu_final, sigma_cal) for EMOS serving, or None if unservable.

    The #658 layer contract:
    1. mu_raw = plain equal-weight mean of the stack members available on
       *state* — the same variable definition EMOS trains on.
    2. (a, b, c, d) applied via apply_emos. EMOS's intercept absorbs the
       static ensemble bias, which is why the rolling residual correction is
       NOT part of this path (it learns the same bias — applying both would
       remove it twice).
    3. The decayed intraday delta (state.intraday_delta_f) layers ON TOP of
       the calibrated mean: it is a genuine nowcast signal that fixed-lead
       training cannot capture.

    sigma_cal note (issue #665): passing minutes_to_settlement selects
    coefficients from the lead bin nearest to it instead of a single
    fixed-lead row (see apply_emos/_select_emos_row), so d is properly
    identified once more than one lead bin has been retrained for a city.
    Until then (or when the caller omits minutes_to_settlement) this is
    unchanged from the single-lead-bin behaviour the #658 layer contract
    originally shipped with. The envelope's remaining-climb floor
    (ENVELOPE_SIGMA_CLIMB_FRACTION, #653) still supplies intraday widening on
    top of whatever sigma_cal this returns.

    Returns None when no stack member forecast is available on the state —
    callers must fall back to legacy behavior.
    """
    members = [
        getattr(state, attr, None) for attr in _SERVING_MEMBERS
        if getattr(state, attr, None) is not None
    ]
    if not members:
        return None
    mu_raw = sum(members) / len(members)
    mu_cal, sigma_cal = apply_emos(
        mu_raw, sigma_raw, city, db, minutes_to_settlement=minutes_to_settlement
    )
    mu_final = mu_cal + (state.intraday_delta_f or 0.0)
    return mu_final, sigma_cal


def apply_emos(
    mu_raw: float, sigma_raw: float, city: str, db,
    minutes_to_settlement: "float | None" = None,
) -> tuple[float, float]:
    """Apply EMOS linear correction: mu_cal = a + b*mu, sigma_cal = c + d*sigma.

    Falls back to (mu_raw, sigma_raw) if no coefficients found.

    Args:
        minutes_to_settlement: Optional (issue #665). When given, the
            coefficient row is selected from the lead bin nearest to it (see
            _select_emos_row) instead of the fixed lead_hours=24 row. Omitting
            it reproduces pre-#665 behaviour exactly.
    """
    row = _select_emos_row(city, db, minutes_to_settlement)
    if row is None:
        return mu_raw, sigma_raw
    a, b, c, d = row["a"], row["b"], row["c"], row["d"]
    mu_cal = a + b * mu_raw
    sigma_cal = c + d * sigma_raw
    if sigma_cal <= 0:
        log.warning("[emos] sigma_cal=%.4f <= 0 for %s — using raw sigma", sigma_cal, city)
        sigma_cal = sigma_raw
    return mu_cal, sigma_cal


def _check_ready_for_promotion(city: str, db) -> bool:
    """Return True when a city is cleared to serve emos_primary.

    Two independent signals clear a city:

    - An operator override of ``emos_primary`` (set via the dashboard promote
      endpoint, which already enforced shadow readiness), or
    - An ``emos_primary`` calibration row flagged ``ready_for_promotion=1``
      (typically written by the offline retrain script).

    The scanner uses this as a redundant safety re-check after ``get_city_mode``
    returns ``emos_primary``; honouring the override here keeps the two in sync,
    so a dashboard-promoted city is not silently dropped back to legacy.

    Issue #449: ``db.get_emos_coefficients`` below resolves forecast_source AND
    sigma_source from the active FORECAST_STACK / EMOS_SIGMA_SOURCE bot_config
    keys (both default to their pre-#449 values), so this check transparently
    follows whichever sigma track an operator has made active — no signature
    change needed here, same as it already did for forecast_source (#659).
    """
    if db.get_emos_effective_mode(city) == "emos_primary":
        # Mirror get_city_mode exactly — the CRPS sample guard still applies.
        return _primary_allowed(city, db)
    row = db.get_emos_coefficients(city, "emos_primary")
    return row is not None and row.get("ready_for_promotion") == 1

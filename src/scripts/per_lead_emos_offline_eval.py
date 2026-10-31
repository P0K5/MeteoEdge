"""Phase A offline evaluation: per-lead-bin EMOS coefficients vs today's
24h-only fit, replayed against same-day entry gates on a held-out time split
(issue #1453, "Amended 2026-10-31 (PM)" scope).

**Analysis only -- no live-path writes.** This module never calls
``scripts/run_emos_shadow.py``, never writes to ``emos_calibration``,
``bot_config``, or any other live table, and never touches promotion/serving
code. Every DB access goes through ``ReadOnlyDatabase``
(``src.scripts.emos_shadow_reconstruction``), the same read-only, write-
denying connection issue #1041 built. Not wired into cron/CI -- manually
invoked only, like its sibling offline reports
(``emos_shadow_vs_market_report.py``, ``sigma_lever_reconstruction_report.py``).

**Question this answers:** if same-day EMOS serving had used coefficients fit
per lead bin (3/6/12/18/24h) instead of the single 24h-only fit it uses today,
would the entries it would have made (after the real entry gates) have been
more profitable, out of sample?

**Method, posted as the plan comment on issue #1453 before this file was
written (2026-10-31):**

1. **Train/eval time split.** Fit on ``model_forecast_log`` dates strictly
   before ``TRAIN_EVAL_CUTOFF``; evaluate candidates polled on or after it.
   Fitting and evaluating on the same days is in-sample and would make any
   EV comparison meaningless.
2. **Two coefficient sets, both from ``src.model.emos_calibration.fit_emos``
   (imported unchanged)** -- the CRPS-minimising optimiser itself is never
   re-derived:
   - ``24h_only``: one ``(a, b, c, d)`` per city, fit on lead_hours=24 training
     triples, applied at every lead (today's behaviour).
   - ``nearest_bin``: one ``(a, b, c, d)`` per city PER lead bin (3/6/12/18/24)
     with enough training triples, served via the lead bin nearest a
     candidate's own ``minutes_to_settlement`` (``src.model.emos_mode.
     _nearest_lead_hours``, reused). Below ``FULL_WEIGHT_SAMPLES`` own
     triples at a given lead, the city's own fit is blended with its
     cross-station pooling group's fit at that SAME lead
     (``pooling_group``/``shrinkage_weight``/``blend_coefficients``, all
     imported unchanged from ``emos_calibration`` -- the same partial-pooling
     ``run_emos_shadow.py`` already uses for undertrained cities, issue #798).
   ``fetch_training_data()`` itself returns undated triples, so it cannot be
   split by cutoff after the fact -- ``_fetch_dated_triples`` below mirrors
   its join logic (same ``model_forecast_log`` row fetch, same GFS-duplicate-
   era exclusion via the imported ``GFS_DATA_VALID_FROM``, same clamped-sigma
   exclusion via the imported ``_is_clamped_legacy_sigma``) while keeping the
   date. This is the one disclosed, deliberate duplication in this module;
   every other production function is imported and called, not re-derived.
3. **Candidate population.** Every scanner poll from ``logs/bracket_evals.*
   .jsonl`` (issue #826) restricted to same-day, high-direction rows, polled
   on or after the cutoff -- NOT the ``scan_decisions`` DB table, which is
   ``PRIMARY KEY (station, ticker, date)`` and upserted, so it only ever
   retains the LAST poll per market. Same-day entries fire at the FIRST
   qualifying poll (3-18h out), so replaying that needs every poll.
4. **Per-poll reconstruction**, reusing ``src.scripts.
   emos_shadow_reconstruction`` verbatim for every piece except the
   coefficients themselves (which that module reads from the live
   ``emos_calibration`` table -- exactly what Phase A must not read from or
   write to): ``reconstruct_mu_raw`` (equal-weight stack mean from
   ``model_forecast_log`` at the nearest lead bin), ``reconstruct_
   intraday_delta`` (``delta_f * decay_factor`` from ``intraday_corrections``,
   algebraically the same quantity ``compute_correction`` computes -- common
   to both arms, so any slack here cancels in the paired comparison),
   ``reconstruct_current_high``/``reconstruct_latest_temp`` (causal,
   ``ts <= poll_ts``, never leaking the eventual outcome).
5. **Bracket probability**: ``src.model.envelope.true_probability_yes``,
   called exactly as ``scanner.py``'s ``emos_primary`` branch does --
   ``corrected_mu_f`` set to the variant's ``mu_final``, ``forecast_stddev``
   set to the variant's ``sigma_cal``, ``obs_bias_offset_f=None``,
   ``use_ensemble_sigma=False`` (the override IS the calibration).
6. **Entry gates replayed**, reusing imported constants/functions, never
   re-derived: ``MIN_EDGE_CENTS``, ``MAX_EDGE_CENTS``, ``MIN_PRICE_CENTS``,
   ``MAX_CONFIDENCE_YES_FOR_NO``, ``MODEL_PROB_CAP`` (``src.config``), the
   margin gate via ``src.strategy.scanner.no_entry_margin_gap`` (imported),
   and the taker fee via ``src.strategy.fee.estimate_fee_cents``. Also
   ``MIN_CONFIDENCE_YES`` on the YES side -- one addition beyond the issue's
   explicit 6-gate list, flagged rather than silently applied, since it's the
   real YES-entry condition in ``scanner.py`` today. MAE suppression,
   day-mismatch forcing, and shadow/live routing are deliberately excluded
   (matches "evaluate all 30 cities as if each were emos_primary").
   The margin gate's own input (``state.forecast_high_f``, the RAW NWS
   number -- not the EMOS mean) is reconstructed from the single
   ``model="nws"`` rows in ``model_forecast_log``, identical for both arms,
   so it cannot bias the paired comparison between them.
7. **Policy constants held at TODAY's live values** for the whole eval
   window, not whatever was historically live on each date (e.g.
   ``MAX_CONFIDENCE_YES_FOR_NO`` changed 0.05->0.10 on 2026-10-11). A
   deliberate simplification -- evaluating "as if today's policy had always
   been in force" -- disclosed here rather than silently mixed.
8. **First-qualifying-poll selection, per arm, independently.** For each
   ``(station, ticker, end_date)`` and EACH arm, polls are walked in ``ts``
   order and the first one whose recomputed gate passes (YES or NO) is kept
   as that arm's trade -- mirrors ``run.py``'s shadow dedup (trade created on
   the first qualifying candidate). The two arms can select different polls,
   or even different sides, for the same market.
9. **Outcomes**: ``src.scripts.bss_market_vs_model_report.
   resolve_candidate_outcomes`` (imported, unchanged) -- Gamma-first,
   observed-daily-high fallback, the same resolution M3 uses.
10. **Pre-registered decision rule** (restated from the issue, applied
    exactly, never adjusted after seeing results): GO only if, out of
    sample, (1) total pnl/contract is higher with nearest-bin coefficients,
    (2) NO-side pnl is not lower, and (3) a paired bootstrap 95% CI of the
    pnl difference excludes zero. Otherwise NO or INCONCLUSIVE with numbers.

Usage::

    python -m src.scripts.per_lead_emos_offline_eval \\
        --db data/meteoedge.db --train-eval-cutoff 2026-09-15
"""
from __future__ import annotations

import argparse
import logging
import math
import random
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import (  # noqa: E402
    BRACKET_EVALS_JSONL, FORECAST_STDDEV_F, MAX_CONFIDENCE_YES_FOR_NO,
    MAX_EDGE_CENTS, MIN_CONFIDENCE_YES, MIN_EDGE_CENTS, MIN_FORECAST_BRACKET_MARGIN_F,
    MIN_PRICE_CENTS, MODEL_PROB_CAP, STATIONS, get_live_config, station_city,
)
from src.model.crps_score import crps_gaussian  # noqa: E402
from src.model.emos_calibration import (  # noqa: E402
    GFS_DATA_VALID_FROM,
    _is_clamped_legacy_sigma,
    blend_coefficients,
    fit_emos,
    pooling_group,
    shrinkage_weight,
)
from src.model.emos_mode import (  # noqa: E402
    _active_stack_models,
    _nearest_lead_hours,
    resolve_sigma_raw,
)
from src.model.envelope import Bracket, WeatherState, true_probability_yes  # noqa: E402
from src.scripts.bss_market_vs_model_report import (  # noqa: E402
    DEFAULT_DB_PATH,
    filter_rows_since,
    load_bracket_eval_rows,
    resolve_candidate_outcomes,
)
from src.scripts.emos_shadow_reconstruction import (  # noqa: E402
    ReadOnlyDatabase,
    _station_local_now,
    reconstruct_current_high,
    reconstruct_intraday_delta,
    reconstruct_latest_temp,
    reconstruct_mu_raw,
)
from src.strategy.fee import estimate_fee_cents  # noqa: E402
from src.strategy.scanner import STATION_TO_CITY, no_entry_margin_gap  # noqa: E402

log = logging.getLogger(__name__)

LEAD_BINS: tuple[int, ...] = (3, 6, 12, 18, 24)

# Mirrors run_emos_shadow.py's _run_calibration constants (issue #798) -- not
# importable, since they are local to that function, not module-level. Plain
# data-sufficiency thresholds, not business logic, so duplicating the two int
# literals here carries little drift risk.
POOLED_MIN_CITY_SAMPLES = 5
FULL_WEIGHT_SAMPLES = 60

# The 7 cities promoted to emos_primary as of the epic #1449 state table
# (2026-10-10), reported separately per the issue's acceptance criteria.
PROMOTED_CITIES: frozenset[str] = frozenset({
    "Miami", "Seoul", "Kuala Lumpur", "Singapore", "Panama City", "London", "Paris",
})

Coeffs = "tuple[float, float, float, float]"


class _StubSigmaState:
    """Minimal stand-in for resolve_sigma_raw's state arg (ensemble_sigma_f
    unavailable in this offline reconstruction, same as emos_shadow_
    reconstruction._StubSigmaState -- redefined locally to avoid importing
    yet another private name for a one-line stub)."""

    ensemble_sigma_f = None


def _city_to_station(city: str) -> "str | None":
    for row in STATIONS:
        if row[3] == city:
            return row[0]
    return None


@dataclass
class OfflineEvalCache:
    """Memoizes repeated reconstruction queries within one ``run()`` call.

    Performance-only -- correctness is unaffected since every cached lookup
    is a deterministic function of its key. Two independent facts make this
    a large win rather than a premature optimisation: (1) ``model_forecast_
    log``/``observations`` lookups for the SAME (station, date) recur across
    every lead bin being fitted (6x) and across a city's own + its pooling
    group's fetch; (2) ALL brackets (tickers) for a given station are
    evaluated from the SAME scan poll, so they share the exact same
    ``minutes_to_settlement``/``poll_ts`` -- caching on
    (station, date[, poll_ts/minutes_to_settlement]) turns what would be one
    DB round-trip per (ticker, poll) into one per (station, poll), typically
    a 10-100x reduction given how many brackets share one station-poll.
    """

    obs_high: "dict[tuple, object]" = None
    current_high: "dict[tuple, object]" = None
    latest_temp: "dict[tuple, object]" = None
    mu_raw: "dict[tuple, object]" = None
    intraday_delta: "dict[tuple, float]" = None
    nws_forecast: "dict[tuple, object]" = None

    def __post_init__(self):
        for field in ("obs_high", "current_high", "latest_temp", "mu_raw",
                      "intraday_delta", "nws_forecast"):
            if getattr(self, field) is None:
                setattr(self, field, {})


def _cached_obs_high(cache: OfflineEvalCache, db, station: str, date_str: str):
    key = (station, date_str)
    if key not in cache.obs_high:
        cache.obs_high[key] = db.get_daily_obs_high(station, date_str)
    return cache.obs_high[key]


def _cached_current_high(cache: OfflineEvalCache, db, station: str, date_str: str, poll_ts: str):
    key = (station, date_str, poll_ts)
    if key not in cache.current_high:
        cache.current_high[key] = reconstruct_current_high(db, station, date_str, poll_ts)
    return cache.current_high[key]


def _cached_latest_temp(cache: OfflineEvalCache, db, station: str, poll_ts: str):
    key = (station, poll_ts)
    if key not in cache.latest_temp:
        cache.latest_temp[key] = reconstruct_latest_temp(db, station, poll_ts)
    return cache.latest_temp[key]


def _cached_mu_raw(cache: OfflineEvalCache, db, station: str, date_str: str, minutes_to_settlement: float):
    key = (station, date_str, minutes_to_settlement)
    if key not in cache.mu_raw:
        cache.mu_raw[key] = reconstruct_mu_raw(db, station, date_str, minutes_to_settlement)
    return cache.mu_raw[key]


def _cached_intraday_delta(cache: OfflineEvalCache, db, city: str, date_str: str, poll_ts: str):
    key = (city, date_str, poll_ts)
    if key not in cache.intraday_delta:
        cache.intraday_delta[key] = reconstruct_intraday_delta(db, city, date_str, poll_ts)
    return cache.intraday_delta[key]


def _cached_nws_forecast(cache: OfflineEvalCache, db, station: str, date_str: str, minutes_to_settlement: float):
    key = (station, date_str, minutes_to_settlement)
    if key not in cache.nws_forecast:
        cache.nws_forecast[key] = _reconstruct_nws_forecast(db, station, date_str, minutes_to_settlement)
    return cache.nws_forecast[key]


# ---------------------------------------------------------------------------
# Step 1-2: dated training triples + the two coefficient sets
# ---------------------------------------------------------------------------

def _fetch_dated_triples(
    db, station: str, lead_hours: int, regime: "frozenset[str]", sigma_source: str,
    cache: "OfflineEvalCache | None" = None,
    date_lo: "str | None" = None, date_hi: "str | None" = None,
) -> "list[tuple[str, float, float, float]]":
    """Mirror ``emos_calibration.fetch_training_data``'s join logic, dated.

    Returns ``(date_str, mu_f, sigma_f, obs_high)`` tuples for *station*,
    restricted to ``date_lo <= date < date_hi`` (either bound optional).
    ``fit_emos`` (imported, unmodified) does the actual fitting; this
    function only reproduces the row-fetch/join/exclusion steps
    ``fetch_training_data`` does not expose with dates attached (see the
    module docstring, point 2).
    """
    rows = db.get_forecast_log_by_lead(station, since_date="2000-01-01", lead_hours=lead_hours)
    rows = [r for r in rows if r.get("model") in regime]
    rows = [
        r for r in rows
        if not (r.get("model") == "gfs" and r.get("date", "") < GFS_DATA_VALID_FROM)
    ]

    default_sigma = float(FORECAST_STDDEV_F)
    date_mu_accum: "dict[str, list[float]]" = {}
    date_sigma: "dict[str, object]" = {}
    _SENTINEL = object()
    for row in rows:
        d = row["date"]
        date_mu_accum.setdefault(d, []).append(float(row["forecast_high_f"]))
        if sigma_source == "fixed":
            continue
        if date_sigma.get(d) is None:
            raw_sigma = row.get("sigma_f")
            if raw_sigma is not None and _is_clamped_legacy_sigma(row):
                date_sigma[d] = _SENTINEL
                continue
            date_sigma[d] = float(raw_sigma) if raw_sigma is not None else None

    out: "list[tuple[str, float, float, float]]" = []
    for date_str, mu_list in date_mu_accum.items():
        if date_lo is not None and date_str < date_lo:
            continue
        if date_hi is not None and date_str >= date_hi:
            continue
        sigma_f = date_sigma.get(date_str)
        if sigma_f is _SENTINEL:
            continue
        if sigma_f is None:
            sigma_f = default_sigma
        obs_high = (
            _cached_obs_high(cache, db, station, date_str) if cache is not None
            else db.get_daily_obs_high(station, date_str)
        )
        if obs_high is None:
            continue
        mu_f = sum(mu_list) / len(mu_list)
        if any(math.isnan(v) or math.isinf(v) for v in (mu_f, sigma_f, obs_high)):
            continue
        out.append((date_str, mu_f, sigma_f, obs_high))
    return out


def _fit_with_pooling(
    db, city: str, lead_hours: int, regime: "frozenset[str]", sigma_source: str,
    train_until: str, pooled_cache: dict, cache: "OfflineEvalCache | None" = None,
) -> "Coeffs | None":
    """Fit *city*'s coefficients at *lead_hours*, blended with its pooling
    group below FULL_WEIGHT_SAMPLES own triples (issue #798 partial pooling,
    reused via the imported pooling_group/shrinkage_weight/blend_coefficients
    -- this function only orchestrates, it re-derives no probability math).
    """
    station = _city_to_station(city)
    if station is None:
        return None
    own = _fetch_dated_triples(
        db, station, lead_hours, regime, sigma_source, cache=cache, date_hi=train_until,
    )
    n_city = len(own)
    if n_city == 0:
        return None
    own_triples = [(mu, sigma, y) for (_d, mu, sigma, y) in own]

    if n_city >= FULL_WEIGHT_SAMPLES:
        return fit_emos(own_triples)
    if n_city < POOLED_MIN_CITY_SAMPLES:
        return None

    group = pooling_group(city)
    if group is None:
        return None
    cache_key = (group, lead_hours)
    if cache_key not in pooled_cache:
        pooled_triples: "list[tuple[float, float, float]]" = []
        for station_cfg in STATIONS:
            other_city = station_city(station_cfg)
            if pooling_group(other_city) != group:
                continue
            other_station = _city_to_station(other_city)
            if other_station is None:
                continue
            rows = _fetch_dated_triples(
                db, other_station, lead_hours, regime, sigma_source, cache=cache, date_hi=train_until,
            )
            pooled_triples.extend((mu, sigma, y) for (_d, mu, sigma, y) in rows)
        pooled_cache[cache_key] = fit_emos(pooled_triples) if pooled_triples else None
    pooled_fit = pooled_cache[cache_key]
    if pooled_fit is None:
        return None

    city_fit = fit_emos(own_triples)
    weight = shrinkage_weight(n_city, FULL_WEIGHT_SAMPLES)
    return blend_coefficients(city_fit, pooled_fit, weight)


@dataclass
class CityCoefficients:
    city: str
    coeff_24h_only: "Coeffs | None"
    by_lead: "dict[int, Coeffs]"


def fit_all_coefficients(
    db, cities: "list[str]", regime: "frozenset[str]", sigma_source: str, train_until: str,
    cache: "OfflineEvalCache | None" = None,
) -> "dict[str, CityCoefficients]":
    """Fit both coefficient sets for every city, training data strictly
    before *train_until* (ISO date). See module docstring point 2."""
    out: "dict[str, CityCoefficients]" = {}
    pooled_cache: dict = {}
    for city in cities:
        coeff_24h = _fit_with_pooling(db, city, 24, regime, sigma_source, train_until, pooled_cache, cache=cache)
        by_lead: "dict[int, Coeffs]" = {}
        for lead in LEAD_BINS:
            coeff = _fit_with_pooling(db, city, lead, regime, sigma_source, train_until, pooled_cache, cache=cache)
            if coeff is not None:
                by_lead[lead] = coeff
        out[city] = CityCoefficients(city=city, coeff_24h_only=coeff_24h, by_lead=by_lead)
    return out


# ---------------------------------------------------------------------------
# Per-lead held-out CRPS (visibility metric, AC item 2 -- not the decision rule)
# ---------------------------------------------------------------------------

def per_lead_crps(
    db, cities: "dict[str, CityCoefficients]", regime: "frozenset[str]", sigma_source: str,
    eval_from: str, cache: "OfflineEvalCache | None" = None,
) -> "dict[int, dict[str, object]]":
    """Mean CRPS per lead bin on eval-window triples, for each arm.

    ``24h_only``: the city's 24h-fit coefficients applied to that lead bin's
    eval triples. ``nearest_bin``: the city's own lead-specific coefficients
    (falls back to skipping the city for that lead if it has none).
    """
    result: "dict[int, dict[str, object]]" = {}
    for lead in LEAD_BINS:
        scores_24h: "list[float]" = []
        scores_bin: "list[float]" = []
        for city, coeffs in cities.items():
            station = _city_to_station(city)
            if station is None:
                continue
            triples = _fetch_dated_triples(
                db, station, lead, regime, sigma_source, cache=cache, date_lo=eval_from,
            )
            if not triples:
                continue
            if coeffs.coeff_24h_only is not None:
                a, b, c, d = coeffs.coeff_24h_only
                for _d, mu, sigma, y in triples:
                    scores_24h.append(crps_gaussian(a + b * mu, c + d * sigma, y))
            bin_coeff = coeffs.by_lead.get(lead)
            if bin_coeff is not None:
                a, b, c, d = bin_coeff
                for _d, mu, sigma, y in triples:
                    scores_bin.append(crps_gaussian(a + b * mu, c + d * sigma, y))
        result[lead] = {
            "n_24h_only": len(scores_24h),
            "crps_24h_only": sum(scores_24h) / len(scores_24h) if scores_24h else None,
            "n_nearest_bin": len(scores_bin),
            "crps_nearest_bin": sum(scores_bin) / len(scores_bin) if scores_bin else None,
        }
    return result


# ---------------------------------------------------------------------------
# Step 4-6: per-poll reconstruction + entry-gate replay
# ---------------------------------------------------------------------------

@dataclass
class GateConfig:
    min_edge_cents: float
    max_edge_cents: float
    min_price_cents: float
    max_conf_yes_for_no: float
    min_conf_yes: float
    model_prob_cap: float
    min_margin_f: float
    deb_enabled: bool
    sigma_climb_fraction: float
    use_ensemble_sigma: bool


def _live_gate_config(db) -> GateConfig:
    """Read today's live-tunable gate thresholds (module docstring point 7:
    held constant across the whole eval window, deliberately)."""
    live = get_live_config(db)
    return GateConfig(
        min_edge_cents=float(live.get("MIN_EDGE_CENTS", MIN_EDGE_CENTS)),
        max_edge_cents=float(live.get("MAX_EDGE_CENTS", MAX_EDGE_CENTS)),
        min_price_cents=float(live.get("MIN_PRICE_CENTS", MIN_PRICE_CENTS)),
        max_conf_yes_for_no=float(live.get("MAX_CONFIDENCE_YES_FOR_NO", MAX_CONFIDENCE_YES_FOR_NO)),
        min_conf_yes=MIN_CONFIDENCE_YES,
        model_prob_cap=float(live.get("MODEL_PROB_CAP", MODEL_PROB_CAP)),
        min_margin_f=float(live.get("MIN_FORECAST_BRACKET_MARGIN_F", MIN_FORECAST_BRACKET_MARGIN_F)),
        deb_enabled=bool(live.get("DEB_ENABLED", False)),
        sigma_climb_fraction=float(live.get("ENVELOPE_SIGMA_CLIMB_FRACTION", 0.5)),
        use_ensemble_sigma=bool(live.get("USE_ENSEMBLE_SIGMA", True)),
    )


def _clamp_p_yes(p: float, cap: float) -> float:
    cap_lower = round(1.0 - cap, 10)
    return min(max(p, cap_lower), cap)


def evaluate_entry(
    p_yes_raw: float, yes_ask_cents: float, no_ask_cents: float,
    margin_gap: "float | None", cfg: GateConfig,
) -> "tuple[str, float, float, float] | None":
    """Replay scanner.py's YES-then-NO entry-gate precedence (lines 1037/1077
    there) using the SAME imported constants/formula. Returns
    (side, price_cents, p_yes, ev_cents) or None.
    """
    p_yes = _clamp_p_yes(p_yes_raw, cfg.model_prob_cap)
    fee = estimate_fee_cents(min(yes_ask_cents, no_ask_cents))
    ev_yes = p_yes * 100 - yes_ask_cents - fee
    ev_no = (1 - p_yes) * 100 - no_ask_cents - fee

    if ev_yes >= cfg.min_edge_cents and p_yes >= cfg.min_conf_yes and yes_ask_cents >= cfg.min_price_cents:
        if ev_yes > cfg.max_edge_cents:
            return None
        return ("YES", yes_ask_cents, p_yes, ev_yes)

    if ev_no >= cfg.min_edge_cents and p_yes <= cfg.max_conf_yes_for_no and no_ask_cents >= cfg.min_price_cents:
        if ev_no > cfg.max_edge_cents:
            return None
        if margin_gap is not None and margin_gap < cfg.min_margin_f:
            return None
        return ("NO", no_ask_cents, p_yes, ev_no)

    return None


def _reconstruct_nws_forecast(
    db, station: str, date_str: str, minutes_to_settlement: float,
) -> "float | None":
    """Raw NWS forecast at the nearest lead bin -- the margin gate's own
    input (``state.forecast_high_f``, not the EMOS mean -- see module
    docstring point 6). Identical for both arms by construction."""
    rows = db.get_forecast_log_for_date(station, date_str)
    candidates = [r for r in rows if r.get("model") == "nws" and r.get("lead_hours") is not None]
    if not candidates:
        return None
    leads = [c["lead_hours"] for c in candidates]
    nearest = _nearest_lead_hours(minutes_to_settlement / 60.0, leads)
    match = next(c for c in candidates if c["lead_hours"] == nearest)
    return match["forecast_high_f"]


def _mu_sigma_for_variant(
    db, station: str, city: str, date_str: str, poll_ts: str, minutes_to_settlement: float,
    coeffs: CityCoefficients, variant: str, use_ensemble_sigma: bool,
    cache: "OfflineEvalCache | None" = None,
) -> "tuple[float, float] | None":
    """(mu_final, sigma_cal) for *variant* ('24h_only' | 'nearest_bin'),
    reconstructed from persisted tables -- same #658 layer contract
    apply_emos/emos_serving_mu implement, applied with THIS module's own
    offline-fit coefficients instead of a DB-read row (module docstring
    point 2's whole reason for existing)."""
    mu_raw = (
        _cached_mu_raw(cache, db, station, date_str, minutes_to_settlement) if cache is not None
        else reconstruct_mu_raw(db, station, date_str, minutes_to_settlement)
    )
    if mu_raw is None:
        return None

    if variant == "24h_only":
        coeff = coeffs.coeff_24h_only
    else:
        if not coeffs.by_lead:
            coeff = None
        else:
            nearest = _nearest_lead_hours(minutes_to_settlement / 60.0, list(coeffs.by_lead.keys()))
            coeff = coeffs.by_lead[nearest]
    if coeff is None:
        return None

    sigma_raw = resolve_sigma_raw(_StubSigmaState(), use_ensemble_sigma, FORECAST_STDDEV_F)
    a, b, c, d = coeff
    mu_cal = a + b * mu_raw
    sigma_cal = c + d * sigma_raw
    if sigma_cal <= 0:
        sigma_cal = sigma_raw
    intraday_delta = (
        _cached_intraday_delta(cache, db, city, date_str, poll_ts) if cache is not None
        else reconstruct_intraday_delta(db, city, date_str, poll_ts)
    )
    return mu_cal + intraday_delta, sigma_cal


@dataclass
class PollCandidate:
    station: str
    ticker: str
    end_date: str
    ts: str
    bracket_low: float
    bracket_high: float
    yes_ask: float
    no_ask: float
    minutes_to_settlement: float


def _evaluate_poll(
    db, poll: PollCandidate, coeffs: CityCoefficients, variant: str, cfg: GateConfig,
    cache: "OfflineEvalCache | None" = None,
) -> "tuple[str, float, float, float] | None":
    """Full per-poll pipeline for one variant: reconstruct inputs, compute
    p_yes via true_probability_yes (reused, module docstring point 5), then
    replay the entry gate (point 6). Returns evaluate_entry's result or None
    -- for any unreconstructable input (no mu_raw, no coefficients for this
    lead, no current_high/latest_temp, no station timezone), returns None
    rather than guessing (never a silent approximation)."""
    station = poll.station
    city = STATION_TO_CITY.get(station, station)
    date_str = poll.end_date

    resolved = _mu_sigma_for_variant(
        db, station, city, date_str, poll.ts, poll.minutes_to_settlement,
        coeffs, variant, cfg.use_ensemble_sigma, cache=cache,
    )
    if resolved is None:
        return None
    mu_final, sigma_cal = resolved

    if cache is not None:
        current_high = _cached_current_high(cache, db, station, date_str, poll.ts)
        latest_temp = _cached_latest_temp(cache, db, station, poll.ts)
    else:
        current_high = reconstruct_current_high(db, station, date_str, poll.ts)
        latest_temp = reconstruct_latest_temp(db, station, poll.ts)
    if current_high is None or latest_temp is None:
        return None
    now_local = _station_local_now(station, poll.ts)
    if now_local is None:
        return None
    try:
        settlement_date = _date.fromisoformat(date_str[:10])
    except ValueError:
        return None

    bracket = Bracket(
        ticker=poll.ticker, low_f=poll.bracket_low, high_f=poll.bracket_high,
        yes_ask_cents=int(poll.yes_ask), yes_ask_size=0,
        no_ask_cents=int(poll.no_ask), no_ask_size=0,
    )
    state = WeatherState(
        station=station, now_local=now_local, sunset_local=now_local,
        current_high_f=float(current_high), current_high_time=now_local,
        latest_temp_f=float(latest_temp), latest_temp_time=now_local,
        forecast_high_f=None, corrected_mu_f=mu_final,
    )
    p_yes = true_probability_yes(
        bracket, state, minutes_to_settlement=poll.minutes_to_settlement,
        forecast_stddev=sigma_cal, deb_enabled=cfg.deb_enabled,
        sigma_climb_fraction=cfg.sigma_climb_fraction, use_ensemble_sigma=False,
        settlement_date=settlement_date,
    )

    margin_gap = None
    nws_fc = (
        _cached_nws_forecast(cache, db, station, date_str, poll.minutes_to_settlement) if cache is not None
        else _reconstruct_nws_forecast(db, station, date_str, poll.minutes_to_settlement)
    )
    if nws_fc is not None:
        margin_state = WeatherState(
            station=station, now_local=now_local, sunset_local=now_local,
            current_high_f=float(current_high), current_high_time=now_local,
            latest_temp_f=float(latest_temp), latest_temp_time=now_local,
            forecast_high_f=nws_fc,
        )
        margin_gap = no_entry_margin_gap(bracket, margin_state, market_date=settlement_date)

    return evaluate_entry(p_yes, poll.yes_ask, poll.no_ask, margin_gap, cfg)


# ---------------------------------------------------------------------------
# Step 3 + 8: candidate population + first-qualifying-poll selection
# ---------------------------------------------------------------------------

def load_same_day_polls(bracket_evals_path: Path, since: str) -> "dict[tuple, list[dict]]":
    """Load bracket_evals rows, keep same-day/high-direction/since-filtered,
    group by (station, ticker, end_date), sorted ascending by poll ts."""
    rows = load_bracket_eval_rows(bracket_evals_path)
    rows, _ = filter_rows_since(rows, since)

    groups: "dict[tuple, list[dict]]" = defaultdict(list)
    for row in rows:
        flag = row.get("is_next_day_flag")
        try:
            is_next_day = bool(int(flag)) if flag is not None else False
        except (TypeError, ValueError):
            is_next_day = False
        if is_next_day:
            continue
        if (row.get("direction") or "high") != "high":
            continue
        end_date = (row.get("end_date") or "")[:10]
        if not end_date or not row.get("station") or not row.get("ticker"):
            continue
        if row.get("minutes_to_settlement") is None:
            continue
        if row.get("yes_ask") is None or row.get("no_ask") is None:
            continue
        if row.get("bracket_low") is None or row.get("bracket_high") is None:
            continue
        groups[(row["station"], row["ticker"], end_date)].append(row)

    for key, polls in groups.items():
        polls.sort(key=lambda r: r.get("ts") or "")
    return groups


@dataclass
class ArmTrade:
    station: str
    ticker: str
    end_date: str
    side: str
    price_cents: float
    p_yes: float
    poll_ts: str
    bracket_low: float
    bracket_high: float
    question: "str | None" = None


def select_first_qualifying_trades(
    db, groups: "dict[tuple, list[dict]]", coeffs_by_city: "dict[str, CityCoefficients]",
    variant: str, cfg: GateConfig, cache: "OfflineEvalCache | None" = None,
) -> "list[ArmTrade]":
    """For each (station, ticker, end_date), walk its polls in order and keep
    the first one this variant's recomputed gate passes (module docstring
    point 8)."""
    trades: "list[ArmTrade]" = []
    for (station, ticker, end_date), polls in groups.items():
        city = STATION_TO_CITY.get(station, station)
        coeffs = coeffs_by_city.get(city)
        if coeffs is None:
            continue
        for row in polls:
            poll = PollCandidate(
                station=station, ticker=ticker, end_date=end_date, ts=row.get("ts") or "",
                bracket_low=row["bracket_low"], bracket_high=row["bracket_high"],
                yes_ask=row["yes_ask"], no_ask=row["no_ask"],
                minutes_to_settlement=row["minutes_to_settlement"],
            )
            result = _evaluate_poll(db, poll, coeffs, variant, cfg, cache=cache)
            if result is not None:
                side, price_cents, p_yes, _ev = result
                trades.append(ArmTrade(
                    station=station, ticker=ticker, end_date=end_date, side=side,
                    price_cents=price_cents, p_yes=p_yes, poll_ts=poll.ts,
                    bracket_low=poll.bracket_low, bracket_high=poll.bracket_high,
                    question=row.get("question"),
                ))
                break
    return trades


# ---------------------------------------------------------------------------
# Step 9-10: outcomes, pnl, paired bootstrap, decision rule
# ---------------------------------------------------------------------------

def trade_pnl(side: str, price_cents: float, yes_won: bool) -> float:
    """Per-contract pnl in the SAME convention as src/scripts/settle.py:
    (100 - price)/100 on a win, -price/100 on a loss, no fee deduction."""
    won = yes_won if side == "YES" else (not yes_won)
    return (100 - price_cents) / 100 if won else -price_cents / 100


def attach_outcomes(trades: "list[ArmTrade]", db_path: Path, use_gamma: bool, allow_network: bool) -> "dict[tuple, bool]":
    """Resolve yes_won per (station, ticker, end_date) via resolve_candidate_
    outcomes (imported, unchanged -- module docstring point 9). Returns a
    dict keyed by that triple since multiple arms may share a ticker-day."""
    rows = [
        {
            "station": t.station, "ticker": t.ticker, "end_date": t.end_date,
            "bracket_low": t.bracket_low, "bracket_high": t.bracket_high,
            "question": t.question, "direction": "high",
        }
        for t in trades
    ]
    resolved, _counts = resolve_candidate_outcomes(rows, db_path, use_gamma=use_gamma, allow_network=allow_network)
    out: "dict[tuple, bool]" = {}
    for r in resolved:
        out[(r["station"], r["ticker"], r["end_date"])] = bool(r["yes_won"])
    return out


def paired_bootstrap_ci(
    pairs: "list[tuple[float, float]]", n_resamples: int = 10_000, seed: int = 1453,
) -> "tuple[float, float, float]":
    """95% percentile bootstrap CI for mean(nearest_bin_pnl - 24h_only_pnl).

    *pairs* is one (24h_only_pnl, nearest_bin_pnl) tuple per candidate in the
    population (0.0 for an arm that did not trade that candidate) -- resampling
    at the candidate level keeps the pairing intact. Returns
    (mean_diff, ci_lo, ci_hi).
    """
    if not pairs:
        return 0.0, 0.0, 0.0
    diffs = [nb - h24 for h24, nb in pairs]
    n = len(diffs)
    mean_diff = sum(diffs) / n
    rng = random.Random(seed)
    means = []
    for _ in range(n_resamples):
        resample = [diffs[rng.randrange(n)] for _ in range(n)]
        means.append(sum(resample) / n)
    means.sort()
    lo = means[int(0.025 * n_resamples)]
    hi = means[min(int(0.975 * n_resamples), n_resamples - 1)]
    return mean_diff, lo, hi


@dataclass
class ArmSummary:
    n_trades: int
    n_wins: int
    total_pnl: float
    no_trades: int
    no_wins: int
    no_pnl: float
    yes_trades: int
    yes_pnl: float


def summarize_trades(trades: "list[ArmTrade]", outcomes: "dict[tuple, bool]") -> ArmSummary:
    n_trades = n_wins = no_trades = no_wins = yes_trades = 0
    total_pnl = no_pnl = yes_pnl = 0.0
    for t in trades:
        key = (t.station, t.ticker, t.end_date)
        if key not in outcomes:
            continue
        yes_won = outcomes[key]
        pnl = trade_pnl(t.side, t.price_cents, yes_won)
        won = yes_won if t.side == "YES" else (not yes_won)
        n_trades += 1
        n_wins += int(won)
        total_pnl += pnl
        if t.side == "NO":
            no_trades += 1
            no_wins += int(won)
            no_pnl += pnl
        else:
            yes_trades += 1
            yes_pnl += pnl
    return ArmSummary(
        n_trades=n_trades, n_wins=n_wins, total_pnl=total_pnl,
        no_trades=no_trades, no_wins=no_wins, no_pnl=no_pnl,
        yes_trades=yes_trades, yes_pnl=yes_pnl,
    )


def decide(summary_24h: ArmSummary, summary_bin: ArmSummary, ci: "tuple[float, float, float]") -> str:
    """Apply the pre-registered decision rule (issue #1453) exactly."""
    _mean, ci_lo, ci_hi = ci
    pnl_higher = summary_bin.total_pnl > summary_24h.total_pnl
    no_not_lower = summary_bin.no_pnl >= summary_24h.no_pnl
    ci_excludes_zero = ci_lo > 0 or ci_hi < 0
    if pnl_higher and no_not_lower and ci_excludes_zero:
        return "GO"
    if summary_bin.total_pnl <= summary_24h.total_pnl:
        return "NO-GO"
    return "INCONCLUSIVE"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run(
    db_path: Path, bracket_evals_path: Path, train_eval_cutoff: str,
    use_gamma: bool, allow_network: bool,
) -> dict:
    ro_db = ReadOnlyDatabase(db_path)
    cache = OfflineEvalCache()
    try:
        regime = _active_stack_models(ro_db)
        live = get_live_config(ro_db)
        sigma_source = "ensemble" if live.get("USE_ENSEMBLE_SIGMA", True) else "fixed"
        cfg = _live_gate_config(ro_db)

        cities = [station_city(cfg_row) for cfg_row in STATIONS]
        coeffs_by_city = fit_all_coefficients(ro_db, cities, regime, sigma_source, train_eval_cutoff, cache=cache)
        crps = per_lead_crps(ro_db, coeffs_by_city, regime, sigma_source, train_eval_cutoff, cache=cache)

        groups = load_same_day_polls(bracket_evals_path, train_eval_cutoff)
        trades_24h = select_first_qualifying_trades(ro_db, groups, coeffs_by_city, "24h_only", cfg, cache=cache)
        trades_bin = select_first_qualifying_trades(ro_db, groups, coeffs_by_city, "nearest_bin", cfg, cache=cache)

        outcomes = attach_outcomes(
            trades_24h + trades_bin, db_path, use_gamma=use_gamma, allow_network=allow_network,
        )

        def _filtered(trades, cities_filter=None):
            if cities_filter is None:
                return trades
            return [t for t in trades if STATION_TO_CITY.get(t.station, t.station) in cities_filter]

        all_cities = frozenset(cities)
        summary_24h_all = summarize_trades(_filtered(trades_24h, all_cities), outcomes)
        summary_bin_all = summarize_trades(_filtered(trades_bin, all_cities), outcomes)
        summary_24h_promoted = summarize_trades(_filtered(trades_24h, PROMOTED_CITIES), outcomes)
        summary_bin_promoted = summarize_trades(_filtered(trades_bin, PROMOTED_CITIES), outcomes)

        pnl_24h_by_key: "dict[tuple, float]" = {}
        for t in trades_24h:
            key = (t.station, t.ticker, t.end_date)
            if key in outcomes:
                pnl_24h_by_key[key] = trade_pnl(t.side, t.price_cents, outcomes[key])
        pnl_bin_by_key: "dict[tuple, float]" = {}
        for t in trades_bin:
            key = (t.station, t.ticker, t.end_date)
            if key in outcomes:
                pnl_bin_by_key[key] = trade_pnl(t.side, t.price_cents, outcomes[key])

        all_keys = set(pnl_24h_by_key) | set(pnl_bin_by_key)
        pairs = [
            (pnl_24h_by_key.get(k, 0.0), pnl_bin_by_key.get(k, 0.0))
            for k in all_keys
        ]
        ci = paired_bootstrap_ci(pairs)
        verdict = decide(summary_24h_all, summary_bin_all, ci)

        return {
            "train_eval_cutoff": train_eval_cutoff,
            "crps_per_lead": crps,
            "all_cities": {"24h_only": summary_24h_all, "nearest_bin": summary_bin_all},
            "promoted_cities": {"24h_only": summary_24h_promoted, "nearest_bin": summary_bin_promoted},
            "bootstrap_ci": ci,
            "verdict": verdict,
            "n_candidates": len(all_keys),
        }
    finally:
        ro_db._conn.close()


def _fmt_summary(label: str, s: ArmSummary) -> str:
    wr = s.n_wins / s.n_trades * 100 if s.n_trades else 0.0
    return (
        f"{label}: n={s.n_trades} win={wr:.1f}% total_pnl={s.total_pnl:+.2f}  "
        f"(NO n={s.no_trades} pnl={s.no_pnl:+.2f}; YES n={s.yes_trades} pnl={s.yes_pnl:+.2f})"
    )


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--bracket-evals", type=Path, default=BRACKET_EVALS_JSONL)
    ap.add_argument("--train-eval-cutoff", default="2026-09-15", metavar="YYYY-MM-DD",
                     help="Fit on dates strictly before this; evaluate candidates polled on/after it.")
    ap.add_argument("--no-gamma", action="store_true")
    ap.add_argument("--no-network", action="store_true")
    args = ap.parse_args(argv)

    result = run(
        args.db, args.bracket_evals, args.train_eval_cutoff,
        use_gamma=not args.no_gamma, allow_network=not args.no_network,
    )

    print(f"# Per-lead EMOS offline evaluation (issue #1453 Phase A)\n")
    print(f"Train/eval cutoff: {result['train_eval_cutoff']}")
    print(f"Candidates in paired population: {result['n_candidates']}\n")

    print("## Per-lead held-out CRPS\n")
    for lead, stats in sorted(result["crps_per_lead"].items()):
        print(f"  lead={lead:>2}h  24h_only crps={stats['crps_24h_only']!r} (n={stats['n_24h_only']})"
              f"  nearest_bin crps={stats['crps_nearest_bin']!r} (n={stats['n_nearest_bin']})")

    print("\n## All 30 cities\n")
    print(" ", _fmt_summary("24h_only   ", result["all_cities"]["24h_only"]))
    print(" ", _fmt_summary("nearest_bin", result["all_cities"]["nearest_bin"]))

    print("\n## 7 promoted cities\n")
    print(" ", _fmt_summary("24h_only   ", result["promoted_cities"]["24h_only"]))
    print(" ", _fmt_summary("nearest_bin", result["promoted_cities"]["nearest_bin"]))

    mean_diff, ci_lo, ci_hi = result["bootstrap_ci"]
    print(f"\nPaired bootstrap mean(nearest_bin - 24h_only) pnl/contract: "
          f"{mean_diff:+.4f}  95% CI [{ci_lo:+.4f}, {ci_hi:+.4f}]")
    print(f"\n## Decision: {result['verdict']}\n")
    return 0


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    raise SystemExit(main())

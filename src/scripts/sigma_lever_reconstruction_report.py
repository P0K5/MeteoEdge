"""Sigma-lever reconstruction vs. market -- #885 pre-registration (issue #1048).

Answers the ONE open M3 follow-up question the plan left explicitly open
(``docs/REMEDIATION_PLAN.md``, "Pre-registration -- the sigma-lever
reconstruction, signed off 2026-08-26", added by PR #1047 -- read that
section in full before touching this file; it is the fixed spec, this
module is only its implementation): does substituting a real per-station-day
ensemble sigma for the served constant (``FORECAST_STDDEV_F = 2.0``) improve
Brier skill against the market on the M3 population, enough to justify
landing #885/#893 for real?

**Read-only, always** -- same non-negotiable as #1041/#1044. Every DB access
goes through ``ReadOnlyDatabase`` (``src.scripts.emos_shadow_reconstruction``),
which opens ``data/meteoedge.db`` with ``mode=ro`` and a write-denying SQLite
authorizer. The live bot is actively writing to that file and this module
must never block it or risk a write.

**Reuses production code and existing report machinery, never re-derives
it** (the same constraint #1041/#1044 held themselves to):

- Population/exclusion/outcome-resolution/BSS math: ``apply_exclusions``,
  ``dedupe_one_per_bracket_day``, ``filter_rows_since``,
  ``resolve_candidate_outcomes``, ``compute_bss``, ``market_p_yes``,
  ``sharpness_histogram`` -- all imported from
  ``bss_market_vs_model_report`` (the M3 gate script) unchanged.
- ``load_bracket_eval_rows_for_reconstruction`` (the current_high/latest_temp
  raw-field merge) -- imported from ``emos_shadow_vs_market_report``
  unchanged.
- ``reconstruct_current_high``/``reconstruct_latest_temp``/
  ``reconstruct_intraday_delta``/``_station_local_now`` -- imported from
  ``emos_shadow_reconstruction`` unchanged.
- Murphy (1973) Brier decomposition -- ``src.model.murphy_decomposition``
  (new shared helper, not hand-rolled here).

**Three reconstructed probability variants per row, mu held fixed across all
three** (the pre-registration's explicit constraint -- only ``forecast_stddev``
varies):

1. ``baseline``    -- ``forecast_stddev = FORECAST_STDDEV_F`` (2.0), the
   legacy-served constant.
2. ``naive_floor`` -- ``forecast_stddev = max(raw_member_sigma, SIGMA_FLOOR_F)``,
   the UNFLOORED per-day ``gefs``-channel ``model_forecast_log.sigma_f``
   (issue #555), floored only at this consumption site.
3. ``calibrated``  -- ``forecast_stddev`` from
   ``src.model.ensemble_sigma``'s calibration branch (``_calibrated_sigma``),
   fed the aggregate ``sigma_f`` as its ``sigma_naive`` input directly
   (``model_forecast_log`` retains no per-member values to re-derive spread
   from -- the issue's own guidance for this gap).

All three variants use the SAME mu: a legacy/DEB reconstruction (DEB blend +
the persisted intraday delta), deliberately NOT the EMOS path
``emos_shadow_reconstruction.reconstruct_emos_mu_sigma`` uses --
#885/#893 wire the DIRECT-SUBSTITUTION path (``envelope.py``'s
``true_probability_yes``, ``use_ensemble_sigma`` branch), which serves on
DEB-corrected mu, not EMOS mu. See ``reconstruct_mu_legacy`` for the exact
construction and its one documented gap (the #307 residual correction is not
reconstructed).

**Per-variant population, never partially reconstructed within a variant's
own required inputs.** A row missing anything ALL THREE variants need (mu,
current_high/latest_temp, station-local day) is dropped from all three. A
row with a good mu but no ``gefs`` log for that station/date still gets a
``baseline`` value; ``naive_floor``/``calibrated`` are simply absent for
that row (pre-registration: "a variant is never compared against a headline
figure computed on a different population than the one that variant
actually covers").

Usage::

    python -m src.scripts.sigma_lever_reconstruction_report --since 2026-08-06
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date as _date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import BRACKET_EVALS_JSONL, FORECAST_STDDEV_F, get_live_config  # noqa: E402
from src.model.deb_hourly_consensus import compute_deb_mu_f  # noqa: E402
from src.model.deb_weighting import get_weights  # noqa: E402
from src.model.emos_mode import _nearest_lead_hours  # noqa: E402
from src.model.ensemble_sigma import (  # noqa: E402
    MIN_CALIBRATION_SAMPLES, SIGMA_FLOOR_F, _calibrated_sigma, _load_calibration_pairs,
)
from src.model.envelope import Bracket, WeatherState, true_probability_yes  # noqa: E402
from src.model.intraday_correction import _get_station_region  # noqa: E402
from src.model.murphy_decomposition import (  # noqa: E402
    format_murphy_decomposition, murphy_decomposition,
)
from src.scripts.bss_market_vs_model_report import (  # noqa: E402
    DEFAULT_DB_PATH,
    apply_exclusions,
    build_reliability,
    compute_bss,
    dedupe_one_per_bracket_day,
    filter_rows_since,
    format_reliability,
    format_sharpness,
    market_p_yes,
    resolve_candidate_outcomes,
    sharpness_histogram,
)
from src.scripts.emos_shadow_reconstruction import (  # noqa: E402
    ReadOnlyDatabase,
    _station_local_now,
    reconstruct_current_high,
    reconstruct_intraday_delta,
    reconstruct_latest_temp,
)
from src.scripts.emos_shadow_vs_market_report import (  # noqa: E402
    load_bracket_eval_rows_for_reconstruction,
)
from src.strategy.scanner import STATION_TO_CITY  # noqa: E402

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

DEFAULT_OUT_DIR = Path("backtest_results")
# Same window the M3 gate and #1041's EMOS-shadow reconstruction use, for
# direct comparability of BSS numbers across all three reports.
DEFAULT_SINCE = "2026-08-06"

VARIANT_BASELINE = "baseline"
VARIANT_NAIVE_FLOOR = "naive_floor"
VARIANT_CALIBRATED = "calibrated"
VARIANTS = (VARIANT_BASELINE, VARIANT_NAIVE_FLOOR, VARIANT_CALIBRATED)
SIGMA_VARIANTS = (VARIANT_NAIVE_FLOOR, VARIANT_CALIBRATED)

VARIANT_LABELS = {
    VARIANT_BASELINE: "Legacy-reconstructed baseline (forecast_stddev=FORECAST_STDDEV_F=2.0)",
    VARIANT_NAIVE_FLOOR: "Per-day naive-floor sigma (max(raw GEFS member sigma, SIGMA_FLOOR_F))",
    VARIANT_CALIBRATED: "Per-day calibrated sigma (ensemble_sigma._calibrated_sigma)",
}

# Documented, approximate proxy for the pre-registration's "dawn-closing"
# cohort (KATL/KHOU/KORD/SBGR, 11:00Z final poll, ~12h from settlement --
# docs/REMEDIATION_PLAN.md's #1048 section, issue #1021). NOT a byte-
# identical reproduction of #1021's 12 identified near-uniform ladders --
# that diagnostic needed the FULL bracket ladder per station-day to measure
# mass/sharpness; this module scores one bracket row at a time and has no
# such view. This is the closest reconstructable proxy from a single row:
# the named stations, at their final (lowest minutes_to_settlement) poll for
# a station-day, when that poll is still unusually far from settlement.
DAWN_COHORT_STATIONS = frozenset({"KATL", "KHOU", "KORD", "SBGR"})
DAWN_MIN_MINUTES_TO_SETTLEMENT = 600.0  # ~10h; pre-registration cites ~12h

REQUIRED_CAVEATS = """\
> **PRE-REGISTERED READ, NOT PART OF M3 (issue #1048 / PR #1047).** Scores
> THREE reconstructed offline probabilities -- a legacy-reconstructed
> baseline and two per-day ensemble-sigma variants -- against the market, on
> the identical population and methodology the M3 gate used. It answers a
> narrower question than M3: "if a per-day ensemble sigma had been served
> instead of the fixed 2.0F constant, how would Brier skill have changed?"
>
> **1. Mu reconstruction gap.** All three variants share one reconstructed
> mu: a DEB blend (`compute_deb_mu_f`) plus the persisted intraday delta.
> The #307 per-city rolling residual correction, which IS applied on top of
> this in live serving, is NOT reconstructed here (same "no signal" gap
> `emos_shadow_reconstruction` documents for `obs_bias_offset_f`).
>
> **2. Dawn-cohort proxy.** The "dawn-closing" sensitivity split below uses
> an approximate station+lead-time proxy, not the exact 12-row set #1021
> identified (see `DAWN_COHORT_STATIONS` docstring for why an exact
> reproduction isn't reconstructable from single bracket rows).
>
> **3. Reconstruction has documented, non-guessable gaps** (next-day rows,
> low-direction rows, rows with no matching `model_forecast_log` entry,
> rows `current_high`/`latest_temp` can't be recovered from `observations`)
> -- the reconstructed n and station-day counts will be at or below the M3
> gate's own 316/300, per the pre-registration's explicit expectation.
"""


def is_dawn_cohort_row(row: dict) -> bool:
    """See ``DAWN_COHORT_STATIONS`` module docstring for the exact, documented
    proxy this implements (not an exact reproduction of #1021's 12 rows)."""
    station = row.get("station")
    mins = row.get("minutes_to_settlement")
    if station not in DAWN_COHORT_STATIONS or mins is None:
        return False
    return mins >= DAWN_MIN_MINUTES_TO_SETTLEMENT


def _row_key(row: dict) -> tuple:
    return (row.get("station"), row.get("ticker"), row.get("end_date"))


# ---------------------------------------------------------------------------
# Reconstruction: legacy/DEB mu (shared across all three variants)
# ---------------------------------------------------------------------------

def reconstruct_deb_mu_raw(
    db, station: str, city: str, date_str: str, minutes_to_settlement: float,
) -> "float | None":
    """DEB-blended forecast high for (station, date) -- the ``deb_mu_f`` the
    legacy/DEB path computes at scan time (``src.weather.builder.
    _build_one_station``), BEFORE the intraday correction, reconstructed
    from persisted ``model_forecast_log`` rows instead of a fresh live
    fetch.

    Reuses production code, never re-derives it: ``compute_deb_mu_f`` (the
    exact blend function ``builder.py`` calls) and ``get_weights`` (the
    exact persisted-weight reader ``builder.py``/``compute_correction``
    call) -- only the per-model input values are sourced from
    ``model_forecast_log``'s nearest lead bin (issue #665's
    ``_nearest_lead_hours``, reused directly -- the same idiom
    ``emos_shadow_reconstruction.reconstruct_mu_raw`` uses for the stack
    mean) instead of a live API fetch.

    Returns None when no NWS/Open-Meteo/GFS forecast is logged for this
    (station, date) -- mirrors ``compute_deb_mu_f``'s own "all inputs None"
    return.
    """
    rows = db.get_forecast_log_for_date(station, date_str)
    by_model: "dict[str, list[dict]]" = {}
    for row in rows:
        if row.get("lead_hours") is None:
            continue  # legacy nowcast rows carry no lead bin -- not comparable
        by_model.setdefault(row["model"], []).append(row)

    lead_target = minutes_to_settlement / 60.0

    def _nearest_value(model: str) -> "float | None":
        candidates = by_model.get(model)
        if not candidates:
            return None
        leads = [c["lead_hours"] for c in candidates]
        nearest = _nearest_lead_hours(lead_target, leads)
        match = next(c for c in candidates if c["lead_hours"] == nearest)
        return match["forecast_high_f"]

    forecast_nws = _nearest_value("nws")
    forecast_open_meteo = _nearest_value("open_meteo")
    forecast_gfs = _nearest_value("gfs")
    if forecast_nws is None and forecast_open_meteo is None and forecast_gfs is None:
        return None

    station_region = _get_station_region(city)
    weights = get_weights(db, city, station_region=station_region)
    return compute_deb_mu_f(
        forecast_nws, forecast_open_meteo, weights,
        forecast_gfs=forecast_gfs, station_region=station_region,
    )


def reconstruct_mu_legacy(
    db, station: str, city: str, date_str: str, poll_ts: str,
    minutes_to_settlement: float,
) -> "float | None":
    """Legacy/DEB-path forecast mean -- mirrors ``corrected_mu_f`` as
    ``src.weather.builder._build_one_station`` actually builds it: DEB blend
    (``reconstruct_deb_mu_raw``) plus the SAME decayed intraday delta
    ``emos_shadow_reconstruction.reconstruct_intraday_delta`` already
    reconstructs for the EMOS path (issue #1041) -- reused verbatim rather
    than re-deriving ``compute_correction``, which needs a live network
    fetch (``build_consensus``) this offline, read-only module cannot and
    must not perform.

    Documented gap (see module docstring's caveat #1): the #307 per-city
    rolling residual correction is NOT applied here.

    Returns None when the DEB blend itself is unreconstructable (no
    NWS/Open-Meteo/GFS forecast logged for this station/date).
    """
    deb_mu_raw = reconstruct_deb_mu_raw(db, station, city, date_str, minutes_to_settlement)
    if deb_mu_raw is None:
        return None
    intraday_delta = reconstruct_intraday_delta(db, city, date_str, poll_ts)
    return deb_mu_raw + intraday_delta


# ---------------------------------------------------------------------------
# Reconstruction: the two per-day ensemble-sigma variants
# ---------------------------------------------------------------------------

def reconstruct_sigma_variants(
    db, station: str, date_str: str, minutes_to_settlement: float,
) -> "dict[str, float] | None":
    """Return ``{'raw_member_sigma', 'naive_floor', 'calibrated'}`` for
    (station, date), or None when no ``gefs``-channel ``model_forecast_log``
    row with a non-null ``sigma_f`` exists for this station/date (issue
    #555: this is the UNFLOORED per-day member stdev capture-time already
    persists -- no new capture needed).

    - ``raw_member_sigma``: the persisted value, unchanged.
    - ``naive_floor``: ``max(raw_member_sigma, SIGMA_FLOOR_F)``.
    - ``calibrated``: ``raw_member_sigma`` run through
      ``ensemble_sigma._calibrated_sigma`` against this station's own
      historical ``(sigma_naive, abs_error)`` pairs (``_load_calibration_pairs``,
      reused verbatim). Falls back to the SAME value as ``naive_floor`` when
      fewer than ``MIN_CALIBRATION_SAMPLES`` historical pairs exist -- the
      identical fallback ``compute_ensemble_sigma`` itself uses. This module
      cannot call ``compute_ensemble_sigma`` directly: it takes raw ensemble
      MEMBERS, not an aggregate sigma, and ``model_forecast_log`` retains
      only the aggregate ``sigma_f`` -- per the issue's own guidance, that
      aggregate is used as a direct ``sigma_naive`` input to
      ``_calibrated_sigma`` instead.
    """
    rows = db.get_forecast_log_for_date(station, date_str)
    gefs_rows = [
        r for r in rows
        if r.get("model") == "gefs" and r.get("lead_hours") is not None
        and r.get("sigma_f") is not None
    ]
    if not gefs_rows:
        return None

    lead_target = minutes_to_settlement / 60.0
    leads = [r["lead_hours"] for r in gefs_rows]
    nearest = _nearest_lead_hours(lead_target, leads)
    match = next(r for r in gefs_rows if r["lead_hours"] == nearest)
    raw_sigma = float(match["sigma_f"])
    naive_floor = max(raw_sigma, SIGMA_FLOOR_F)

    pairs = _load_calibration_pairs(station, db)
    if len(pairs) >= MIN_CALIBRATION_SAMPLES:
        calibrated = _calibrated_sigma(raw_sigma, pairs)
    else:
        calibrated = naive_floor

    return {
        "raw_member_sigma": raw_sigma,
        "naive_floor": naive_floor,
        "calibrated": calibrated,
    }


# ---------------------------------------------------------------------------
# Per-row reconstruction: all three variants at once
# ---------------------------------------------------------------------------

def reconstruct_bracket_row_variants(db, row: dict) -> "dict[str, float] | None":
    """Return ``{'baseline': p, 'naive_floor': p, 'calibrated': p}`` (the
    last two possibly absent) for one deduped ``bracket_evals`` row, or None
    when the row is out of scope or unreconstructable for even the baseline
    variant.

    Scope (identical to ``emos_shadow_reconstruction.reconstruct_bracket_row``,
    same rationale): next-day rows and non-"high"-direction rows are out of
    scope; ``current_high``/``latest_temp`` are reconstructed from
    ``observations`` the same causal way (issue #1044) when absent from the
    row.

    Population-per-variant rule (pre-registration, PR #1047): the shared
    inputs every variant needs (mu, current_high, latest_temp, station-local
    day) gate ALL THREE variants together -- a row missing any of those is
    dropped entirely (returns None), never partially reconstructed. The
    GEFS-only inputs (``naive_floor``/``calibrated`` sigma) gate ONLY those
    two keys -- a row with a good mu but no ``gefs`` log for that
    station/date still gets a ``baseline`` value.
    """
    if row.get("is_next_day_flag"):
        try:
            if int(row["is_next_day_flag"]):
                return None
        except (TypeError, ValueError):
            pass
    direction = row.get("direction") or "high"
    if direction != "high":
        return None

    station = row.get("station")
    end_date = (row.get("end_date") or "")[:10]
    mins_left = row.get("minutes_to_settlement")
    poll_ts = row.get("ts", "")
    current_high = row.get("current_high")
    latest_temp = row.get("latest_temp")
    bracket_low = row.get("bracket_low")
    bracket_high = row.get("bracket_high")
    yes_ask = row.get("yes_ask")
    no_ask = row.get("no_ask")
    if not station or not end_date or mins_left is None:
        return None
    if current_high is None:
        current_high = reconstruct_current_high(db, station, end_date, poll_ts)
    if latest_temp is None:
        latest_temp = reconstruct_latest_temp(db, station, poll_ts)
    if current_high is None or latest_temp is None:
        return None
    if bracket_low is None or bracket_high is None or yes_ask is None or no_ask is None:
        return None

    city = STATION_TO_CITY.get(station, station)
    mu_final = reconstruct_mu_legacy(db, station, city, end_date, poll_ts, mins_left)
    if mu_final is None:
        return None

    now_local = _station_local_now(station, poll_ts)
    if now_local is None:
        return None

    try:
        settlement_date = _date.fromisoformat(end_date)
    except ValueError:
        return None

    bracket = Bracket(
        ticker=row.get("ticker", ""),
        low_f=bracket_low, high_f=bracket_high,
        yes_ask_cents=int(yes_ask), yes_ask_size=0,
        no_ask_cents=int(no_ask), no_ask_size=0,
    )
    state = WeatherState(
        station=station, now_local=now_local, sunset_local=now_local,
        current_high_f=float(current_high), current_high_time=now_local,
        latest_temp_f=float(latest_temp), latest_temp_time=now_local,
        forecast_high_f=None,  # unused -- corrected_mu_f (below) takes priority
        corrected_mu_f=mu_final,
    )
    live_config = get_live_config(db)
    deb_enabled = live_config.get("DEB_ENABLED", False)
    sigma_climb_fraction = live_config.get("ENVELOPE_SIGMA_CLIMB_FRACTION", 0.5)

    def _p_yes(forecast_stddev: float) -> float:
        return true_probability_yes(
            bracket, state, minutes_to_settlement=mins_left,
            forecast_stddev=forecast_stddev, deb_enabled=deb_enabled,
            sigma_climb_fraction=sigma_climb_fraction,
            use_ensemble_sigma=False, settlement_date=settlement_date,
        )

    result: "dict[str, float]" = {VARIANT_BASELINE: _p_yes(FORECAST_STDDEV_F)}

    sigma_variants = reconstruct_sigma_variants(db, station, end_date, mins_left)
    if sigma_variants is not None:
        result[VARIANT_NAIVE_FLOOR] = _p_yes(sigma_variants["naive_floor"])
        result[VARIANT_CALIBRATED] = _p_yes(sigma_variants["calibrated"])

    return result


# ---------------------------------------------------------------------------
# Scoring: exclusions -> outcome resolution -> BSS/Murphy, per variant
# ---------------------------------------------------------------------------

def _score_variant_population(
    variant_rows: "list[dict]", db_path: Path, use_gamma: bool, allow_network: bool,
) -> "tuple[list[dict], dict, dict]":
    """``apply_exclusions`` -> ``resolve_candidate_outcomes`` for one
    variant's row population. Mirrors ``bss_market_vs_model_report.
    run_report``'s Pass-2 pipeline exactly, parameterised over which
    ``p_yes_raw`` column was populated for this variant."""
    kept, exclusion_counts = apply_exclusions(variant_rows)
    exclusion_counts["input_rows_all_dates"] = len(variant_rows)
    if not kept:
        return [], exclusion_counts, {}
    samples, outcome_counts = resolve_candidate_outcomes(
        kept, db_path, use_gamma=use_gamma, allow_network=allow_network,
    )
    return samples, exclusion_counts, outcome_counts


def _stats_for_samples(samples: "list[dict]") -> "dict | None":
    """(global BSS stats, Murphy decomposition, n_station_days) for one
    fully-resolved sample list, dawn-inclusive."""
    if not samples:
        return None
    global_stats = compute_bss(samples)
    model_samples = [(r["p_yes_raw"], r["yes_won"]) for r in samples]
    market_samples = [(market_p_yes(r), r["yes_won"]) for r in samples]
    return {
        "global": global_stats,
        "murphy": murphy_decomposition(model_samples),
        "model_samples": model_samples,
        "market_samples": market_samples,
        "n_station_days": len({(r["station"], r.get("settlement_date")) for r in samples}),
        "n": len(samples),
    }


def _restricted_to_keys(samples: "list[dict]", keys: "set[tuple]") -> "list[dict]":
    return [s for s in samples if _row_key(s) in keys]


# ---------------------------------------------------------------------------
# Stopping rule (PR #1047, fixed before any run -- applied mechanically)
# ---------------------------------------------------------------------------

STOPPING_RULE_ROWS = (
    (
        "ΔBSS < +0.05",
        "Stand down. Doesn't even clear M3's own \"marginal\" bar "
        "(0 < BSS <= 0.05). Sigma lever stood down; M3's negative verdict "
        "stands as final per the decision rule -- pivot or shut down, do "
        "not land #885/#893 as a live change.",
    ),
    (
        "+0.05 <= ΔBSS, reconstructed BSS still <= 0",
        "Land #885/#893, re-test, don't decide on the reconstruction "
        "alone. Crosses the marginal bar but doesn't flip the sign; a "
        "reconstruction carries its own error that a fresh powered "
        "live/shadow window resolves and this offline pass cannot. Land "
        "for real, then run a new M3-style gate on the post-cutover "
        "window -- do not treat the offline number itself as the verdict.",
    ),
    (
        "Reconstructed BSS > 0",
        "Land #885/#893 and prioritize the re-gate immediately -- same "
        "action as the row above, higher urgency.",
    ),
)


def apply_stopping_rule(
    best_delta_bss: "float | None", best_variant_bss: "float | None",
) -> "tuple[int | None, str]":
    """Return (row_index, explanation) per PR #1047's 3-row stopping-rule
    table, applied mechanically -- no new judgment calls.

    ``best_delta_bss``/``best_variant_bss`` are the sigma variant (of the
    two) whose ΔBSS-vs-same-population-baseline is largest -- "best of the
    two sigma variants" per the table's own header.
    """
    if best_delta_bss is None or best_variant_bss is None:
        return None, "n/a -- insufficient data (no sigma variant scored) to evaluate the stopping rule."
    if best_delta_bss < 0.05:
        return 0, STOPPING_RULE_ROWS[0][1]
    if best_variant_bss <= 0:
        return 1, STOPPING_RULE_ROWS[1][1]
    return 2, STOPPING_RULE_ROWS[2][1]


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def _fmt(x, spec=".4f"):
    return format(x, spec) if x is not None else "n/a"


def build_sigma_lever_report(
    variant_results: dict,
    reconstruction_counts: dict,
    run_date: str,
    since: "str | None",
) -> str:
    lines = ["# Sigma-Lever Reconstruction vs. Market (issue #1048 / PR #1047 pre-registration)\n"]
    lines.append(f"**Run date:** {run_date}  ")
    lines.append("**Data source:** `logs/bracket_evals.*.jsonl` (issue #826, M3's own population)  ")
    lines.append("**Spec:** `docs/REMEDIATION_PLAN.md`, \"Pre-registration -- the sigma-lever "
                 "reconstruction, signed off 2026-08-26\" (PR #1047) -- fixed before this run.  ")
    if since:
        lines.append(f"**Poll-date window:** rows polled on or after **{since}** "
                     "(M3's own window)  \n")
    else:
        lines.append("**Poll-date window:** none given -- NOT comparable to M3.  \n")
    lines.append(REQUIRED_CAVEATS)
    lines.append("\n---\n")

    lines.append("## Reconstruction funnel\n")
    lines.append("Shared inputs (mu, current_high/latest_temp, station-local day) gate all "
                 "three variants together; the `gefs` sigma log gates ONLY `naive_floor`/"
                 "`calibrated` (see `reconstruct_bracket_row_variants` docstring).\n")
    lines.append("| Stage | Count |")
    lines.append("|---|---|")
    lines.append(f"| De-duplicated rows considered | {reconstruction_counts['n_considered']} |")
    lines.append(f"| Out of scope: next-day rows | {reconstruction_counts['n_next_day']} |")
    lines.append(f"| Out of scope: non-\"high\"-direction rows | "
                 f"{reconstruction_counts['n_non_high_direction']} |")
    lines.append(f"| Unreconstructable (missing mu/obs/timezone inputs) | "
                 f"{reconstruction_counts['n_unreconstructable']} |")
    lines.append(f"| Reconstructed: `baseline` available | "
                 f"{reconstruction_counts['n_baseline_reconstructed']} |")
    lines.append(f"| Of those, no `gefs` sigma log (drops `naive_floor`/`calibrated` only) | "
                 f"{reconstruction_counts['n_gefs_missing']} |")
    lines.append("")

    per_variant_summary = {}
    for variant in VARIANTS:
        vr = variant_results[variant]
        samples = vr["samples"]
        exclusion_counts = vr["exclusion_counts"]
        outcome_counts = vr["outcome_counts"]
        stats = _stats_for_samples(samples)

        lines.append(f"## Variant: {VARIANT_LABELS[variant]}\n")
        input_rows = exclusion_counts.get("input_rows", 0)
        lines.append("| Stage | Count |")
        lines.append("|---|---|")
        lines.append(f"| Input rows (post reconstruction) | {input_rows} |")
        lines.append(f"| Kept after row exclusions | "
                     f"{exclusion_counts.get('kept_after_row_exclusions', 0)} |")
        lines.append(f"| No outcome resolvable | "
                     f"{outcome_counts.get('n_unresolvable', 0)} |")
        if stats is None:
            lines.append("| **Final sample (n)** | **0 -- not scoreable** |")
            lines.append("")
            per_variant_summary[variant] = None
            continue
        lines.append(f"| **Final sample (n)** | **{stats['n']}** |")
        lines.append(f"| **Effective sample size (station-days)** | "
                     f"**{stats['n_station_days']}** |")
        lines.append("")

        g = stats["global"]
        lines.append("**Global result (dawn-closing cohort INCLUDED -- the headline number):**\n")
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        lines.append(f"| n | {g['n']} |")
        lines.append(f"| BS_model | {_fmt(g['bs_model'])} |")
        lines.append(f"| BS_market | {_fmt(g['bs_market'])} |")
        lines.append(f"| BSS | {_fmt(g['bss'])} |")
        lines.append("")

        excl_samples = [s for s in samples if not is_dawn_cohort_row(s)]
        excl_stats_global = compute_bss(excl_samples) if excl_samples else None
        lines.append("**Sensitivity: dawn-closing cohort EXCLUDED (recorded, never substituted "
                     "for the headline number above; see caveat #2):**\n")
        if excl_stats_global is None:
            lines.append("n/a -- no rows survive after excluding the dawn cohort.\n")
        else:
            lines.append("| Metric | Value |")
            lines.append("|---|---|")
            lines.append(f"| n | {excl_stats_global['n']} |")
            lines.append(f"| BSS | {_fmt(excl_stats_global['bss'])} |")
            lines.append("")

        lines.append(format_murphy_decomposition(stats["murphy"], f"{variant}: Murphy decomposition"))
        lines.append("")
        lines.append(format_reliability(build_reliability(stats["model_samples"]), f"{variant}: model"))
        lines.append(format_reliability(build_reliability(stats["market_samples"]),
                                        f"{variant}: market (symmetrized)"))
        lines.append(format_sharpness(sharpness_histogram([p for p, _ in stats["model_samples"]]),
                                      f"{variant}: model sharpness"))
        lines.append("")
        per_variant_summary[variant] = stats

    # -----------------------------------------------------------------
    # Same-population ΔBSS: each sigma variant vs. baseline RESTRICTED to
    # that variant's own final sample keys (pre-registration's explicit
    # "never compared against a headline figure computed on a different
    # population" rule).
    # -----------------------------------------------------------------
    lines.append("\n---\n")
    lines.append("## ΔBSS vs. same-population baseline\n")
    lines.append("Each sigma variant's BSS is compared against the legacy-reconstructed "
                 "baseline's BSS, RESTRICTED to the exact same set of resolved "
                 "(station, ticker, end_date) rows the sigma variant covers -- never against "
                 "the baseline's own (larger) headline population.\n")
    lines.append("| Sigma variant | n (shared) | BSS (sigma variant) | BSS (baseline, same rows) | ΔBSS |")
    lines.append("|---|---|---|---|---|")

    baseline_samples_full = variant_results[VARIANT_BASELINE]["samples"]
    delta_by_variant: "dict[str, float | None]" = {}
    variant_bss_by_variant: "dict[str, float | None]" = {}
    for variant in SIGMA_VARIANTS:
        vr_samples = variant_results[variant]["samples"]
        if not vr_samples or not baseline_samples_full:
            lines.append(f"| {variant} | 0 | n/a | n/a | n/a |")
            delta_by_variant[variant] = None
            variant_bss_by_variant[variant] = None
            continue
        keys = {_row_key(s) for s in vr_samples}
        restricted_baseline = _restricted_to_keys(baseline_samples_full, keys)
        # Only compare over rows present on BOTH sides -- a variant row whose
        # baseline counterpart independently failed exclusion is dropped
        # from this comparison (not from the variant's own headline stats
        # above), so the delta is always computed on a truly shared set.
        shared_keys = {_row_key(s) for s in restricted_baseline} & keys
        vr_shared = _restricted_to_keys(vr_samples, shared_keys)
        baseline_shared = _restricted_to_keys(restricted_baseline, shared_keys)
        variant_bss_full = compute_bss(vr_samples)["bss"]
        variant_bss_by_variant[variant] = variant_bss_full
        if not vr_shared or not baseline_shared:
            lines.append(f"| {variant} | 0 | {_fmt(variant_bss_full)} | n/a | n/a |")
            delta_by_variant[variant] = None
            continue
        vr_bss_shared = compute_bss(vr_shared)["bss"]
        baseline_bss_shared = compute_bss(baseline_shared)["bss"]
        delta = (vr_bss_shared - baseline_bss_shared
                 if vr_bss_shared is not None and baseline_bss_shared is not None else None)
        delta_by_variant[variant] = delta
        lines.append(f"| {variant} | {len(shared_keys)} | {_fmt(vr_bss_shared)} | "
                     f"{_fmt(baseline_bss_shared)} | {_fmt(delta)} |")
    lines.append("")

    # -----------------------------------------------------------------
    # Stopping rule -- applied mechanically, best of the two sigma variants.
    # -----------------------------------------------------------------
    lines.append("\n---\n")
    lines.append("## Stopping rule (PR #1047, fixed before this run)\n")
    valid_deltas = {v: d for v, d in delta_by_variant.items() if d is not None}
    if valid_deltas:
        best_variant = max(valid_deltas, key=lambda v: valid_deltas[v])
        best_delta = valid_deltas[best_variant]
        best_bss = variant_bss_by_variant.get(best_variant)
    else:
        best_variant, best_delta, best_bss = None, None, None

    row_idx, explanation = apply_stopping_rule(best_delta, best_bss)
    lines.append(f"**Best sigma variant:** {best_variant or 'n/a'}  ")
    lines.append(f"**ΔBSS (best variant vs. same-population baseline):** {_fmt(best_delta)}  ")
    lines.append(f"**Reconstructed BSS (best variant, its own full population):** {_fmt(best_bss)}  ")
    lines.append("")
    lines.append("| # | Table row | Applies? |")
    lines.append("|---|---|---|")
    for i, (label, _text) in enumerate(STOPPING_RULE_ROWS):
        marker = "**YES**" if i == row_idx else ""
        lines.append(f"| {i + 1} | {label} | {marker} |")
    lines.append("")
    lines.append(f"**Verdict:** {explanation}\n")

    lines.append("\n---\n")
    lines.append("## Methodology notes\n")
    lines.append("- `p_model` = reconstructed legacy/DEB-path P(YES) with one of three "
                 "`forecast_stddev` substitutions -- see `reconstruct_bracket_row_variants` "
                 "and `reconstruct_mu_legacy`/`reconstruct_sigma_variants` for the exact "
                 "construction (reuses `compute_deb_mu_f`/`get_weights`/"
                 "`reconstruct_intraday_delta`/`true_probability_yes`/`_calibrated_sigma` "
                 "directly, never re-derives their math).")
    lines.append("- `p_market` = `(yes_ask + (100 - no_ask)) / 200` -- identical to M3's "
                 "`market_p_yes()` (imported, not reimplemented).")
    lines.append("- Exclusion funnel, de-duplication, and outcome resolution are IMPORTED "
                 "from `bss_market_vs_model_report` unchanged -- identical to the M3 gate "
                 "except for the probability column.")
    lines.append("- Murphy (1973) decomposition: `src.model.murphy_decomposition`, binned on "
                 "the gate's own `BUCKET_EDGES` -- see that module's docstring for the exact "
                 "binned-vs-actual-BS distinction.")
    lines.append("")
    return "\n".join(lines)


def run_report(
    bracket_evals: "Path | None",
    db_path: Path,
    out_dir: Path,
    run_date: "str | None" = None,
    since: "str | None" = DEFAULT_SINCE,
    use_gamma: bool = True,
    allow_network: bool = True,
) -> int:
    """Load, reconstruct (3 variants), score, and (if there is real data)
    write the report. Self-gates (logs a reason, returns 0) exactly like
    ``bss_market_vs_model_report.run_report``/``emos_shadow_vs_market_report.
    run_report`` -- no local `logs/`, no readable DB, or no row survives
    reconstruction/outcome resolution for ANY variant.
    """
    run_date = run_date or datetime.now(timezone.utc).date().isoformat()
    source_path = bracket_evals or BRACKET_EVALS_JSONL
    raw_rows = load_bracket_eval_rows_for_reconstruction(source_path)
    if not raw_rows:
        log.info("[sigma-lever] no rows found under %s -- nothing to score.", source_path)
        return 0

    raw_rows, n_before_since = filter_rows_since(raw_rows, since)
    if since and not raw_rows:
        log.info("[sigma-lever] no rows polled on or after %s -- nothing to score.", since)
        return 0

    deduped = dedupe_one_per_bracket_day(raw_rows)

    if not db_path.exists():
        log.info("[sigma-lever] no readable database at %s -- cannot reconstruct "
                 "sigma-lever probabilities. Not writing a report.", db_path)
        return 0

    reconstruction_counts = {
        "n_considered": len(deduped), "n_next_day": 0, "n_non_high_direction": 0,
        "n_unreconstructable": 0, "n_baseline_reconstructed": 0, "n_gefs_missing": 0,
    }
    variants_by_key: "dict[tuple, dict]" = {}
    ro_db = ReadOnlyDatabase(db_path)
    try:
        for row in deduped:
            is_next_day = False
            flag = row.get("is_next_day_flag")
            if flag is not None:
                try:
                    is_next_day = bool(int(flag))
                except (TypeError, ValueError):
                    is_next_day = False
            if is_next_day:
                reconstruction_counts["n_next_day"] += 1
                continue
            direction = row.get("direction") or "high"
            if direction != "high":
                reconstruction_counts["n_non_high_direction"] += 1
                continue

            result = reconstruct_bracket_row_variants(ro_db, row)
            if result is None:
                reconstruction_counts["n_unreconstructable"] += 1
                continue
            reconstruction_counts["n_baseline_reconstructed"] += 1
            if VARIANT_NAIVE_FLOOR not in result:
                reconstruction_counts["n_gefs_missing"] += 1
            variants_by_key[_row_key(row)] = result
    finally:
        ro_db._conn.close()

    if not variants_by_key:
        log.info("[sigma-lever] no row could be reconstructed -- not writing a report.")
        return 0

    variant_results = {}
    for variant in VARIANTS:
        variant_rows = []
        for row in deduped:
            result = variants_by_key.get(_row_key(row))
            if result is None or variant not in result:
                continue
            variant_rows.append({**row, "p_yes_raw": result[variant]})
        samples, exclusion_counts, outcome_counts = _score_variant_population(
            variant_rows, db_path, use_gamma, allow_network,
        )
        exclusion_counts["input_rows_all_dates"] = len(raw_rows) + n_before_since
        exclusion_counts["dropped_before_since"] = n_before_since
        exclusion_counts["since"] = since
        variant_results[variant] = {
            "samples": samples, "exclusion_counts": exclusion_counts,
            "outcome_counts": outcome_counts,
        }

    if not any(variant_results[v]["samples"] for v in VARIANTS):
        log.info("[sigma-lever] no row survived scoring for any variant -- not writing a report.")
        return 0

    report = build_sigma_lever_report(variant_results, reconstruction_counts, run_date, since)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"sigma_lever_reconstruction_{run_date}.md"
    out_path.write_text(report, encoding="utf-8")
    log.info("[sigma-lever] wrote %s", out_path)
    return 0


def main(argv: "list[str] | None" = None) -> int:
    """CLI entry point. ``python -m src.scripts.sigma_lever_reconstruction_report
    [--since YYYY-MM-DD] [--bracket-evals PATH] [--db PATH] [--out DIR]
    [--run-date YYYY-MM-DD] [--no-gamma] [--no-network]``."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bracket-evals", type=Path, default=None,
                    help="Override the bracket_evals JSONL path")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--run-date", default=None,
                    help="Report date stamp (default: today, UTC)")
    ap.add_argument("--since", default=DEFAULT_SINCE, metavar="YYYY-MM-DD",
                    help="Score only rows POLLED on or after this UTC date. "
                         "Defaults to the M3 gate's own window (2026-08-06).")
    ap.add_argument("--no-gamma", action="store_true",
                    help="Resolve purely from observed daily highs. Diagnostic use only.")
    ap.add_argument("--no-network", action="store_true",
                    help="Serve Gamma resolutions from the local cache only.")
    args = ap.parse_args(argv)
    return run_report(
        args.bracket_evals, args.db, args.out, args.run_date, args.since,
        use_gamma=not args.no_gamma, allow_network=not args.no_network,
    )


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    raise SystemExit(main())

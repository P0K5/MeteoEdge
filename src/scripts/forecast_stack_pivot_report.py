"""Forecast-stack x per-day-sigma joint variants vs. market -- #1057 pre-registration.

Answers the last open question before the pivot-vs-shutdown call is made
final (``docs/REMEDIATION_PLAN.md``, "Pre-registration -- the forecast-stack
x sigma pivot measurement, signed off 2026-08-26", added by PR #1056 -- read
that section in full before touching this file; it is the fixed spec, this
module is only its implementation). Three levers have been tested one at a
time: M0 (M3 itself), EMOS mu-correction (#1041/#1044), and ensemble sigma
(#1048/#1049, stood down). None has tested a materially different forecast
source **combined with** real per-day sigma -- the interaction this module
measures.

**Read-only, always** -- same non-negotiable as #1041/#1044/#1048. Every DB
access goes through ``ReadOnlyDatabase``
(``src.scripts.emos_shadow_reconstruction``), reused verbatim. The live bot
is actively writing to ``data/meteoedge.db`` and this module must never
block it or risk a write.

**Reuses production code and #1048's own machinery, never re-derives it:**

- Population/exclusion/outcome-resolution/BSS math: ``apply_exclusions``,
  ``dedupe_one_per_bracket_day``, ``filter_rows_since``,
  ``resolve_candidate_outcomes``, ``compute_bss``, ``market_p_yes``,
  ``sharpness_histogram``, ``build_reliability``, ``format_reliability``,
  ``format_sharpness`` -- all imported from ``bss_market_vs_model_report``
  (the M3 gate script) unchanged.
- ``load_bracket_eval_rows_for_reconstruction`` -- imported from
  ``emos_shadow_vs_market_report`` unchanged.
- ``reconstruct_current_high``/``reconstruct_latest_temp``/``_station_local_now``
  -- imported from ``emos_shadow_reconstruction`` unchanged.
- The legacy-reconstructed baseline mu (``reconstruct_mu_legacy``) and the
  per-day ensemble-sigma reconstruction (``reconstruct_sigma_variants``,
  specifically its ``naive_floor`` value) -- imported from
  ``sigma_lever_reconstruction_report`` (#1048/#1049) unchanged, per this
  issue's explicit instruction to reuse rather than re-derive them.
- Murphy (1973) Brier decomposition -- ``src.model.murphy_decomposition``.
- The dawn-cohort sensitivity proxy (``is_dawn_cohort_row``) -- imported from
  ``sigma_lever_reconstruction_report`` unchanged.

**Four variants, mu construction only for the first three (sigma held at
``FORECAST_STDDEV_F`` = 2.0, isolating the mu question from the already-
answered sigma question):**

1. ``hrrr_nbm``-mean -- equal-weight mean of ``FORECAST_STACK_MODELS["hrrr_nbm"]``
   (``{nws, open_meteo, hrrr, nbm}``) at the nearest lead bin. Structurally
   identical to ``emos_shadow_reconstruction.reconstruct_mu_raw``, but that
   function reads the ACTIVE stack via ``_active_stack_models(db)`` (live
   config, still ``baseline``) -- every variant here is hypothetical, so
   ``reconstruct_stack_mu_raw`` below takes an EXPLICIT stack instead.
2. ``intl_ecmwf_icon``-mean -- same construction,
   ``FORECAST_STACK_MODELS["intl_ecmwf_icon"]``.
3. ``nbm``-alone -- the raw NBM ``forecast_high_f`` at the nearest lead bin,
   sourced directly from ``model_forecast_log`` (``model="nbm"``, per
   ``MODEL_STATE_ATTRS``) -- no blending, no DEB, no EMOS equal-weight mean.
4. ``joint`` -- whichever of variants 1-3 has the best (least negative /
   most positive) BSS on its OWN resolved population, re-scored with
   ``forecast_stddev`` from ``reconstruct_sigma_variants``'s ``naive_floor``
   value (identical to ``calibrated`` in the current regime -- #1048's
   RESOLVED note) instead of the constant 2.0.

The legacy-reconstructed baseline (``reconstruct_mu_legacy`` + constant
``FORECAST_STDDEV_F``, #1048's own baseline construction) is also
reconstructed and scored -- it is the comparator the stopping rule's ΔBSS is
measured against, restricted to the JOINT variant's own resolved population
(the same convention #1048 uses for its sigma-variant deltas).

**Per-variant population, never partially reconstructed within a variant's
own required inputs.** A row missing anything ALL FOUR mu-scored variants
need (current_high/latest_temp, station-local day, bracket bounds) is
dropped from every variant. Within that shared scope, each of
``legacy_baseline``/``hrrr_nbm``/``intl_ecmwf_icon``/``nbm`` is reconstructed
independently -- a row with, say, no NBM forecast logged still gets the
other three. ``joint`` additionally requires the winning mu-variant AND a
``gefs`` sigma log for that station/date; missing either drops the row from
``joint`` only.

Usage::

    python -m src.scripts.forecast_stack_pivot_report --since 2026-08-06
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date as _date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import (  # noqa: E402
    BRACKET_EVALS_JSONL, FORECAST_STACK_MODELS, FORECAST_STDDEV_F, MODEL_STATE_ATTRS,
    get_live_config,
)
from src.model.emos_mode import _nearest_lead_hours  # noqa: E402
from src.model.envelope import Bracket, WeatherState, true_probability_yes  # noqa: E402
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
    reconstruct_latest_temp,
)
from src.scripts.emos_shadow_vs_market_report import (  # noqa: E402
    load_bracket_eval_rows_for_reconstruction,
)
from src.scripts.sigma_lever_reconstruction_report import (  # noqa: E402
    is_dawn_cohort_row,
    reconstruct_mu_legacy,
    reconstruct_sigma_variants,
)
from src.strategy.scanner import STATION_TO_CITY  # noqa: E402

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

DEFAULT_OUT_DIR = Path("backtest_results")
# Same window the M3 gate, #1041's EMOS-shadow reconstruction, and #1048's
# sigma-lever reconstruction use, for direct comparability of BSS numbers.
DEFAULT_SINCE = "2026-08-06"

VARIANT_LEGACY_BASELINE = "legacy_baseline"
VARIANT_HRRR_NBM = "hrrr_nbm"
VARIANT_ECMWF_ICON = "intl_ecmwf_icon"
VARIANT_NBM_ALONE = "nbm"
VARIANT_JOINT = "joint"

# The three mu-only variants the joint variant picks its winner from, in a
# fixed order so tie-breaking (equal BSS) is deterministic.
STACK_VARIANTS = (VARIANT_HRRR_NBM, VARIANT_ECMWF_ICON, VARIANT_NBM_ALONE)
ALL_VARIANTS = (VARIANT_LEGACY_BASELINE,) + STACK_VARIANTS + (VARIANT_JOINT,)

VARIANT_LABELS = {
    VARIANT_LEGACY_BASELINE: (
        "Legacy-reconstructed baseline (DEB mu, forecast_stddev="
        "FORECAST_STDDEV_F=2.0) -- #1048's own baseline construction"
    ),
    VARIANT_HRRR_NBM: (
        "hrrr_nbm-mean (equal-weight {nws, open_meteo, hrrr, nbm}, forecast_stddev=2.0)"
    ),
    VARIANT_ECMWF_ICON: (
        "intl_ecmwf_icon-mean (equal-weight {nws, open_meteo, ecmwf, icon}, "
        "forecast_stddev=2.0)"
    ),
    VARIANT_NBM_ALONE: "nbm-alone (raw NBM forecast_high_f, no blending, forecast_stddev=2.0)",
    VARIANT_JOINT: (
        "joint (best of the 3 mu variants above, re-scored with the naive_floor "
        "per-day sigma from #1048)"
    ),
}

PIVOT_BAR = 0.15

REQUIRED_CAVEATS = """\
> **PRE-REGISTERED READ, NOT PART OF M3 (issue #1057 / PR #1056).** Scores
> FOUR reconstructed offline probabilities -- a legacy-reconstructed
> baseline, two hypothetical-stack mu variants, a single-model mu variant,
> and a joint (best-mu x per-day-sigma) variant -- against the market, on
> the identical population and methodology the M3 gate used.
>
> **1. Mu reconstruction gap.** The legacy baseline shares #1048's
> documented gap: the #307 per-city rolling residual correction, applied on
> top of DEB mu in live serving, is NOT reconstructed here. The three
> hypothetical-stack variants have no analogous "live serving" to gap
> against -- they were never served, by construction.
>
> **2. Dawn-cohort proxy.** The "dawn-closing" sensitivity split below uses
> the same approximate station+lead-time proxy #1048 defines (see
> ``sigma_lever_reconstruction_report.DAWN_COHORT_STATIONS`` docstring),
> not an exact reproduction of #1021's 12 identified rows.
>
> **3. Reconstruction has documented, non-guessable gaps** (next-day rows,
> low-direction rows, rows with no matching ``model_forecast_log`` entry for
> a given stack, rows ``current_high``/``latest_temp`` can't be recovered
> from ``observations``) -- reconstructed n and station-day counts will be
> at or below the M3 gate's own 316/300, and can differ PER VARIANT since
> each hypothetical stack has its own missing-model gaps.
>
> **4. The joint variant's winner is chosen from THIS run's own numbers.**
> Selecting "best of variants 1-3 by BSS" and then re-scoring only that one
> with per-day sigma is, by construction, an optimistic estimate relative to
> pre-committing to one stack in advance -- named here because the
> pre-registration itself does not correct for it.
"""


def _row_key(row: dict) -> tuple:
    return (row.get("station"), row.get("ticker"), row.get("end_date"))


def _fmt(x, spec=".4f"):
    return format(x, spec) if x is not None else "n/a"


# ---------------------------------------------------------------------------
# Reconstruction: explicit-stack mu (variants 1-2) and raw single-model mu
# (variant 3)
# ---------------------------------------------------------------------------

def reconstruct_stack_mu_raw(
    db, station: str, date_str: str, minutes_to_settlement: float, stack_models: frozenset,
) -> "float | None":
    """Equal-weight mean of an EXPLICIT hypothetical stack's members for
    (station, date) -- structurally identical to
    ``emos_shadow_reconstruction.reconstruct_mu_raw``, but that function
    reads the ACTIVE stack via ``_active_stack_models(db)`` (live config,
    still ``baseline``), which would silently reconstruct the wrong stack
    for every hypothetical scored here. *stack_models* is one of
    ``FORECAST_STACK_MODELS``'s values, passed explicitly instead.

    Sources per-model values from ``model_forecast_log`` at the lead bin
    nearest to ``minutes_to_settlement`` (issue #665's
    ``_nearest_lead_hours``, reused directly). Returns ``None`` when no
    member of *stack_models* has a row for this (station, date).
    """
    models = sorted(m for m in stack_models if m in MODEL_STATE_ATTRS)
    if not models:
        return None

    rows = db.get_forecast_log_for_date(station, date_str)
    by_model: "dict[str, list[dict]]" = {}
    for row in rows:
        if row.get("lead_hours") is None:
            continue  # legacy nowcast rows carry no lead bin -- not comparable
        by_model.setdefault(row["model"], []).append(row)

    lead_target = minutes_to_settlement / 60.0
    values: "list[float]" = []
    for model in models:
        candidates = by_model.get(model)
        if not candidates:
            continue
        leads = [c["lead_hours"] for c in candidates]
        nearest = _nearest_lead_hours(lead_target, leads)
        match = next(c for c in candidates if c["lead_hours"] == nearest)
        values.append(match["forecast_high_f"])

    if not values:
        return None
    return sum(values) / len(values)


def reconstruct_nbm_alone_raw(
    db, station: str, date_str: str, minutes_to_settlement: float,
) -> "float | None":
    """Raw NBM ``forecast_high_f`` at the nearest lead bin -- no blending,
    no DEB, no EMOS equal-weight mean. Sourced directly from
    ``model_forecast_log`` rows tagged ``model="nbm"`` (``MODEL_STATE_ATTRS``
    maps the tag to its scan-time attribute; this reads the persisted log,
    not a live ``WeatherState``). Returns ``None`` when no NBM row is logged
    for this (station, date).
    """
    rows = db.get_forecast_log_for_date(station, date_str)
    candidates = [
        r for r in rows if r.get("model") == "nbm" and r.get("lead_hours") is not None
    ]
    if not candidates:
        return None
    lead_target = minutes_to_settlement / 60.0
    leads = [c["lead_hours"] for c in candidates]
    nearest = _nearest_lead_hours(lead_target, leads)
    match = next(c for c in candidates if c["lead_hours"] == nearest)
    return match["forecast_high_f"]


# ---------------------------------------------------------------------------
# Per-row reconstruction: shared context, then each mu variant independently
# ---------------------------------------------------------------------------

def _reconstruct_row_context(db, row: dict) -> "dict | None":
    """Shared, variant-independent reconstruction: scope checks
    (same-day/high-direction only, mirrors ``emos_shadow_reconstruction.
    reconstruct_bracket_row``/``sigma_lever_reconstruction_report.
    reconstruct_bracket_row_variants``), ``current_high``/``latest_temp``
    (issue #1044), station-local day, and the ``Bracket``/live-config
    inputs ``true_probability_yes`` needs. Returns ``None`` when the row is
    out of scope or any SHARED input is unreconstructable -- a row that
    fails here is dropped from every variant, per this issue's
    per-variant-population rule.
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
    live_config = get_live_config(db)
    return {
        "station": station,
        "city": STATION_TO_CITY.get(station, station),
        "end_date": end_date,
        "poll_ts": poll_ts,
        "mins_left": mins_left,
        "bracket": bracket,
        "current_high": float(current_high),
        "latest_temp": float(latest_temp),
        "now_local": now_local,
        "settlement_date": settlement_date,
        "deb_enabled": live_config.get("DEB_ENABLED", False),
        "sigma_climb_fraction": live_config.get("ENVELOPE_SIGMA_CLIMB_FRACTION", 0.5),
    }


def _p_yes_for_context(ctx: dict, mu_final: float, forecast_stddev: float) -> float:
    state = WeatherState(
        station=ctx["station"], now_local=ctx["now_local"], sunset_local=ctx["now_local"],
        current_high_f=ctx["current_high"], current_high_time=ctx["now_local"],
        latest_temp_f=ctx["latest_temp"], latest_temp_time=ctx["now_local"],
        forecast_high_f=None,  # unused -- corrected_mu_f (below) takes priority
        corrected_mu_f=mu_final,
    )
    return true_probability_yes(
        ctx["bracket"], state, minutes_to_settlement=ctx["mins_left"],
        forecast_stddev=forecast_stddev, deb_enabled=ctx["deb_enabled"],
        sigma_climb_fraction=ctx["sigma_climb_fraction"],
        use_ensemble_sigma=False, settlement_date=ctx["settlement_date"],
    )


def reconstruct_bracket_row_mus(db, row: dict) -> "dict | None":
    """Return ``{'mus': {variant: mu}, 'sigma_variants': {...} | None,
    'ctx': ctx}`` for one deduped ``bracket_evals`` row, or ``None`` when the
    row is out of scope or every mu variant (including the legacy baseline)
    is unreconstructable.

    ``mus`` holds whichever of ``legacy_baseline``/``hrrr_nbm``/
    ``intl_ecmwf_icon``/``nbm`` could be reconstructed -- each independently,
    per this issue's per-variant-population rule (a row missing NBM in
    ``model_forecast_log`` still gets the other three, if reconstructable).
    ``sigma_variants`` is ``reconstruct_sigma_variants``'s result (or
    ``None`` if no ``gefs`` log exists for this station/date) -- consumed
    later, once the joint variant's winning mu source is known.
    """
    ctx = _reconstruct_row_context(db, row)
    if ctx is None:
        return None

    mus: "dict[str, float]" = {}
    mu_legacy = reconstruct_mu_legacy(
        db, ctx["station"], ctx["city"], ctx["end_date"], ctx["poll_ts"], ctx["mins_left"],
    )
    if mu_legacy is not None:
        mus[VARIANT_LEGACY_BASELINE] = mu_legacy

    mu_hrrr_nbm = reconstruct_stack_mu_raw(
        db, ctx["station"], ctx["end_date"], ctx["mins_left"],
        FORECAST_STACK_MODELS["hrrr_nbm"],
    )
    if mu_hrrr_nbm is not None:
        mus[VARIANT_HRRR_NBM] = mu_hrrr_nbm

    mu_ecmwf_icon = reconstruct_stack_mu_raw(
        db, ctx["station"], ctx["end_date"], ctx["mins_left"],
        FORECAST_STACK_MODELS["intl_ecmwf_icon"],
    )
    if mu_ecmwf_icon is not None:
        mus[VARIANT_ECMWF_ICON] = mu_ecmwf_icon

    mu_nbm = reconstruct_nbm_alone_raw(db, ctx["station"], ctx["end_date"], ctx["mins_left"])
    if mu_nbm is not None:
        mus[VARIANT_NBM_ALONE] = mu_nbm

    if not mus:
        return None

    sigma_variants = reconstruct_sigma_variants(db, ctx["station"], ctx["end_date"], ctx["mins_left"])
    return {"mus": mus, "sigma_variants": sigma_variants, "ctx": ctx}


# ---------------------------------------------------------------------------
# Joint-variant selection -- DB-free, standalone (testable without a fixture)
# ---------------------------------------------------------------------------

def select_best_mu_variant(variant_bss: "dict[str, float | None]") -> "str | None":
    """Pick the mu-only variant (of ``STACK_VARIANTS``) with the best (least
    negative / most positive) BSS on its own resolved population -- the
    joint variant's mu source, per the pre-registration.

    Ties broken by ``STACK_VARIANTS``'s fixed order (deterministic, not
    dict-iteration-order-dependent). Returns ``None`` when every variant's
    BSS is ``None`` (nothing scoreable).
    """
    best_variant: "str | None" = None
    best_bss: "float | None" = None
    for variant in STACK_VARIANTS:
        bss = variant_bss.get(variant)
        if bss is None:
            continue
        if best_bss is None or bss > best_bss:
            best_variant = variant
            best_bss = bss
    return best_variant


# ---------------------------------------------------------------------------
# Scoring: exclusions -> outcome resolution -> BSS/Murphy, per variant
# ---------------------------------------------------------------------------

def _score_variant_population(
    variant_rows: "list[dict]", db_path: Path, use_gamma: bool, allow_network: bool,
) -> "tuple[list[dict], dict, dict]":
    """``apply_exclusions`` -> ``resolve_candidate_outcomes`` for one
    variant's row population. Mirrors #1048's own helper of the same name,
    which mirrors ``bss_market_vs_model_report.run_report``'s Pass-2
    pipeline exactly, parameterised over which ``p_yes_raw`` column was
    populated for this variant."""
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
# Stopping rule (PR #1056, fixed before any run -- applied mechanically)
# ---------------------------------------------------------------------------

def apply_stopping_rule(joint_delta_bss: "float | None") -> str:
    """Return the verdict string for the pre-registered +0.15 bar, applied
    mechanically -- no new judgment calls.

    ``joint_delta_bss`` is the JOINT variant's BSS minus the
    legacy-reconstructed baseline's BSS, both restricted to the joint
    variant's own resolved population (same convention #1048 used for its
    sigma-variant deltas).
    """
    if joint_delta_bss is None:
        return ("n/a -- insufficient data (joint variant not scoreable) to evaluate the "
                "stopping rule. Per the pre-registration, a missed measurement by the "
                "2026-08-29 17:00 UTC deadline defaults to SHUTDOWN.")
    if joint_delta_bss >= PIVOT_BAR:
        return (f"ΔBSS = {joint_delta_bss:.4f} >= +{PIVOT_BAR:.2f} -- **CLEARS THE BAR**. "
                "Proceed to scoping a pivot epic (candidate forecast-stack switch, "
                "M4-style entry-rule rebuild, a fresh powered M3-style re-gate on the "
                "pivoted model) -- not directly back to live trading; the halt from "
                "#1053/#1054 stays in effect until a fresh gate passes.")
    return (f"ΔBSS = {_fmt(joint_delta_bss)} < +{PIVOT_BAR:.2f} -- **BELOW THE BAR**. Per the "
            "pre-registration, the default outcome is SHUTDOWN, not another open question.")


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def build_forecast_stack_pivot_report(
    variant_results: dict,
    reconstruction_counts: dict,
    best_mu_variant: "str | None",
    run_date: str,
    since: "str | None",
) -> str:
    lines = ["# Forecast-Stack x Per-Day-Sigma Joint Variants vs. Market "
             "(issue #1057 / PR #1056 pre-registration)\n"]
    lines.append(f"**Run date:** {run_date}  ")
    lines.append("**Data source:** `logs/bracket_evals.*.jsonl` (issue #826, M3's own population)  ")
    lines.append("**Spec:** `docs/REMEDIATION_PLAN.md`, \"Pre-registration -- the forecast-stack "
                 "x sigma pivot measurement, signed off 2026-08-26\" (PR #1056) -- fixed before "
                 "this run.  ")
    if since:
        lines.append(f"**Poll-date window:** rows polled on or after **{since}** "
                     "(M3's own window)  \n")
    else:
        lines.append("**Poll-date window:** none given -- NOT comparable to M3.  \n")
    lines.append(REQUIRED_CAVEATS)
    lines.append("\n---\n")

    lines.append("## Reconstruction funnel\n")
    lines.append("Shared inputs (current_high/latest_temp, station-local day, bracket bounds) "
                 "gate all variants together; each mu source (legacy DEB, per-stack means, raw "
                 "NBM) is then reconstructed independently -- a row missing one model's log "
                 "still yields the others (see `reconstruct_bracket_row_mus` docstring).\n")
    lines.append("| Stage | Count |")
    lines.append("|---|---|")
    lines.append(f"| De-duplicated rows considered | {reconstruction_counts['n_considered']} |")
    lines.append(f"| Out of scope: next-day rows | {reconstruction_counts['n_next_day']} |")
    lines.append(f"| Out of scope: non-\"high\"-direction rows | "
                 f"{reconstruction_counts['n_non_high_direction']} |")
    lines.append(f"| Unreconstructable (missing shared current_high/latest_temp/timezone inputs) | "
                 f"{reconstruction_counts['n_unreconstructable']} |")
    lines.append(f"| Reconstructed: at least one mu variant available | "
                 f"{reconstruction_counts['n_any_mu_reconstructed']} |")
    lines.append("")

    per_variant_summary = {}
    for variant in (VARIANT_LEGACY_BASELINE,) + STACK_VARIANTS:
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

    lines.append(f"## Winning mu variant (of the 3 stack candidates): "
                 f"**{best_mu_variant or 'n/a -- none scoreable'}**\n")
    lines.append("Selected by best (least negative / most positive) BSS on its own resolved "
                 "population, per `select_best_mu_variant` -- the mu source the JOINT variant "
                 "below re-scores with per-day sigma.\n")

    joint_vr = variant_results[VARIANT_JOINT]
    joint_samples = joint_vr["samples"]
    joint_exclusion_counts = joint_vr["exclusion_counts"]
    joint_outcome_counts = joint_vr["outcome_counts"]
    joint_stats = _stats_for_samples(joint_samples)
    lines.append(f"## Variant: {VARIANT_LABELS[VARIANT_JOINT]}\n")
    lines.append("| Stage | Count |")
    lines.append("|---|---|")
    lines.append(f"| Input rows (post reconstruction) | "
                 f"{joint_exclusion_counts.get('input_rows', 0)} |")
    lines.append(f"| Kept after row exclusions | "
                 f"{joint_exclusion_counts.get('kept_after_row_exclusions', 0)} |")
    lines.append(f"| No outcome resolvable | {joint_outcome_counts.get('n_unresolvable', 0)} |")
    if joint_stats is None:
        lines.append("| **Final sample (n)** | **0 -- not scoreable** |")
        lines.append("")
    else:
        lines.append(f"| **Final sample (n)** | **{joint_stats['n']}** |")
        lines.append(f"| **Effective sample size (station-days)** | "
                     f"**{joint_stats['n_station_days']}** |")
        lines.append("")
        jg = joint_stats["global"]
        lines.append("**Global result (dawn-closing cohort INCLUDED -- the headline number):**\n")
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        lines.append(f"| n | {jg['n']} |")
        lines.append(f"| BS_model | {_fmt(jg['bs_model'])} |")
        lines.append(f"| BS_market | {_fmt(jg['bs_market'])} |")
        lines.append(f"| BSS | {_fmt(jg['bss'])} |")
        lines.append("")
        lines.append(format_murphy_decomposition(joint_stats["murphy"], "joint: Murphy decomposition"))
        lines.append("")
        lines.append(format_reliability(build_reliability(joint_stats["model_samples"]), "joint: model"))
        lines.append(format_reliability(build_reliability(joint_stats["market_samples"]),
                                        "joint: market (symmetrized)"))
        lines.append(format_sharpness(sharpness_histogram([p for p, _ in joint_stats["model_samples"]]),
                                      "joint: model sharpness"))
        lines.append("")

    # -----------------------------------------------------------------
    # Same-population ΔBSS: joint vs legacy baseline, restricted to the
    # joint variant's own final sample keys.
    # -----------------------------------------------------------------
    lines.append("\n---\n")
    lines.append("## ΔBSS: joint variant vs. legacy-reconstructed baseline\n")
    lines.append("The joint variant's BSS is compared against the legacy-reconstructed "
                 "baseline's BSS, RESTRICTED to the exact same set of resolved "
                 "(station, ticker, end_date) rows the joint variant covers -- never against "
                 "the baseline's own (larger) headline population. This is the number the "
                 "+0.15 stopping-rule bar is applied to.\n")
    lines.append("| Variant | n (shared) | BSS (variant) | BSS (baseline, same rows) | ΔBSS |")
    lines.append("|---|---|---|---|---|")

    baseline_samples_full = variant_results[VARIANT_LEGACY_BASELINE]["samples"]
    joint_delta_bss: "float | None" = None
    if not joint_samples or not baseline_samples_full:
        lines.append(f"| joint | 0 | n/a | n/a | n/a |")
    else:
        keys = {_row_key(s) for s in joint_samples}
        restricted_baseline = _restricted_to_keys(baseline_samples_full, keys)
        shared_keys = {_row_key(s) for s in restricted_baseline} & keys
        joint_shared = _restricted_to_keys(joint_samples, shared_keys)
        baseline_shared = _restricted_to_keys(restricted_baseline, shared_keys)
        if not joint_shared or not baseline_shared:
            joint_bss_full = compute_bss(joint_samples)["bss"]
            lines.append(f"| joint | 0 | {_fmt(joint_bss_full)} | n/a | n/a |")
        else:
            joint_bss_shared = compute_bss(joint_shared)["bss"]
            baseline_bss_shared = compute_bss(baseline_shared)["bss"]
            if joint_bss_shared is not None and baseline_bss_shared is not None:
                joint_delta_bss = joint_bss_shared - baseline_bss_shared
            lines.append(f"| joint | {len(shared_keys)} | {_fmt(joint_bss_shared)} | "
                         f"{_fmt(baseline_bss_shared)} | {_fmt(joint_delta_bss)} |")
    lines.append("")

    # -----------------------------------------------------------------
    # Stopping rule -- applied mechanically.
    # -----------------------------------------------------------------
    lines.append("\n---\n")
    lines.append("## Stopping rule (PR #1056, fixed before this run)\n")
    lines.append(f"**Bar:** ΔBSS >= +{PIVOT_BAR:.2f} on the joint variant  ")
    lines.append(f"**ΔBSS (joint vs. same-population legacy baseline):** {_fmt(joint_delta_bss)}  ")
    lines.append(f"**Deadline:** 2026-08-29 17:00 UTC -- a missed or failed measurement defaults "
                 f"to shutdown, per the pre-registration.  ")
    lines.append("")
    lines.append(f"**Verdict:** {apply_stopping_rule(joint_delta_bss)}\n")

    lines.append("\n---\n")
    lines.append("## Methodology notes\n")
    lines.append("- `p_model` = reconstructed P(YES) under one of four mu/sigma substitutions "
                 "-- see `reconstruct_bracket_row_mus`/`reconstruct_stack_mu_raw`/"
                 "`reconstruct_nbm_alone_raw` for the exact construction (reuses "
                 "`reconstruct_mu_legacy`/`reconstruct_sigma_variants`/`true_probability_yes` "
                 "directly, never re-derives their math).")
    lines.append("- `p_market` = `(yes_ask + (100 - no_ask)) / 200` -- identical to M3's "
                 "`market_p_yes()` (imported, not reimplemented).")
    lines.append("- Exclusion funnel, de-duplication, and outcome resolution are IMPORTED "
                 "from `bss_market_vs_model_report` unchanged -- identical to the M3 gate "
                 "except for the probability column.")
    lines.append("- Murphy (1973) decomposition: `src.model.murphy_decomposition`, binned on "
                 "the gate's own `BUCKET_EDGES`.")
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
    """Load, reconstruct (4 variants), select the joint variant's mu source,
    score, and (if there is real data) write the report. Self-gates (logs a
    reason, returns 0) exactly like ``sigma_lever_reconstruction_report.
    run_report`` -- no local `logs/`, no readable DB, or no row survives
    reconstruction/outcome resolution for ANY variant.
    """
    run_date = run_date or datetime.now(timezone.utc).date().isoformat()
    source_path = bracket_evals or BRACKET_EVALS_JSONL
    raw_rows = load_bracket_eval_rows_for_reconstruction(source_path)
    if not raw_rows:
        log.info("[forecast-stack-pivot] no rows found under %s -- nothing to score.", source_path)
        return 0

    raw_rows, n_before_since = filter_rows_since(raw_rows, since)
    if since and not raw_rows:
        log.info("[forecast-stack-pivot] no rows polled on or after %s -- nothing to score.", since)
        return 0

    deduped = dedupe_one_per_bracket_day(raw_rows)

    if not db_path.exists():
        log.info("[forecast-stack-pivot] no readable database at %s -- cannot reconstruct "
                 "forecast-stack probabilities. Not writing a report.", db_path)
        return 0

    reconstruction_counts = {
        "n_considered": len(deduped), "n_next_day": 0, "n_non_high_direction": 0,
        "n_unreconstructable": 0, "n_any_mu_reconstructed": 0,
    }
    reconstructions_by_key: "dict[tuple, dict]" = {}
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

            result = reconstruct_bracket_row_mus(ro_db, row)
            if result is None:
                reconstruction_counts["n_unreconstructable"] += 1
                continue
            reconstruction_counts["n_any_mu_reconstructed"] += 1
            reconstructions_by_key[_row_key(row)] = result
    finally:
        ro_db._conn.close()

    if not reconstructions_by_key:
        log.info("[forecast-stack-pivot] no row could be reconstructed -- not writing a report.")
        return 0

    # Phase 1: score the legacy baseline and the 3 mu-only variants (sigma
    # fixed at FORECAST_STDDEV_F for all four).
    variant_results = {}
    for variant in (VARIANT_LEGACY_BASELINE,) + STACK_VARIANTS:
        variant_rows = []
        for row in deduped:
            result = reconstructions_by_key.get(_row_key(row))
            if result is None or variant not in result["mus"]:
                continue
            p_yes = _p_yes_for_context(result["ctx"], result["mus"][variant], FORECAST_STDDEV_F)
            variant_rows.append({**row, "p_yes_raw": p_yes})
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

    # Phase 2: pick the joint variant's mu source -- best (least negative /
    # most positive) BSS of the 3 stack variants, on their own populations.
    stack_variant_bss = {
        variant: compute_bss(variant_results[variant]["samples"])["bss"]
        if variant_results[variant]["samples"] else None
        for variant in STACK_VARIANTS
    }
    best_mu_variant = select_best_mu_variant(stack_variant_bss)

    # Phase 3: score the joint variant -- winning mu, per-day naive_floor
    # sigma. Rows missing either the winning mu or a gefs sigma log are
    # dropped from this variant only.
    joint_rows = []
    if best_mu_variant is not None:
        for row in deduped:
            result = reconstructions_by_key.get(_row_key(row))
            if result is None or best_mu_variant not in result["mus"]:
                continue
            sigma_variants = result["sigma_variants"]
            if sigma_variants is None:
                continue
            p_yes = _p_yes_for_context(
                result["ctx"], result["mus"][best_mu_variant], sigma_variants["naive_floor"],
            )
            joint_rows.append({**row, "p_yes_raw": p_yes})
    joint_samples, joint_exclusion_counts, joint_outcome_counts = _score_variant_population(
        joint_rows, db_path, use_gamma, allow_network,
    )
    joint_exclusion_counts["input_rows_all_dates"] = len(raw_rows) + n_before_since
    joint_exclusion_counts["dropped_before_since"] = n_before_since
    joint_exclusion_counts["since"] = since
    variant_results[VARIANT_JOINT] = {
        "samples": joint_samples, "exclusion_counts": joint_exclusion_counts,
        "outcome_counts": joint_outcome_counts,
    }

    if not any(variant_results[v]["samples"] for v in ALL_VARIANTS):
        log.info("[forecast-stack-pivot] no row survived scoring for any variant -- "
                 "not writing a report.")
        return 0

    report = build_forecast_stack_pivot_report(
        variant_results, reconstruction_counts, best_mu_variant, run_date, since,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"forecast_stack_pivot_{run_date}.md"
    out_path.write_text(report, encoding="utf-8")
    log.info("[forecast-stack-pivot] wrote %s", out_path)
    return 0


def main(argv: "list[str] | None" = None) -> int:
    """CLI entry point. ``python -m src.scripts.forecast_stack_pivot_report
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

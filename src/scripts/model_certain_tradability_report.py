"""Model-certain population tradability test (issue #1063, #909 follow-up).

**Does NOT reopen M3.** M3's verdict (BSS = -0.4123) stands, final. That gate
scored the *contested* population -- rows where the model's `p_yes_raw` and
the market's price disagreed enough to be interesting -- and, by design,
excluded the population this script scores: rows where `p_yes_raw == 0.0`
(`is_model_certain_price`, issue #820/#909). #909's own DECIDED-2026-08-10
note flagged that exclusion as final for the SKILL question and explicitly
left the TRADING question -- "is selling these rows at their market price
profitable after the spread?" -- for "a future gate". This is that gate.

Post-#920, an exact `p_yes_raw == 0.0` is not a certainty-shortcut artifact
(the diagnosis that justified excluding it from M3): it is
`conditional_bracket_probability` returning 0.0 because the bracket sits
entirely outside `[current_high, max_env]` -- a structurally correct
envelope/nowcast claim ("the daily high cannot reach this bracket anymore"),
not a Gaussian forecast opinion. So there is nothing to score for skill (the
model asserts nothing probabilistic here), but there is a live question about
whether the *market's own price* for these brackets is exploitable.

**This is an EV question, not a Brier question.** `market_p_yes()` (the
symmetrized mid used throughout `bss_market_vs_model_report`) must NOT be
used anywhere in this module -- buying NO costs `no_ask`, not the mid, and
the model's own `p_yes_raw` for this population is uniformly 0.0 by
construction and carries no pricing information. The empirical resolution
rate of each bucket is the only usable probability here::

    EV_per_contract(cents) = (1 - p_yes) * (100 - no_ask) - p_yes * no_ask

where `p_yes` is the bucket's own observed YES rate (Wilson 95% CI via
`certainty_exclusion_check.wilson_interval`, reused unchanged), never
`p_yes_raw`.

Population, loading, de-duplication and outcome resolution are all reused
UNCHANGED from `bss_market_vs_model_report` / `resolve_bracket_outcomes` --
this module never re-derives any of that, only adds the EV layer on top:

  * `load_bracket_eval_rows` / `filter_rows_since` / `dedupe_one_per_bracket_day`
    -- identical loading, windowing (`--since 2026-08-06`, the M3 window) and
    one-row-per-bracket-day de-duplication.
  * `is_model_certain_price` -- the SAME predicate `apply_exclusions` tests
    at its `p_yes_raw_zero_artifact` stage. Applying it directly to the
    de-duplicated rows reproduces exactly the population that stage would
    drop, without re-running the whole exclusion cascade (which drops
    missing-price / rail rows too -- this population must keep them, see
    below).
  * `resolve_candidate_outcomes` -- the SAME Gamma-first / observed-daily-high
    outcome resolution the M3 gate uses.

**This population still contains 1c/99c rail rows.** `apply_exclusions` runs
`p_yes_raw_zero_artifact` BEFORE `rail_1c_99c`, so a row that is both
model-certain and market-certain lands here, not in the rail bucket. That is
in-scope, not a bug to filter out -- it is exactly why the `no_ask` bucketing
matters: at `no_ask=98`, `p_yes=1%` -> EV=+1.00c; at `no_ask=99` -> EV=+0.99*1
- 0.01*99 = exactly 0.00c. A single pooled EV number across this whole
population would average away that structure, so this script reports EV
per `no_ask` bucket as the primary result and a pooled figure only "for
reference", never as the answer.

Read-only, always. Never writes to `data/meteoedge.db`, `bracket_evals`, or
any live table -- outcome resolution goes through `resolve_candidate_outcomes`
/ `resolve_bracket_outcomes`, which only ever opens the DB `mode=ro`. No live
trading of any kind: #1053/#1054's halt is unconditional and unaffected by
this script's result either way.

Usage::

    python -m src.scripts.model_certain_tradability_report --since 2026-08-06
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.scripts.bss_market_vs_model_report import (  # noqa: E402
    BRACKET_EVALS_JSONL,
    DEFAULT_DB_PATH,
    DEFAULT_OUT_DIR,
    _connect_ro,
    dedupe_one_per_bracket_day,
    filter_rows_since,
    is_model_certain_price,
    load_bracket_eval_rows,
    resolve_candidate_outcomes,
)
from src.scripts.certainty_exclusion_check import wilson_interval  # noqa: E402
from src.scripts.m3_window_diagnostics import classify_width  # noqa: E402

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pre-registered stopping rule (docs/REMEDIATION_PLAN.md, "M3b", signed off
# 2026-08-26, before any number was computed for this population).
# ---------------------------------------------------------------------------

#: Minimum net EV per contract (cents), evaluated at the 95% UPPER bound of
#: the pooled YES rate, required for a PASS.
EV_PASS_THRESHOLD_CENTS = 0.5

#: Minimum resolved-row sample size for a verdict to be read at all.
MIN_RESOLVED_N = 1000

#: Minimum number of separate `no_ask` buckets that must ALSO be positive at
#: their own 95% upper bound for a PASS -- a single lucky bucket is not a
#: broad enough edge to act on.
MIN_POSITIVE_BUCKETS = 3

#: `no_ask` bucket edges (cents). The last bucket is unbounded above by
#: construction (`no_ask` is clamped to <= 99 everywhere in this codebase --
#: see `bss_market_vs_model_report.RAIL_HIGH_CENTS` -- so "99" already covers
#: the top of the real line; nothing above 99 is expected to appear, and if
#: it did it would still land in this bucket rather than being silently
#: dropped).
NO_ASK_BUCKETS = ("<=95", "96", "97", "98", "99")

#: `minutes_to_settlement` bucket edges (minutes). Chosen to test the
#: envelope hypothesis in docs/REMEDIATION_PLAN.md: edge should concentrate
#: late in the day, when `[current_high, max_env]` is most informative.
MINUTES_BUCKETS = (
    ("0-60", 0.0, 60.0),
    ("60-180", 60.0, 180.0),
    ("180-360", 180.0, 360.0),
    ("360-720", 360.0, 720.0),
    ("720+", 720.0, float("inf")),
)


# ---------------------------------------------------------------------------
# Population selection (issue #1063 / #909)
# ---------------------------------------------------------------------------

def select_model_certain_population(rows: "list[dict]") -> "list[dict]":
    """Rows `apply_exclusions` drops at its `p_yes_raw_zero_artifact` stage.

    `is_model_certain_price` is `row.get("p_yes_raw") == 0.0`, which is
    already `False` for a row with a missing `p_yes_raw` (`None == 0.0` is
    `False`), so this reproduces exactly the exclusion-cascade stage's
    population without re-running the cascade (which would ALSO drop
    missing-price and rail rows this population must keep -- see the module
    docstring).
    """
    return [r for r in rows if is_model_certain_price(r)]


# ---------------------------------------------------------------------------
# EV math (issue #1063). No `market_p_yes` anywhere below this line.
# ---------------------------------------------------------------------------

def ev_per_contract(p_yes: float, no_ask: float) -> float:
    """Expected value (cents) of buying one NO contract at `no_ask`.

    `p_yes` must be an EMPIRICAL resolution rate (a bucket's observed YES
    rate, or a stress-test variant of one) -- never `p_yes_raw`, which is
    uniformly 0.0 for this entire population by construction and carries no
    pricing information to evaluate against.
    """
    return (1.0 - p_yes) * (100.0 - no_ask) - p_yes * no_ask


def bucket_no_ask(no_ask: "float | None") -> "str | None":
    """Bucket a `no_ask` price (cents) into one of `NO_ASK_BUCKETS`.

    Boundaries are inclusive on the LOW side of each bucket name: exactly
    96.0 lands in `"96"`, not `"<=95"` or `"97"`.
    """
    if no_ask is None:
        return None
    if no_ask <= 95:
        return "<=95"
    if no_ask <= 96:
        return "96"
    if no_ask <= 97:
        return "97"
    if no_ask <= 98:
        return "98"
    return "99"


def bucket_minutes(minutes: "float | None") -> "str | None":
    """Bucket `minutes_to_settlement` into `MINUTES_BUCKETS`.

    Each bin is `[lo, hi)` except the last, which is `[720, inf)`. A
    negative or missing value is undeterminable and returns `None` rather
    than being guessed into the first bin.
    """
    if minutes is None or minutes < 0:
        return None
    for label, lo, hi in MINUTES_BUCKETS:
        if lo <= minutes < hi:
            return label
    return None


def ladder_kind(row: dict) -> str:
    """Bracket-width fingerprint, reusing `m3_window_diagnostics.classify_width`
    (the same parser-fingerprint convention the M3 diagnostics use) rather
    than inventing a new width classification here.
    """
    lo, hi = row.get("bracket_low"), row.get("bracket_high")
    if lo is None or hi is None:
        return "other"
    return classify_width(hi - lo)


def group_ev_stats(rows: "list[dict]") -> dict:
    """Resolution-rate and EV summary for one group of rows.

    A single helper backs every breakdown this report prints (`no_ask`
    bucket, `minutes_to_settlement` bucket, station, ladder kind, and the
    pooled/aggregate figure): each computes the group's own empirical YES
    rate from its resolved rows, then reports:

    * ``ev_point`` -- mean of `ev_per_contract(observed_rate, row's own
      no_ask)` over the group's resolved rows. Using each row's own
      `no_ask` (rather than a single group-average price) keeps the EV
      honest about price dispersion within a bucket.
    * ``ev_upper`` -- the same, but evaluated at the 95% UPPER bound of the
      group's Wilson CI -- pessimistic for a NO seller (a higher YES rate
      means the sold insurance pays out more often).
    * ``ev_2x`` -- the same, at 2x the observed rate (capped at 1.0).
    * ``total_ev_point`` -- the SUM (not mean) of the same per-row values,
      used for concentration reporting below.

    Rows missing `yes_won` (unresolved) or `no_ask` are excluded from the
    resolved set; ``n`` still counts every row handed in.
    """
    n = len(rows)
    resolved = [r for r in rows if r.get("yes_won") is not None and r.get("no_ask") is not None]
    n_resolved = len(resolved)
    if n_resolved == 0:
        return {
            "n": n, "n_resolved": 0, "n_yes": 0, "observed_yes": None,
            "ci": None, "ev_point": None, "ev_upper": None, "ev_2x": None,
            "total_ev_point": None,
        }
    n_yes = sum(1 for r in resolved if r["yes_won"])
    observed = n_yes / n_resolved
    ci = wilson_interval(n_yes, n_resolved)
    per_row_point = [ev_per_contract(observed, r["no_ask"]) for r in resolved]
    ev_point = mean(per_row_point)
    total_ev_point = sum(per_row_point)
    ev_upper = None
    if ci is not None:
        ev_upper = mean(ev_per_contract(ci[1], r["no_ask"]) for r in resolved)
    p_2x = min(1.0, 2.0 * observed)
    ev_2x = mean(ev_per_contract(p_2x, r["no_ask"]) for r in resolved)
    return {
        "n": n, "n_resolved": n_resolved, "n_yes": n_yes, "observed_yes": observed,
        "ci": ci, "ev_point": ev_point, "ev_upper": ev_upper, "ev_2x": ev_2x,
        "total_ev_point": total_ev_point,
    }


def group_rows(rows: "list[dict]", key_fn) -> "dict[str, list[dict]]":
    """Group rows by ``key_fn(row)``, dropping rows the key function cannot
    classify (returns None)."""
    out: "dict[str, list[dict]]" = defaultdict(list)
    for row in rows:
        key = key_fn(row)
        if key is not None:
            out[key].append(row)
    return dict(out)


# ---------------------------------------------------------------------------
# Concentration (issue #1063): share of total EV from one station / bucket.
# ---------------------------------------------------------------------------

#: Above this share, an edge is concentrated enough in one place that the
#: report must say so plainly rather than just showing the table.
CONCENTRATION_WARN_SHARE = 0.5


def concentration_by(rows: "list[dict]", by_bucket: "dict[str, list[dict]]") -> dict:
    """Share of pooled point-estimate EV contributed by the largest group.

    Every row is valued using its OWN `no_ask`-bucket's empirical rate (the
    same rate `by_bucket`'s per-bucket `group_ev_stats` already computed),
    so a station's and a bucket's share are directly comparable fractions of
    the SAME total -- the total portfolio point-estimate EV across every
    resolved, priced row in the population.
    """
    bucket_rate: "dict[str, float]" = {}
    for label, members in by_bucket.items():
        stats = group_ev_stats(members)
        if stats["observed_yes"] is not None:
            bucket_rate[label] = stats["observed_yes"]

    row_ev: "list[tuple[dict, float]]" = []
    for row in rows:
        if row.get("yes_won") is None or row.get("no_ask") is None:
            continue
        label = bucket_no_ask(row["no_ask"])
        rate = bucket_rate.get(label)
        if rate is None:
            continue
        row_ev.append((row, ev_per_contract(rate, row["no_ask"])))

    total = sum(ev for _, ev in row_ev)

    def _top_share(key_fn) -> "tuple[str, float, float] | None":
        totals: "dict[str, float]" = defaultdict(float)
        for row, ev in row_ev:
            key = key_fn(row)
            if key is not None:
                totals[key] += ev
        if not totals or not total:
            return None
        top_key = max(totals, key=lambda k: totals[k])
        return top_key, totals[top_key], totals[top_key] / total

    return {
        "total_ev": total,
        "n_priced": len(row_ev),
        "top_station": _top_share(lambda r: r.get("station")),
        "top_no_ask_bucket": _top_share(lambda r: bucket_no_ask(r.get("no_ask"))),
    }


# ---------------------------------------------------------------------------
# Worst realized drawdown (issue #1063): play resolved rows in poll order.
# ---------------------------------------------------------------------------

def realized_pnl_cents(row: dict) -> "float | None":
    """Realized PnL (cents) of buying one NO contract at `no_ask` and holding
    to settlement. `None` if the row cannot be priced/resolved."""
    no_ask = row.get("no_ask")
    yes_won = row.get("yes_won")
    if no_ask is None or yes_won is None:
        return None
    return -no_ask if yes_won else (100.0 - no_ask)


def worst_drawdown_cents(rows: "list[dict]") -> "dict":
    """Maximum cumulative drawdown (cents) playing *rows* in chronological
    (`ts`) order, one contract each, at `no_ask` cost basis.

    Rows with no parseable `ts` sort last (stable) rather than raising --
    this is a diagnostic replay, not the settlement pipeline.
    """
    priced = [r for r in rows if realized_pnl_cents(r) is not None]
    ordered = sorted(priced, key=lambda r: r.get("ts") or "9999")
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for row in ordered:
        cumulative += realized_pnl_cents(row)
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    return {"n": len(ordered), "max_drawdown_cents": max_dd, "final_cumulative_cents": cumulative}


# ---------------------------------------------------------------------------
# Stopping rule (pre-registered, docs/REMEDIATION_PLAN.md "M3b")
# ---------------------------------------------------------------------------

def apply_stopping_rule(pooled: dict, bucket_stats: "dict[str, dict]") -> "tuple[str, str]":
    """Apply the pre-registered PASS/FAIL rule mechanically. Returns (verdict, reasoning).

    PASS iff net EV >= EV_PASS_THRESHOLD_CENTS/contract at the 95% UPPER
    bound of the POOLED YES rate, on n >= MIN_RESOLVED_N resolved rows, AND
    positive in >= MIN_POSITIVE_BUCKETS separate `no_ask` buckets -- each
    evaluated at THAT bucket's OWN 95% upper bound (the same pessimistic-for-
    a-NO-seller evaluation, applied per-bucket rather than re-using the
    pooled bound). No judgement call beyond what is pre-registered.
    """
    n = pooled["n_resolved"]
    ev_upper = pooled["ev_upper"]
    positive_buckets = [
        label for label, stats in bucket_stats.items()
        if stats.get("ev_upper") is not None and stats["ev_upper"] > 0
    ]
    n_positive = len(positive_buckets)

    n_ok = n >= MIN_RESOLVED_N
    ev_ok = ev_upper is not None and ev_upper >= EV_PASS_THRESHOLD_CENTS
    buckets_ok = n_positive >= MIN_POSITIVE_BUCKETS

    ev_txt = "n/a" if ev_upper is None else f"{ev_upper:+.3f}c/contract"
    reasoning = (
        f"Pooled EV at the 95% upper bound: {ev_txt} "
        f"({'>= ' if ev_ok else '< '}{EV_PASS_THRESHOLD_CENTS}c required). "
        f"n_resolved={n} ({'>= ' if n_ok else '< '}{MIN_RESOLVED_N} required). "
        f"Positive at their own upper bound in {n_positive}/{len(bucket_stats)} "
        f"no_ask buckets ({', '.join(positive_buckets) or 'none'}) "
        f"({'>= ' if buckets_ok else '< '}{MIN_POSITIVE_BUCKETS} required)."
    )
    if n_ok and ev_ok and buckets_ok:
        return "PASS", reasoning
    return "FAIL", reasoning


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def _fmt_pct(v: "float | None") -> str:
    return "n/a" if v is None else f"{100 * v:.1f}%"


def _fmt_ev(v: "float | None") -> str:
    return "n/a" if v is None else f"{v:+.3f}c"


def _fmt_ci(ci: "tuple[float, float] | None") -> str:
    return "n/a" if ci is None else f"{100 * ci[0]:.1f}% - {100 * ci[1]:.1f}%"


def _breakdown_table(title: str, groups: "dict[str, list[dict]]", order: "list[str] | None" = None) -> "list[str]":
    lines = [f"## {title}\n"]
    lines.append("| Group | n | resolved | observed YES | 95% CI (Wilson) | EV point | EV @95% upper | EV @2x |")
    lines.append("|---|---|---|---|---|---|---|---|")
    keys = order if order is not None else sorted(groups)
    stats_by_key = {}
    for key in keys:
        members = groups.get(key, [])
        stats = group_ev_stats(members)
        stats_by_key[key] = stats
        lines.append(
            f"| {key} | {stats['n']} | {stats['n_resolved']} | "
            f"{_fmt_pct(stats['observed_yes'])} | {_fmt_ci(stats['ci'])} | "
            f"{_fmt_ev(stats['ev_point'])} | {_fmt_ev(stats['ev_upper'])} | "
            f"{_fmt_ev(stats['ev_2x'])} |"
        )
    lines.append("")
    return lines, stats_by_key


def build_report(rows: "list[dict]", run_date: str, since: "str | None",
                 funnel_counts: dict, outcome_meta: dict) -> str:
    """Assemble the markdown report. *rows* is the de-duplicated,
    model-certain, outcome-resolved population (unresolved rows may still be
    present with `yes_won` absent -- the per-group helpers drop them from
    rates, never guess)."""
    pooled = group_ev_stats(rows)

    by_no_ask = group_rows(rows, lambda r: bucket_no_ask(r.get("no_ask")))
    by_minutes = group_rows(rows, lambda r: bucket_minutes(r.get("minutes_to_settlement")))
    by_station = group_rows(rows, lambda r: r.get("station"))
    by_ladder = group_rows(rows, ladder_kind)

    lines = []
    lines.append("# Model-Certain Population Tradability Test (issue #1063, #909 follow-up)\n")
    lines.append(f"**Run date:** {run_date}  ")
    lines.append(
        "**DOES NOT REOPEN M3.** M3's verdict (BSS = -0.4123) stands, final. "
        "This scores a DIFFERENT, gate-EXCLUDED population "
        "(`p_yes_raw == 0.0`, `is_model_certain_price`) as an EV/execution "
        "question, not a Brier skill question -- see the module docstring "
        "and `docs/REMEDIATION_PLAN.md`'s \"M3b\" section.  \n"
    )
    lines.append(
        "**No live trading regardless of result.** #1053/#1054's halt is "
        "unconditional and unaffected by this report either way.  \n"
    )
    lines.append(
        f"**Window:** rows polled on or after `{since}`  \n" if since else
        "**Window:** ALL of `bracket_evals` -- **NO `--since` CUTOFF**. "
        "`bracket_evals` spans incompatible probability eras (pre-#917, "
        "pre-#920, clean); an unwindowed run mixes them. Not a valid M3b "
        "reading.  \n"
    )
    lines.append("\n---\n")

    lines.append("## Population funnel\n")
    lines.append("| Stage | Count |")
    lines.append("|---|---|")
    lines.append(f"| De-duplicated bracket-days in window | {funnel_counts.get('deduped', 0)} |")
    lines.append(f"| Model-certain (`p_yes_raw == 0.0`) -- this population | {funnel_counts.get('model_certain', 0)} |")
    lines.append(f"| Also at the 1c/99c market rail (in-scope, not filtered) | {funnel_counts.get('also_rail', 0)} |")
    lines.append(f"| Resolved to an outcome | {pooled['n_resolved']} |")
    lines.append(f"| Unresolvable (dropped, never guessed) | {outcome_meta.get('n_unresolvable', 0)} |")
    lines.append(f"| Station-days | {outcome_meta.get('n_station_days', 0)} |")
    lines.append("")

    lines.append("## EV formula\n")
    lines.append(
        "```\n"
        "EV_per_contract(cents) = (1 - p_yes) * (100 - no_ask) - p_yes * no_ask\n"
        "```\n"
        "`p_yes` is the EMPIRICAL resolution rate of the group a row falls in "
        "(never `p_yes_raw`, which is uniformly 0.0 for this whole population "
        "by construction). `market_p_yes()` is never used in this report.\n"
    )

    lines.append(
        "## EV by `no_ask` bucket -- the primary result\n\n"
        "A single pooled EV number across this population is meaningless: "
        "the exclusion cascade runs `p_yes_raw_zero_artifact` before "
        "`rail_1c_99c`, so this population spans real price dispersion from "
        "well below the rail up to it. Report each bucket separately.\n"
    )
    no_ask_table, no_ask_stats = _breakdown_table("EV by no_ask bucket", by_no_ask, list(NO_ASK_BUCKETS))
    lines.extend(no_ask_table)

    lines.append("## EV by minutes-to-settlement bucket\n")
    lines.append(
        "Tests the envelope hypothesis: edge should concentrate late in the "
        "day, when `[current_high, max_env]` is most informative.\n"
    )
    minutes_table, _ = _breakdown_table(
        "EV by minutes-to-settlement bucket", by_minutes, [b[0] for b in MINUTES_BUCKETS])
    lines.extend(minutes_table)

    station_table, _ = _breakdown_table("EV by station", by_station)
    lines.extend(station_table)

    ladder_table, _ = _breakdown_table("EV by ladder kind (bracket width)", by_ladder)
    lines.extend(ladder_table)

    conc = concentration_by(rows, by_no_ask)
    lines.append("## Concentration\n")
    lines.append(
        "Share of total point-estimate EV (each row valued at ITS OWN "
        "`no_ask` bucket's empirical rate) contributed by the single largest "
        "station and the single largest `no_ask` bucket. An edge concentrated "
        "in one place is fragile, not a product -- stated plainly if either "
        f"exceeds {int(100 * CONCENTRATION_WARN_SHARE)}%.\n"
    )
    lines.append(f"Total priced EV: {_fmt_ev(conc['total_ev'])} over {conc['n_priced']} contracts.\n")
    for label, top in (("station", conc["top_station"]), ("no_ask bucket", conc["top_no_ask_bucket"])):
        if top is None:
            lines.append(f"- Top {label}: n/a (no priced rows).\n")
            continue
        key, group_total, share = top
        warn = " -- **CONCENTRATED, see above**" if abs(share) >= CONCENTRATION_WARN_SHARE else ""
        lines.append(f"- Top {label}: `{key}` -- {_fmt_ev(group_total)} ({100 * share:.1f}% of total){warn}\n")
    lines.append("")

    lines.append("## Tail stress\n")
    lines.append(
        "Pooled EV recomputed at the 95% UPPER bound of the pooled YES rate "
        "(pessimistic for a NO seller) and at 2x the pooled observed rate.\n"
    )
    lines.append("| Evaluation | EV per contract |")
    lines.append("|---|---|")
    lines.append(f"| Point estimate (pooled) | {_fmt_ev(pooled['ev_point'])} |")
    lines.append(f"| 95% upper bound (pooled) | {_fmt_ev(pooled['ev_upper'])} |")
    lines.append(f"| 2x observed rate (pooled) | {_fmt_ev(pooled['ev_2x'])} |")
    lines.append(
        "\n**\"For reference\" only -- not the answer.** This pools every "
        "`no_ask` bucket together and is exactly the meaningless-average this "
        "report warns against above; read the per-bucket table for the real "
        "picture.\n"
    )

    dd = worst_drawdown_cents(rows)
    lines.append("## Worst realized drawdown\n")
    lines.append(
        f"Playing {dd['n']} resolved, priced rows in chronological poll order "
        f"(`ts`), one NO contract each at `no_ask` cost basis: maximum "
        f"cumulative drawdown was **{dd['max_drawdown_cents']:.2f}c**; final "
        f"cumulative PnL **{dd['final_cumulative_cents']:+.2f}c**.\n"
    )

    verdict, reasoning = apply_stopping_rule(pooled, no_ask_stats)
    lines.append("## Stopping rule -- pre-registered, applied mechanically\n")
    lines.append(
        f"| Result | Verdict |\n|---|---|\n"
        f"| Net EV >= +{EV_PASS_THRESHOLD_CENTS}c/contract at the 95% UPPER "
        f"bound of the pooled YES rate, n >= {MIN_RESOLVED_N} resolved rows, "
        f"AND positive in >= {MIN_POSITIVE_BUCKETS} separate `no_ask` buckets "
        f"(at their own upper bound) | **PASS** -- a genuine, broad-enough "
        f"edge to warrant a follow-up proposal. Still not a live re-enable on "
        f"its own -- #1053/#1054's halt stays in effect regardless. |\n"
        f"| Anything else | **FAIL** -- the thesis is closed on all three "
        f"classes (contested/M3, market-certain, model-certain), not just "
        f"the one M3 scored. |\n"
    )
    lines.append(f"### Verdict: **{verdict}**\n")
    lines.append(reasoning + "\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_report(bracket_evals: "Path | None", db_path: Path, out_dir: Path,
               run_date: "str | None" = None, since: "str | None" = None,
               use_gamma: bool = True, allow_network: bool = True) -> int:
    run_date = run_date or datetime.now(timezone.utc).date().isoformat()

    source_path = bracket_evals or BRACKET_EVALS_JSONL
    raw_rows = load_bracket_eval_rows(source_path)
    if not raw_rows:
        log.info(
            "[m3b] no rows found under %s (rotated sources) -- nothing to "
            "score. Not writing a report.", source_path,
        )
        return 0

    raw_rows, n_before_since = filter_rows_since(raw_rows, since)
    if since and not raw_rows:
        log.info("[m3b] no rows polled on or after %s -- nothing to score.", since)
        return 0
    if n_before_since:
        log.info("[m3b] --since %s dropped %d pre-cutoff rows", since, n_before_since)

    deduped = dedupe_one_per_bracket_day(raw_rows)
    population = select_model_certain_population(deduped)
    if not population:
        log.info("[m3b] no model-certain rows in window -- nothing to score.")
        return 0

    also_rail = sum(
        1 for r in population
        if r.get("yes_ask") is not None and r.get("no_ask") is not None
        and (r["yes_ask"] <= 1 or r["yes_ask"] >= 99 or r["no_ask"] <= 1 or r["no_ask"] >= 99)
    )

    if _connect_ro(db_path) is None:
        log.info("[m3b] no readable database at %s -- cannot resolve outcomes. "
                 "Not writing a report.", db_path)
        return 0

    samples, outcome_counts = resolve_candidate_outcomes(
        population, db_path, use_gamma=use_gamma, allow_network=allow_network,
    )
    if not samples:
        log.info("[m3b] no row could be resolved from Gamma or observations -- "
                 "not writing a report.")
        return 0

    funnel_counts = {
        "deduped": len(deduped),
        "model_certain": len(population),
        "also_rail": also_rail,
    }
    report = build_report(samples, run_date, since, funnel_counts, outcome_counts)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"model_certain_tradability_{run_date}.md"
    out_path.write_text(report, encoding="utf-8")
    log.info("[m3b] wrote %s (n_resolved=%d)", out_path, len(
        [s for s in samples if s.get("yes_won") is not None and s.get("no_ask") is not None]))
    return 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bracket-evals", type=Path, default=None)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--run-date", default=None)
    ap.add_argument("--since", default="2026-08-06", metavar="YYYY-MM-DD",
                    help="Score only rows POLLED on or after this UTC date "
                         "(same window as the M3 gate). Filters on poll "
                         "time, not settlement date.")
    ap.add_argument("--no-gamma", action="store_true")
    ap.add_argument("--no-network", action="store_true")
    args = ap.parse_args(argv)
    return run_report(
        args.bracket_evals, args.db, args.out, args.run_date, args.since,
        use_gamma=not args.no_gamma, allow_network=not args.no_network,
    )


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    raise SystemExit(main())

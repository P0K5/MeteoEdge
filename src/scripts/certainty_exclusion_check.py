"""Are the BSS report's two certainty exclusions still justified? (issue #822)

``bss_market_vs_model_report`` drops two classes of row before scoring:

* ``p_yes_raw == 0.0`` -- the #820 certainty-shortcut artifact. The evening
  window priced tomorrow's market with today's finished observations, the
  envelope collapsed, and the shortcut emitted an exact 0.0 that was never a
  model opinion.
* ``yes_ask``/``no_ask`` at the 1c/99c rail -- a clipped exchange price, not a
  freely-produced market opinion.

On the 2026-07-29 Pass-2 population those two removed **64.4%** of all input
rows between them. #820 is now merged, so the first exclusion's premise may no
longer hold -- and whether it holds moves the M3 verdict a long way in either
direction. This tool answers that question **without computing a BSS**, so it
can be run before the gate without spoiling it.

Why a separate module rather than a flag on the report: this must be
structurally incapable of printing a skill number on the retained population.
It never computes one.

---------------------------------------------------------------------------
The decision has two independent halves
---------------------------------------------------------------------------

**(A) Provenance -- is an exact 0.0 still a code artifact?**  A well-formed
normal-CDF bracket probability is tiny but essentially never *bit-exact* 0.0;
reaching exactly 0.0 means a shortcut branch or a float underflow. So the
split between bit-exact zeros and merely-tiny probabilities is a provenance
signal readable from the data, and this tool reports it. It is not conclusive:
confirming the #820 shortcut is gone is a question about the code, and the
report says so rather than pretending the data settles it.

**(B) Calibration -- is the certainty honest?**  If rows claiming P(YES)=0
resolve YES at a material rate, the certainty is false whatever produced it.
The pre-fix Pass-1 data showed **24.0%** observed YES in its 0.00-0.02 bucket,
which is what a broken certainty looks like.

Both halves must point the same way before the exclusion is dropped.

---------------------------------------------------------------------------
Symmetry -- the trap this tool exists to prevent
---------------------------------------------------------------------------

The two exclusions are a matched pair: one removes rows the MODEL is certain
about, the other rows the MARKET is certain about. Dropping the model's
exclusion while keeping the market's would hand the model its easy wins while
still denying the market its own -- a biased comparison that would look like
skill. Any change must therefore be argued for both classes together, so this
tool always reports both and refuses to recommend an asymmetric change.
"""

from __future__ import annotations

import argparse
import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from src.scripts.bss_market_vs_model_report import (
    BRACKET_EVALS_JSONL,
    CANDIDATES_CSV,
    DEFAULT_DB_PATH,
    DEFAULT_OUT_DIR,
    POPULATION_ALL_BRACKET,
    POPULATION_GATE_SELECTED,
    POPULATIONS,
    RAIL_HIGH_CENTS,
    RAIL_LOW_CENTS,
    dedupe_one_per_bracket_day,
    filter_rows_since,
    load_bracket_eval_rows,
    load_candidate_rows,
    market_p_yes,
    resolve_candidate_outcomes,
)

log = logging.getLogger(__name__)

#: Largest observed-YES rate at which a claim of P(YES)=0 is still defensible.
#: A 0.0 claim rounds to 0% at any reporting precision, so any material excess
#: is a misstatement rather than noise; 2pp sits an order of magnitude below
#: the 24.0% the pre-#820 data showed, so it separates "fixed" from "still
#: broken" without needing a judgement call at the boundary. Wilson intervals
#: are reported alongside so an underpowered estimate is visible as such.
HONESTY_BAR = 0.02

#: Below this, a probability is "tiny but computed"; at exactly 0.0 it is a
#: shortcut or an underflow. The gap between the two counts is the provenance
#: signal described in the module docstring.
TINY_PROBABILITY = 1e-6

CLASS_MODEL_CERTAIN = "model_certain"
CLASS_MARKET_CERTAIN = "market_certain"
CLASS_BOTH_CERTAIN = "both_certain"
CLASS_CONTESTED = "contested"

#: Fixed before any number was seen, mirroring how the M3 gate itself is
#: pre-registered in docs/REMEDIATION_PLAN.md. Written as (condition, verdict)
#: so the report can print the rule above the figures it is applied to.
DECISION_RULE = (
    ("The 95% interval straddles the 2% bar",
     "**Underpowered -- not a decision.** The sample cannot place the rate on either "
     "side of the bar, so it is consistent with both an honest and a false certainty. "
     "Keep the exclusion and re-run when the class is larger. This branch is checked "
     "first, so a point estimate is never read past what its interval supports."),
    ("Model-certain rows resolve YES at > 2%",
     "**The certainty is false.** Whatever emits an exact 0.0 is still wrong, so the "
     "exclusion stays for the gate -- and that is a finding in its own right: file it. "
     "Scoring these rows would not be measuring a model opinion."),
    ("Model-certain rows resolve YES at <= 2% AND bit-exact zeros remain",
     "**Honest but still artifact-shaped.** The calibration half passes, the provenance "
     "half does not. Keep the exclusion and settle provenance by reading the envelope "
     "shortcut in the code -- data cannot close this half."),
    ("Model-certain rows resolve YES at <= 2% AND no bit-exact zeros remain",
     "**A genuine, well-calibrated opinion.** The #820 premise no longer holds. Drop the "
     "exclusion -- but only together with a matching decision on the market-certain "
     "class, per the symmetry rule below."),
)


def wilson_interval(k: int, n: int, z: float = 1.96) -> "tuple[float, float] | None":
    """Wilson 95% score interval for a binomial rate.

    Preferred over the normal approximation because these rates sit near 0,
    where the normal interval famously produces negative bounds and collapses
    to zero width at k=0 -- exactly the regime this tool operates in.
    """
    if n <= 0:
        return None
    phat = k / n
    denom = 1.0 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    margin = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def is_model_certain(row: dict) -> bool:
    """The model claims impossibility: an exact-zero raw probability."""
    return row.get("p_yes_raw") == 0.0


def is_market_certain(row: dict) -> bool:
    """A quoted side sits at the exchange's 1c/99c rail -- a clipped price."""
    yes_ask, no_ask = row.get("yes_ask"), row.get("no_ask")
    if yes_ask is None or no_ask is None:
        return False
    return (yes_ask <= RAIL_LOW_CENTS or yes_ask >= RAIL_HIGH_CENTS
            or no_ask <= RAIL_LOW_CENTS or no_ask >= RAIL_HIGH_CENTS)


def classify_certainty(rows: "list[dict]") -> "tuple[dict[str, list[dict]], dict[str, int]]":
    """Split *rows* into the four certainty classes.

    ``apply_exclusions`` tests ``p_yes_raw == 0.0`` before the rail, so a row
    that is both lands in the zero bucket and its rail-ness is invisible in the
    report's funnel. That overlap is broken out here rather than folded away,
    because a large overlap would mean the two exclusions are not independent
    and the symmetry argument needs stating differently.
    """
    classes: "dict[str, list[dict]]" = {
        CLASS_MODEL_CERTAIN: [], CLASS_MARKET_CERTAIN: [],
        CLASS_BOTH_CERTAIN: [], CLASS_CONTESTED: [],
    }
    counts: dict = defaultdict(int)
    for row in rows:
        if row.get("p_yes_raw") is None:
            counts["undiagnosable_missing_p_yes_raw"] += 1
            continue
        if row.get("yes_ask") is None or row.get("no_ask") is None:
            counts["undiagnosable_missing_market_price"] += 1
            continue
        model_c, market_c = is_model_certain(row), is_market_certain(row)
        if model_c and market_c:
            classes[CLASS_BOTH_CERTAIN].append(row)
        elif model_c:
            classes[CLASS_MODEL_CERTAIN].append(row)
        elif market_c:
            classes[CLASS_MARKET_CERTAIN].append(row)
        else:
            classes[CLASS_CONTESTED].append(row)
    counts["input_rows"] = len(rows)
    for name, members in classes.items():
        counts[f"n_{name}"] = len(members)
    return classes, dict(counts)


def provenance_split(rows: "list[dict]") -> "dict[str, int]":
    """Bit-exact zeros vs. merely-tiny probabilities among model-certain rows.

    A normal-CDF bracket probability underflows to exactly 0.0 only in extreme
    tails; a shortcut branch assigns it directly. The counts do not prove which
    happened, but an all-bit-exact population is the artifact's signature and a
    population with a tail of tiny-but-nonzero values is not.
    """
    exact = sum(1 for r in rows if r.get("p_yes_raw") == 0.0)
    return {"bit_exact_zero": exact, "n": len(rows)}


def class_stats(rows: "list[dict]") -> dict:
    """Outcome and prediction summary for one certainty class.

    ``honesty_gap`` is observed YES minus what the class's own prices implied.
    For model-certain rows the model's claim is 0 by construction, so the gap
    is the observed rate itself; for market-certain rows the rail can sit at
    either end of the book, so the market's own implied probability is the
    reference rather than an assumed zero.
    """
    resolved = [r for r in rows if r.get("yes_won") is not None]
    n = len(resolved)
    if not n:
        return {"n_total": len(rows), "n_resolved": 0, "n_yes": 0,
                "observed_yes": None, "mean_market_p": None,
                "honesty_gap": None, "ci": None, "station_days": 0}
    n_yes = sum(1 for r in resolved if r["yes_won"])
    observed = n_yes / n
    mean_market = sum(market_p_yes(r) for r in resolved) / n
    station_days = len({
        (r.get("station"), r.get("settlement_date") or r.get("end_date"))
        for r in resolved
    })
    return {
        "n_total": len(rows),
        "n_resolved": n,
        "n_yes": n_yes,
        "observed_yes": observed,
        "mean_market_p": mean_market,
        "honesty_gap": observed - mean_market,
        "ci": wilson_interval(n_yes, n),
        "station_days": station_days,
    }


def verdict(model_stats: dict, provenance: dict) -> "tuple[str, str]":
    """Apply DECISION_RULE mechanically. Returns (label, reasoning)."""
    observed = model_stats.get("observed_yes")
    if observed is None:
        return ("INDETERMINATE -- no model-certain row could be resolved",
                "No outcome could be attached to any model-certain row, so neither half "
                "of the decision can be evaluated. This is not a result.")
    ci = model_stats.get("ci")
    if observed <= HONESTY_BAR and ci and ci[1] > HONESTY_BAR:
        # The point estimate clears the bar but the interval does not. Calling
        # that "honest" would be reading a rate the sample cannot support --
        # the same error as reading an underpowered BSS as a verdict.
        return ("UNDERPOWERED -- the sample cannot place the rate against the bar",
                f"Model-certain rows resolve YES at {observed:.1%}, below the "
                f"{HONESTY_BAR:.0%} bar, but the 95% interval reaches {ci[1]:.1%} -- so "
                f"the data is equally consistent with an honest certainty and a false "
                f"one. **Keep the exclusion and re-run when the class is larger.** "
                f"Deciding from the point estimate alone would be the underpowered-BSS "
                f"mistake in a different costume.")
    if observed > HONESTY_BAR:
        return ("KEEP the exclusion -- the certainty is FALSE",
                f"Model-certain rows resolve YES at {observed:.1%}, above the "
                f"{HONESTY_BAR:.0%} bar. An exact-0.0 claim is materially wrong at that "
                f"rate, so these rows are not a model opinion worth scoring. File the "
                f"finding: something still emits false certainty post-#820.")
    if provenance.get("bit_exact_zero", 0) > 0:
        return ("KEEP the exclusion -- calibration passes, provenance does not",
                f"Model-certain rows resolve YES at {observed:.1%}, within the "
                f"{HONESTY_BAR:.0%} bar, so the certainty is honest. But all "
                f"{provenance['bit_exact_zero']} of them are bit-exact 0.0, which is the "
                f"shortcut's signature rather than a computed probability. Settle "
                f"provenance in the code before dropping the exclusion.")
    return ("DROP the exclusion -- but only together with the market-certain class",
            f"Model-certain rows resolve YES at {observed:.1%} and none is a bit-exact "
            f"zero, so both halves point the same way: these are genuine, "
            f"well-calibrated opinions and the #820 premise no longer holds.")


def outcome_key(row: dict) -> tuple:
    """Join key between a classified row and its resolved copy.

    ``resolve_candidate_outcomes`` rebuilds each row (``{**row, ...}``) rather
    than mutating it, so identity cannot be used to pair them up -- an
    ``id()``-keyed join silently matches nothing and leaves every outcome
    ``None``. Rows are deduplicated to one per (station, ticker, bracket-day)
    before classification, so this triple is unique across the population.
    """
    return (row.get("station"), row.get("ticker"),
            (row.get("settlement_date") or row.get("end_date") or "")[:10])


def attach_outcomes(classes: "dict[str, list[dict]]", resolved: "list[dict]") -> int:
    """Copy ``yes_won``/``settlement_date`` back onto the classified rows.

    Rows the resolver could not decide are absent from *resolved* and keep
    ``yes_won`` unset -- ``class_stats`` counts only rows that carry one, so an
    unresolvable row is dropped from the rates rather than guessed at. Returns
    the number of rows matched, which the caller can sanity-check.
    """
    by_key = {outcome_key(r): r for r in resolved}
    matched = 0
    for members in classes.values():
        for row in members:
            got = by_key.get(outcome_key(row))
            if got is None:
                continue
            row["yes_won"] = got.get("yes_won")
            row["settlement_date"] = got.get("settlement_date")
            matched += 1
    return matched


def _fmt_pct(v: "float | None") -> str:
    return "n/a" if v is None else f"{100 * v:.1f}%"


def _class_table(name: str, stats: dict) -> "list[str]":
    ci = stats.get("ci")
    ci_txt = "n/a" if not ci else f"{100 * ci[0]:.1f}% – {100 * ci[1]:.1f}%"
    return [
        f"| {name} | {stats['n_total']} | {stats['n_resolved']} | "
        f"{stats['station_days']} | {_fmt_pct(stats['observed_yes'])} | {ci_txt} | "
        f"{_fmt_pct(stats['mean_market_p'])} |"
    ]


def build_report(classes: dict, counts: dict, run_date: str, population: str,
                 resolved_counts: dict, since: "str | None" = None) -> str:
    """Render the check. Deliberately contains no BSS and no skill number."""
    model_s = class_stats(classes[CLASS_MODEL_CERTAIN] + classes[CLASS_BOTH_CERTAIN])
    market_s = class_stats(classes[CLASS_MARKET_CERTAIN] + classes[CLASS_BOTH_CERTAIN])
    contested_s = class_stats(classes[CLASS_CONTESTED])
    provenance = provenance_split(classes[CLASS_MODEL_CERTAIN] + classes[CLASS_BOTH_CERTAIN])

    src = ("`logs/bracket_evals.*.jsonl` (#826, all evaluated brackets)"
           if population == POPULATION_ALL_BRACKET
           else "`logs/candidates.*.csv.gz` (archived, gate-selected)")

    lines = [
        "# Certainty-exclusion check (issue #822)\n",
        f"**Run date:** {run_date}  ",
        f"**Population:** {population} -- {src}  ",
        # The window belongs in the record, not just in someone's shell
        # history. This verdict is pre-registered and read back weeks later;
        # "which rows were classified" is the first thing it must answer.
        (f"**Window:** brackets polled on or after {since}  " if since else
         "**Window:** ALL of bracket_evals -- **NO `--since` CUTOFF**. If this "
         "spans the pre-2026-08-06 window the zeros counted here were "
         "manufactured by #917/#920 and the result is not usable.  "),
        "**Contains no BSS.** This decides which rows the M3 gate should score; it "
        "deliberately computes no skill number, so it can be run before the gate "
        "without spoiling the pre-registration.\n",
        "\n---\n",
        "## The pre-registered rule\n",
        "Fixed before any figure below was seen.\n",
        "| Condition | Verdict |",
        "|---|---|",
    ]
    for cond, verd in DECISION_RULE:
        lines.append(f"| {cond} | {verd} |")
    lines.append("")
    lines.append(
        "> **Symmetry constraint.** The two exclusions are a matched pair -- one removes "
        "rows the MODEL is certain about, the other rows the MARKET is certain about. "
        "Dropping one alone hands that side its easy wins while denying the other its "
        "own, which reads as skill and is not. Any change applies to both classes or "
        "to neither.\n"
    )

    lines.append("## Certainty classes\n")
    lines.append(
        "> **These counts will not match the BSS report's exclusion funnel, and should "
        "not.** That funnel de-duplicates first and then excludes, so the final "
        "poll's fate determines whether a bracket-day enters the scored population. "
        "This check de-duplicates first too (same ordering, #915), so each bracket-day "
        "appears once and is classified by the model's FINAL word on it. The counts "
        "still differ because the classification logic (certainty vs. undiagnosable vs. "
        "rail) differs from the report's exclusion funnel.\n"
    )
    lines.append("| Class | rows | resolved | station-days | observed YES | 95% CI (Wilson) "
                 "| mean market P(YES) |")
    lines.append("|---|---|---|---|---|---|---|")
    lines.extend(_class_table("Model-certain (`p_yes_raw == 0.0`)", model_s))
    lines.extend(_class_table("Market-certain (1c/99c rail)", market_s))
    lines.extend(_class_table("Contested (what the gate scores today)", contested_s))
    lines.append("")
    n_both = counts.get("n_both_certain", 0)
    if n_both:
        lines.append(
            f"**{n_both}** row(s) are certain on BOTH sides. `apply_exclusions` tests the "
            f"zero first, so the report's funnel attributes them to `p_yes_raw == 0.0` "
            f"alone and their rail-ness is invisible there. They are counted into both "
            f"class rows above, which are therefore not disjoint.\n"
        )
    else:
        lines.append(
            "No row is certain on both sides, so the two classes above are disjoint and "
            "the exclusions are independent.\n"
        )
    undiag = (counts.get("undiagnosable_missing_p_yes_raw", 0)
              + counts.get("undiagnosable_missing_market_price", 0))
    if undiag:
        lines.append(
            f"Excluded from this check entirely: **{undiag}** row(s) missing "
            f"`p_yes_raw` or a market price -- neither certainty question is askable "
            f"of them.\n"
        )

    lines.append("## Provenance -- is an exact 0.0 computed or asserted?\n")
    lines.append(
        f"Of {provenance['n']} model-certain rows, **{provenance['bit_exact_zero']}** are "
        f"bit-exact `0.0`. A normal-CDF bracket probability underflows to exactly zero "
        f"only in extreme tails, so a population that is entirely bit-exact is the "
        f"shortcut's signature, not a computed tail.\n"
    )
    lines.append(
        "> This half is **not conclusive from data**. Confirming the #820 envelope "
        "shortcut no longer runs is a question about the code, and no outcome rate can "
        "answer it -- an artifact that happens to be right is still an artifact.\n"
    )

    label, reasoning = verdict(model_s, provenance)
    lines.append("## Verdict\n")
    lines.append(f"**{label}**\n")
    lines.append(reasoning + "\n")

    if resolved_counts:
        lines.append("## Outcome resolution\n")
        lines.append(
            f"Gamma {resolved_counts.get('resolved_from_gamma', 0)}, "
            f"observed-daily fallback {resolved_counts.get('resolved_from_metar', 0)}, "
            f"unresolvable and dropped {resolved_counts.get('n_unresolvable', 0)}. "
            f"Direction-aware: `low` markets resolve against the observed daily LOW "
            f"(#867).\n"
        )

    lines.append("\n---\n")
    lines.append("## What this check cannot tell you\n")
    lines.append(
        "- **Whether dropping an exclusion helps or hurts the model.** That is the "
        "gate's job, and knowing it in advance is exactly what the pre-registration "
        "exists to prevent. Decide from the rule above, then run the gate.\n"
        "- **Whether the #820 shortcut still runs.** See the provenance note.\n"
        "- **Anything about the contested population's skill.** The contested row above "
        "reports its size and base rate only.\n"
    )
    return "\n".join(lines)


def run_check(db_path: Path, out_dir: Path, run_date: "str | None" = None, *,
              population: str = POPULATION_ALL_BRACKET,
              candidates_csv: Path = CANDIDATES_CSV,
              bracket_evals: "Path | None" = None,
              use_gamma: bool = True, allow_network: bool = True,
              since: "str | None" = None) -> int:
    run_date = run_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if population == POPULATION_ALL_BRACKET:
        rows = load_bracket_eval_rows(bracket_evals or BRACKET_EVALS_JSONL)
    else:
        rows = load_candidate_rows(candidates_csv)

    # Without this the check reads every era of bracket_evals at once -- and
    # the eras are not comparable for THIS question in particular. It asks
    # whether an exact-zero p_yes_raw is an honest opinion or an artifact; in
    # the 2026-07-24..08-05 window those zeros were manufactured by #917 and
    # #920, so including them measures defects already removed and guarantees
    # "keep the exclusion" whatever the current code does.
    #
    # Same filter and the same poll-time semantics as the gate (#936, #941):
    # contamination is a property of when the probability was COMPUTED.
    if not since:
        log.warning(
            "[certainty] NO --since CUTOFF. Classifying every era of "
            "bracket_evals at once. The pre-2026-08-06 window's exact zeros "
            "were produced by #917/#920, so including them measures removed "
            "defects and predetermines 'keep the exclusion'.")
    rows, n_dropped = filter_rows_since(rows, since)
    if since:
        log.info("[certainty] --since %s kept %d rows, dropped %d pre-cutoff",
                 since, len(rows), n_dropped)
    if not rows:
        log.error("[certainty] no input rows for population=%s -- nothing to check",
                  population)
        return 1

    # Dedupe BEFORE classifying: the question is what the model's FINAL word on
    # each bracket-day was. The report uses the same ordering (#915) -- dedupe
    # first, then exclude, so a bracket whose last poll is excluded is dropped
    # rather than falling back to an earlier one.
    deduped = dedupe_one_per_bracket_day(rows)
    classes, counts = classify_certainty(deduped)

    to_resolve = (classes[CLASS_MODEL_CERTAIN] + classes[CLASS_MARKET_CERTAIN]
                  + classes[CLASS_BOTH_CERTAIN] + classes[CLASS_CONTESTED])
    resolved, rcounts = resolve_candidate_outcomes(
        to_resolve, db_path, use_gamma=use_gamma, allow_network=allow_network)
    matched = attach_outcomes(classes, resolved)
    if resolved and not matched:
        log.error("[certainty] resolver returned %d rows but none joined back -- "
                  "refusing to report rates off an empty join", len(resolved))
        return 1

    report = build_report(classes, counts, run_date, population, rcounts,
                          since=since)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"certainty_exclusion_check_{run_date}.md"
    out_path.write_text(report, encoding="utf-8")

    model_s = class_stats(classes[CLASS_MODEL_CERTAIN] + classes[CLASS_BOTH_CERTAIN])
    label, _ = verdict(
        model_s, provenance_split(classes[CLASS_MODEL_CERTAIN] + classes[CLASS_BOTH_CERTAIN]))
    log.info("[certainty] model-certain n=%d observed_yes=%s -- %s",
             model_s["n_resolved"], _fmt_pct(model_s["observed_yes"]), label)
    log.info("[certainty] wrote %s", out_path)
    return 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--run-date", default=None)
    ap.add_argument("--population", choices=POPULATIONS, default=POPULATION_ALL_BRACKET,
                    help="Which population's exclusions to check. Defaults to "
                         "'all-bracket' -- the population the M3 gate scores.")
    ap.add_argument("--candidates-csv", type=Path, default=CANDIDATES_CSV)
    ap.add_argument("--bracket-evals", type=Path, default=None)
    ap.add_argument("--since", default=None,
                    help="Only classify brackets POLLED on or after this date "
                         "(YYYY-MM-DD, UTC). Use the clean-data clock start -- "
                         "without it the contaminated pre-fix window is "
                         "included and the answer is predetermined.")
    ap.add_argument("--no-gamma", action="store_true")
    ap.add_argument("--no-network", action="store_true")
    args = ap.parse_args(argv)
    return run_check(
        args.db, args.out, args.run_date,
        population=args.population,
        candidates_csv=args.candidates_csv,
        bracket_evals=args.bracket_evals,
        use_gamma=not args.no_gamma,
        allow_network=not args.no_network,
        since=args.since,
    )


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    raise SystemExit(main())

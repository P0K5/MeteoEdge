"""EMOS-shadow-vs-market skill test -- M3-parallel, read-only (issue #1041).

M3 (``bss_market_vs_model_report.py --population all-bracket``) scores the
LEGACY served model (fixed sigma=2.0, ``p_yes_raw``) against the market. EMOS
is 100% shadow -- nothing it computes has ever reached ``bracket_evals`` --
so a negative M3 verdict says nothing about EMOS one way or the other.

This script runs the SAME scoring methodology (exclusion funnel, de-dup,
outcome resolution, BSS math -- all imported from ``bss_market_vs_model_report``
UNCHANGED, never re-derived) against a RECONSTRUCTED EMOS-shadow probability
instead of the legacy ``p_yes_raw`` (``src.scripts.emos_shadow_reconstruction``).

**This module does not modify ``bss_market_vs_model_report.py``; it imports
it.** That module is not part of this diff at all -- every name this file
uses from it (``apply_exclusions``, ``dedupe_one_per_bracket_day``,
``filter_rows_since``, ``resolve_candidate_outcomes``, ``compute_bss``,
``market_p_yes``, ``build_reliability``/``format_reliability``,
``sharpness_histogram``/``format_sharpness``, ``verdict_label``,
``load_bracket_eval_rows``, ``DEFAULT_DB_PATH``) is a plain import, and its
default CLI invocation (``python -m src.scripts.bss_market_vs_model_report``,
no flags) is therefore byte-for-byte unaffected by this file's existence --
see ``test_emos_shadow_vs_market_report.py::TestDoesNotAffectBssMarketVsModelReport``
for the standing regression check.

**This is a distinct, parallel experiment, not part of M3.** It must not
touch, slow, or risk the live M3 clean-data collection window in any way:

- It is a standalone module (see above -- ``bss_market_vs_model_report.py``
  is imported from, never modified).
- Every DB access goes through ``ReadOnlyDatabase``
  (``src.scripts.emos_shadow_reconstruction``), a read-only connection with a
  write-denying SQLite authorizer.
- Output goes to ``backtest_results/emos_shadow_vs_market_<date>.md`` --
  never ``bss_market_vs_model_pass1_*``/``pass2_*``, which are the actual M3
  gate artifacts.
- Not wired into cron/CI/the bot's runtime. Manually invoked only.

**Mandatory caveats, read before citing any number below** (see
``REQUIRED_CAVEATS``, always rendered into the report):

1. Every city sits at ~28/60 EMOS_MIN_SAMPLES_PROMOTION samples in this
   window -- this scores an UNDERTRAINED model. Treat any result the same
   way Pass 1 is treated: directional, not a verdict, in either direction.
2. The ``sigma_source='ensemble'`` coefficients used here were fit against
   rows that were actually scored with FIXED sigma=2.0
   (``ensemble_sigma_f`` never populated in this window) -- so this result
   reflects EMOS's mu-correction ONLY, not the sharper-sigma model
   #885/#893 are meant to eventually produce.

Usage::

    python -m src.scripts.emos_shadow_vs_market_report --since 2026-08-06
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import BRACKET_EVALS_JSONL  # noqa: E402
from src.scripts.bss_market_vs_model_report import (  # noqa: E402
    DEFAULT_DB_PATH,
    apply_exclusions,
    build_reliability,
    compute_bss,
    dedupe_one_per_bracket_day,
    filter_rows_since,
    format_reliability,
    format_sharpness,
    load_bracket_eval_rows,
    market_p_yes,
    resolve_candidate_outcomes,
    sharpness_histogram,
    verdict_label,
)
from src.scripts.emos_shadow_reconstruction import (  # noqa: E402
    ReadOnlyDatabase,
    reconstruct_bracket_row,
)
from src.utils.log_rotation import iter_rotated_jsonl  # noqa: E402

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

DEFAULT_OUT_DIR = Path("backtest_results")

# Same population/window M3 uses, for direct comparability of the two BSS
# numbers (issue #1041's explicit requirement).
DEFAULT_SINCE = "2026-08-06"

REQUIRED_CAVEATS = """\
> **NOT THE M3 DECISION GATE -- A PARALLEL, EXPLORATORY READ (issue #1041).**
> This scores a RECONSTRUCTED EMOS-shadow probability (EMOS shadow's current
> coefficients, applied offline to the same population M3 scores) against the
> market, using the identical BSS methodology M3 uses. It answers a different
> question than M3: "if EMOS shadow's forecasts had been served, how would
> they have scored?" -- not "how did the legacy served model score?"
>
> **1. Undertrained model.** Every city sits at roughly 28 of the 60
> `EMOS_MIN_SAMPLES_PROMOTION` CRPS-logged shadow days required for
> promotion in this window. Treat any BSS below the same way Pass 1 is
> treated: directional, not a verdict, and must not be argued from in
> either direction.
>
> **2. Fixed-sigma mislabeling.** The `sigma_source='ensemble'` coefficients
> used here were fit against rows that were actually scored with FIXED
> sigma=2.0 (`ensemble_sigma_f` was never populated in this window). This
> result therefore reflects EMOS's `(a, b)` mu-correction ONLY -- it cannot
> test the sigma lever `(c, d)` was meant to calibrate, and says nothing
> about the sharper-sigma model issues #885/#893 are meant to eventually
> produce.
"""


def load_bracket_eval_rows_for_reconstruction(base: "Path") -> "list[dict]":
    """``load_bracket_eval_rows`` (imported, unchanged) plus the two extra raw
    fields reconstruction needs that Pass 2's normalized row shape does not
    carry: ``current_high`` and ``latest_temp`` (``WeatherState.current_high_f``
    / ``latest_temp_f`` at scan time -- see ``scanner.py``'s ``snap`` dict).

    Deliberately does NOT reimplement or modify ``load_bracket_eval_rows`` --
    calls it verbatim for the canonical Pass-2-identical shape, then makes a
    SECOND pass over the same JSONL source (``iter_rotated_jsonl``, the same
    reader ``load_bracket_eval_rows`` uses) to read the two extra raw fields.
    The two passes are over the same deterministically-ordered source
    (``iter_rotated_jsonl`` yields oldest-first across the same
    ``rotated_sources`` file list both calls resolve identically), so a
    positional zip is safe.
    """
    normalized = load_bracket_eval_rows(base)
    raw = list(iter_rotated_jsonl(base))
    if len(raw) != len(normalized):
        # Should be impossible (both read the exact same source), but never
        # silently mis-zip a state field onto the wrong bracket-row.
        raise RuntimeError(
            f"bracket_evals row-count mismatch: normalized={len(normalized)} "
            f"raw={len(raw)} -- refusing to merge state fields positionally."
        )
    for norm_row, raw_row in zip(normalized, raw):
        norm_row["current_high"] = raw_row.get("current_high")
        norm_row["latest_temp"] = raw_row.get("latest_temp")
    return normalized


def build_emos_report(
    samples: "list[dict]",
    exclusion_counts: dict,
    reconstruction_counts: dict,
    outcome_counts: dict,
    n_no_settlement: int,
    run_date: str,
    since: "str | None",
) -> str:
    """Assemble the markdown report. Mirrors the shape of
    ``bss_market_vs_model_report.build_report`` (same funnel/BSS/reliability/
    sharpness presentation) but is its own, independent render -- this report
    is not one of the two M3 passes and must not borrow their disclaimer
    text or decision-gate framing.
    """
    global_stats = compute_bss(samples)
    model_samples = [(r["p_yes_raw"], r["yes_won"]) for r in samples]
    market_samples = [(market_p_yes(r), r["yes_won"]) for r in samples]

    lines = ["# EMOS-Shadow-vs-Market Skill Test (issue #1041)\n"]
    lines.append(f"**Run date:** {run_date}  ")
    lines.append("**Data source:** `logs/bracket_evals.*.jsonl` (issue #826, same "
                 "population M3 Pass 2 scores)  ")
    lines.append("**Probability source:** reconstructed EMOS-shadow "
                 "`(mu_final, sigma_cal)` via `apply_emos`/`resolve_sigma_raw` + "
                 "`true_probability_yes` -- see `src.scripts.emos_shadow_reconstruction`  ")
    if since:
        lines.append(f"**Poll-date window:** rows polled on or after **{since}** "
                     "(same `--since` M3 uses, for direct comparability)  \n")
    else:
        lines.append("**Poll-date window:** none given -- NOT comparable to a "
                     "windowed M3 run.  \n")
    lines.append(REQUIRED_CAVEATS)
    lines.append("\n---\n")

    lines.append("## Reconstruction funnel\n")
    lines.append("Reconstruction runs on the de-duplicated, `--since`-filtered "
                 "population, BEFORE the row-exclusion cascade below (which then "
                 "runs unchanged on the substituted probability).\n")
    lines.append("| Stage | Count |")
    lines.append("|---|---|")
    lines.append(f"| De-duplicated rows considered | {reconstruction_counts['n_considered']} |")
    lines.append(f"| Reconstructed (EMOS-shadow p available) | "
                 f"{reconstruction_counts['n_reconstructed']} |")
    lines.append(f"| Out of scope: next-day rows | "
                 f"{reconstruction_counts['n_next_day']} |")
    lines.append(f"| Out of scope: non-\"high\"-direction rows | "
                 f"{reconstruction_counts['n_non_high_direction']} |")
    lines.append(f"| Unreconstructable: missing required fields or no "
                 f"`model_forecast_log`/timezone data | "
                 f"{reconstruction_counts['n_unreconstructable']} |")
    lines.append("")

    lines.append("## Exclusion funnel (identical cascade to "
                 "`bss_market_vs_model_report.apply_exclusions`)\n")
    input_rows = exclusion_counts.get("input_rows", 0)

    def _pct(n, d):
        return f"{(n or 0) / d * 100:.1f}%" if d else "n/a"

    lines.append("| Stage | Count | Share (of population) |")
    lines.append("|---|---|---|")
    lines.append(f"| Input rows (post reconstruction) | {input_rows} | 100% |")
    for label, key in (
        ("Excluded: missing/unreconstructable EMOS probability", "missing_p_yes_raw"),
        ("Excluded: EMOS probability == 0.0 (certainty artifact)",
         "p_yes_raw_zero_artifact"),
        ("Excluded: missing market price", "missing_market_price"),
        ("Excluded: fabricated 50/50 price", "fabricated_50_50_price"),
        ("Excluded: 1c/99c rail", "rail_1c_99c"),
    ):
        n = exclusion_counts.get(key, 0)
        lines.append(f"| {label} | {n} | {_pct(n, input_rows)} |")
    kept_after_row = exclusion_counts.get("kept_after_row_exclusions", 0)
    lines.append(f"| Kept after row exclusions | {kept_after_row} | "
                 f"{_pct(kept_after_row, input_rows)} |")
    lines.append(f"| Excluded: no outcome resolvable | {n_no_settlement} | "
                 f"{_pct(n_no_settlement, kept_after_row)} |")
    lines.append(f"| **Final de-duplicated sample (n)** | **{global_stats['n']}** | "
                 f"**{_pct(global_stats['n'], kept_after_row)}** |")
    lines.append(f"| **Effective sample size (station-days)** | "
                 f"**{outcome_counts.get('n_station_days', 0)}** | station-days, "
                 f"not bracket-rows |")
    lines.append("")

    lines.append("## Global result\n")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| n | {global_stats['n']} |")
    bs_m, bs_k, bss = global_stats["bs_model"], global_stats["bs_market"], global_stats["bss"]
    lines.append(f"| BS_model (EMOS-shadow-implied) | {bs_m:.4f} |" if bs_m is not None
                 else "| BS_model | n/a |")
    lines.append(f"| BS_market | {bs_k:.4f} |" if bs_k is not None else "| BS_market | n/a |")
    lines.append(f"| BSS | {bss:.4f} |" if bss is not None else "| BSS | n/a |")
    lines.append(f"| Directional reading (NOT a verdict -- see caveats above) | "
                 f"{verdict_label(bss)} |")
    lines.append("")

    lines.append("## Reliability\n")
    lines.append(format_reliability(build_reliability(model_samples), "EMOS-shadow-implied"))
    lines.append(format_reliability(build_reliability(market_samples),
                                    "Market (symmetrized implied P(YES))"))

    lines.append("\n## Sharpness\n")
    lines.append(format_sharpness(sharpness_histogram([p for p, _ in model_samples]),
                                  "EMOS-shadow-implied"))
    lines.append(format_sharpness(sharpness_histogram([p for p, _ in market_samples]),
                                  "Market (symmetrized implied P(YES))"))

    lines.append("\n---\n")
    lines.append("## Methodology notes\n")
    lines.append("- `p_model` = reconstructed EMOS-shadow-implied P(YES). See "
                 "`src.scripts.emos_shadow_reconstruction` for the exact "
                 "reconstruction (reuses `apply_emos`/`resolve_sigma_raw`/"
                 "`true_probability_yes` directly, never re-derives their math).")
    lines.append("- `p_market` = `(yes_ask + (100 - no_ask)) / 200` -- identical to "
                 "M3's `market_p_yes()` (imported, not reimplemented).")
    lines.append("- Exclusion funnel, de-duplication, and outcome resolution "
                 "(`resolve_bracket_outcomes`, Gamma-first with observed-daily-high "
                 "fallback) are IMPORTED from `bss_market_vs_model_report` "
                 "unchanged -- identical to M3 Pass 2 except for the probability "
                 "column.")
    lines.append("- Reconstruction is scoped to same-day, high-direction rows only "
                 "(see the reconstruction funnel above and "
                 "`emos_shadow_reconstruction.reconstruct_bracket_row`'s "
                 "docstring for what is out of scope and why).")
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
    """Load, reconstruct, score, and (if there is real data) write the report.

    Self-gates (logs a reason, returns 0) exactly like
    ``bss_market_vs_model_report.run_report`` -- no local `logs/`, no
    readable DB, or no row survives reconstruction/outcome resolution.
    """
    run_date = run_date or datetime.now(timezone.utc).date().isoformat()
    source_path = bracket_evals or BRACKET_EVALS_JSONL
    raw_rows = load_bracket_eval_rows_for_reconstruction(source_path)
    if not raw_rows:
        log.info("[emos-shadow] no rows found under %s -- nothing to score.", source_path)
        return 0

    raw_rows, n_before_since = filter_rows_since(raw_rows, since)
    if since and not raw_rows:
        log.info("[emos-shadow] no rows polled on or after %s -- nothing to score.", since)
        return 0

    deduped = dedupe_one_per_bracket_day(raw_rows)

    if not db_path.exists():
        log.info("[emos-shadow] no readable database at %s -- cannot reconstruct "
                 "EMOS-shadow probabilities. Not writing a report.", db_path)
        return 0

    ro_db = ReadOnlyDatabase(db_path)
    try:
        reconstruction_counts = {
            "n_considered": len(deduped), "n_reconstructed": 0,
            "n_next_day": 0, "n_non_high_direction": 0, "n_unreconstructable": 0,
        }
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
                row["p_yes_raw"] = None
                continue
            direction = row.get("direction") or "high"
            if direction != "high":
                reconstruction_counts["n_non_high_direction"] += 1
                row["p_yes_raw"] = None
                continue

            emos_p = reconstruct_bracket_row(ro_db, row)
            if emos_p is None:
                reconstruction_counts["n_unreconstructable"] += 1
                row["p_yes_raw"] = None
            else:
                reconstruction_counts["n_reconstructed"] += 1
                row["p_yes_raw"] = emos_p
    finally:
        ro_db._conn.close()

    if reconstruction_counts["n_reconstructed"] == 0:
        log.info("[emos-shadow] no row could be reconstructed -- not writing a report.")
        return 0

    kept, exclusion_counts = apply_exclusions(deduped)
    exclusion_counts["input_rows_all_dates"] = len(raw_rows) + n_before_since
    exclusion_counts["dropped_before_since"] = n_before_since
    exclusion_counts["since"] = since

    if not kept:
        log.info("[emos-shadow] no row survived the exclusion cascade -- not writing a report.")
        return 0

    samples, outcome_counts = resolve_candidate_outcomes(
        kept, db_path, use_gamma=use_gamma, allow_network=allow_network,
    )
    n_unresolved = outcome_counts.get("n_unresolvable", 0)
    if not samples:
        log.info("[emos-shadow] no row could be resolved from Gamma or observations "
                 "-- not writing a report.")
        return 0

    report = build_emos_report(
        samples, exclusion_counts, reconstruction_counts, outcome_counts,
        n_unresolved, run_date, since,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"emos_shadow_vs_market_{run_date}.md"
    out_path.write_text(report, encoding="utf-8")
    log.info("[emos-shadow] wrote %s (n=%d, %d station-days)", out_path, len(samples),
             outcome_counts.get("n_station_days", 0))
    return 0


def main(argv: "list[str] | None" = None) -> int:
    """CLI entry point: parse args and call ``run_report``.

    ``python -m src.scripts.emos_shadow_vs_market_report [--since YYYY-MM-DD]
    [--bracket-evals PATH] [--db PATH] [--out DIR] [--run-date YYYY-MM-DD]
    [--no-gamma] [--no-network]``. See each ``--help`` string below for the
    per-flag contract; ``--since`` defaults to ``DEFAULT_SINCE`` (2026-08-06,
    M3's window). Returns the process exit code (0 always -- see
    ``run_report``'s self-gating docstring for why a "nothing to score" run
    is not an error).
    """
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bracket-evals", type=Path, default=None,
                    help="Override the bracket_evals JSONL path")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--run-date", default=None,
                    help="Report date stamp (default: today, UTC)")
    ap.add_argument("--since", default=DEFAULT_SINCE, metavar="YYYY-MM-DD",
                    help="Score only rows POLLED on or after this UTC date. "
                         "Defaults to the same window M3 uses (2026-08-06), for "
                         "direct comparability of the two BSS numbers.")
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

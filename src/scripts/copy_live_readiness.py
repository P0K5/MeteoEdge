"""Pre-registered copy-trading go-live readiness report (issue #1255).

**Read-only. Never writes anything, never pauses/follows/resumes a
wallet.** Prints a per-followed-wallet report from settled
``copy_positions``, built to answer one question with a fixed, pre-agreed
yardstick: "is this wallet's paper track record strong enough to justify
real capital?" -- decided *before* looking at the numbers, so the
goalposts can't move once we see them.

**Why this exists.** The 2026-09-29 by-hand analysis of ``0x9243`` showed
the naive per-trade 95% CI is misleading: fills on the same market are
correlated, so treating each fill as an independent sample overstates
confidence. Per-trade, ``0x9243`` looked significant (CI [+0.73, +4.91],
n=36); deduped into decisions (one entry per ``(market, outcome_index)``,
matching ``copy_wallet_health.py``'s convention) the CI widens to
[-0.49, +7.04] -- and its PnL is dominated by its first day (+$88 of
+$102) and a single market (3 fills, +$52). Nothing computed this
automatically, so it was done by hand. This script is that computation,
made reproducible and run against every followed wallet, not just the one
under suspicion.

**Decisions, not fills.** Settled rows are deduped via
``copy_wallet_health.dedupe_decisions`` -- the SAME function
``_realized_pnl_pause_reason`` uses for its own P&L check -- so this
report and the auto-pause job can never silently disagree about what
counts as one decision. A decision's timestamp (for the last-7-day window
below) is the latest ``settled_at`` among its contributing fills.

**Method: normal approximation, no scipy** (explicit technical-notes
requirement). 95% CI of the per-decision mean PnL is ``mean +/- 1.96 *
sd/sqrt(n)`` using the sample standard deviation (``ddof=1``). ``sd`` (and
therefore the CI) is undefined for fewer than 2 decisions.

**The pre-registered gate (fixed by issue #1255).** A wallet PASSES only
if ALL of:

1. at least ``GATE_MIN_DECISIONS`` deduped decisions, all-time;
2. the all-time per-decision 95% CI lower bound is > 0;
3. the last-``LAST_N_DAYS``-day mean PnL per decision is > 0;
4. the single best (highest-PnL) decision is <= ``GATE_MAX_CONCENTRATION``
   of the wallet's all-time total PnL.

These four constants are module constants, commented with this issue, on
purpose -- **changing any of them needs a new issue, not an edit to this
file**, so a losing wallet can never be waved through by quietly loosening
the bar after the fact. See docs/design/copy-trading-architecture.md,
"Go/no-go gate" (phase 7), for the documented gate.

A wallet failing more than one condition is reported against the FIRST
one checked (order above) -- mirrors ``copy_wallet_health.py``'s "first
pause reason that fires wins" convention; this is a report field, not a
promise that the other three conditions passed.

**Paused wallets are reported, but always FAIL** with
``failing_condition="paused"`` regardless of what their stats say -- a
paused wallet has already been judged unhealthy by a separate signal, and
this gate is additive, not a path to override that.

**On today's data, every wallet is expected to FAIL** -- none has 150
deduped decisions yet (see the "Copy-trading go-live readiness" note,
2026-09-29). That is the correct, honest output of a pre-registered gate
looked at before the sample is large enough, not a bug in this script.

Usage::

    python -m src.scripts.copy_live_readiness
    python -m src.scripts.copy_live_readiness --json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.data.db import Database  # noqa: E402
from src.scripts.copy_wallet_health import dedupe_decisions  # noqa: E402

#: Normal-approximation z-score for a 95% confidence interval.
CONFIDENCE_Z = 1.96

#: Width of the "recent" window used for the last-N-days mean check.
LAST_N_DAYS = 7

# --- Pre-registered gate (issue #1255) -------------------------------
# Fixed by that issue. Changing any of these four values requires a new
# issue, not an edit to this file -- see the module docstring.
GATE_MIN_DECISIONS = 150
GATE_MIN_ALL_TIME_CI_LOWER_BOUND = 0.0
GATE_MIN_LAST_7D_MEAN = 0.0
GATE_MAX_CONCENTRATION = 0.25
# -----------------------------------------------------------------------


def _parse_ts(ts: "str | None") -> "datetime | None":
    """Best-effort ISO-8601 parse, tolerant of a trailing ``Z`` (this
    codebase's writers use ``+00:00``, but this stays defensive against
    any future/foreign row). Returns ``None`` on anything unparseable
    rather than raising -- a report is never worth crashing over one bad
    timestamp.

    A naive (offset-less) parse is treated as UTC rather than returned
    as-is: every caller compares the result against an aware ``cutoff``
    (``datetime.now(timezone.utc) - timedelta(...)``), and comparing a
    naive and an aware ``datetime`` raises ``TypeError`` -- exactly the
    crash this function's own docstring promises never happens. This
    codebase's writers always emit an explicit offset, so this only
    matters for a malformed/foreign row, but that is precisely the case
    this function exists to survive.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _stats(values: "list[float]") -> dict:
    """Return ``{'n', 'total', 'mean', 'sd', 'ci_lower', 'ci_upper'}`` for
    *values* using the normal approximation (mean +/- 1.96*sd/sqrt(n),
    sample sd with ``ddof=1``). ``sd``/``ci_lower``/``ci_upper`` are
    ``None`` when *values* has fewer than 2 entries (sample sd is
    undefined); ``mean`` is ``None`` only when *values* is empty.
    """
    n = len(values)
    if n == 0:
        return {"n": 0, "total": 0.0, "mean": None, "sd": None, "ci_lower": None, "ci_upper": None}

    total = sum(values)
    mean = total / n
    if n < 2:
        return {"n": n, "total": total, "mean": mean, "sd": None, "ci_lower": None, "ci_upper": None}

    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    sd = math.sqrt(variance)
    margin = CONFIDENCE_Z * sd / math.sqrt(n)
    return {
        "n": n, "total": total, "mean": mean, "sd": sd,
        "ci_lower": mean - margin, "ci_upper": mean + margin,
    }


def _decisions_needed_for_positive_ci(mean: "float | None", sd: "float | None") -> "int | None":
    """Approximate number of decisions needed for the all-time 95% CI
    lower bound to clear 0, HOLDING the current *mean* and *sd* fixed
    (solving ``mean - 1.96*sd/sqrt(n) > 0`` for ``n``).

    Returns ``None`` when the answer isn't meaningful: *mean* unknown, or
    <= 0 (no amount of additional same-distribution sampling would clear
    the bar), or *sd* unknown (fewer than 2 decisions so far -- nothing to
    extrapolate from). This is explicitly an approximation: it assumes the
    mean and sd observed so far are representative of future decisions,
    which is not guaranteed.
    """
    if mean is None or mean <= 0:
        return None
    if sd is None:
        return None
    if sd == 0:
        return 1
    n_needed = (CONFIDENCE_Z * sd / mean) ** 2
    return max(1, math.ceil(n_needed))


def _evaluate_gate(
    status: str, all_time: dict, last_7d: dict, concentration: "float | None",
) -> "tuple[str, str | None]":
    """Apply the pre-registered gate in order; return ``(verdict,
    failing_condition)``. The FIRST failing condition is named -- a wallet
    failing multiple conditions is not guaranteed to have the other three
    reported as passing, only that they weren't reached.
    """
    if status == "paused":
        return "FAIL", "paused"

    if all_time["n"] < GATE_MIN_DECISIONS:
        return "FAIL", "min_decisions"

    if all_time["ci_lower"] is None or not (all_time["ci_lower"] > GATE_MIN_ALL_TIME_CI_LOWER_BOUND):
        return "FAIL", "all_time_ci_lower_bound"

    if last_7d["mean"] is None or not (last_7d["mean"] > GATE_MIN_LAST_7D_MEAN):
        return "FAIL", "last_7d_mean"

    if concentration is None or concentration > GATE_MAX_CONCENTRATION:
        return "FAIL", "concentration"

    return "PASS", None


def evaluate_wallet(wallet: dict, settled_rows: "list[dict]", now: "datetime | None" = None) -> dict:
    """Build the full readiness report entry for one followed wallet.

    *wallet* is a ``copy_wallets_followed`` row (``address``, ``status``,
    ``paused_reason``, ...); *settled_rows* is that wallet's
    ``get_settled_copy_positions()`` result. *now* defaults to the current
    UTC time and exists so tests can pin the last-N-days window
    deterministically.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=LAST_N_DAYS)

    decisions = dedupe_decisions(settled_rows)

    all_pnls = [d["pnl"] for d in decisions]
    all_time = _stats(all_pnls)

    recent_pnls = [
        d["pnl"] for d in decisions
        if (ts := _parse_ts(d.get("settled_at"))) is not None and ts >= cutoff
    ]
    last_7d = _stats(recent_pnls)

    best_decision_pnl = max(all_pnls) if all_pnls else None
    total_pnl = all_time["total"]
    if best_decision_pnl is not None and total_pnl > 0:
        concentration = best_decision_pnl / total_pnl
    else:
        # total_pnl <= 0: "share of total" is not a meaningful fraction
        # (and in practice condition 2 above already fails first whenever
        # the all-time mean isn't positive, since mean>0 is implied by
        # ci_lower>0) -- treated as fail-safe, never as "passes".
        concentration = None

    decisions_needed = _decisions_needed_for_positive_ci(all_time["mean"], all_time["sd"])

    verdict, failing_condition = _evaluate_gate(wallet["status"], all_time, last_7d, concentration)

    return {
        "address": wallet["address"],
        "status": wallet["status"],
        "paused_reason": wallet.get("paused_reason"),
        "all_time": all_time,
        "last_7d": last_7d,
        "best_decision_pnl": best_decision_pnl,
        "concentration": concentration,
        "decisions_needed_for_positive_ci": decisions_needed,
        "verdict": verdict,
        "failing_condition": failing_condition,
    }


def build_report(db, now: "datetime | None" = None) -> "list[dict]":
    """Return one readiness entry (see ``evaluate_wallet``) per followed
    wallet, regardless of status -- paused wallets are included (and
    always FAIL) so the report is a complete picture of every wallet ever
    promoted, not just the currently-active ones."""
    wallets = db.get_followed_wallets()
    report = []
    for wallet in wallets:
        settled = db.get_settled_copy_positions(wallet["address"])
        report.append(evaluate_wallet(wallet, settled, now=now))
    return report


def _fmt(value, nd: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{nd}f}"


def print_report(report: "list[dict]") -> None:
    print(f"Copy-trading live-readiness report -- {len(report)} followed wallet(s).")
    print(
        f"Gate: >= {GATE_MIN_DECISIONS} decisions, all-time 95% CI lower bound > 0, "
        f"last {LAST_N_DAYS}d mean > 0, best decision <= "
        f"{GATE_MAX_CONCENTRATION:.0%} of total PnL."
    )
    for w in report:
        print(f"\n{w['address']}  status={w['status']}", end="")
        if w["paused_reason"]:
            print(f"  paused_reason={w['paused_reason']}")
        else:
            print()

        at, l7 = w["all_time"], w["last_7d"]
        print(
            f"  all-time: n={at['n']} total={_fmt(at['total'])} mean={_fmt(at['mean'])} "
            f"sd={_fmt(at['sd'])} 95% CI=[{_fmt(at['ci_lower'])}, {_fmt(at['ci_upper'])}]"
        )
        print(
            f"  last {LAST_N_DAYS}d:  n={l7['n']} total={_fmt(l7['total'])} mean={_fmt(l7['mean'])} "
            f"sd={_fmt(l7['sd'])} 95% CI=[{_fmt(l7['ci_lower'])}, {_fmt(l7['ci_upper'])}]"
        )
        print(
            f"  concentration: {_fmt(w['concentration'], nd=3)} "
            f"(best single decision pnl={_fmt(w['best_decision_pnl'])})"
        )
        needed = w["decisions_needed_for_positive_ci"]
        print(
            "  approx. decisions needed for all-time CI lower bound > 0: "
            + (str(needed) if needed is not None else "n/a (mean <= 0 or < 2 decisions so far)")
        )
        verdict_line = f"  VERDICT: {w['verdict']}"
        if w["failing_condition"]:
            verdict_line += f"  (failing: {w['failing_condition']})"
        print(verdict_line)


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--json", action="store_true",
        help="Emit the report as JSON instead of the human-readable text report.",
    )
    args = ap.parse_args(argv)

    db = Database()
    report = build_report(db)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    raise SystemExit(main())

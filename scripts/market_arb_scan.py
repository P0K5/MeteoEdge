#!/usr/bin/env python3
"""Model-free arbitrage scan over the archived bracket-evaluation log.

Read-only. Touches nothing but ``logs/bracket_evals*.jsonl[.gz]``: no database,
no network, no writes anywhere except the report path you pass in. It cannot
contaminate the M3 clean window -- it never opens ``meteoedge.db`` and never
recomputes a probability. It also ignores ``p_yes``/``p_yes_raw`` entirely; the
model does not appear in any number it prints.

Two questions, both about the MARKET's own prices:

1. COMPLEMENT  -- per bracket, is ``yes_ask + no_ask < 100c``?  YES and NO for
   one condition merge back into $1 on Polymarket, so buying both below 100c is
   riskless up to fees.

2. LADDER (dutch book) -- per (station, settlement_date, direction, poll hour),
   the brackets are mutually exclusive and exhaustive, so exactly N-1 of the N
   NO legs pay $1.  Buying every NO costs ``sum(no_ask)`` and returns
   ``(N-1) * 100c``.  Equivalently, ``sum(yes_ask) > 100c`` is the same trade
   seen from the other side.

Both are reported gross and net of the taker fee, and both are reported ONLY on
ladders that look complete -- a gap-free ladder is the precondition for the
dutch book, and #917 is the reason we can no longer assume it.

CAVEAT ON DEPTH, stated up front so the output is not over-read: ``yes_ask_size``
and ``no_ask_size`` are hardcoded to 0 unless ``ENABLE_CLOB_ENRICHMENT=true``,
and they are not persisted to ``bracket_evals`` at all.  This script therefore
measures HOW OFTEN and HOW WIDE an arb was quoted, never how much size it would
have absorbed.  A high hit rate here is a reason to turn CLOB enrichment on and
measure capacity -- it is not, by itself, money.

Usage -- from the repo root, which must be the cwd (``LOG_DIR`` is relative):

    python -m scripts.market_arb_scan
    python scripts/market_arb_scan.py --since 2026-08-06
    python scripts/market_arb_scan.py --since 2026-06-01 --out /tmp/arb.md

On the bot host, prefer the no-checkout form so the working tree never leaves
master -- the tree there IS production, and a branch switch changes the code
the bot imports on its next restart:

    cd ~/MeteoEdge
    git fetch -q origin claude/project-profit-opportunities-af8639
    git show origin/claude/project-profit-opportunities-af8639:scripts/market_arb_scan.py \
        > /tmp/market_arb_scan.py
    .venv/bin/python /tmp/market_arb_scan.py --out /tmp/market_arb_scan.md

``git show`` writes to stdout and touches neither HEAD nor the index, so there
is no window in which the host is on another branch and no cleanup to forget.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

def _repo_root() -> Path:
    """Locate the repo whether or not this file lives inside it.

    The intended invocation on the bot host copies this script to /tmp so the
    checkout never leaves master (see the runbook below), which puts
    ``__file__`` outside the repo entirely. The current working directory is
    then the only reliable anchor -- and it has to be the repo regardless,
    because ``src.config`` defines ``LOG_DIR = Path("logs")``, relative to cwd.
    """
    cwd = Path.cwd()
    for cand in (cwd, *cwd.parents):
        if (cand / "src" / "config.py").is_file():
            return cand
    return Path(__file__).resolve().parent.parent


REPO_ROOT = _repo_root()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# --- optional repo imports, with standalone fallbacks -----------------------
try:
    from src.config import BRACKET_EVALS_JSONL
except Exception:                                        # pragma: no cover
    BRACKET_EVALS_JSONL = REPO_ROOT / "logs" / "bracket_evals.jsonl"

try:
    from src.utils.log_rotation import iter_rotated_jsonl
except Exception:                                        # pragma: no cover
    import gzip

    def iter_rotated_jsonl(base, include_compressed: bool = True):
        base = Path(base)
        pats = [base.name, base.stem + ".*.jsonl"]
        if include_compressed:
            pats.append(base.stem + ".*.jsonl.gz")
        seen = set()
        for pat in pats:
            for path in sorted(base.parent.glob(pat)):
                if path in seen or not path.is_file():
                    continue
                seen.add(path)
                op = gzip.open(path, "rt") if path.suffix == ".gz" else open(path)
                with op as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            try:
                                yield json.loads(line)
                            except json.JSONDecodeError:
                                continue

try:
    from src.strategy.fee import estimate_fee_cents
except Exception:                                        # pragma: no cover
    def estimate_fee_cents(price_cents: int) -> float:
        p = price_cents / 100.0
        return max(1.0, 7.0 * p * (1 - p))


def _wilson(hits: int, n: int) -> "tuple[float, float]":
    """95% Wilson interval, as percentages. Same convention as #909."""
    if n == 0:
        return (0.0, 0.0)
    z, phat = 1.96, hits / n
    denom = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half) * 100, min(1.0, centre + half) * 100)


def _pct(values, q):
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


def _fmt(x, nd=2):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{nd}f}"


# ---------------------------------------------------------------------------
def scan(since: "str | None", path: Path) -> dict:
    rows, skipped_no_price, skipped_pre_since = [], 0, 0

    for rec in iter_rotated_jsonl(path):
        poll_ts = rec.get("poll_ts") or ""
        if since and poll_ts[:10] < since:
            skipped_pre_since += 1
            continue
        ya, na = rec.get("yes_ask"), rec.get("no_ask")
        if ya is None or na is None:
            skipped_no_price += 1
            continue
        try:
            ya, na = int(ya), int(na)
        except (TypeError, ValueError):
            skipped_no_price += 1
            continue
        rows.append({
            "station": rec.get("station"),
            "ticker": rec.get("ticker"),
            "sd": rec.get("settlement_date"),
            "dir": rec.get("direction") or "high",
            "poll": poll_ts[:13],
            "lo": rec.get("bracket_low"),
            "hi": rec.get("bracket_high"),
            "yes": ya,
            "no": na,
            "mins": rec.get("minutes_to_settlement"),
        })

    # --- 1. complement ------------------------------------------------------
    comp_gross, comp_net, comp_by_station = [], [], defaultdict(list)
    for r in rows:
        total = r["yes"] + r["no"]
        gross = 100 - total
        # Two taker legs, each priced at its own side.
        net = gross - estimate_fee_cents(r["yes"]) - estimate_fee_cents(r["no"])
        comp_gross.append(gross)
        comp_net.append(net)
        comp_by_station[r["station"]].append(net)

    # --- 2. ladder ----------------------------------------------------------
    ladders = defaultdict(list)
    for r in rows:
        ladders[(r["station"], r["sd"], r["dir"], r["poll"])].append(r)

    lad_stats, incomplete, rail_only = [], 0, 0
    for key, legs in ladders.items():
        n = len(legs)
        if n < 3:
            incomplete += 1
            continue
        # Completeness: contiguous, non-overlapping bracket boundaries.  Open-ended
        # end brackets legitimately carry a None bound, so only the interior is checked.
        interior = sorted(
            (l for l in legs if l["lo"] is not None and l["hi"] is not None),
            key=lambda l: l["lo"],
        )
        gapped = False
        for a, b in zip(interior, interior[1:]):
            if abs(float(b["lo"]) - float(a["hi"])) > 0.51:
                gapped = True
                break
        if gapped or len(interior) < n - 2:
            incomplete += 1
            continue
        if all(l["yes"] <= 1 or l["yes"] >= 99 for l in legs):
            rail_only += 1

        sum_yes = sum(l["yes"] for l in legs)
        sum_no = sum(l["no"] for l in legs)
        # Buy every NO: cost sum_no, payout (n-1)*100.
        gross = (n - 1) * 100 - sum_no
        net = gross - sum(estimate_fee_cents(l["no"]) for l in legs)
        mins = [l["mins"] for l in legs if isinstance(l["mins"], (int, float))]
        lad_stats.append({
            "key": key, "n": n, "sum_yes": sum_yes, "sum_no": sum_no,
            "gross": gross, "net": net,
            "mins": min(mins) if mins else None,
            "all_rail": all(l["yes"] <= 1 or l["yes"] >= 99 for l in legs),
        })

    return {
        "rows": rows, "n_rows": len(rows),
        "skipped_no_price": skipped_no_price, "skipped_pre_since": skipped_pre_since,
        "comp_gross": comp_gross, "comp_net": comp_net, "comp_by_station": comp_by_station,
        "ladders": lad_stats, "n_ladder_groups": len(ladders),
        "incomplete": incomplete, "rail_only": rail_only,
    }


def report(res: dict, since: "str | None", path: Path) -> str:
    L: list[str] = []
    a = L.append
    a("# Market-side arbitrage scan (model-free)")
    a("")
    a(f"**Source:** `{path}` (rotated + gzipped)  ")
    a(f"**Window:** {since or 'ALL retained'}  ")
    a(f"**Rows with both asks:** {res['n_rows']:,}  ")
    a(f"**Dropped:** {res['skipped_no_price']:,} missing a price, "
      f"{res['skipped_pre_since']:,} before `--since`")
    a("")
    a("> Depth is unknown: `bracket_evals` carries no ask sizes. Everything below is "
      "**how often and how wide**, never how much size would fill.")
    a("")

    # --- complement ---
    cg, cn = res["comp_gross"], res["comp_net"]
    n = len(cg)
    a("## 1. Complement arb — `yes_ask + no_ask < 100c`")
    a("")
    if not n:
        a("_No priced rows._")
    else:
        hits_g = sum(1 for x in cg if x > 0)
        hits_n = sum(1 for x in cn if x > 0)
        lo_g, hi_g = _wilson(hits_g, n)
        lo_n, hi_n = _wilson(hits_n, n)
        a("| measure | value |")
        a("|---|---|")
        a(f"| rows | {n:,} |")
        a(f"| median `yes+no` | {_fmt(statistics.median(x for x in cg))}c below 100 |")
        a(f"| **gross hits** (`sum < 100`) | **{hits_g:,} ({100*hits_g/n:.2f}%)**, "
          f"95% CI {lo_g:.2f}–{hi_g:.2f}% |")
        a(f"| **net hits** (after both taker fees) | **{hits_n:,} ({100*hits_n/n:.3f}%)**, "
          f"95% CI {lo_n:.3f}–{hi_n:.3f}% |")
        a(f"| best gross | {max(cg):.0f}c |")
        a(f"| best net | {max(cn):.2f}c |")
        a(f"| p99 / p95 / p50 gross | {_fmt(_pct(cg,0.99),0)} / {_fmt(_pct(cg,0.95),0)} "
          f"/ {_fmt(_pct(cg,0.50),0)}c |")
        a("")
        if hits_n:
            a("### Net-positive complements by station")
            a("")
            a("| station | rows | net hits | hit % | best net |")
            a("|---|---|---|---|---|")
            for st, vals in sorted(res["comp_by_station"].items(),
                                   key=lambda kv: -sum(1 for v in kv[1] if v > 0)):
                h = sum(1 for v in vals if v > 0)
                if h:
                    a(f"| {st} | {len(vals):,} | {h:,} | {100*h/len(vals):.2f}% | "
                      f"{max(vals):.2f}c |")
            a("")

    # --- ladder ---
    lads = res["ladders"]
    a("## 2. Ladder dutch book — buy every NO")
    a("")
    a(f"Groups keyed `(station, settlement_date, direction, poll hour)`: "
      f"{res['n_ladder_groups']:,} total, **{len(lads):,} complete**, "
      f"{res['incomplete']:,} rejected as short/gapped, {res['rail_only']:,} "
      "all-rail (every leg pinned at 1c/99c).")
    a("")
    if not lads:
        a("_No complete ladders in the window._")
    else:
        gs = [l["gross"] for l in lads]
        ns_ = [l["net"] for l in lads]
        sy = [l["sum_yes"] for l in lads]
        hg = sum(1 for x in gs if x > 0)
        hn = sum(1 for x in ns_ if x > 0)
        lo_g, hi_g = _wilson(hg, len(lads))
        lo_n, hi_n = _wilson(hn, len(lads))
        a("| measure | value |")
        a("|---|---|")
        a(f"| complete ladders | {len(lads):,} |")
        a(f"| median legs per ladder | {statistics.median(l['n'] for l in lads):.0f} |")
        a(f"| **median `sum(yes_ask)`** | **{statistics.median(sy):.0f}c** "
          "(100 = fair; >100 = dutch book) |")
        a(f"| p95 / p99 `sum(yes_ask)` | {_fmt(_pct(sy,0.95),0)} / {_fmt(_pct(sy,0.99),0)}c |")
        a(f"| **gross hits** | **{hg:,} ({100*hg/len(lads):.2f}%)**, "
          f"95% CI {lo_g:.2f}–{hi_g:.2f}% |")
        a(f"| **net hits** (all N taker fees) | **{hn:,} ({100*hn/len(lads):.3f}%)**, "
          f"95% CI {lo_n:.3f}–{hi_n:.3f}% |")
        a(f"| best gross / best net | {max(gs):.0f}c / {max(ns_):.2f}c |")
        a("")
        top = sorted(lads, key=lambda l: -l["net"])[:15]
        a("### 15 widest ladders by net edge")
        a("")
        a("| station | settlement | poll (UTC h) | legs | sum(yes) | gross | net | mins left | all-rail |")
        a("|---|---|---|---|---|---|---|---|---|")
        for l in top:
            st, sd, dr, poll = l["key"]
            a(f"| {st} | {sd} | {poll} | {l['n']} | {l['sum_yes']}c | "
              f"{l['gross']:.0f}c | {l['net']:.2f}c | {_fmt(l['mins'],0)} | "
              f"{'yes' if l['all_rail'] else ''} |")
        a("")

    a("## How to read this")
    a("")
    a("- **Net hit rate ~0%** closes the question: the arb bots have it, and items 1-2 of "
      "the alternatives memo are dead. That is a useful, cheap negative.")
    a("- **Net hits concentrated in all-rail ladders** is usually not real — a ladder pinned "
      "at 1c/99c across every leg reflects a quote convention, not a fillable book.")
    a("- **Net hits on non-rail ladders, repeatedly, at the same stations/hours** is the "
      "signal worth chasing. Next step is `ENABLE_CLOB_ENRICHMENT=true` for a week to get "
      "depth, since nothing here can tell you whether the size was $3 or $3,000.")
    a("- **Read the gross column too, because the fee model is unvalidated.** "
      "`estimate_fee_cents` has a **1c-per-contract floor**, so an N-leg ladder is charged "
      "at least Nc before it can show a net edge -- on a 9-leg ladder that alone eats 9c and "
      "makes a net hit nearly impossible by construction. Its own docstring says the "
      "coefficient was never checked against fills (`scripts/fee_calibration.py`, MAE <= 0.25c). "
      "If the real taker fee on these markets is near zero, **gross is the honest column** and "
      "the net one is an artifact of an unvalidated constant. Validating the fee model is a "
      "prerequisite for believing either number -- and it is cheap.")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="Only polls on/after this UTC date (YYYY-MM-DD).")
    ap.add_argument("--bracket-evals", type=Path, default=Path(BRACKET_EVALS_JSONL),
                    help="Override the bracket_evals JSONL base path.")
    ap.add_argument("--out", type=Path,
                    help="Also write the report here (default: stdout only).")
    args = ap.parse_args()

    path = args.bracket_evals
    if not path.parent.exists():
        print(f"[arb] no log directory at {path.parent} -- nothing to scan", file=sys.stderr)
        return 0

    res = scan(args.since, path)
    if not res["n_rows"]:
        print(f"[arb] no priced bracket_evals rows found under {path} "
              f"(since={args.since or 'all'})", file=sys.stderr)
        return 0

    text = report(res, args.since, path)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"\n[arb] wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Does the M3 collection window conserve probability mass? (issues #917/#920)

Two defects in how bracket probabilities are computed were found on 2026-07-31,
both feeding ``p_yes_raw`` -- the quantity the M3 gate scores. Both are now
fixed and deployed, and this tool is what established that:

* **#917** -- degF dash-range brackets integrated at half width. Merged
  2026-07-31, verified live in this data on 2026-08-01.
* **#920** -- brackets outside the surviving interval zeroed without
  renormalising the remainder. Merged 2026-08-05, verified live 2026-08-06.
  The expensive end was the *top* cut (~15% of mass), not the bottom
  censoring the issue was originally filed against (~3%).

A third, **#921** (one ladder summing to 1.156), was measured at 1.04 and
stood down as immaterial; it is not a gate precondition.

The clean-data clock therefore starts **2026-08-06**. This tool's job is no
longer to find those defects but to keep proving the window stays clean: mass
conservation is the invariant whose absence let both bugs live for weeks, and
nothing else in the stack checks it. Run it over the live window before the
gate, and treat any deficient day as a regression.

---------------------------------------------------------------------------
1. Was #917 actually deployed, and when?
---------------------------------------------------------------------------

It is code-only, so it takes effect only after ``meteoedge-run`` restarts. If
that never happened, *every* day of the window is pre-fix rather than only the
first seven.

Rather than trusting a systemd timestamp, this reads the answer out of the data
itself: **bracket width is a fingerprint of the parser version.**

===========  =========================  ==========================
width (degF)  ladder                     meaning
===========  =========================  ==========================
1.0          degF dash-range, pre-#917  the bug, live
2.0          degF dash-range, post-#917 fixed
1.8          degC via _LABEL_EXACT      always was correct
===========  =========================  ==========================

The day 1.0-wide ladders stop appearing is the day #917 reached production. If
they never stop, it never deployed.

---------------------------------------------------------------------------
2. How much of the window does #920 touch?
---------------------------------------------------------------------------

Mass conservation is the direct test: a gap-free ladder must sum to ~1.0.

``bracket_evals`` carries no ``current_high``, so censoring is detected from
the ladder's own shape instead. #920 zeroes every bracket *below* an
already-observed high, which leaves a contiguous run of zeros at the bottom
followed by live mass above. Counting that prefix gives both whether censoring
fired and how far up it reached -- without needing the observation.

Splitting ladders into censored and uncensored separates the two live defects,
which otherwise superimpose:

* **uncensored** ladders can only lose mass to #917
* **censored** ladders lose it to #917 *and* #920

An uncensored deficit that disappears on the deploy date, alongside a censored
deficit that persists, was the signature of "#917 fixed, #920 still live".
Post-2026-08-06 both columns read ~1.00; if the censored column ever drops
away from the uncensored one again, a renormalisation path has regressed.

---------------------------------------------------------------------------
3. Is the clock still running, and how fast is it accruing?
---------------------------------------------------------------------------

The first day on which *both* populations conserve mass is the earliest
possible start. The report names it, or says plainly that no such day exists
-- which, on a window starting at the clock date, would mean a regression.

Accrual is reported in **scoreable** station-days -- after the gate's own
exclusions -- because that is what the 300 bar counts, and it runs materially
below the raw station-day rate the health report prints.

Read-only. Writes nothing, touches no database.
"""

from __future__ import annotations

import argparse
import logging
from collections import Counter, defaultdict
from pathlib import Path

from src.scripts.bss_market_vs_model_report import (
    BRACKET_EVALS_JSONL,
    RAIL_HIGH_CENTS,
    RAIL_LOW_CENTS,
    load_bracket_eval_rows,
)
from src.scripts.daily_health_report import M3_CLEAN_DATA_CLOCK_START

log = logging.getLogger(__name__)

#: A ladder conserves mass when its probabilities sum to ~1.0. The band is
#: deliberately wider than #917's synthetic-test tolerance (0.02): production
#: ladders can be genuinely incomplete (a market not yet listed, a bracket
#: pulled mid-day), and this must not read that as a defect.
MASS_LOW = 0.90
MASS_HIGH = 1.10

#: Parser fingerprints, in degF. Real widths carry float noise from degC
#: conversion, so each is matched within a tolerance rather than exactly.
WIDTH_F_PREFIX = 1.0      # degF dash-range, pre-#917 -- the bug
WIDTH_F_FIXED = 2.0       # degF dash-range, post-#917
WIDTH_C = 1.8             # degC via _LABEL_EXACT -- never affected
WIDTH_TOL = 0.15

LADDER_KINDS = ("degF pre-#917 (1.0F)", "degF post-#917 (2.0F)",
                "degC (1.8F)", "other")

#: Compact labels for the kind-by-censoring table. The parser version is part
#: of the kind, so a ``F1.0`` row appearing after the deploy date is itself a
#: finding rather than a formatting quirk.
KIND_SHORT = (("F1.0", LADDER_KINDS[0]), ("F2.0", LADDER_KINDS[1]),
              ("degC", LADDER_KINDS[2]), ("othr", "other"))


def classify_width(width: "float | None") -> str:
    """Map a modal bracket width to the parser version that produced it."""
    if width is None:
        return "other"
    if abs(width - WIDTH_F_PREFIX) <= WIDTH_TOL:
        return LADDER_KINDS[0]
    if abs(width - WIDTH_F_FIXED) <= WIDTH_TOL:
        return LADDER_KINDS[1]
    if abs(width - WIDTH_C) <= WIDTH_TOL:
        return LADDER_KINDS[2]
    return "other"


def group_ladders(rows: "list[dict]") -> "list[dict]":
    """Collapse bracket rows into one entry per evaluated ladder.

    A ladder is one station's full set of brackets at one poll for one
    settlement day. ``bracket_evals`` is append-only and writes every bracket
    of a poll under the same ``poll_ts``, so that triple reconstructs it
    exactly -- unlike ``scan_decisions``, which upserts on
    ``(station, ticker, date)`` and therefore keeps only each market's last
    write, assembling "ladders" from brackets observed at different times.

    ``is_next_day`` is part of the key because a station can carry a same-day
    and a next-day ladder in the same poll; pooling them would sum two
    distributions and manufacture a mass excess that is not there.
    """
    buckets: "dict[tuple, list[dict]]" = defaultdict(list)
    for row in rows:
        key = (row.get("station"), row.get("ts"),
               (row.get("end_date") or "")[:10], row.get("is_next_day_flag"))
        buckets[key].append(row)

    ladders = []
    for (station, ts, end_date, is_next_day), members in buckets.items():
        ladders.append({
            "station": station, "ts": ts, "end_date": end_date,
            "is_next_day": is_next_day, "brackets": members,
            "day": (ts or "")[:10],
            **ladder_stats(members),
        })
    return ladders


def ladder_stats(brackets: "list[dict]") -> dict:
    """Mass, censoring depth and parser fingerprint for one ladder.

    ``leading_zeros`` counts the contiguous run of zero-probability brackets at
    the BOTTOM of the ladder -- #920's signature, since it zeroes everything
    below an already-observed high. A zero higher up with live mass beneath it
    is something else entirely and is deliberately not counted here.
    """
    usable = [b for b in brackets if b.get("p_yes_raw") is not None]
    if not usable:
        return {"n_brackets": len(brackets), "mass": None, "leading_zeros": 0,
                "width": None, "kind": "other", "censored": False,
                "open_bottom": False, "open_top": False, "closed_ladder": True,
                "gaps": 0, "gap_width": 0.0, "trailing_zeros": 0,
                "truncation": "none"}

    ordered = sorted(usable, key=lambda b: (b.get("bracket_low") is None,
                                            b.get("bracket_low") or 0.0))
    mass = sum(b["p_yes_raw"] for b in ordered)

    leading = 0
    for b in ordered:
        if b["p_yes_raw"] == 0.0:
            leading += 1
        else:
            break

    # Trailing zeros are the OTHER truncation, and counting only the leading
    # ones was the flaw in this tool's first reading. `envelope.py:301` returns
    # 0.0 for any bracket with `lo > max_env`, which zeroes the upper tail --
    # including the open-ended [y, 200] bracket where that tail's mass lives.
    # It is the same truncate-without-renormalise defect as #920, at the top
    # rather than the bottom, and measuring only the bottom made #920 look
    # immaterial when the two ends together are what drains the ladder.
    trailing = 0
    for b in reversed(ordered):
        if b["p_yes_raw"] == 0.0:
            trailing += 1
        else:
            break
    # An all-zero ladder would otherwise be counted at both ends at once.
    if leading + trailing > len(ordered):
        trailing = 0

    widths = [round(b["bracket_high"] - b["bracket_low"], 2)
              for b in ordered
              if b.get("bracket_high") is not None and b.get("bracket_low") is not None]
    width = Counter(widths).most_common(1)[0][0] if widths else None

    # Open-ended end brackets ("92F or above" -> [92, 200], "55F or below" ->
    # [-50, 56]) are where a ladder's tail mass lives. A ladder without them
    # cannot sum to 1.0 however correct the model is, because the market simply
    # offers nowhere for the tails to go -- so a deficit on such a ladder is
    # market structure, not a defect, and must not be read as one.
    open_bottom = any(b.get("bracket_low") is not None and b["bracket_low"] <= -40
                      for b in ordered)
    open_top = any(b.get("bracket_high") is not None and b["bracket_high"] >= 150
                   for b in ordered)

    # Gaps between consecutive brackets. A ladder built by the parser is
    # contiguous by construction -- _LABEL_LTE yields [-50, _to_f(N+1)), which
    # meets the first _LABEL_EXACT bracket exactly, and _LABEL_GTE meets the
    # last. So a gap here is not a parsing artifact: it means brackets that
    # exist in the market never reached the log, and the "ladder" being summed
    # is only part of one. That makes a mass deficit a completeness problem
    # rather than a probability problem -- a different defect with a different
    # owner, so it is measured rather than assumed either way.
    gaps, gap_width = 0, 0.0
    for a, b in zip(ordered, ordered[1:]):
        if a.get("bracket_high") is None or b.get("bracket_low") is None:
            continue
        delta = b["bracket_low"] - a["bracket_high"]
        if delta > 0.05:
            gaps += 1
            gap_width += delta

    return {
        "n_brackets": len(ordered),
        "mass": mass,
        "leading_zeros": leading,
        "open_bottom": open_bottom,
        "open_top": open_top,
        "closed_ladder": not (open_bottom or open_top),
        "gaps": gaps,
        "gap_width": gap_width,
        "trailing_zeros": trailing,
        # Which end(s) the distribution was cut at. "both" is the collapsed
        # envelope: after the daily peak `expected_additional_rise` -> 0, so
        # max_env falls onto current_high and the two shortcuts fire together,
        # leaving only the bracket straddling the observed high. That is the
        # RKSI 0.288 case, and it is a post-peak poll.
        "truncation": (
            "both" if leading and trailing
            else "bottom" if leading
            else "top" if trailing
            else "none"),
        # A ladder that is ALL zeros is not evidence of censoring depth -- it
        # is a dead ladder, and counting it as maximally censored would drag
        # the censored-population mass toward zero for the wrong reason.
        "censored": 0 < leading < len(ordered),
        "width": width,
        "kind": classify_width(width),
    }


def width_census(ladders: "list[dict]") -> "dict[str, Counter]":
    """Ladder-kind counts per day -- the #917 deployment fingerprint."""
    per_day: "dict[str, Counter]" = defaultdict(Counter)
    for lad in ladders:
        per_day[lad["day"]][lad["kind"]] += 1
    return per_day


def deploy_day(per_day: "dict[str, Counter]") -> "str | None":
    """First day on which no pre-#917 ladder appears, given one appeared before.

    ``None`` means either the fix never reached production, or the window
    contains no pre-fix day to transition from.
    """
    days = sorted(per_day)
    seen_buggy = False
    for day in days:
        if per_day[day][LADDER_KINDS[0]]:
            seen_buggy = True
        elif seen_buggy:
            return day
    return None


def mass_by_day(ladders: "list[dict]") -> "dict[str, dict]":
    """Mass-conservation summary per day, split by censoring.

    The split is the point: uncensored ladders isolate #917, censored ladders
    carry #917 and #920 together, so comparing the two columns attributes a
    deficit to one defect or the other instead of reporting their sum.
    """
    per_day: "dict[str, dict]" = defaultdict(
        lambda: {"censored": [], "uncensored": []})
    for lad in ladders:
        if lad["mass"] is None:
            continue
        per_day[lad["day"]]["censored" if lad["censored"] else "uncensored"].append(
            lad["mass"])

    out = {}
    for day, groups in per_day.items():
        row = {}
        for name, masses in groups.items():
            if not masses:
                row[name] = None
                continue
            row[name] = {
                "n": len(masses),
                "mean": sum(masses) / len(masses),
                "deficient": sum(1 for m in masses if m < MASS_LOW),
                "excessive": sum(1 for m in masses if m > MASS_HIGH),
            }
        out[day] = row
    return out


def conserves(stats: "dict | None") -> bool:
    """Does a day's population conserve mass? Absent data is not a pass."""
    if not stats or not stats["n"]:
        return False
    return MASS_LOW <= stats["mean"] <= MASS_HIGH and stats["deficient"] == 0


def earliest_clean_day(per_day: "dict[str, dict]") -> "str | None":
    """First day where BOTH populations conserve mass.

    Both, because a day where only uncensored ladders are healthy is a day
    a truncation is being applied without renormalisation -- #920's signature,
    and the shape every day before 2026-08-06 had.
    """
    for day in sorted(per_day):
        row = per_day[day]
        if conserves(row.get("censored")) and conserves(row.get("uncensored")):
            return day
    return None


def scoreable_station_days(rows: "list[dict]") -> "dict[str, int]":
    """Station-days per day that survive the gate's own exclusions.

    The 300 bar counts these, not raw station-days -- the health report's
    ``362/300`` counts every station-day whether or not it carries a scoreable
    row (see #932). Mirrors ``apply_exclusions``: drop exact-zero model
    probabilities and rail-clipped market prices.
    """
    per_day: "dict[str, set]" = defaultdict(set)
    for row in rows:
        p = row.get("p_yes_raw")
        yes_ask, no_ask = row.get("yes_ask"), row.get("no_ask")
        if p is None or p == 0.0 or yes_ask is None or no_ask is None:
            continue
        if (yes_ask <= RAIL_LOW_CENTS or yes_ask >= RAIL_HIGH_CENTS
                or no_ask <= RAIL_LOW_CENTS or no_ask >= RAIL_HIGH_CENTS):
            continue
        day = (row.get("ts") or "")[:10]
        if day and row.get("station"):
            per_day[day].add((row["station"], (row.get("end_date") or "")[:10]))
    return {d: len(v) for d, v in per_day.items()}


def _fmt(v, spec=".3f"):
    return "  --  " if v is None else format(v, spec)


def _cell(stats: "dict | None") -> str:
    if not stats or not stats["n"]:
        return "     --      "
    flag = "ok " if conserves(stats) else "BAD"
    return f"{stats['mean']:.3f} n={stats['n']:<4d} def={stats['deficient']:<4d} {flag}"


def build_report(rows: "list[dict]", since: str) -> str:
    ladders = group_ladders(rows)
    ladders = [lad for lad in ladders if lad["day"] >= since]
    if not ladders:
        return f"No ladders in bracket_evals on or after {since}."

    census = width_census(ladders)
    masses = mass_by_day(ladders)
    scoreable = scoreable_station_days(
        [r for r in rows if (r.get("ts") or "")[:10] >= since])
    deployed = deploy_day(census)
    clean = earliest_clean_day(masses)

    out = [
        "=" * 78,
        f"M3 WINDOW DIAGNOSTICS -- ladders from {since}",
        "=" * 78,
        "",
        "1. PARSER VERSION BY DAY  (#917 deployment fingerprint)",
        "-" * 78,
        "   1.0F-wide ladders are the pre-#917 bug. The day they stop is the day",
        "   the fix reached production.",
        "",
        f"   {'day':<12}{'preF 1.0':>10}{'postF 2.0':>11}{'degC 1.8':>10}{'other':>8}",
    ]
    for day in sorted(census):
        c = census[day]
        out.append(f"   {day:<12}{c[LADDER_KINDS[0]]:>10}{c[LADDER_KINDS[1]]:>11}"
                   f"{c[LADDER_KINDS[2]]:>10}{c['other']:>8}")
    out.append("")
    if deployed:
        out.append(f"   => #917 reached production on {deployed}.")
    elif any(census[d][LADDER_KINDS[0]] for d in census):
        out.append("   => #917 has NOT reached production -- 1.0F ladders are still")
        out.append("      being written. Every day above is pre-fix. Restart")
        out.append("      meteoedge-run before reading anything else here.")
    else:
        out.append("   => No pre-#917 ladders in range; nothing to date.")

    out += [
        "",
        "2. MASS CONSERVATION BY DAY  (a gap-free ladder must sum to ~1.0)",
        "-" * 78,
        "   uncensored ladders can only lose mass to #917.",
        "   censored ladders lose it to #917 AND #920 -- so a deficit that",
        "   persists only in the censored column is #920, still live.",
        "",
        f"   {'day':<12}{'uncensored':<30}{'censored':<30}",
    ]
    for day in sorted(masses):
        row = masses[day]
        out.append(f"   {day:<12}{_cell(row.get('uncensored')):<30}"
                   f"{_cell(row.get('censored')):<30}")

    out += [
        "",
        "2b. MASS BY LADDER KIND  (the mix that a pooled mean hides)",
        "-" * 78,
        "   #917 only ever touched degF dash-range ladders, and those are a",
        "   minority of the population -- ~5 US stations against ~25 degC cities.",
        "   A large degF improvement therefore barely moves a pooled mean, and",
        "   #920's contribution is only readable by comparing censored to",
        "   uncensored WITHIN a kind, where the mix is held constant.",
        "",
        f"   {'kind':<8}{'uncensored':<20}{'censored':<20}{'ladders':>8}",
    ]
    for kind_label, kind in KIND_SHORT:
        sub = [lad for lad in ladders if lad["kind"] == kind]
        if not sub:
            continue
        out.append(f"   {kind_label:<8}{_period_stats(sub, False):<20}"
                   f"{_period_stats(sub, True):<20}{len(sub):>8}")
    out.append("")
    out.append("   A kind whose censored and uncensored columns agree is a kind")
    out.append("   #920 is NOT the main mass sink for. A deficit present in both")
    out.append("   is something else -- incomplete ladders and the upper-tail")
    out.append("   envelope cut are the first two candidates, and neither is #920.")

    out += [
        "",
        "3. SCOREABLE STATION-DAYS  (what the 300 bar actually counts)",
        "-" * 78,
        "   After the gate's exclusions. The health report's raw count (#932)",
        "   runs well above this.",
        "",
        f"   {'day':<12}{'scoreable':>11}",
    ]
    total = 0
    for day in sorted(scoreable):
        total += scoreable[day]
        out.append(f"   {day:<12}{scoreable[day]:>11}")
    n_days = len(scoreable) or 1
    out.append("")
    out.append(f"   mean {total / n_days:.1f} scoreable station-days/day "
               f"over {n_days} day(s)")

    out += ["", "=" * 78, "VERDICT", "=" * 78, ""]
    if clean:
        out += [
            f"   Earliest day where BOTH populations conserve mass: {clean}",
            "",
            "   #917 and #920 are both merged and deployed, so on a window at or",
            "   after 2026-08-06 this is the clock still running clean. Mass",
            "   conservation is necessary, not sufficient -- it says the plumbing",
            "   is intact, not that the model is calibrated.",
        ]
    else:
        out += [
            "   NO day in range conserves mass in both populations.",
            "   The clean-data clock CANNOT start yet, and the M3 gate must not run.",
        ]
        bad = [d for d in sorted(masses)
               if not conserves(masses[d].get("censored"))
               and conserves(masses[d].get("uncensored"))]
        if bad:
            out += [
                "",
                f"   {len(bad)} day(s) conserve mass when uncensored but not when",
                "   censored -- #920's signature. Expected before 2026-08-06;",
                "   after it, a renormalisation regression.",
            ]
    rate = total / n_days
    if rate > 0:
        out += ["",
                f"   At {rate:.1f} scoreable station-days/day, 300 takes "
                f"~{300 / rate:.0f} days once the clock starts."]
    out.append("")
    return "\n".join(out)


def _period_stats(ladders: "list[dict]", censored: bool) -> str:
    masses = [lad["mass"] for lad in ladders
              if lad["mass"] is not None and lad["censored"] is censored]
    if not masses:
        return "    --      "
    bad = sum(1 for m in masses if not MASS_LOW <= m <= MASS_HIGH)
    return f"{sum(masses) / len(masses):.2f} ({bad}/{len(masses)} bad)"


def brief_report(rows: "list[dict]", since: str) -> str:
    """The same verdict in ~10 lines, for reading or relaying off a phone.

    Compresses the per-day tables by collapsing them around the #917
    transition: every day is either before it or after it, and within a period
    the censored/uncensored split is what carries the information. Nothing is
    decided differently here -- it is the full report's arithmetic, printed
    smaller.
    """
    ladders = [lad for lad in group_ladders(rows) if lad["day"] >= since]
    if not ladders:
        return f"M3 WINDOW: no ladders on or after {since}."

    census = width_census(ladders)
    deployed = deploy_day(census)
    clean = earliest_clean_day(mass_by_day(ladders))
    scoreable = scoreable_station_days(
        [r for r in rows if (r.get("ts") or "")[:10] >= since])
    days = sorted({lad["day"] for lad in ladders})
    rate = sum(scoreable.values()) / max(1, len(scoreable))

    out = [f"M3 WINDOW  {days[0]}..{days[-1]}"]
    if deployed:
        out.append(f"#917 live from {deployed}")
        periods = [("pre ", [x for x in ladders if x["day"] < deployed]),
                   ("post", [x for x in ladders if x["day"] >= deployed])]
    elif any(census[d][LADDER_KINDS[0]] for d in census):
        out.append("#917 NOT in production -- 1.0F ladders still being written")
        periods = [("all ", ladders)]
    else:
        out.append("#917 transition not visible in range")
        periods = [("all ", ladders)]

    # Split by ladder KIND as well as censoring. Blending them hides both
    # effects: #917 only ever touched degF dash-range ladders, and those are a
    # minority of the population (~5 US stations against ~25 degC cities), so a
    # large degF improvement barely moves a pooled mean. Comparing censored to
    # uncensored *within* a kind controls for that mix, which is the only way
    # to read #920's contribution off this data.
    out.append(f"{'kind':<7}{'per':<5}{'uncensored':<16}{'censored':<16}")
    for kind_label, kind in KIND_SHORT:
        for label, group in periods:
            sub = [x for x in group if x["kind"] == kind]
            if not sub:
                continue
            out.append(f"{kind_label:<7}{label:<5}{_period_stats(sub, False):<16}"
                       f"{_period_stats(sub, True):<16}")

    # Coverage, per kind. If the low-mass kinds are exactly the ones without
    # open-ended end brackets, their deficit is the market's shape rather than
    # the model's error -- and gating M3 on it would be gating on the wrong
    # thing entirely.
    # Mass by WHICH END the ladder was truncated at. This is the reading the
    # leading-zeros-only view could not produce: if "none" conserves mass and
    # "top"/"both" do not, the deficit is the envelope-ceiling cut at
    # envelope.py:301 -- #920's defect at the other end of the distribution.
    out.append(f"{'kind':<7}{'cut':<7}{'n':<7}{'mass':<7}")
    for kind_label, kind in KIND_SHORT:
        sub = [x for x in ladders if x["kind"] == kind and x["mass"] is not None]
        if not sub:
            continue
        for cut in ("none", "bottom", "top", "both"):
            grp = [x for x in sub if x["truncation"] == cut]
            if not grp:
                continue
            out.append(f"{kind_label:<7}{cut:<7}{len(grp):<7}"
                       f"{sum(x['mass'] for x in grp) / len(grp):<7.2f}")

    out.append(f"scoreable {rate:.1f}/day")
    if clean:
        out.append(f"VERDICT: mass conserved from {clean} (clock start 2026-08-06)")
    else:
        out.append("VERDICT: no clean day -- gate must not run")
        if rate > 0:
            out.append(f"         then ~{300 / rate:.0f} days to 300")
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bracket-evals", type=Path, default=BRACKET_EVALS_JSONL)
    ap.add_argument("--since", default=M3_CLEAN_DATA_CLOCK_START,
                    help="Window start (default: the live clean-data clock, "
                         f"{M3_CLEAN_DATA_CLOCK_START}). Pass an earlier date to "
                         "re-read the contaminated window.")
    ap.add_argument("--brief", action="store_true",
                    help="~10 lines instead of the full tables -- same verdict, "
                         "sized to relay from a phone")
    return ap


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)

    rows = load_bracket_eval_rows(args.bracket_evals)
    if not rows:
        log.error("[m3diag] no rows in %s", args.bracket_evals)
        return 1
    print(brief_report(rows, args.since) if args.brief
          else build_report(rows, args.since))
    return 0


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    raise SystemExit(main())

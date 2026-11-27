"""Pre-registered forward test H4: model-free favourite-longshot rule
(issue #1545).

**Pre-registration.** Issue #1545's body is the pre-registration for this
rule, written 2026-11-27 before any evaluation data existed. The rule,
population, metric and decision thresholds below must match that issue body
exactly -- amend this module only in step with an "Amended <date> (PM)" line
on the issue itself.

**Rule under test.** Universe: every ``direction='high'`` bracket in
``logs/snapshots.*.jsonl[.gz]`` (same-day and next-day markets alike --
``scanner.py`` has only ever written ``direction='high'`` rows to this log,
so a missing field is treated as 'high' too). Entry: the FIRST poll
(earliest ``ts``) where either side's ask is in **[80c, 97c]** (both
inclusive) -- buy that side at that ask, one entry per market (ticker),
ignoring every later poll.

**Payoff per contract:** ``win - ask/100 - fee``, where
``fee = fee_rate * p * (1 - p)``, ``p = ask/100``. ``fee_rate`` defaults to
0.01 -- Polymarket's published 1% weather taker rate (issue #1544). This is
a deliberately SEPARATE, explicitly-parameterised fee model from
``src.strategy.fee.estimate_fee_cents`` (pinned at the live trading engine's
own ``WEATHER_FEE_RATE=0.05%`` constant): #1545's pre-registration specifies
its own rate and this forward test must score against exactly that rate, not
silently inherit whatever the live engine's constant happens to be.
``ROI = payoff / p``.

**Evaluation population.** Markets whose first qualifying poll (within the
loaded window) is at or after ``--since`` (default ``ISSUE_1545_T0``,
2026-11-24T00:00:00Z -- the pre-registration's own T0, and the moment the
Sep-Oct/Nov analysis stopped looking at data). Loading only starts at
``--since`` by design -- a market's whole polling window is at most a day or
two (same-day/next-day settlement), so this one flag doubles as both the
load window and the population cutoff, matching the issue's own flag list.

**Outcome resolution.** The ``market_resolutions`` table (``market`` column
= condition id, same string as a snapshot row's ``ticker``; ``resolved_yes``
column), falling back to ``logs/gamma_resolution_cache.v2.json`` via
``resolve_bracket_outcomes.load_gamma_cache`` (imported, unchanged -- same
cache file and format as #850/#860's resolver, one fewer place that needs to
independently agree on it). Analysis only: this module never writes to
``market_resolutions``, ``trades``, or any other live table.

**Clustering.** The bootstrap resamples CLUSTERS of ``(station,
settlement_date)`` -- never individual trades -- because every bracket
sharing a station-day shares the same observed outcome and is therefore not
an independent draw (same effective-sample-size reasoning
``resolve_bracket_outcomes`` documents).

**Known unknowns (see the issue).** Order-book depth isn't logged, so
whether a EUR5 stake actually fills at the top-of-book ask is unverified --
this module scores the rule as quoted, not as filled.

Usage (read-only; CLI)::

    python -m src.scripts.favourite_rule_eval
    python -m src.scripts.favourite_rule_eval --since 2026-11-24T00:00:00+00:00 \\
        --until 2026-12-15T00:00:00+00:00 --fee-rate 0.01

``section(db, today)`` is the ``(db, today)``-signature adapter
``daily_health_report.py`` imports directly (mirrors
``src.data.log_error_classes.section``) -- it ignores both arguments (opens
its own read-only connection, like every other offline report in this
package) and prints a short DESCRIPTIVE status: n toward the 2,000-market
checkpoint, mean ROI, and the clustered 95% CI. Descriptive only -- it never
applies #1545's own LIVE-PILOT-ELIGIBLE/DROP/INCONCLUSIVE decision rule
(that needs a bootstrap read by a human, same discipline as
#1507/#1452/#1453).
"""
from __future__ import annotations

import argparse
import logging
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup -- make `src` importable from this script's location.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
if str(_HERE.parents[1]) not in sys.path:
    sys.path.insert(0, str(_HERE.parents[1]))

from dateutil import parser as dtparse  # noqa: E402

from src.config import SNAPSHOTS_JSONL  # noqa: E402
from src.utils.log_rotation import iter_rotated_jsonl  # noqa: E402
from src.scripts.resolve_bracket_outcomes import (  # noqa: E402
    DEFAULT_DB_PATH,
    DEFAULT_GAMMA_CACHE_PATH,
    _connect_ro,
    load_gamma_cache,
)

log = logging.getLogger(__name__)

#: T0 from #1545's pre-registration -- start of the evaluation population.
ISSUE_1545_T0 = "2026-11-24T00:00:00+00:00"

#: Pre-registered checkpoints: an interim read at n=2,000 evaluated markets
#: (about 3 weeks), continuing to n=4,000 if INCONCLUSIVE.
ISSUE_1545_CHECKPOINT_N = 2000
ISSUE_1545_FINAL_N = 4000

#: Entry band, in integer cents -- matches the snapshot's own
#: yes_ask/no_ask unit (clamped integer cents, see src.model.envelope.Bracket).
ENTRY_LO_CENTS = 80
ENTRY_HI_CENTS = 97

#: Published Polymarket weather taker fee rate used by this pre-registration,
#: as a fraction (issue #1544). Deliberately not src.strategy.fee's constant
#: -- see module docstring.
DEFAULT_FEE_RATE = 0.01

#: Open-ended-bracket sentinel convention (mirrors
#: src.dashboard.api._format_bracket_range): bounds outside [-49, 199] mean
#: "no lower/upper bound".
OPEN_ENDED_LOW_SENTINEL = -49.0
OPEN_ENDED_HIGH_SENTINEL = 199.0


# ---------------------------------------------------------------------------
# Loading + entry selection (pure, unit-tested)
# ---------------------------------------------------------------------------

def _parse_ts(ts: "str | None") -> "datetime | None":
    """Parse a timestamp into an aware UTC ``datetime``. None if unparseable
    -- callers must drop a row they can't date, never guess."""
    if not ts:
        return None
    try:
        dt = dtparse.parse(str(ts))
    except (ValueError, OverflowError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_snapshots(
    since: str = ISSUE_1545_T0,
    until: "str | None" = None,
    base: Path = SNAPSHOTS_JSONL,
) -> "list[dict]":
    """Stream every ``direction='high'`` snapshot row with ``ts`` in
    ``[since, until)`` across the rotated ``.jsonl``/``.jsonl.gz`` archive.

    A row with a missing/unparseable ``ts`` is dropped. ``direction``
    defaults to 'high' for rows that predate issue #876 -- every row
    ``scanner.py`` has ever written to this log carries ``direction='high'``,
    there has never been a 'low' row here.

    Self-gating like ``resolve_bracket_outcomes``: a missing ``logs/``
    directory logs an honest message and returns an empty list rather than
    raising.
    """
    since_dt = _parse_ts(since)
    until_dt = _parse_ts(until) if until else None
    out: "list[dict]" = []
    if not base.parent.exists():
        log.info("[favourite_rule_eval] %s missing -- no local logs/, skipping", base.parent)
        return out
    for row in iter_rotated_jsonl(base):
        if (row.get("direction") or "high") != "high":
            continue
        ts = _parse_ts(row.get("ts"))
        if ts is None:
            continue
        if since_dt is not None and ts < since_dt:
            continue
        if until_dt is not None and ts >= until_dt:
            continue
        out.append(row)
    return out


def select_first_qualifying_entries(
    snapshots: "list[dict]",
    lo_cents: int = ENTRY_LO_CENTS,
    hi_cents: int = ENTRY_HI_CENTS,
) -> "list[dict]":
    """One entry per ticker: the FIRST (earliest ``ts``) poll where either
    side's ask is in ``[lo_cents, hi_cents]`` (both inclusive).

    Returns one dict per qualifying ticker::

        {ticker, station, date, side ('YES'|'NO'), ask_cents,
         is_next_day (bool), bracket_low, bracket_high, ts}

    A ticker with no qualifying poll in *snapshots* is simply absent. Rows
    missing ``ticker``/``station``/``date`` are dropped (can't be keyed or
    clustered). Geometrically, ``yes_ask + no_ask`` sums to ~100 so both
    sides qualifying on the same poll should not happen -- handled
    defensively (not assumed) by keeping the higher of the two asks, the
    more extreme "favourite" the rule is about.
    """
    best: "dict[str, dict]" = {}
    best_dt: "dict[str, datetime]" = {}
    for row in snapshots:
        ticker = row.get("ticker")
        station = row.get("station")
        settlement_date = row.get("date")
        if not ticker or not station or not settlement_date:
            continue

        yes_ask = row.get("yes_ask")
        no_ask = row.get("no_ask")
        candidates = []
        if isinstance(yes_ask, (int, float)) and lo_cents <= yes_ask <= hi_cents:
            candidates.append(("YES", yes_ask))
        if isinstance(no_ask, (int, float)) and lo_cents <= no_ask <= hi_cents:
            candidates.append(("NO", no_ask))
        if not candidates:
            continue

        ts_dt = _parse_ts(row.get("ts"))
        if ts_dt is None:
            continue
        if ticker in best_dt and ts_dt >= best_dt[ticker]:
            continue  # already have an earlier (or equal) qualifying poll

        side, ask_cents = max(candidates, key=lambda c: c[1])
        best_dt[ticker] = ts_dt
        best[ticker] = {
            "ticker": ticker, "station": station, "date": settlement_date,
            "side": side, "ask_cents": ask_cents,
            "is_next_day": bool(row.get("is_next_day") or 0),
            "bracket_low": row.get("bracket_low"), "bracket_high": row.get("bracket_high"),
            "ts": row.get("ts"),
        }
    return list(best.values())


# ---------------------------------------------------------------------------
# Outcome resolution
# ---------------------------------------------------------------------------

def load_outcomes(
    tickers: "set[str]",
    db_path: "Path | None" = DEFAULT_DB_PATH,
    gamma_cache_path: "Path | None" = DEFAULT_GAMMA_CACHE_PATH,
) -> "dict[str, bool]":
    """Return ``{ticker: resolved_yes}`` from ``market_resolutions``, falling
    back to the Gamma resolution cache for anything not in the DB.

    Read-only: opens *db_path* via ``resolve_bracket_outcomes._connect_ro``
    (never creates the file). A ticker resolved by neither source is simply
    absent -- not yet settled, not a guess.
    """
    out: "dict[str, bool]" = {}
    if tickers and db_path is not None:
        con = _connect_ro(Path(db_path))
        if con is not None:
            try:
                placeholders = ",".join("?" * len(tickers))
                cur = con.execute(
                    f"SELECT market, resolved_yes FROM market_resolutions "
                    f"WHERE market IN ({placeholders})",
                    tuple(tickers),
                )
                for market, resolved_yes in cur.fetchall():
                    out[market] = bool(resolved_yes)
            except sqlite3.OperationalError as exc:
                log.warning("[favourite_rule_eval] market_resolutions read failed: %s", exc)
            finally:
                con.close()

    missing = tickers - out.keys()
    if missing:
        cache = load_gamma_cache(gamma_cache_path)
        for t in missing:
            if t in cache:
                out[t] = cache[t]
    return out


# ---------------------------------------------------------------------------
# Payoff / fee / splits (pure, unit-tested)
# ---------------------------------------------------------------------------

def fee_fraction(p: float, fee_rate: float = DEFAULT_FEE_RATE) -> float:
    """``fee = fee_rate * p * (1 - p)``, as a fraction of $1 notional. See
    module docstring for why this is not ``src.strategy.fee.estimate_fee_cents``."""
    return fee_rate * p * (1.0 - p)


def is_open_ended(bracket_low, bracket_high) -> "bool | None":
    """True iff either bound is the open-ended sentinel (see
    ``OPEN_ENDED_LOW_SENTINEL``/``OPEN_ENDED_HIGH_SENTINEL``). None if a
    bound is missing/non-numeric -- callers must treat that as "unknown",
    not "interior"."""
    if bracket_low is None or bracket_high is None:
        return None
    try:
        lo, hi = float(bracket_low), float(bracket_high)
    except (TypeError, ValueError):
        return None
    return lo <= OPEN_ENDED_LOW_SENTINEL or hi >= OPEN_ENDED_HIGH_SENTINEL


def ask_band(ask_cents: int) -> str:
    """80-84 / 85-89 / 90-97 split -- issue #1545's own band breakdown."""
    if ask_cents <= 84:
        return "80-84"
    if ask_cents <= 89:
        return "85-89"
    return "90-97"


def build_trade_rows(
    entries: "list[dict]",
    outcomes: "dict[str, bool]",
    fee_rate: float = DEFAULT_FEE_RATE,
) -> "list[dict]":
    """Attach payoff/ROI/cluster key to every entry with a KNOWN outcome.

    An entry whose ticker is not yet in *outcomes* is dropped -- not yet
    settled, not scored as a loss or a win.
    """
    rows = []
    for e in entries:
        resolved_yes = outcomes.get(e["ticker"])
        if resolved_yes is None:
            continue
        won = resolved_yes if e["side"] == "YES" else (not resolved_yes)
        p = e["ask_cents"] / 100.0
        fee = fee_fraction(p, fee_rate)
        payoff = (1.0 if won else 0.0) - p - fee
        roi = payoff / p if p else None
        rows.append({
            **e,
            "resolved_yes": resolved_yes,
            "won": won,
            "fee": fee,
            "payoff": payoff,
            "roi": roi,
            "ask_band": ask_band(e["ask_cents"]),
            "open_ended": is_open_ended(e.get("bracket_low"), e.get("bracket_high")),
            "cluster_key": (e["station"], e["date"]),
        })
    return rows


def clustered_bootstrap(
    rows: "list[dict]", n_resamples: int = 10_000, seed: int = 1545,
) -> dict:
    """Mean ROI + a clustered-bootstrap 95% CI and 5th percentile.

    Resamples CLUSTERS of ``cluster_key`` (station, settlement_date) with
    replacement -- not individual trades -- since brackets sharing a
    station-day are not independent draws. Returns
    ``{"n", "mean_roi", "ci_lo", "ci_hi", "p5"}``; all stats None when
    *rows* is empty.
    """
    rois = [r["roi"] for r in rows if r["roi"] is not None]
    n = len(rois)
    if n == 0:
        return {"n": 0, "mean_roi": None, "ci_lo": None, "ci_hi": None, "p5": None}
    mean_roi = sum(rois) / n

    clusters: "dict[tuple, list[float]]" = defaultdict(list)
    for r in rows:
        if r["roi"] is not None:
            clusters[r["cluster_key"]].append(r["roi"])
    cluster_list = list(clusters.values())
    n_clusters = len(cluster_list)
    if n_clusters == 0:
        return {"n": n, "mean_roi": mean_roi, "ci_lo": None, "ci_hi": None, "p5": None}

    rng = random.Random(seed)
    means = []
    for _ in range(n_resamples):
        sample_rois: "list[float]" = []
        for _ in range(n_clusters):
            sample_rois.extend(cluster_list[rng.randrange(n_clusters)])
        means.append(sum(sample_rois) / len(sample_rois) if sample_rois else 0.0)
    means.sort()
    ci_lo = means[int(0.025 * n_resamples)]
    ci_hi = means[min(int(0.975 * n_resamples), n_resamples - 1)]
    p5 = means[int(0.05 * n_resamples)]
    return {"n": n, "mean_roi": mean_roi, "ci_lo": ci_lo, "ci_hi": ci_hi, "p5": p5}


def build_splits(rows: "list[dict]") -> "dict[str, dict]":
    """Per-split n/win_rate/mean_roi: side, ask band, same-day vs next-day,
    open-ended vs interior (issue #1545's own split list)."""

    def _summary(subset: "list[dict]") -> dict:
        n = len(subset)
        if not n:
            return {"n": 0, "win_rate": None, "mean_roi": None}
        wins = sum(1 for r in subset if r["won"])
        rois = [r["roi"] for r in subset if r["roi"] is not None]
        return {
            "n": n,
            "win_rate": wins / n,
            "mean_roi": (sum(rois) / len(rois)) if rois else None,
        }

    out: "dict[str, dict]" = {}
    for side in ("YES", "NO"):
        out[f"side={side}"] = _summary([r for r in rows if r["side"] == side])
    for band in ("80-84", "85-89", "90-97"):
        out[f"band={band}"] = _summary([r for r in rows if r["ask_band"] == band])
    out["same_day"] = _summary([r for r in rows if not r["is_next_day"]])
    out["next_day"] = _summary([r for r in rows if r["is_next_day"]])
    out["open_ended"] = _summary([r for r in rows if r["open_ended"] is True])
    out["interior"] = _summary([r for r in rows if r["open_ended"] is False])
    return out


# ---------------------------------------------------------------------------
# End-to-end evaluation
# ---------------------------------------------------------------------------

def evaluate(
    since: str = ISSUE_1545_T0,
    until: "str | None" = None,
    fee_rate: float = DEFAULT_FEE_RATE,
    db_path: "Path | None" = DEFAULT_DB_PATH,
    snapshot_base: Path = SNAPSHOTS_JSONL,
    gamma_cache_path: "Path | None" = DEFAULT_GAMMA_CACHE_PATH,
    n_resamples: int = 10_000,
) -> dict:
    """Load -> select entries -> resolve outcomes -> clustered bootstrap.

    Returns a dict with ``n_entries`` (qualifying markets found, including
    ones still awaiting settlement), ``n_resolved``/``mean_roi``/``ci_lo``/
    ``ci_hi``/``p5`` (the bootstrap, see ``clustered_bootstrap``), ``n_days``,
    ``markets_per_day``, ``eur_per_day_at_5`` (at a EUR5 stake), ``splits``,
    and ``checkpoint_n``.
    """
    snapshots = load_snapshots(since=since, until=until, base=snapshot_base)
    entries = select_first_qualifying_entries(snapshots)
    tickers = {e["ticker"] for e in entries}
    outcomes = load_outcomes(tickers, db_path=db_path, gamma_cache_path=gamma_cache_path)
    rows = build_trade_rows(entries, outcomes, fee_rate=fee_rate)

    boot = clustered_bootstrap(rows, n_resamples=n_resamples)
    splits = build_splits(rows)

    n_days = len({r["date"] for r in rows})
    markets_per_day = (boot["n"] / n_days) if n_days else None
    eur_per_day = (
        boot["mean_roi"] * 5.0 * markets_per_day
        if boot["mean_roi"] is not None and markets_per_day is not None
        else None
    )

    return {
        "since": since, "until": until, "fee_rate": fee_rate,
        "n_entries": len(entries), "n_resolved": boot["n"], "n_days": n_days,
        "markets_per_day": markets_per_day, "eur_per_day_at_5": eur_per_day,
        "mean_roi": boot["mean_roi"], "ci_lo": boot["ci_lo"], "ci_hi": boot["ci_hi"],
        "p5": boot["p5"], "splits": splits, "checkpoint_n": ISSUE_1545_CHECKPOINT_N,
    }


def _fmt_pct(x: "float | None") -> str:
    return f"{x * 100:+.2f}%" if x is not None else "n/a"


def format_report(result: dict) -> str:
    """Render ``evaluate()``'s result as the CLI's plain-text report."""
    header = f"H4 favourite-longshot rule -- since {result['since']}"
    if result.get("until"):
        header += f" until {result['until']}"
    lines = [
        header, "-" * 60,
        f"n entries (qualifying, incl. unresolved): {result['n_entries']}",
        f"n resolved (scored): {result['n_resolved']} "
        f"(checkpoint {result['checkpoint_n']})",
    ]
    if result["mean_roi"] is None:
        lines.append("  no resolved trades yet")
        return "\n".join(lines)

    lines.append(
        f"mean ROI: {_fmt_pct(result['mean_roi'])}  "
        f"95% CI [{_fmt_pct(result['ci_lo'])}, {_fmt_pct(result['ci_hi'])}]  "
        f"p5 {_fmt_pct(result['p5'])}"
    )
    if result["markets_per_day"] is not None:
        lines.append(
            f"markets/day: {result['markets_per_day']:.1f}  "
            f"EUR/day @ EUR5 stake: {result['eur_per_day_at_5']:+.2f}"
        )
    lines.append("")
    lines.append("Splits:")
    for name, s in result["splits"].items():
        if not s["n"]:
            continue
        wr = f"{s['win_rate'] * 100:.1f}%" if s["win_rate"] is not None else "n/a"
        roi = _fmt_pct(s["mean_roi"])
        lines.append(f"  {name}: n={s['n']} win%={wr} meanROI={roi}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# daily_health_report.py adapter
# ---------------------------------------------------------------------------

def section(db, today) -> "tuple[list[str], dict]":
    """Report-section adapter (``(db, today)`` builder signature) --
    ``daily_health_report.py`` imports and calls this directly (mirrors
    ``src.data.log_error_classes.section``). Ignores both arguments: opens
    its own read-only DB connection, like every other offline report in this
    package.

    Descriptive only -- never applies #1545's own decision rule. A smaller
    ``n_resamples`` than the CLI default keeps this cheap enough for a
    report meant to run in seconds even once the population has grown toward
    the 2,000/4,000-market checkpoints.
    """
    t0_date = ISSUE_1545_T0.split("T")[0]
    lines = [f"H4 Favourite Rule Forward Test (since {t0_date})", "-" * 50]
    try:
        result = evaluate(n_resamples=2000)
        marker = (" [CHECKPOINT REACHED]"
                  if result["n_resolved"] >= ISSUE_1545_CHECKPOINT_N else "")
        lines.append(
            f"  n={result['n_resolved']}/{ISSUE_1545_CHECKPOINT_N} toward "
            f"the checkpoint ({result['n_entries']} entries found, some may "
            f"still be awaiting settlement){marker}"
        )
        if result["mean_roi"] is None:
            lines.append("  No resolved H4 entries yet.")
        else:
            lines.append(
                f"  mean ROI {_fmt_pct(result['mean_roi'])}, "
                f"95% CI [{_fmt_pct(result['ci_lo'])}, {_fmt_pct(result['ci_hi'])}], "
                f"p5 {_fmt_pct(result['p5'])}"
            )
    except Exception:
        log.exception("[favourite_rule_eval] section build failed")
        lines.append("  (unavailable -- query error)")
    lines.append("")
    return lines, {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: "list[str] | None" = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default=ISSUE_1545_T0,
                    help="earliest snapshot ts to load (ISO8601, default T0)")
    ap.add_argument("--until", default=None, help="exclusive upper bound (ISO8601)")
    ap.add_argument("--fee-rate", type=float, default=DEFAULT_FEE_RATE,
                    help="Polymarket weather taker rate as a fraction (default 0.01)")
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--n-resamples", type=int, default=10_000)
    args = ap.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"{db_path} not found -- market_resolutions lookups will "
              f"fall back to the Gamma cache only")
        db_path = None

    result = evaluate(
        since=args.since, until=args.until, fee_rate=args.fee_rate,
        db_path=db_path, n_resamples=args.n_resamples,
    )
    print(format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

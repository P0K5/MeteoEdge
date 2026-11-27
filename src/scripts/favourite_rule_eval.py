"""Pre-registered forward test H4: model-free favourite-longshot rule
(issue #1545).

**Pre-registration.** Issue #1545's body is the pre-registration for this
rule, written 2026-11-27 before any evaluation data existed. The rule,
population, metric and decision thresholds below must match that issue body
exactly -- amend this module only in step with an "Amended <date> (PM)" line
on the issue itself. "Amended 2026-11-27 (PM)" corrected the issue's own
backtest table (an earlier sample-builder had checked the rule only once per
UTC hour, not at every poll) and declared a secondary rule, H4-C -- see
"Entry rules A and C" below.

**Entry rules A and C.** Universe for both: every ``direction='high'``
bracket in ``logs/snapshots.*.jsonl[.gz]`` (same-day and next-day markets
alike -- ``scanner.py`` has only ever written ``direction='high'`` rows to
this log, so a missing field is treated as 'high' too).

- **A (registered, primary).** Entry: the FIRST poll (earliest ``ts``,
  among EVERY poll) where either side's ask is in **[80c, 97c]** (both
  inclusive) -- buy that side at that ask, one entry per market (ticker),
  ignoring every later poll.
- **C (declared 2026-11-27, secondary).** Identical to A, except only each
  market's FIRST poll within each UTC hour (keyed on ``ts[:13]``) is
  eligible to be checked at all -- the entry is taken at the first such
  TOP-OF-HOUR poll where either ask is in [80c, 97c]. C was found while
  debugging A's backtest (flickering/stale top-of-book quotes inflated A's
  apparent edge in a 2026-11-27 re-score), so it carries selection risk and
  is reported alongside A, never in place of it, with its own independent
  LIVE-PILOT-ELIGIBLE/DROP/INCONCLUSIVE verdict on post-T0 data only.
  ``select_first_qualifying_entries(..., hourly=True)`` implements C;
  ``hourly=False`` (default) is A. ``--entry-rule {every_poll,hourly,both}``
  (default ``both``) selects which rule(s) a given run reports.

**Payoff per contract (both rules):** ``win - ask/100 - fee``, where
``fee = fee_rate * p * (1 - p)``, ``p = ask/100``. ``fee_rate`` defaults to
0.01 -- Polymarket's published 1% weather taker rate (issue #1544, merged in
#1547: ``src.strategy.fee.WEATHER_FEE_RATE`` is 1% too as of that fix). This
module keeps its OWN explicit ``fee_rate`` parameter rather than importing
``src.strategy.fee.estimate_fee_cents`` directly: #1545 is a pre-registered
forward test and must score against the exact rate specified when it was
registered, not whatever the live trading engine's own fee constant happens
to be on any later day it is run -- the two constants agreeing today (both
0.01) is coincidental, not a coupling, and a future change to one must not
silently change the other. ``ROI = payoff / p``.

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
package) and prints a short DESCRIPTIVE status for BOTH rules (A and C)
side by side: n toward the 2,000-market checkpoint, mean ROI, and the
clustered 95% CI. Descriptive only -- it never applies #1545's own
LIVE-PILOT-ELIGIBLE/DROP/INCONCLUSIVE decision rule (that needs a bootstrap
read by a human, same discipline as #1507/#1452/#1453).
"""
from __future__ import annotations

import argparse
import logging
import random
import re
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
from src.utils.log_rotation import _read_jsonl_file, rotated_sources  # noqa: E402
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
#: as a fraction (issue #1544; src.strategy.fee.WEATHER_FEE_RATE matches
#: today, but the two are deliberately not coupled -- see module docstring).
DEFAULT_FEE_RATE = 0.01

#: Open-ended-bracket sentinel convention (mirrors
#: src.dashboard.api._format_bracket_range): bounds outside [-49, 199] mean
#: "no lower/upper bound".
OPEN_ENDED_LOW_SENTINEL = -49.0
OPEN_ENDED_HIGH_SENTINEL = 199.0

#: Entry-rule keys accepted by --entry-rule / evaluate(entry_rule=...), and
#: the issue's own A/C labels for each (see module docstring).
ENTRY_RULE_EVERY_POLL = "every_poll"  # Rule A -- registered, primary
ENTRY_RULE_HOURLY = "hourly"          # Rule C -- declared 2026-11-27, secondary
ENTRY_RULE_BOTH = "both"
ENTRY_RULE_CHOICES = (ENTRY_RULE_EVERY_POLL, ENTRY_RULE_HOURLY, ENTRY_RULE_BOTH)
RULE_LABELS = {ENTRY_RULE_EVERY_POLL: "A", ENTRY_RULE_HOURLY: "C"}


# ---------------------------------------------------------------------------
# Loading + entry selection (pure, unit-tested)
# ---------------------------------------------------------------------------

def _parse_ts(ts: "str | None") -> "datetime | None":
    """Parse a timestamp into an aware UTC ``datetime``. None if unparseable
    -- callers must drop a row they can't date, never guess.

    Tries the stdlib ``datetime.fromisoformat`` first (a C-implemented
    exact match for every ``ts`` ``scanner.py`` has ever written, e.g.
    ``datetime.now(timezone.utc).isoformat()``'s own output) before falling
    back to ``dateutil.parser.parse`` for anything looser. A full
    multi-month backtest run streams millions of snapshot rows through this
    function -- dateutil's parse is 1-2 orders of magnitude slower per call
    than fromisoformat, and was the dominant cost in an early profiling run
    of this module against production data (issue #1545 review).
    """
    if not ts:
        return None
    s = str(ts)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = dtparse.parse(s)
        except (ValueError, OverflowError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


#: Matches the "YYYY-MM-DD" date rotated_sources() embeds in a dated
#: filename, e.g. "snapshots.2026-11-24.jsonl.gz" -- see _file_date.
_DATE_IN_FILENAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _file_date(path: Path):
    """Extract the rotation date from a dated snapshot filename, or None
    for the undated legacy plain file (and anything else unparseable)."""
    m = _DATE_IN_FILENAME_RE.search(path.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


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

    **File-level date pruning.** This archive accumulates one dated file
    per day forever (``SNAPSHOT_RETAIN_DAYS=365`` in ``log_rotation.py``),
    so with months of history behind it, decompressing and JSON-parsing
    EVERY file on EVERY call -- then throwing most rows away in the per-row
    ``since``/``until`` filter below -- gets slower every day this module's
    daily-health-report section runs, in direct tension with that report's
    own "runs in ~seconds" design constraint. A dated file outside
    ``[since.date(), until.date()]`` is skipped entirely, trusting the
    filename's own rotation date (this is how ``log_rotation.py`` always
    writes these files -- a given day's rows always live in that day's
    dated file; an adversarially mis-dated row would still be correctly
    excluded here, matching what ``log_rotation.py``'s own writers
    guarantee never happens). The legacy undated plain file (pre-rotation
    data, or a same-day symlink target) has no filename date and is always
    read.

    Self-gating like ``resolve_bracket_outcomes``: a missing ``logs/``
    directory logs an honest message and returns an empty list rather than
    raising.
    """
    since_dt = _parse_ts(since)
    until_dt = _parse_ts(until) if until else None
    since_date = since_dt.date() if since_dt is not None else None
    until_date = until_dt.date() if until_dt is not None else None

    out: "list[dict]" = []
    if not base.parent.exists():
        log.info("[favourite_rule_eval] %s missing -- no local logs/, skipping", base.parent)
        return out

    for path in rotated_sources(base):
        file_date = _file_date(path)
        if file_date is not None:
            if since_date is not None and file_date < since_date:
                continue
            if until_date is not None and file_date > until_date:
                continue
        for row in _read_jsonl_file(path, is_gz=(path.suffix == ".gz")):
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


def _first_poll_per_ticker_hour(snapshots: "list[dict]") -> "list[dict]":
    """Reduce *snapshots* to one row per (ticker, UTC-hour) -- the FIRST
    (earliest ``ts``) poll in that hour, keyed on ``ts[:13]``
    ('YYYY-MM-DDTHH'), the same hourly key
    ``calibration_report.pick_samples`` uses for its hourly dedup.

    Implements the "checked once per UTC hour" half of rule H4-C (issue
    #1545, "Amended 2026-11-27 (PM)"): non-top-of-hour polls are removed
    from consideration entirely, BEFORE ``select_first_qualifying_entries``
    looks for a qualifying ask -- a poll that only briefly shows a
    qualifying ask mid-hour must never be eligible under C, even if it was
    the earliest qualifying poll overall (that is exactly what A would pick,
    and the two rules diverging there is the point of C).

    A row missing ``ticker``/``ts``, or with an unparseable ``ts``, is
    dropped -- can't be keyed or ordered.
    """
    best: "dict[tuple, dict]" = {}
    best_dt: "dict[tuple, datetime]" = {}
    for row in snapshots:
        ticker = row.get("ticker")
        ts_raw = row.get("ts")
        if not ticker or not ts_raw:
            continue
        ts_dt = _parse_ts(ts_raw)
        if ts_dt is None:
            continue
        hour_key = (ticker, str(ts_raw)[:13])
        if hour_key in best_dt and ts_dt >= best_dt[hour_key]:
            continue
        best_dt[hour_key] = ts_dt
        best[hour_key] = row
    return list(best.values())


def select_first_qualifying_entries(
    snapshots: "list[dict]",
    lo_cents: int = ENTRY_LO_CENTS,
    hi_cents: int = ENTRY_HI_CENTS,
    hourly: bool = False,
) -> "list[dict]":
    """One entry per ticker: the FIRST (earliest ``ts``) poll where either
    side's ask is in ``[lo_cents, hi_cents]`` (both inclusive).

    ``hourly=False`` (default) is rule A -- the originally registered
    rule: every poll is eligible. ``hourly=True`` is rule C (issue #1545's
    2026-11-27 amendment): *snapshots* is first reduced to one poll per
    (ticker, UTC-hour) via ``_first_poll_per_ticker_hour`` -- only
    top-of-hour polls are ever eligible -- and the same first-qualifying
    selection below then runs on that reduced population.

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
    if hourly:
        snapshots = _first_poll_per_ticker_hour(snapshots)

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

def _evaluate_one_rule(
    entries: "list[dict]", outcomes: "dict[str, bool]", fee_rate: float, n_resamples: int,
) -> dict:
    """Shared tail of the pipeline for a single entry rule (A or C): build
    trade rows, bootstrap, splits, markets/day and EUR/day."""
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
        "n_entries": len(entries), "n_resolved": boot["n"], "n_days": n_days,
        "markets_per_day": markets_per_day, "eur_per_day_at_5": eur_per_day,
        "mean_roi": boot["mean_roi"], "ci_lo": boot["ci_lo"], "ci_hi": boot["ci_hi"],
        "p5": boot["p5"], "splits": splits,
    }


def evaluate(
    since: str = ISSUE_1545_T0,
    until: "str | None" = None,
    fee_rate: float = DEFAULT_FEE_RATE,
    db_path: "Path | None" = DEFAULT_DB_PATH,
    snapshot_base: Path = SNAPSHOTS_JSONL,
    gamma_cache_path: "Path | None" = DEFAULT_GAMMA_CACHE_PATH,
    n_resamples: int = 10_000,
    entry_rule: str = ENTRY_RULE_BOTH,
) -> dict:
    """Load -> select entries (for rule A, C, or both) -> resolve outcomes
    -> clustered bootstrap, per rule.

    *entry_rule* is one of ``ENTRY_RULE_CHOICES`` (``"every_poll"`` = A,
    ``"hourly"`` = C, ``"both"`` = default). Snapshots are loaded once and
    outcomes are resolved once for the UNION of both rules' tickers, even
    when *entry_rule* is ``"both"`` -- each rule only ever differs in which
    polls are eligible to be checked, not in how outcomes are resolved.

    Returns ``{"since", "until", "fee_rate", "entry_rule", "checkpoint_n",
    "rules"}``, where ``rules`` maps each requested rule key
    (``"every_poll"``/``"hourly"``) to a dict with ``label`` ('A'/'C'),
    ``n_entries`` (qualifying markets found, including ones still awaiting
    settlement), ``n_resolved``/``mean_roi``/``ci_lo``/``ci_hi``/``p5``
    (the bootstrap, see ``clustered_bootstrap``), ``n_days``,
    ``markets_per_day``, ``eur_per_day_at_5`` (at a EUR5 stake), and
    ``splits``.
    """
    if entry_rule not in ENTRY_RULE_CHOICES:
        raise ValueError(f"entry_rule must be one of {ENTRY_RULE_CHOICES}, got {entry_rule!r}")
    rule_keys = (
        (ENTRY_RULE_EVERY_POLL, ENTRY_RULE_HOURLY) if entry_rule == ENTRY_RULE_BOTH
        else (entry_rule,)
    )

    snapshots = load_snapshots(since=since, until=until, base=snapshot_base)

    entries_by_rule: "dict[str, list[dict]]" = {}
    all_tickers: "set[str]" = set()
    for rk in rule_keys:
        entries = select_first_qualifying_entries(snapshots, hourly=(rk == ENTRY_RULE_HOURLY))
        entries_by_rule[rk] = entries
        all_tickers |= {e["ticker"] for e in entries}

    outcomes = load_outcomes(all_tickers, db_path=db_path, gamma_cache_path=gamma_cache_path)

    rules: "dict[str, dict]" = {}
    for rk in rule_keys:
        rule_result = _evaluate_one_rule(entries_by_rule[rk], outcomes, fee_rate, n_resamples)
        rules[rk] = {"label": RULE_LABELS[rk], **rule_result}

    return {
        "since": since, "until": until, "fee_rate": fee_rate, "entry_rule": entry_rule,
        "checkpoint_n": ISSUE_1545_CHECKPOINT_N, "rules": rules,
    }


def _fmt_pct(x: "float | None") -> str:
    return f"{x * 100:+.2f}%" if x is not None else "n/a"


def _format_rule_report(label: str, r: dict, checkpoint_n: int) -> "list[str]":
    """Render one rule's block of ``evaluate()``'s result (shared by
    ``format_report`` and ``section``)."""
    lines = [
        f"[Rule {label}] n entries (qualifying, incl. unresolved): {r['n_entries']}",
        f"[Rule {label}] n resolved (scored): {r['n_resolved']} (checkpoint {checkpoint_n})",
    ]
    if r["mean_roi"] is None:
        lines.append(f"[Rule {label}]   no resolved trades yet")
        return lines
    lines.append(
        f"[Rule {label}] mean ROI: {_fmt_pct(r['mean_roi'])}  "
        f"95% CI [{_fmt_pct(r['ci_lo'])}, {_fmt_pct(r['ci_hi'])}]  "
        f"p5 {_fmt_pct(r['p5'])}"
    )
    if r["markets_per_day"] is not None:
        lines.append(
            f"[Rule {label}] markets/day: {r['markets_per_day']:.1f}  "
            f"EUR/day @ EUR5 stake: {r['eur_per_day_at_5']:+.2f}"
        )
    lines.append(f"[Rule {label}] Splits:")
    for name, s in r["splits"].items():
        if not s["n"]:
            continue
        wr = f"{s['win_rate'] * 100:.1f}%" if s["win_rate"] is not None else "n/a"
        roi = _fmt_pct(s["mean_roi"])
        lines.append(f"  {name}: n={s['n']} win%={wr} meanROI={roi}")
    return lines


def format_report(result: dict) -> str:
    """Render ``evaluate()``'s result as the CLI's plain-text report --
    rules A and/or C (per ``result['entry_rule']``), side by side."""
    header = f"H4 favourite-longshot rule -- since {result['since']}"
    if result.get("until"):
        header += f" until {result['until']}"
    lines = [header, "-" * 60]
    for r in result["rules"].values():
        lines.extend(_format_rule_report(r["label"], r, result["checkpoint_n"]))
        lines.append("")
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

    Descriptive only -- never applies #1545's own decision rule, for either
    rule. A smaller ``n_resamples`` than the CLI default keeps this cheap
    enough for a report meant to run in seconds even once the population has
    grown toward the 2,000/4,000-market checkpoints.
    """
    t0_date = ISSUE_1545_T0.split("T")[0]
    lines = [f"H4 Favourite Rule Forward Test (since {t0_date})", "-" * 50]
    try:
        result = evaluate(n_resamples=2000, entry_rule=ENTRY_RULE_BOTH)
        for r in result["rules"].values():
            label = r["label"]
            marker = (" [CHECKPOINT REACHED]"
                      if r["n_resolved"] >= ISSUE_1545_CHECKPOINT_N else "")
            lines.append(
                f"  [Rule {label}] n={r['n_resolved']}/{ISSUE_1545_CHECKPOINT_N} "
                f"toward the checkpoint ({r['n_entries']} entries found, some "
                f"may still be awaiting settlement){marker}"
            )
            if r["mean_roi"] is None:
                lines.append(f"  [Rule {label}] No resolved entries yet.")
            else:
                lines.append(
                    f"  [Rule {label}] mean ROI {_fmt_pct(r['mean_roi'])}, "
                    f"95% CI [{_fmt_pct(r['ci_lo'])}, {_fmt_pct(r['ci_hi'])}], "
                    f"p5 {_fmt_pct(r['p5'])}"
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
    ap.add_argument("--entry-rule", choices=ENTRY_RULE_CHOICES, default=ENTRY_RULE_BOTH,
                    help="A: every_poll (registered, primary); C: hourly "
                         "(secondary, #1545 2026-11-27 amendment); both (default)")
    args = ap.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"{db_path} not found -- market_resolutions lookups will "
              f"fall back to the Gamma cache only")
        db_path = None

    result = evaluate(
        since=args.since, until=args.until, fee_rate=args.fee_rate,
        db_path=db_path, n_resamples=args.n_resamples, entry_rule=args.entry_rule,
    )
    print(format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Resolve YES/NO outcomes for EVERY evaluated bracket (issue #850).

**Scope.** This module builds one reusable capability: given the full
population of evaluated brackets logged by ``_write_bracket_evaluations``
(``logs/bracket_evals.*.jsonl``, issue #826), resolve each bracket's outcome
directly from observed weather, **independent of the ``settlements`` table**.

Why this exists (see ``docs/REMEDIATION_PLAN.md``, issue #822's M3 decision
gate): the existing Pass-1 tool
(``src/scripts/bss_market_vs_model_report.py``) joins against ``settlements``
and drops any row with "No definitive settlement match in the settlements
table". ``settlements`` is only populated for brackets that were actually
*traded* (~156 rows all-time) -- joining the full evaluated population
against it collapses n from hundreds of station-days down to ~20. That
exclusion is structurally wrong for Pass 2, which needs the full untraded
population: whether a bracket resolved YES or NO is a fact about the
weather, not about whether MeteoEdge traded it.

**Ground truth.** YES iff the station's observed daily high -- MAX(temp_f)
among ``observations`` rows for that station, grouped by the station's LOCAL
calendar day (``STATION_TZ``) -- falls in ``[bracket_low, bracket_high]``.
This mirrors the local-day-grouping algorithm already used in
``src.data.db.Database.get_daily_obs_high`` and
``src.scripts.backfill_live_settlements._observed_daily_high`` (issue #810:
grouping by the observation's raw UTC date, instead of the station-local
date, is exactly the timezone bug that fix closed -- this module must not
regress it). Unlike ``get_daily_obs_high``, this module talks to the
database through a **read-only** connection (``mode=ro``, matching
``bss_market_vs_model_report._connect_ro``) rather than instantiating
``Database``, because ``Database.__init__`` runs schema migrations and an
unconditional ``DELETE FROM deb_weight_log`` purge on open (see
``db.py::_purge_...``) -- a write side effect this read-only dry-run/
validation tool must never trigger against the production DB.

**Effective sample size.** All ~11 brackets evaluated on a station-day share
the SAME observed daily high, so they are not independent draws -- the
statistical power of any BSS/reliability test built on this data is bounded
by the number of distinct (station, settlement_date) pairs ("station-days"),
not the bracket-row count. This module resolves outcomes per bracket-row
(each bracket needs its own YES/NO -- only the bracket containing the
observed high resolves YES) but reports both counts, and computing the
per-station-day observed high is itself done once per (station,
settlement_date), not once per row.

**Not in scope here (issue #822 Pass 2's job, not this module's):** computing
BS_model / BS_market / BSS itself, market-implied probability, reliability
curves, or the M3 verdict. This module's public entry point,
``resolve_bracket_outcomes()``, is the reusable outcome-resolution capability
Pass 2 is expected to import and call directly::

    from src.scripts.resolve_bracket_outcomes import resolve_bracket_outcomes

    resolved_rows, counts = resolve_bracket_outcomes()
    # resolved_rows: bracket_evals rows (deduped one-per-(station, ticker,
    # settlement_date), lowest minutes_to_settlement) plus two added keys:
    #   "observed_high"  -- float, the station-local-day observed daily high
    #   "resolved_yes"   -- bool, True iff bracket_low <= observed_high <= bracket_high
    # counts: exclusion-funnel dict, including counts["n_station_days"] --
    # the effective-sample-size figure Pass 2 should report as the headline n.

Correctness check: ``cross_check_against_settlements()`` compares this
module's independently-computed ``resolved_yes`` against
``settlements.resolved_yes`` on the ~156-bracket overlap where both exist.
They must match -- a mismatch means this module's ground truth (or
``settlements``' truth) is wrong, not that the two are free to disagree.

**Issue #858.** The check above only has statistical power on the overlap
between ``bracket_evals`` and ``settlements`` -- and ``bracket_evals`` only
started logging 2026-07-24 (#826) while ``settlements`` mostly predates that,
so in production the overlap can be (and, on 2026-07-25, was) zero: a
technically-passing check with nothing behind it. ``cross_check_against_
settlements_direct()`` is a second, independent correctness check that never
goes through ``bracket_evals`` at all: it resolves every ``settlements`` row
straight from that row's own ``station``/``bracket_low``/``bracket_high``
against an independently-recomputed observed high, then compares to that
SAME row's ``resolved_yes``. Because ``settlements.ts`` is the settle-service
RUN timestamp (see ``src.data.settlements.SettlementWriter.record_settlement``),
not the settlement date, the settlement date for each row is instead derived
by joining to ``trades`` on ``ticker`` and reusing ``settle.resolve_trade_date()``
-- the same ``end_date``-preferred, station-local-``ts``-fallback convention
``backfill_live_settlements.py`` already relies on for exactly this problem.
A row whose ticker has no matching ``trades`` row (so no trustworthy date)
is skipped, not guessed.

Usage (dry-run CLI; prints and writes a report; never writes to the DB)::

    python -m src.scripts.resolve_bracket_outcomes
    python -m src.scripts.resolve_bracket_outcomes --bracket-evals logs/bracket_evals.jsonl \\
        --db data/meteoedge.db --out backtest_results

Self-gating, like ``bss_market_vs_model_report``: with no local ``logs/`` or
``data/meteoedge.db`` (both gitignored, host-only -- see #822's dev-sandbox
comment thread), this logs an honest message and writes nothing rather than
fabricating a report.
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytz
from dateutil import parser as dtparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import BRACKET_EVALS_JSONL, STATION_TZ  # noqa: E402
from src.scripts.settle import resolve_trade_date  # noqa: E402
from src.utils.log_rotation import iter_rotated_jsonl  # noqa: E402

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

DEFAULT_DB_PATH = Path(os.getenv("DB_PATH", "data/meteoedge.db"))
DEFAULT_OUT_DIR = Path("backtest_results")


# ---------------------------------------------------------------------------
# Loading logs/bracket_evals.*.jsonl
# ---------------------------------------------------------------------------

def load_bracket_eval_rows(base: Path = BRACKET_EVALS_JSONL) -> "list[dict]":
    """Load every row across the rotated ``bracket_evals`` JSONL archive."""
    return list(iter_rotated_jsonl(base))


def dedupe_one_per_bracket_day(rows: "list[dict]") -> "list[dict]":
    """Keep exactly one row per (station, ticker, settlement_date).

    Keeps the row with the lowest ``minutes_to_settlement`` -- the final,
    closest-to-resolution poll -- mirroring
    ``bss_market_vs_model_report.dedupe_one_per_bracket_day`` (same idea,
    keyed on ``settlement_date`` instead of ``end_date`` since that's the
    field ``bracket_evals`` rows carry). Rows missing station/ticker/
    settlement_date are dropped (can't be keyed).
    """
    best: "dict[tuple, dict]" = {}
    for row in rows:
        station = row.get("station")
        ticker = row.get("ticker")
        settlement_date = row.get("settlement_date")
        if not station or not ticker or not settlement_date:
            continue
        key = (station, ticker, settlement_date)
        prev = best.get(key)
        mts = row.get("minutes_to_settlement")
        if prev is None or (mts is not None and (
                prev.get("minutes_to_settlement") is None or mts < prev["minutes_to_settlement"])):
            best[key] = row
    return list(best.values())


# ---------------------------------------------------------------------------
# Observed daily high (ground truth, independent of settlements)
# ---------------------------------------------------------------------------

def _connect_ro(db_path: "Path | None") -> "sqlite3.Connection | None":
    """Open *db_path* read-only. Returns None if missing/unopenable.

    Never creates the file (unlike plain ``sqlite3.connect``) -- this module
    must never write to production databases. Mirrors
    ``bss_market_vs_model_report._connect_ro``.
    """
    if db_path is None:
        return None
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.OperationalError as exc:
        log.warning("[resolve_bracket_outcomes] could not open %s read-only: %s", db_path, exc)
        return None


def compute_observed_highs(
    db_path: "Path | None", station_dates: "set[tuple[str, str]]"
) -> "dict[tuple[str, str], float]":
    """Return {(station, settlement_date): observed_high_f} for every pair in
    *station_dates* that has at least one observation on record.

    ``observed_high`` = MAX(temp_f) among ``observations`` rows for that
    station whose timestamp falls on ``settlement_date`` in the station's
    LOCAL calendar day (``STATION_TZ``) -- NOT the observation's raw UTC
    date (issue #810: that was the timezone bug). A (station, date) pair
    absent from ``observations`` is simply absent from the returned dict --
    this function never guesses a value.

    Read-only: opens *db_path* via ``mode=ro`` and never writes.
    """
    if not station_dates:
        return {}
    con = _connect_ro(db_path)
    if con is None:
        return {}
    try:
        wanted = set(station_dates)
        stations = sorted({s for s, _ in wanted})

        parsed_dates: "list[date]" = []
        for _, d in wanted:
            try:
                parsed_dates.append(date.fromisoformat(str(d)[:10]))
            except (ValueError, TypeError):
                continue
        if not stations or not parsed_dates:
            return {}

        # Fetch a window generously padded around the requested date range --
        # a full day either side covers every station's UTC offset, mirroring
        # get_daily_obs_high's date_minus_1 / date_plus_2 padding.
        window_from = (min(parsed_dates) - timedelta(days=1)).isoformat()
        window_to = (max(parsed_dates) + timedelta(days=2)).isoformat()

        placeholders = ",".join("?" * len(stations))
        cur = con.execute(
            f"SELECT station, ts, temp_f FROM observations "
            f"WHERE station IN ({placeholders}) AND ts >= ? AND ts < ? "
            f"AND temp_f IS NOT NULL ORDER BY ts",
            (*stations, window_from, window_to),
        )

        tz_cache: "dict[str, object]" = {}
        highs: "dict[tuple[str, str], float]" = {}
        for row in cur.fetchall():
            station, ts_str, temp_f = row["station"], row["ts"], row["temp_f"]
            tz_name = STATION_TZ.get(station)
            if not tz_name:
                continue
            tz = tz_cache.get(tz_name)
            if tz is None:
                try:
                    tz = pytz.timezone(tz_name)
                except pytz.UnknownTimeZoneError:
                    continue
                tz_cache[tz_name] = tz
            try:
                t = dtparse.parse(ts_str)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                local_date = t.astimezone(tz).date().isoformat()
            except (ValueError, OverflowError, TypeError):
                continue

            key = (station, local_date)
            if key not in wanted:
                continue
            try:
                temp_f = float(temp_f)
            except (TypeError, ValueError):
                continue
            if key not in highs or temp_f > highs[key]:
                highs[key] = temp_f
        return highs
    except sqlite3.OperationalError as exc:
        log.warning("[resolve_bracket_outcomes] observations read failed: %s", exc)
        return {}
    finally:
        con.close()


def resolve_outcome(
    bracket_low: "float | None", bracket_high: "float | None", observed_high: "float | None"
) -> "bool | None":
    """YES iff ``observed_high`` falls in ``[bracket_low, bracket_high]``.

    Returns None (undeterminable) if any input is missing or non-numeric --
    callers must treat None as "no truth available", not False.
    """
    if bracket_low is None or bracket_high is None or observed_high is None:
        return None
    try:
        return float(bracket_low) <= float(observed_high) <= float(bracket_high)
    except (TypeError, ValueError):
        return None


def resolve_bracket_rows(
    rows: "list[dict]", observed_highs: "dict[tuple[str, str], float]"
) -> "tuple[list[dict], dict]":
    """Attach ``observed_high`` and ``resolved_yes`` to every resolvable row.

    Rows for a (station, settlement_date) with no observed high on record are
    dropped (counted, not guessed). Returns (resolved_rows, counts).
    """
    counts: dict = defaultdict(int)
    counts["input_rows"] = len(rows)
    out = []
    for row in rows:
        station = row.get("station")
        settlement_date = row.get("settlement_date")
        if not station or not settlement_date:
            counts["missing_station_or_settlement_date"] += 1
            continue
        observed_high = observed_highs.get((station, settlement_date))
        if observed_high is None:
            counts["no_observed_high"] += 1
            continue
        resolved_yes = resolve_outcome(row.get("bracket_low"), row.get("bracket_high"), observed_high)
        if resolved_yes is None:
            counts["missing_bracket_bounds"] += 1
            continue
        out.append({**row, "observed_high": observed_high, "resolved_yes": resolved_yes})
    counts["resolved_rows"] = len(out)
    return out, dict(counts)


# ---------------------------------------------------------------------------
# Public entry point (#822 Pass 2 imports/calls this directly)
# ---------------------------------------------------------------------------

def resolve_bracket_outcomes(
    bracket_evals_base: Path = BRACKET_EVALS_JSONL,
    db_path: "Path | None" = None,
) -> "tuple[list[dict], dict]":
    """Load, dedupe, and resolve the outcome of every evaluated bracket.

    Independent of ``settlements`` -- the only DB table read is
    ``observations``. Returns (resolved_rows, counts):

    - ``resolved_rows``: one dict per (station, ticker, settlement_date) --
      the ``bracket_evals`` row plus ``observed_high`` (float) and
      ``resolved_yes`` (bool).
    - ``counts``: exclusion-funnel dict. ``counts["n_bracket_rows"]`` is the
      resolved bracket-row count; ``counts["n_station_days"]`` is the
      distinct (station, settlement_date) count -- the effective-sample-size
      figure for any downstream BSS/reliability power discussion, since all
      brackets sharing a station-day are not independent draws.
    """
    db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH

    raw_rows = load_bracket_eval_rows(bracket_evals_base)
    deduped = dedupe_one_per_bracket_day(raw_rows)

    station_dates = {
        (r["station"], r["settlement_date"]) for r in deduped
        if r.get("station") and r.get("settlement_date")
    }
    observed_highs = compute_observed_highs(db_path, station_dates)

    resolved_rows, counts = resolve_bracket_rows(deduped, observed_highs)
    counts["raw_rows"] = len(raw_rows)
    counts["deduped_bracket_rows"] = len(deduped)
    counts["n_bracket_rows"] = len(resolved_rows)
    counts["n_station_days"] = len({(r["station"], r["settlement_date"]) for r in resolved_rows})
    return resolved_rows, counts


# ---------------------------------------------------------------------------
# Correctness check: compare against settlements.resolved_yes
# ---------------------------------------------------------------------------

def load_settlement_outcomes(db_path: "Path | None") -> "dict[str, bool]":
    """Return {ticker: resolved_yes} from the ``settlements`` table.

    Used ONLY for the cross-check below -- outcome resolution itself never
    depends on this table. Mirrors
    ``bss_market_vs_model_report.load_settlement_outcomes``.
    """
    con = _connect_ro(db_path)
    if con is None:
        return {}
    try:
        cur = con.execute("SELECT ticker, resolved_yes FROM settlements")
        return {r["ticker"]: bool(r["resolved_yes"]) for r in cur.fetchall()}
    except sqlite3.OperationalError as exc:
        log.warning("[resolve_bracket_outcomes] settlements read failed: %s", exc)
        return {}
    finally:
        con.close()


def cross_check_against_settlements(
    resolved_rows: "list[dict]", settlement_outcomes: "dict[str, bool]"
) -> dict:
    """Compare this module's ``resolved_yes`` to ``settlements.resolved_yes``
    on every ticker present in both. This is the correctness check for the
    whole module -- a mismatch means the independently-computed ground truth
    disagrees with the traded-market truth and must be investigated, not
    waved off.

    Returns {"n_overlap", "n_match", "n_mismatch", "mismatches"} where
    ``mismatches`` is a list of dicts with enough detail to debug each one.
    """
    n_overlap = 0
    n_match = 0
    mismatches = []
    for row in resolved_rows:
        ticker = row.get("ticker")
        if ticker not in settlement_outcomes:
            continue
        n_overlap += 1
        settlements_yes = settlement_outcomes[ticker]
        our_yes = row["resolved_yes"]
        if bool(our_yes) == bool(settlements_yes):
            n_match += 1
        else:
            mismatches.append({
                "station": row.get("station"),
                "ticker": ticker,
                "settlement_date": row.get("settlement_date"),
                "bracket_low": row.get("bracket_low"),
                "bracket_high": row.get("bracket_high"),
                "observed_high": row.get("observed_high"),
                "resolver_resolved_yes": our_yes,
                "settlements_resolved_yes": settlements_yes,
            })
    return {
        "n_overlap": n_overlap,
        "n_match": n_match,
        "n_mismatch": len(mismatches),
        "mismatches": mismatches,
    }


# ---------------------------------------------------------------------------
# Correctness check #2 (issue #858): resolve directly from settlements'
# OWN fields, bypassing bracket_evals entirely
# ---------------------------------------------------------------------------

def load_settlement_rows(db_path: "Path | None") -> "list[dict]":
    """Return every ``settlements`` row with the columns
    ``cross_check_against_settlements_direct()`` needs: ``ticker``,
    ``station``, ``bracket_low``, ``bracket_high``, ``actual_high_f``,
    ``resolved_yes``. Unlike ``load_settlement_outcomes()`` (which only keeps
    ``resolved_yes`` for the bracket_evals-overlap check), this keeps enough
    of each row to independently re-resolve it without going back through
    ``bracket_evals`` at all.
    """
    con = _connect_ro(db_path)
    if con is None:
        return []
    try:
        cur = con.execute(
            "SELECT ticker, station, bracket_low, bracket_high, actual_high_f, "
            "resolved_yes FROM settlements"
        )
        return [dict(r) for r in cur.fetchall()]
    except sqlite3.OperationalError as exc:
        log.warning("[resolve_bracket_outcomes] settlements read failed: %s", exc)
        return []
    finally:
        con.close()


def _trade_dates_by_ticker(db_path: "Path | None") -> "dict[str, date]":
    """Return {ticker: settlement_date} derived from the ``trades`` table.

    ``settlements.ts`` is the settle-service RUN timestamp (see
    ``src.data.settlements.SettlementWriter.record_settlement`` -- it's
    always ``datetime.now(timezone.utc)`` at write time), NOT the settlement
    date, so it must never be used to date a settlements row. Instead, this
    joins to ``trades`` on ``ticker`` and reuses ``settle.resolve_trade_date()``
    -- the same end_date-preferred, station-local-``ts``-fallback convention
    ``backfill_live_settlements.py`` already relies on for exactly this
    problem -- rather than re-deriving a date from scratch. A ticker with no
    ``trades`` row, or whose ``trades`` row(s) can't be dated, is simply
    absent from the returned dict.
    """
    con = _connect_ro(db_path)
    if con is None:
        return {}
    try:
        cur = con.execute("SELECT ticker, station, ts, end_date FROM trades")
        out: "dict[str, date]" = {}
        for row in cur.fetchall():
            ticker = row["ticker"]
            if not ticker or ticker in out:
                continue
            d = resolve_trade_date(dict(row))
            if d is not None:
                out[ticker] = d
        return out
    except sqlite3.OperationalError as exc:
        log.warning("[resolve_bracket_outcomes] trades read failed: %s", exc)
        return {}
    finally:
        con.close()


def cross_check_against_settlements_direct(db_path: "Path | None") -> dict:
    """Resolve every ``settlements`` row directly from its OWN
    station/bracket_low/bracket_high (and an independently-derived
    settlement date), completely bypassing ``bracket_evals`` and
    ``resolve_bracket_outcomes()``. Then compare the independently-computed
    outcome against that SAME row's ``resolved_yes``.

    This is the correctness check issue #858 asks for: unlike
    ``cross_check_against_settlements()``, its statistical power does not
    depend on ``bracket_evals`` overlapping ``settlements`` in time -- it can
    validate the resolver's core logic (``resolve_outcome`` +
    ``compute_observed_highs``) against the FULL ``settlements`` population.

    Returns a dict with:
    - ``n_settlement_rows``: total rows read from ``settlements``.
    - ``n_no_trade_date``: rows skipped because no ``trades`` row (or none
      that resolve to a date) matches the ticker -- can't independently date
      the row, so it's never guessed.
    - ``n_no_observed_high``: rows skipped because no observation exists for
      that (station, date) once dated.
    - ``n_checked``: rows actually compared (``n_match + n_mismatch``).
    - ``n_match`` / ``n_mismatch`` / ``mismatches``: as in
      ``cross_check_against_settlements()``.
    """
    settlement_rows = load_settlement_rows(db_path)
    n_settlement_rows = len(settlement_rows)
    if n_settlement_rows == 0:
        return {
            "n_settlement_rows": 0,
            "n_no_trade_date": 0,
            "n_no_observed_high": 0,
            "n_checked": 0,
            "n_match": 0,
            "n_mismatch": 0,
            "mismatches": [],
        }

    trade_dates = _trade_dates_by_ticker(db_path)

    dated_rows: "list[tuple[dict, str]]" = []
    n_no_trade_date = 0
    for row in settlement_rows:
        d = trade_dates.get(row.get("ticker"))
        if d is None:
            n_no_trade_date += 1
            continue
        dated_rows.append((row, d.isoformat()))

    station_dates = {
        (row["station"], settlement_date) for row, settlement_date in dated_rows
        if row.get("station")
    }
    observed_highs = compute_observed_highs(db_path, station_dates)

    n_no_observed_high = 0
    n_match = 0
    mismatches = []
    for row, settlement_date in dated_rows:
        observed_high = observed_highs.get((row.get("station"), settlement_date))
        direct_yes = resolve_outcome(row.get("bracket_low"), row.get("bracket_high"), observed_high)
        if direct_yes is None:
            n_no_observed_high += 1
            continue
        settlements_yes = bool(row.get("resolved_yes"))
        if direct_yes == settlements_yes:
            n_match += 1
        else:
            mismatches.append({
                "station": row.get("station"),
                "ticker": row.get("ticker"),
                "settlement_date": settlement_date,
                "bracket_low": row.get("bracket_low"),
                "bracket_high": row.get("bracket_high"),
                "observed_high": observed_high,
                "direct_resolved_yes": direct_yes,
                "settlements_resolved_yes": settlements_yes,
            })

    return {
        "n_settlement_rows": n_settlement_rows,
        "n_no_trade_date": n_no_trade_date,
        "n_no_observed_high": n_no_observed_high,
        "n_checked": n_match + len(mismatches),
        "n_match": n_match,
        "n_mismatch": len(mismatches),
        "mismatches": mismatches,
    }


# ---------------------------------------------------------------------------
# Dry-run CLI
# ---------------------------------------------------------------------------

def build_dry_run_report(
    resolved_rows: "list[dict]", counts: dict, cross_check: dict, direct_check: dict, run_date: str
) -> str:
    lines = []
    lines.append("# Bracket Outcome Resolution -- Dry Run (issue #850)\n")
    lines.append(f"**Run date:** {run_date}  ")
    lines.append(
        "**Data sources:** `logs/bracket_evals.*.jsonl` (issue #826, full evaluated "
        "population) joined to `observations` (meteoedge.db) for ground truth -- "
        "**NOT** joined to `settlements` for outcome resolution (that join is exactly "
        "what collapses the population to ~20 traded brackets; see issue #850).  \n"
    )
    lines.append("\n---\n")

    lines.append("## Exclusion funnel\n")
    lines.append("| Stage | Count |")
    lines.append("|---|---|")
    lines.append(f"| Raw rows (all polls, all stations) | {counts.get('raw_rows', 0)} |")
    lines.append(
        f"| After dedupe (one row per station/ticker/settlement_date, lowest "
        f"minutes_to_settlement) | {counts.get('deduped_bracket_rows', 0)} |"
    )
    lines.append(
        f"| Excluded: missing station/settlement_date | "
        f"{counts.get('missing_station_or_settlement_date', 0)} |"
    )
    lines.append(
        f"| Excluded: no observed high on record for that station-day | "
        f"{counts.get('no_observed_high', 0)} |"
    )
    lines.append(
        f"| Excluded: missing bracket bounds | {counts.get('missing_bracket_bounds', 0)} |"
    )
    lines.append(f"| **Resolved bracket-rows (n)** | **{counts.get('n_bracket_rows', 0)}** |")
    lines.append(
        f"| **Effective sample size -- distinct station-days (n)** | "
        f"**{counts.get('n_station_days', 0)}** |"
    )
    lines.append("")
    lines.append(
        "Note on power: all brackets evaluated on a station-day share the SAME observed "
        "daily high, so they are not independent draws. The station-day figure above, not "
        "the bracket-row figure, is the one to compare against the M3 gate's `n >= 300` "
        "requirement (docs/REMEDIATION_PLAN.md).\n"
    )

    lines.append("## Correctness check: resolved_yes vs. `settlements.resolved_yes`\n")
    lines.append(
        "Every resolved row whose ticker also has a row in `settlements` (the ~156 "
        "traded brackets) is compared here. These must match -- this is the "
        "correctness check for the resolver, not a second independent opinion.\n"
    )
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Overlap with settlements (n) | {cross_check.get('n_overlap', 0)} |")
    lines.append(f"| Matches | {cross_check.get('n_match', 0)} |")
    lines.append(f"| Mismatches | {cross_check.get('n_mismatch', 0)} |")
    lines.append("")

    if cross_check.get("mismatches"):
        lines.append("### Mismatches (investigate before trusting this resolver)\n")
        lines.append(
            "| station | ticker | settlement_date | bracket_low | bracket_high | "
            "observed_high | resolver | settlements |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")
        for m in cross_check["mismatches"]:
            lines.append(
                f"| {m['station']} | {m['ticker']} | {m['settlement_date']} | "
                f"{m['bracket_low']} | {m['bracket_high']} | {m['observed_high']} | "
                f"{m['resolver_resolved_yes']} | {m['settlements_resolved_yes']} |"
            )
        lines.append("")

    lines.append(
        "## Correctness check #2: resolved directly from `settlements` (issue #858)\n"
    )
    lines.append(
        "Independent of the check above -- and of `bracket_evals` entirely. Every "
        "`settlements` row is re-resolved from its OWN `station`/`bracket_low`/"
        "`bracket_high` against an independently-recomputed observed high (dated via "
        "`trades.end_date`/station-local `ts`, NOT `settlements.ts` -- see methodology "
        "notes), then compared to that SAME row's `resolved_yes`. This has statistical "
        "power over the FULL `settlements` population, so it stays informative even when "
        "the check above has near-zero overlap.\n"
    )
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Settlements rows read (n) | {direct_check.get('n_settlement_rows', 0)} |")
    lines.append(
        f"| Excluded: no dateable `trades` row for this ticker | "
        f"{direct_check.get('n_no_trade_date', 0)} |"
    )
    lines.append(
        f"| Excluded: no observed high on record for that station-day | "
        f"{direct_check.get('n_no_observed_high', 0)} |"
    )
    lines.append(f"| **Checked (n)** | **{direct_check.get('n_checked', 0)}** |")
    lines.append(f"| Matches | {direct_check.get('n_match', 0)} |")
    lines.append(f"| Mismatches | {direct_check.get('n_mismatch', 0)} |")
    lines.append("")

    if direct_check.get("mismatches"):
        lines.append("### Mismatches (investigate before trusting this resolver)\n")
        lines.append(
            "| station | ticker | settlement_date | bracket_low | bracket_high | "
            "observed_high | direct | settlements |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")
        for m in direct_check["mismatches"]:
            lines.append(
                f"| {m['station']} | {m['ticker']} | {m['settlement_date']} | "
                f"{m['bracket_low']} | {m['bracket_high']} | {m['observed_high']} | "
                f"{m['direct_resolved_yes']} | {m['settlements_resolved_yes']} |"
            )
        lines.append("")

    lines.append("\n---\n")
    lines.append("## Methodology notes\n")
    lines.append(
        "- Ground truth: MAX(`temp_f`) among `observations` rows for the station, grouped "
        "by the station's LOCAL calendar day (`STATION_TZ`), matching "
        "`Database.get_daily_obs_high` / `backfill_live_settlements._observed_daily_high` "
        "(issue #810 -- grouping by raw UTC date instead is the timezone bug that fix closed)."
    )
    lines.append(
        "- Read-only: this script never opens the DB for writing; `observations` and "
        "`settlements` are both read via a `mode=ro` SQLite connection."
    )
    lines.append(
        "- De-duplication keeps one row per (station, ticker, settlement_date) -- the "
        "lowest-`minutes_to_settlement` poll -- mirroring "
        "`bss_market_vs_model_report.dedupe_one_per_bracket_day`."
    )
    lines.append(
        "- This module does NOT compute BS_model / BS_market / BSS -- that remains issue "
        "#822 Pass 2's own scope, built on top of `resolve_bracket_outcomes()`."
    )
    lines.append(
        "- Correctness check #2 (issue #858) never uses `settlements.ts` as a settlement "
        "date -- it's the settle-service RUN timestamp, not the settlement date (see "
        "`src.data.settlements.SettlementWriter.record_settlement`). The date instead "
        "comes from the matching `trades` row's `end_date` (falling back to that row's "
        "own station-local `ts` day), via `settle.resolve_trade_date()` -- the same "
        "convention `backfill_live_settlements.py` uses."
    )
    lines.append("")
    return "\n".join(lines)


def run_dry_run(
    bracket_evals_base: Path, db_path: Path, out_dir: Path, run_date: "str | None" = None
) -> int:
    """Resolve outcomes, run both correctness checks, and (if there is real
    data for at least one of them) write a report. Self-gating, like
    ``bss_market_vs_model_report.run_report``: no local data -> no report.

    The two correctness checks are independent of each other (issue #858):
    ``cross_check_against_settlements()`` only has power on the
    ``bracket_evals`` <-> ``settlements`` overlap, which can be near-zero in
    production (``bracket_evals`` only started logging 2026-07-24, #826,
    while ``settlements`` mostly predates that). ``cross_check_against_
    settlements_direct()`` never depends on ``bracket_evals`` at all, so a
    report is written whenever EITHER check has real data to show, not only
    when ``bracket_evals`` does.
    """
    run_date = run_date or datetime.now(timezone.utc).date().isoformat()

    resolved_rows, counts = resolve_bracket_outcomes(bracket_evals_base, db_path)

    if counts.get("raw_rows", 0) == 0:
        log.info(
            "[resolve_bracket_outcomes] no rows found under %s (rotated sources) -- "
            "nothing to resolve from bracket_evals. This is expected in a fresh "
            "checkout / dev sandbox; logs/ is gitignored and lives on the bot host. "
            "Falling back to the settlements-direct check only (issue #858).",
            bracket_evals_base,
        )
    elif not resolved_rows:
        log.info(
            "[resolve_bracket_outcomes] no bracket rows resolved to an outcome -- no "
            "matching observations found (data/meteoedge.db missing/empty, or no "
            "observations overlap the bracket_evals date range). Falling back to the "
            "settlements-direct check only (issue #858)."
        )

    settlement_outcomes = load_settlement_outcomes(db_path)
    cross_check = cross_check_against_settlements(resolved_rows, settlement_outcomes)
    direct_check = cross_check_against_settlements_direct(db_path)

    if not resolved_rows and direct_check["n_settlement_rows"] == 0:
        log.info(
            "[resolve_bracket_outcomes] no bracket_evals rows and no settlements rows -- "
            "nothing to report. Not writing a report."
        )
        return 0

    report = build_dry_run_report(resolved_rows, counts, cross_check, direct_check, run_date)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"bracket_outcome_resolution_dryrun_{run_date}.md"
    out_path.write_text(report, encoding="utf-8")
    log.info(
        "[resolve_bracket_outcomes] wrote %s (n_bracket_rows=%d, n_station_days=%d, "
        "settlements cross-check: n_overlap=%d n_mismatch=%d, "
        "settlements-direct check: n_checked=%d n_mismatch=%d)",
        out_path, counts["n_bracket_rows"], counts["n_station_days"],
        cross_check["n_overlap"], cross_check["n_mismatch"],
        direct_check["n_checked"], direct_check["n_mismatch"],
    )
    return 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bracket-evals", type=Path, default=BRACKET_EVALS_JSONL)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--run-date", default=None,
                    help="Report date stamp (default: today, UTC)")
    args = ap.parse_args(argv)
    return run_dry_run(args.bracket_evals, args.db, args.out, args.run_date)


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    raise SystemExit(main())

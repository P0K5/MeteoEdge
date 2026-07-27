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

**Ground truth (issue #860).** Polymarket's definitive on-chain resolution
where the market has settled decisively, falling back to the observed daily
high otherwise -- the SAME precedence ``settle.py`` uses, so this repo has one
resolution policy rather than two. This matters because the METAR-derived
comparison booked the wrong outcome in ~22% of audited settlements (#644),
and #822's M3 gate is the go/no-go decision for the entire trading thesis:
scoring it against a proxy known to be wrong 1 time in 5 could flip the
verdict. Network cost is bounded by a persistent cache -- a settled market's
outcome never changes, so each ticker is fetched at most once ever -- and
``--no-network`` serves from cache only.

**METAR fallback.** YES iff the station's observed daily high -- MAX(temp_f)
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
import json
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

from src.config import BRACKET_EVALS_JSONL, LOG_DIR, STATION_TZ  # noqa: E402
from src.data.polymarket import fetch_market_resolution  # noqa: E402
from src.scripts.settle import resolve_trade_date  # noqa: E402
from src.utils.log_rotation import iter_rotated_jsonl  # noqa: E402

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

DEFAULT_DB_PATH = Path(os.getenv("DB_PATH", "data/meteoedge.db"))
DEFAULT_OUT_DIR = Path("backtest_results")
DEFAULT_GAMMA_CACHE_PATH = LOG_DIR / "gamma_resolution_cache.json"

# Sentinel for "caller didn't specify a cache path -- use the module default".
# Needed because a plain `cache_path=DEFAULT_GAMMA_CACHE_PATH` default binds the
# value at import time, which would make the constant unpatchable (tests must be
# able to redirect the cache away from the real logs/ directory). Resolving the
# module global at CALL time keeps that patchable, while leaving an explicit
# `cache_path=None` free to mean its own thing: no cache at all.
_DEFAULT_CACHE = object()


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


# ---------------------------------------------------------------------------
# Gamma resolution: the AUTHORITATIVE ground truth (issue #860)
# ---------------------------------------------------------------------------
#
# ``settle.py`` (see ``_write_db_settlements``) prefers Polymarket's official
# on-chain resolution over METAR-derived truth, because the METAR comparison
# booked the wrong outcome in ~22% of audited settlements (issue #644). This
# module originally resolved from METAR only, which is exactly why #858's
# settlements-direct correctness check reported a 27.5% mismatch rate on its
# first production run -- it was comparing two DIFFERENT ground truths, one of
# which the team had already concluded is the less trustworthy proxy.
#
# Since #822's M3 gate is the go/no-go decision for the whole trading thesis,
# it must be scored against what actually happened, not against a proxy known
# to be wrong ~1 time in 5. So this module now mirrors ``settle.py``'s
# precedence exactly -- one resolution policy in this repo, not two.

def load_gamma_cache(path: "Path | None") -> "dict[str, bool]":
    """Load the persistent {ticker: resolved_yes} Gamma cache.

    A resolved Polymarket market's outcome never changes once it has settled
    on-chain, so a decisive resolution is cached permanently and that ticker
    is never fetched again -- this is what keeps the network cost of scoring
    hundreds of brackets/day bounded (issue #860).

    A missing/corrupt/unreadable cache file is treated as an empty cache
    (warned, never raised): a cache is an optimisation, and losing it must
    only cost time, never correctness.
    """
    if path is None:
        return {}
    path = Path(path)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("[resolve_bracket_outcomes] gamma cache unreadable (%s): %s", path, exc)
        return {}
    if not isinstance(raw, dict):
        log.warning("[resolve_bracket_outcomes] gamma cache malformed (%s), ignoring", path)
        return {}
    return {str(k): bool(v) for k, v in raw.items() if isinstance(v, bool)}


def save_gamma_cache(path: "Path | None", cache: "dict[str, bool]") -> None:
    """Persist the Gamma cache. Failure is warned, never raised -- see
    ``load_gamma_cache``: the cache must never be able to fail a run."""
    if path is None:
        return
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        log.warning("[resolve_bracket_outcomes] could not write gamma cache (%s): %s", path, exc)


def resolve_gamma_outcomes(
    tickers: "set[str]",
    cache_path: "Path | None" = _DEFAULT_CACHE,
    allow_network: bool = True,
) -> "tuple[dict[str, bool], dict]":
    """Return ({ticker: resolved_yes}, stats) for every ticker Polymarket has
    definitively resolved.

    Only DECISIVE resolutions are returned and cached -- ``fetch_market_
    resolution`` returns None for a market that is unresolved, ambiguous, or
    unreachable, and those are deliberately NOT cached so a later run retries
    them (a market that has not settled yet may settle tomorrow; caching
    "unknown" forever would permanently freeze it out of the M3 population).

    ``allow_network=False`` (the CLI's ``--no-network``) serves purely from
    cache and issues zero HTTP requests, so the whole pipeline stays usable
    offline -- callers fall back to METAR for anything uncached.

    A per-ticker fetch failure degrades that ticker to METAR and never aborts
    the run.

    ``cache_path`` defaults to ``DEFAULT_GAMMA_CACHE_PATH`` (resolved at call
    time -- see ``_DEFAULT_CACHE``); pass ``None`` explicitly to run with no
    cache at all.
    """
    if cache_path is _DEFAULT_CACHE:
        cache_path = DEFAULT_GAMMA_CACHE_PATH
    cache = load_gamma_cache(cache_path)
    stats: dict = defaultdict(int)
    stats["n_requested"] = len(tickers)

    resolutions: "dict[str, bool]" = {}
    to_fetch = []
    for ticker in sorted(tickers):
        if not ticker:
            continue
        if ticker in cache:
            resolutions[ticker] = cache[ticker]
            stats["n_cache_hits"] += 1
        else:
            to_fetch.append(ticker)

    if not allow_network:
        stats["n_skipped_offline"] = len(to_fetch)
        return resolutions, dict(stats)

    cache_dirty = False
    for ticker in to_fetch:
        stats["n_fetched"] += 1
        try:
            resolved = fetch_market_resolution(ticker)
        except Exception as exc:  # noqa: BLE001 -- never let one ticker kill the run
            log.warning(
                "[resolve_bracket_outcomes] gamma fetch failed for %s: %s -- "
                "falling back to METAR for this bracket", str(ticker)[:14], exc,
            )
            stats["n_fetch_errors"] += 1
            continue
        if resolved is None:
            stats["n_indecisive"] += 1
            continue
        resolutions[ticker] = bool(resolved)
        cache[ticker] = bool(resolved)
        cache_dirty = True
        stats["n_newly_resolved"] += 1

    if cache_dirty:
        save_gamma_cache(cache_path, cache)

    return resolutions, dict(stats)


def resolve_bracket_rows(
    rows: "list[dict]",
    observed_highs: "dict[tuple[str, str], float]",
    gamma_resolutions: "dict[str, bool] | None" = None,
) -> "tuple[list[dict], dict]":
    """Attach ``observed_high``, ``resolved_yes`` and ``resolution_source`` to
    every resolvable row.

    Resolution precedence mirrors ``settle.py``'s exactly (issue #860):
    Polymarket's definitive resolution wins when available
    (``resolution_source='gamma'``), otherwise the observed daily high decides
    (``resolution_source='metar'``).

    Note a Gamma-resolved row survives even when no observation exists for
    that station-day -- the official outcome does not need METAR to
    corroborate it -- so switching Gamma on can only grow the resolved
    population, never shrink it. ``observed_high`` is still attached when
    known (it stays useful for diagnostics), and is None when not.

    Rows that Gamma cannot resolve AND that have no observed high on record
    are dropped (counted, never guessed). Returns (resolved_rows, counts).
    """
    gamma_resolutions = gamma_resolutions or {}
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

        gamma_yes = gamma_resolutions.get(row.get("ticker"))
        if gamma_yes is not None:
            resolved_yes = bool(gamma_yes)
            resolution_source = "gamma"
            counts["resolved_from_gamma"] += 1
        else:
            if observed_high is None:
                counts["no_observed_high"] += 1
                continue
            resolved_yes = resolve_outcome(
                row.get("bracket_low"), row.get("bracket_high"), observed_high
            )
            if resolved_yes is None:
                counts["missing_bracket_bounds"] += 1
                continue
            resolution_source = "metar"
            counts["resolved_from_metar"] += 1

        out.append({
            **row,
            "observed_high": observed_high,
            "resolved_yes": resolved_yes,
            "resolution_source": resolution_source,
        })
    counts["resolved_rows"] = len(out)
    return out, dict(counts)


# ---------------------------------------------------------------------------
# Public entry point (#822 Pass 2 imports/calls this directly)
# ---------------------------------------------------------------------------

def resolve_bracket_outcomes(
    bracket_evals_base: Path = BRACKET_EVALS_JSONL,
    db_path: "Path | None" = None,
    use_gamma: bool = True,
    allow_network: bool = True,
    gamma_cache_path: "Path | None" = _DEFAULT_CACHE,
) -> "tuple[list[dict], dict]":
    """Load, dedupe, and resolve the outcome of every evaluated bracket.

    Never joins ``settlements`` for resolution (issue #850 -- that join is
    what collapses the population to the ~156 traded brackets). Ground truth
    is Polymarket's definitive resolution where available, falling back to the
    observed daily high from ``observations`` -- the same precedence
    ``settle.py`` uses (issue #860).

    Args:
        use_gamma:      consult Polymarket for definitive resolutions. False
                        reverts to pure-METAR resolution (the pre-#860
                        behaviour) -- useful to reproduce an older report or
                        to isolate the two sources when debugging.
        allow_network:  when ``use_gamma``, permit HTTP fetches for tickers
                        not already cached. False serves from cache only and
                        issues zero requests (the CLI's ``--no-network``).
        gamma_cache_path: persistent {ticker: resolved_yes} cache, so any
                        ticker is fetched at most once ever.

    Returns (resolved_rows, counts):

    - ``resolved_rows``: one dict per (station, ticker, settlement_date) --
      the ``bracket_evals`` row plus ``observed_high`` (float or None),
      ``resolved_yes`` (bool) and ``resolution_source`` ('gamma' | 'metar').
    - ``counts``: exclusion-funnel dict. ``counts["n_bracket_rows"]`` is the
      resolved bracket-row count; ``counts["n_station_days"]`` is the
      distinct (station, settlement_date) count -- the effective-sample-size
      figure for any downstream BSS/reliability power discussion, since all
      brackets sharing a station-day are not independent draws.
      ``counts["resolved_from_gamma"]`` / ``["resolved_from_metar"]`` break
      the population down by which ground truth decided it.
    """
    db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH

    raw_rows = load_bracket_eval_rows(bracket_evals_base)
    deduped = dedupe_one_per_bracket_day(raw_rows)

    station_dates = {
        (r["station"], r["settlement_date"]) for r in deduped
        if r.get("station") and r.get("settlement_date")
    }
    observed_highs = compute_observed_highs(db_path, station_dates)

    gamma_resolutions: "dict[str, bool]" = {}
    gamma_stats: dict = {}
    if use_gamma:
        tickers = {r["ticker"] for r in deduped if r.get("ticker")}
        gamma_resolutions, gamma_stats = resolve_gamma_outcomes(
            tickers, cache_path=gamma_cache_path, allow_network=allow_network
        )

    resolved_rows, counts = resolve_bracket_rows(deduped, observed_highs, gamma_resolutions)
    counts["raw_rows"] = len(raw_rows)
    counts["deduped_bracket_rows"] = len(deduped)
    counts["n_bracket_rows"] = len(resolved_rows)
    counts["n_station_days"] = len({(r["station"], r["settlement_date"]) for r in resolved_rows})
    counts["gamma"] = gamma_stats
    counts["gamma_enabled"] = bool(use_gamma)
    return resolved_rows, counts


#: Multi-YES collision kinds -- see ``classify_multi_yes``.
COLLISION_BOUNDARY = "boundary"    # adjacent/overlapping brackets -- issue #861
COLLISION_DISJOINT = "disjoint"    # brackets that do not touch -- issue #867
COLLISION_UNKNOWN = "unknown"      # bracket bounds missing, cannot classify


def classify_multi_yes(brackets: "list[tuple]") -> str:
    """Classify a multi-YES station-day by the SHAPE of its colliding brackets.

    The distinction matters because the two shapes have entirely different
    causes and entirely different fixes, and conflating them sends a reader to
    the wrong issue (which is exactly what the 2026-07-26 Pass-1 report did):

    - ``boundary`` -- the YES brackets touch or overlap. An observed high
      landing on a shared edge satisfies ``resolve_outcome``'s inclusive
      ``lo <= x <= hi`` for both. This is issue #861, and it is a question
      about which interval convention is correct.
    - ``disjoint`` -- the YES brackets do not touch, so NO interval convention
      of any kind can produce both. Something upstream returned the wrong
      outcome: on 2026-07-26 four such station-days were all Gamma-resolved,
      with brackets 3.6-21.6 F apart. This is issue #867, and it is a question
      about whether the resolution is keyed to the right market.

    *brackets* is the ``(low, high, source)`` list carried by
    ``detect_multi_yes_station_days`` entries. A bracket with a missing bound
    cannot be placed on the number line, so the day is ``unknown`` rather than
    forced into one bucket.
    """
    bounds = []
    for lo, hi, _source in brackets:
        if lo is None or hi is None:
            return COLLISION_UNKNOWN
        try:
            bounds.append((float(lo), float(hi)))
        except (TypeError, ValueError):
            return COLLISION_UNKNOWN
    if len(bounds) < 2:
        return COLLISION_UNKNOWN

    bounds.sort()
    for (_prev_lo, prev_hi), (next_lo, _next_hi) in zip(bounds, bounds[1:]):
        if next_lo > prev_hi:
            return COLLISION_DISJOINT
    return COLLISION_BOUNDARY


def detect_multi_yes_station_days(resolved_rows: "list[dict]") -> "list[dict]":
    """Return station-days where MORE THAN ONE bracket resolved YES.

    A station-day has exactly one daily high, so at most one bracket can
    contain it -- two YES brackets on the same station-day is a logical
    impossibility and means the resolution is wrong somewhere.

    This is a DIAGNOSTIC, not a fix. Each entry carries a ``collision_kind``
    (see ``classify_multi_yes``) separating the two distinct failure modes:
    ``boundary`` collisions are the interval-convention question of issue #861
    (Celsius-derived brackets whose Fahrenheit conversions share an edge --
    e.g. ZGSZ 84.2-86.0 and 86.0-87.8 are 29-30C and 30-31C, and an observed
    86.0F sits on the shared edge), while ``disjoint`` collisions cannot be an
    interval question at all and point at the resolution source itself
    (issue #867).

    Nothing is auto-corrected in either case: the correct interval convention
    is not yet established (production data fits neither half-open form), and
    ``settle.py`` shares the same inclusive expression, so guessing here would
    silently diverge the two.
    """
    by_day: "dict[tuple, list[dict]]" = defaultdict(list)
    for row in resolved_rows:
        if row.get("resolved_yes"):
            by_day[(row.get("station"), row.get("settlement_date"))].append(row)

    out = []
    for (station, settlement_date), rows in sorted(by_day.items(), key=lambda kv: str(kv[0])):
        if len(rows) > 1:
            brackets = [
                (r.get("bracket_low"), r.get("bracket_high"), r.get("resolution_source"))
                for r in rows
            ]
            out.append({
                "station": station,
                "settlement_date": settlement_date,
                "n_yes": len(rows),
                "observed_high": rows[0].get("observed_high"),
                "brackets": brackets,
                "collision_kind": classify_multi_yes(brackets),
                "sources": sorted({
                    str(r.get("resolution_source")) for r in rows
                    if r.get("resolution_source")
                }),
            })
    return out


def cross_check_gamma_vs_metar(resolved_rows: "list[dict]") -> dict:
    """Measure how often the two ground truths disagree (issue #870).

    ``cross_check_against_settlements`` and ``..._direct`` (#858) both compare
    against ``settlements`` -- the ~156 brackets MeteoEdge actually traded.
    This compares the two ground truths on the FULL evaluated population: for
    every Gamma-resolved row that also has an observed daily high on record,
    what would METAR have said, and how often does that differ?

    Why it matters: #822's M3 verdict is scored on a population that was 95.3%
    Gamma-resolved on 2026-07-26, and we do not otherwise know that truth's
    error rate. #867 found four Gamma collisions, but
    ``detect_multi_yes_station_days`` only sees a collision when BOTH colliding
    brackets are in the sample -- so it reports a floor, not a count. This
    measures the whole overlap.

    **A disagreement is not automatically a Gamma error.** METAR is the known-
    weaker proxy -- #644 measured it booking the wrong outcome in ~22% of
    audited settlements, which is precisely why #860 made Gamma authoritative.
    What the rate bounds is the size of the region where the two truths are
    inconsistent, i.e. how much of an M3 verdict could turn on which one was
    picked. Disagreement concentrated on Celsius-denominated stations would
    corroborate #867's keying hypothesis.

    Returns a dict with ``n_comparable``, ``n_agree``, ``n_disagree``,
    ``disagreement_rate``, the two directional counts, and ``by_station``.
    """
    stats: dict = {
        "n_comparable": 0,
        "n_agree": 0,
        "n_disagree": 0,
        "n_gamma_yes_metar_no": 0,
        "n_gamma_no_metar_yes": 0,
        "n_gamma_rows_without_observed_high": 0,
    }
    by_station: "dict[str, dict]" = defaultdict(lambda: {"n": 0, "n_disagree": 0})

    for row in resolved_rows:
        if row.get("resolution_source") != "gamma":
            continue
        observed_high = row.get("observed_high")
        if observed_high is None:
            stats["n_gamma_rows_without_observed_high"] += 1
            continue
        metar_yes = resolve_outcome(
            row.get("bracket_low"), row.get("bracket_high"), observed_high
        )
        if metar_yes is None:
            stats["n_gamma_rows_without_observed_high"] += 1
            continue

        gamma_yes = bool(row.get("resolved_yes"))
        station = str(row.get("station") or "?")
        stats["n_comparable"] += 1
        by_station[station]["n"] += 1
        if gamma_yes == metar_yes:
            stats["n_agree"] += 1
        else:
            stats["n_disagree"] += 1
            by_station[station]["n_disagree"] += 1
            if gamma_yes:
                stats["n_gamma_yes_metar_no"] += 1
            else:
                stats["n_gamma_no_metar_yes"] += 1

    stats["disagreement_rate"] = (
        stats["n_disagree"] / stats["n_comparable"] if stats["n_comparable"] else None
    )
    stats["by_station"] = {
        st: {**v, "rate": v["n_disagree"] / v["n"] if v["n"] else None}
        for st, v in sorted(by_station.items())
    }
    return stats


def detect_zero_yes_station_days(resolved_rows: "list[dict]") -> "list[dict]":
    """Return station-days where NO bracket resolved YES (issue #870).

    The mirror of ``detect_multi_yes_station_days``, and the reason it exists:
    that function flags only MORE than one YES, so a station-day resolving
    nothing was invisible -- and it biases toward NO, the direction that makes
    the model look bad.

    **Not automatically a bug.** Per #861, US stations carry integer-Fahrenheit
    brackets with genuine GAPS between them (KORD 76-77 then 78-79), so an
    observed high landing in a gap legitimately resolves every bracket NO. The
    signal is therefore in the RATE, and specifically in comparing the
    Gamma-sourced rate against the METAR-sourced rate on comparable days: a
    Gamma-only excess points at the resolution source, while a shared rate is
    bracket geometry doing what it is supposed to do.

    ``observed_high_in_a_gap`` records whether the day's observed high fell
    outside every evaluated bracket, which is what distinguishes the two.
    """
    by_day: "dict[tuple, list[dict]]" = defaultdict(list)
    for row in resolved_rows:
        by_day[(row.get("station"), row.get("settlement_date"))].append(row)

    out = []
    for (station, settlement_date), rows in sorted(by_day.items(), key=lambda kv: str(kv[0])):
        if any(r.get("resolved_yes") for r in rows):
            continue
        observed_high = next(
            (r.get("observed_high") for r in rows if r.get("observed_high") is not None), None
        )
        in_a_gap = None
        if observed_high is not None:
            covered = False
            for r in rows:
                if resolve_outcome(r.get("bracket_low"), r.get("bracket_high"), observed_high):
                    covered = True
                    break
            in_a_gap = not covered
        out.append({
            "station": station,
            "settlement_date": settlement_date,
            "n_brackets": len(rows),
            "observed_high": observed_high,
            "observed_high_in_a_gap": in_a_gap,
            "sources": sorted({
                str(r.get("resolution_source")) for r in rows if r.get("resolution_source")
            }),
        })
    return out


def ladder_completeness(resolved_rows: "list[dict]") -> dict:
    """Brackets-per-station-day distribution (issue #870).

    A Polymarket temperature ladder is ~11 brackets. Wide variance means
    brackets are being dropped before ``_write_bracket_evaluations`` logs them,
    and a partial ladder is not the "full evaluated population" #822's Pass 2
    assumes it is scoring.
    """
    per_day: "dict[tuple, int]" = defaultdict(int)
    for row in resolved_rows:
        per_day[(row.get("station"), row.get("settlement_date"))] += 1

    counts = sorted(per_day.values())
    if not counts:
        return {"n_station_days": 0, "histogram": {}, "median": None, "min": None, "max": None}

    histogram: "dict[int, int]" = defaultdict(int)
    for c in counts:
        histogram[c] += 1
    mid = len(counts) // 2
    median = counts[mid] if len(counts) % 2 else (counts[mid - 1] + counts[mid]) / 2
    return {
        "n_station_days": len(counts),
        "histogram": dict(sorted(histogram.items())),
        "median": median,
        "min": counts[0],
        "max": counts[-1],
    }


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
        # resolution_source arrived as a migration (db.py's ensure-column
        # list), so a DB predating it -- or a minimal test fixture -- may not
        # have the column. Probe rather than SELECT it blindly: the enclosing
        # OperationalError handler returns [], which would silently zero the
        # whole correctness check instead of just omitting one column.
        have_resolution_source = any(
            r["name"] == "resolution_source"
            for r in con.execute("PRAGMA table_info(settlements)").fetchall()
        )
        cols = "ticker, station, bracket_low, bracket_high, actual_high_f, resolved_yes"
        if have_resolution_source:
            cols += ", resolution_source"
        cur = con.execute(f"SELECT {cols} FROM settlements")
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
    # Mismatches split by which ground truth the settlements row itself used
    # (issue #860). A row settled from 'gamma' disagreeing with our METAR
    # recomputation is the EXPECTED ~22% divergence #644 documented -- not
    # evidence this module is broken. A 'metar'-settled row disagreeing is
    # unexplained and genuinely worth investigating, since both sides then
    # claim to be computing the same thing from the same observations.
    mismatch_by_source: dict = defaultdict(int)
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
            source = row.get("resolution_source") or "unknown"
            mismatch_by_source[source] += 1
            mismatches.append({
                "station": row.get("station"),
                "ticker": row.get("ticker"),
                "settlement_date": settlement_date,
                "bracket_low": row.get("bracket_low"),
                "bracket_high": row.get("bracket_high"),
                "observed_high": observed_high,
                "settlements_actual_high_f": row.get("actual_high_f"),
                "direct_resolved_yes": direct_yes,
                "settlements_resolved_yes": settlements_yes,
                "settlements_resolution_source": source,
            })

    return {
        "n_settlement_rows": n_settlement_rows,
        "n_no_trade_date": n_no_trade_date,
        "n_no_observed_high": n_no_observed_high,
        "n_checked": n_match + len(mismatches),
        "n_match": n_match,
        "n_mismatch": len(mismatches),
        "mismatch_by_source": dict(mismatch_by_source),
        "mismatches": mismatches,
    }


# ---------------------------------------------------------------------------
# Dry-run CLI
# ---------------------------------------------------------------------------

def _ground_truth_quality_sections(resolved_rows: "list[dict]") -> "list[str]":
    """Render the three ground-truth quality checks of issue #870.

    Read together they answer one question: **how much can the M3 verdict turn
    on the quality of the outcomes it was scored against?** M3 is ~95%
    Gamma-resolved, and until these run that error rate is simply unknown.
    """
    lines: "list[str]" = []

    # --- 1. Gamma vs METAR -------------------------------------------------
    gm = cross_check_gamma_vs_metar(resolved_rows)
    lines.append("## Ground-truth quality 1/3: Gamma vs METAR (issue #870)\n")
    lines.append(
        "Both truths on the FULL evaluated population, not just the ~156 traded brackets "
        "`cross_check_against_settlements` can see. A disagreement is NOT automatically a "
        "Gamma error -- METAR is the weaker proxy (#644: wrong ~22% of audited "
        "settlements, which is why #860 made Gamma authoritative). What this bounds is how "
        "much of an M3 verdict could turn on which truth was picked.\n"
    )
    if not gm["n_comparable"]:
        lines.append(
            "No Gamma-resolved row also has an observed daily high on record -- nothing to "
            "compare. (Expected when running `--no-gamma`, or before `observations` covers "
            "the Gamma-resolved station-days.)\n"
        )
    else:
        rate = gm["disagreement_rate"]
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        lines.append(f"| Comparable rows (Gamma-resolved AND observed high known) | {gm['n_comparable']} |")
        lines.append(f"| Agree | {gm['n_agree']} |")
        lines.append(f"| **Disagree** | **{gm['n_disagree']}** |")
        lines.append(f"| **Disagreement rate** | **{rate:.1%}** |")
        lines.append(f"| Gamma YES / METAR NO | {gm['n_gamma_yes_metar_no']} |")
        lines.append(f"| Gamma NO / METAR YES | {gm['n_gamma_no_metar_yes']} |")
        lines.append(
            f"| Gamma rows with no observed high (not comparable) | "
            f"{gm['n_gamma_rows_without_observed_high']} |"
        )
        lines.append("")
        worst = sorted(
            (s for s in gm["by_station"].items() if s[1]["n_disagree"]),
            key=lambda kv: (-kv[1]["rate"], kv[0]),
        )[:15]
        if worst:
            lines.append("Stations with any disagreement (worst rate first) -- a cluster on "
                         "Celsius-denominated stations would corroborate #867's keying "
                         "hypothesis:\n")
            lines.append("| Station | Comparable | Disagree | Rate |")
            lines.append("|---|---|---|---|")
            for station, v in worst:
                lines.append(f"| {station} | {v['n']} | {v['n_disagree']} | {v['rate']:.1%} |")
            lines.append("")

    # --- 2. Zero-YES station-days -----------------------------------------
    zero_yes = detect_zero_yes_station_days(resolved_rows)
    n_gap = sum(1 for z in zero_yes if z["observed_high_in_a_gap"] is True)
    n_not_gap = sum(1 for z in zero_yes if z["observed_high_in_a_gap"] is False)
    n_unknown_gap = sum(1 for z in zero_yes if z["observed_high_in_a_gap"] is None)

    lines.append("## Ground-truth quality 2/3: station-days with NO YES bracket (issue #870)\n")
    lines.append(
        "The mirror of the multi-YES check above, which sees only MORE than one YES -- so a "
        "day resolving nothing was invisible, and it biases toward NO (the direction that "
        "flatters the market). NOT automatically a bug: US stations have integer-Fahrenheit "
        "brackets with real GAPS between them (#861), so a high landing in a gap correctly "
        "resolves everything NO.\n"
    )
    lines.append("| Shape | Station-days | Reading |")
    lines.append("|---|---|---|")
    lines.append(f"| Observed high fell in a bracket GAP | {n_gap} | Expected -- bracket "
                 f"geometry working correctly |")
    lines.append(f"| Observed high WAS covered by a bracket, yet nothing resolved YES | "
                 f"{n_not_gap} | **Suspicious** -- a covered high must resolve exactly one "
                 f"bracket YES |")
    lines.append(f"| No observed high on record | {n_unknown_gap} | Undeterminable |")
    lines.append("")
    suspicious = [z for z in zero_yes if z["observed_high_in_a_gap"] is False]
    if suspicious:
        lines.append("| Station | Settlement date | Brackets | Observed high | Sources |")
        lines.append("|---|---|---|---|---|")
        for z in suspicious[:25]:
            lines.append(
                f"| {z['station']} | {z['settlement_date']} | {z['n_brackets']} | "
                f"{z['observed_high']} | {', '.join(z['sources']) or '-'} |"
            )
        if len(suspicious) > 25:
            lines.append(f"| ... | _{len(suspicious) - 25} more_ | | | |")
        lines.append("")

    # --- 3. Ladder completeness -------------------------------------------
    ladder = ladder_completeness(resolved_rows)
    lines.append("## Ground-truth quality 3/3: ladder completeness (issue #870)\n")
    lines.append(
        "A Polymarket temperature ladder is ~11 brackets. Wide variance means brackets are "
        "dropped before `_write_bracket_evaluations` logs them, and a partial ladder is not "
        "the full evaluated population #822's Pass 2 assumes it is scoring.\n"
    )
    if not ladder["n_station_days"]:
        lines.append("No station-days to measure.\n")
    else:
        lines.append(
            f"**Station-days: {ladder['n_station_days']} | brackets per station-day -- "
            f"min {ladder['min']}, median {ladder['median']}, max {ladder['max']}**\n"
        )
        lines.append("| Brackets on the day | Station-days |")
        lines.append("|---|---|")
        for n_brackets, n_days in ladder["histogram"].items():
            lines.append(f"| {n_brackets} | {n_days} |")
        lines.append("")
    return lines


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

    # Ground-truth breakdown (issue #860)
    gamma_stats = counts.get("gamma") or {}
    lines.append("## Ground truth used (issue #860)\n")
    if not counts.get("gamma_enabled", False):
        lines.append(
            "> **Gamma resolution DISABLED for this run** (`--no-gamma`). Every row below "
            "was resolved from METAR observations only -- the pre-#860 behaviour, which "
            "`settle.py` itself does not use. Not suitable for the #822 M3 verdict.\n"
        )
    lines.append(
        "Resolution precedence mirrors `settle.py`: Polymarket's definitive on-chain "
        "resolution wins where available, METAR observed-high decides otherwise. The "
        "METAR comparison booked the wrong outcome in ~22% of audited settlements "
        "(#644), so a high `gamma` share is what makes the M3 gate trustworthy.\n"
    )
    lines.append("| Metric | Count |")
    lines.append("|---|---|")
    lines.append(f"| Resolved from **gamma** (official resolution) | {counts.get('resolved_from_gamma', 0)} |")
    lines.append(f"| Resolved from **metar** (observed daily high) | {counts.get('resolved_from_metar', 0)} |")
    lines.append(f"| Gamma cache hits (no network call) | {gamma_stats.get('n_cache_hits', 0)} |")
    lines.append(f"| Gamma tickers newly fetched | {gamma_stats.get('n_fetched', 0)} |")
    lines.append(f"| ...of which newly resolved + cached | {gamma_stats.get('n_newly_resolved', 0)} |")
    lines.append(
        f"| ...not yet decisively resolved (retried next run) | "
        f"{gamma_stats.get('n_indecisive', 0)} |"
    )
    lines.append(f"| ...fetch errors (degraded to METAR) | {gamma_stats.get('n_fetch_errors', 0)} |")
    if gamma_stats.get("n_skipped_offline"):
        lines.append(
            f"| Skipped -- offline mode (`--no-network`) | "
            f"{gamma_stats.get('n_skipped_offline', 0)} |"
        )
    lines.append("")

    multi_yes = detect_multi_yes_station_days(resolved_rows)
    n_boundary = sum(1 for m in multi_yes if m["collision_kind"] == COLLISION_BOUNDARY)
    n_disjoint = sum(1 for m in multi_yes if m["collision_kind"] == COLLISION_DISJOINT)
    n_unknown = sum(1 for m in multi_yes if m["collision_kind"] == COLLISION_UNKNOWN)

    lines.append("## Diagnostic: station-days with more than one YES bracket\n")
    lines.append(
        "A station-day has exactly ONE daily high, so at most one bracket can contain it. "
        "More than one YES is a logical impossibility and means resolution is wrong "
        "somewhere. The two shapes below have different causes and different fixes -- "
        "do not read them as one number.\n"
    )
    lines.append("| Collision shape | Station-days | What it means |")
    lines.append("|---|---|---|")
    lines.append(
        f"| `boundary` (adjacent/overlapping) | {n_boundary} | Issue #861 -- Celsius-derived "
        f"brackets whose Fahrenheit conversions share an edge (ZGSZ 84.2-86.0 and 86.0-87.8 "
        f"are 29-30C and 30-31C); `resolve_outcome`'s inclusive `lo <= x <= hi` puts a value "
        f"on the shared edge in both. Correct convention not yet established. |"
    )
    lines.append(
        f"| `disjoint` (brackets do not touch) | {n_disjoint} | Issue #867 -- NO interval "
        f"convention can produce both. The resolution source itself returned the wrong "
        f"outcome; suspect ticker/market keying. |"
    )
    if n_unknown:
        lines.append(
            f"| `unknown` | {n_unknown} | Bracket bounds missing -- cannot be placed on the "
            f"number line. |"
        )
    lines.append("")
    lines.append(f"**Affected station-days: {len(multi_yes)}**\n")
    if multi_yes:
        lines.append("| station | settlement_date | shape | n_yes | observed_high | "
                     "brackets (low, high, source) |")
        lines.append("|---|---|---|---|---|---|")
        for m in multi_yes[:25]:
            brackets = "; ".join(f"({b[0]}, {b[1]}, {b[2]})" for b in m["brackets"])
            lines.append(
                f"| {m['station']} | {m['settlement_date']} | `{m['collision_kind']}` | "
                f"{m['n_yes']} | {m['observed_high']} | {brackets} |"
            )
        if len(multi_yes) > 25:
            lines.append(f"| ... | ... | ... | ... | ... | _{len(multi_yes) - 25} more_ |")
        lines.append("")
    lines.append(
        "Reported, deliberately not silently 'fixed' -- `settle.py` shares the same "
        "inclusive interval expression, so changing one call site alone would diverge the "
        "two resolution paths.\n"
    )

    lines.extend(_ground_truth_quality_sections(resolved_rows))
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

    by_source = direct_check.get("mismatch_by_source") or {}
    if by_source:
        lines.append("**Mismatches by the settlements row's OWN resolution source (issue #860):**\n")
        lines.append("| settlements.resolution_source | Mismatches | Interpretation |")
        lines.append("|---|---|---|")
        gamma_family = (
            "**Expected.** That row was settled from Polymarket's official resolution, "
            "which #644 measured as disagreeing with METAR ~22% of the time. Not "
            "evidence this resolver is broken."
        )
        interpretations = {
            "gamma": gamma_family,
            # repair_settlements_from_gamma.py's back-fill pass, which OVERWRITES a
            # settlement using the market's official final price -- authoritative
            # gamma truth, and deliberately so, hence the same reading as 'gamma'.
            "gamma_repair": (
                gamma_family + " (Written by the `repair_settlements_from_gamma` "
                "back-fill, which corrected this row from the official final price.)"
            ),
            "metar": "**Investigate.** Both sides claim to compute the observed daily "
                     "high from the same observations, so a disagreement here is "
                     "unexplained.",
            "unknown": "Row predates the `resolution_source` column -- source unknown, "
                       "so cannot be attributed either way.",
        }
        for source, n in sorted(by_source.items(), key=lambda kv: -kv[1]):
            # Any future gamma-derived source name still reads as gamma-family rather
            # than rendering as an unexplained blank (which is what 'gamma_repair'
            # itself did on the 2026-07-25 run).
            default = gamma_family if str(source).startswith("gamma") else "--"
            lines.append(f"| `{source}` | {n} | {interpretations.get(source, default)} |")
        lines.append("")

        unexplained = sum(
            n for s, n in by_source.items() if not str(s).startswith("gamma") and s != "unknown"
        )
        lines.append(
            f"**Unexplained mismatches (non-gamma-sourced): {unexplained}.** This is the "
            "number that indicates a defect in this resolver; gamma-sourced disagreement "
            "is the documented #644 divergence, not a bug.\n"
        )

    if direct_check.get("mismatches"):
        lines.append("### Mismatches\n")
        lines.append(
            "`observed_high` is what THIS module recomputed from `observations`; "
            "`settlements.actual_high_f` is what the settle path recorded. When those two "
            "differ, the disagreement is about the temperature itself; when they are equal "
            "but the verdicts differ, it is about the resolution rule (bracket-boundary "
            "convention, issue #861) or about gamma-vs-metar precedence.\n"
        )
        lines.append(
            "| station | ticker | settlement_date | bracket_low | bracket_high | "
            "observed_high | settlements.actual_high_f | direct | settlements | source |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for m in direct_check["mismatches"]:
            lines.append(
                f"| {m['station']} | {m['ticker']} | {m['settlement_date']} | "
                f"{m['bracket_low']} | {m['bracket_high']} | {m['observed_high']} | "
                f"{m.get('settlements_actual_high_f')} | "
                f"{m['direct_resolved_yes']} | {m['settlements_resolved_yes']} | "
                f"`{m.get('settlements_resolution_source', 'unknown')}` |"
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
    bracket_evals_base: Path,
    db_path: Path,
    out_dir: Path,
    run_date: "str | None" = None,
    use_gamma: bool = True,
    allow_network: bool = True,
    gamma_cache_path: "Path | None" = _DEFAULT_CACHE,
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

    resolved_rows, counts = resolve_bracket_outcomes(
        bracket_evals_base,
        db_path,
        use_gamma=use_gamma,
        allow_network=allow_network,
        gamma_cache_path=gamma_cache_path,
    )

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
        "ground truth: gamma=%d metar=%d, "
        "settlements cross-check: n_overlap=%d n_mismatch=%d, "
        "settlements-direct check: n_checked=%d n_mismatch=%d %s)",
        out_path, counts["n_bracket_rows"], counts["n_station_days"],
        counts.get("resolved_from_gamma", 0), counts.get("resolved_from_metar", 0),
        cross_check["n_overlap"], cross_check["n_mismatch"],
        direct_check["n_checked"], direct_check["n_mismatch"],
        direct_check.get("mismatch_by_source") or {},
    )
    return 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bracket-evals", type=Path, default=BRACKET_EVALS_JSONL)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--run-date", default=None,
                    help="Report date stamp (default: today, UTC)")
    ap.add_argument("--gamma-cache", type=Path, default=DEFAULT_GAMMA_CACHE_PATH,
                    help="Persistent {ticker: resolved_yes} cache so any ticker is "
                         "fetched from Polymarket at most once ever")
    ap.add_argument("--no-network", action="store_true",
                    help="Never issue HTTP requests: serve Gamma resolutions from the "
                         "cache only and fall back to METAR for anything uncached")
    ap.add_argument("--no-gamma", action="store_true",
                    help="Ignore Polymarket resolutions entirely and resolve purely from "
                         "METAR observed highs (pre-#860 behaviour; NOT suitable for the "
                         "#822 M3 verdict -- see #644)")
    args = ap.parse_args(argv)
    return run_dry_run(
        args.bracket_evals, args.db, args.out, args.run_date,
        use_gamma=not args.no_gamma,
        allow_network=not args.no_network,
        gamma_cache_path=args.gamma_cache,
    )


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    raise SystemExit(main())

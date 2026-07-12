#!/usr/bin/env python3
"""Self-gating prob-cap shadow report (issue #570, delivers the #551 cap decision).

PR #564 (issue #551 stage 1) made every trade candidate log both the capped
``p_yes`` and the raw (pre-clamp) ``p_yes_raw``. This script uses that data,
already accumulating on the bot host, to answer the #551 question -- should
``MODEL_PROB_CAP`` move from 0.95 to 0.97/0.98 -- once >= ``--min-days`` of
history exists.

Self-gating: below the day threshold this script logs one line and exits 0
(safe to schedule daily from day one). At/above the threshold it writes
``backtest_results/prob_cap_shadow_<date>.md``.

Data sources (all host-local files/DB next to analytics.db -- no network,
no GitHub Actions access required):

  * ``logs/candidates.csv`` (date-rotated, see ``src/utils/log_rotation.py``)
    -- one row per *poll* for every candidate that already cleared all live
    gates, capped and raw probabilities included. Used only to count distinct
    dates with data for the self-gate check (fast, no realized-outcome join
    needed).
  * ``logs/settlements.csv`` (flat, append-only, written by
    ``src/scripts/settle.py``) -- the same per-candidate rows as above, plus
    the realized outcome (``yes_won`` / ``pnl_cents``) once settled. This is
    the primary source for clamp-saturation, distribution, and the
    already-admitted side of the cap simulation.
  * ``logs/snapshots.jsonl`` (date-rotated) -- every bracket *evaluated* each
    poll, gated or not (``src/strategy/scanner.py::scan_markets`` emits one of
    these per market per poll regardless of whether entry gates passed).
    Needed because ``candidates.csv``/``settlements.csv`` only contain
    candidates that already cleared ``MIN_EDGE_CENTS`` under the *deployed*
    cap -- a laxer simulated floor can pull sub-threshold brackets over that
    edge bar, and those brackets never became a logged Candidate under the
    real cap. This is the only source that can surface them.

BINDING (PM comment, 2026-07-02, issue #570): ``MAX_CONFIDENCE_YES_FOR_NO`` is
held FIXED at its live value while ``MODEL_PROB_CAP`` is varied in simulation
(the two are never scaled together, even though they are numerically equal
today at 0.05 / 1-0.95). Every simulated NO admission gained over the deployed
cap is attributed to exactly one of two channels -- see
``classify_new_admission_channel()`` for the exact rule and why the
"gate_headroom" channel is expected to be 0 for any cap in [0.95, 1) as long
as the gate stays fixed at 0.05.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import logging
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import (  # noqa: E402
    CANDIDATES_CSV, SETTLEMENTS_CSV, SNAPSHOTS_JSONL,
    MIN_EDGE_CENTS, MAX_EDGE_CENTS, MIN_PRICE_CENTS,
    MAX_CONFIDENCE_YES_FOR_NO, MODEL_PROB_CAP,
)
from src.strategy.fee import estimate_fee_cents  # noqa: E402
from src.utils.log_rotation import rotated_sources, iter_rotated_jsonl  # noqa: E402

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

DEFAULT_CAP_VALUES = (0.95, 0.97, 0.98)

# Issue #682 (2026-07-10 diagnosis): logs/settlements.csv is dead (starved
# since 2026-06-17 by candidates.csv date-rotation, #683) and never carried
# p_yes_raw anyway (pre-#564 schema). meteoedge.db::trades is the real,
# DB-first settled-candidates source; analytics.db::snapshot_archive is the
# population-level saturation source. Same default paths/env vars as
# src/data/db.py and src/data/archive_db.py for consistency.
DEFAULT_METEOEDGE_DB = Path(os.getenv("DB_PATH", "data/meteoedge.db"))
DEFAULT_ANALYTICS_DB = Path(os.getenv("ARCHIVE_DB_PATH", "data/analytics.db"))
# First day of full p_yes_raw coverage on `trades` per the diagnosis (#682).
DEFAULT_SINCE_TS = "2026-07-03"


# ---------------------------------------------------------------------------
# Low-level readers
# ---------------------------------------------------------------------------

def _iter_csv_rows(base: Path) -> Iterator[dict]:
    """Yield dict rows across every rotated (and possibly gzipped) source for *base*.

    Mirrors ``src.utils.log_rotation.iter_rotated_jsonl`` for CSV files -- no
    equivalent CSV helper exists there today. Works transparently for both
    date-rotated files (``candidates.csv``) and flat, never-rotated files
    (``settlements.csv``, which ``rotated_sources`` returns as its own single
    source since it is never opened via ``rotated_path``).
    """
    for path in rotated_sources(base):
        is_gz = path.suffix == ".gz"
        try:
            opener = (
                gzip.open(path, "rt", encoding="utf-8", newline="")
                if is_gz else open(path, "r", encoding="utf-8", newline="")
            )
            with opener as fh:
                yield from csv.DictReader(fh)
        except OSError as exc:
            log.warning("[prob_cap_shadow] could not read %s: %s", path, exc)


def _f(row: dict, key: str) -> "float | None":
    val = row.get(key)
    if val is None or val == "" or val == "None":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _b(row: dict, key: str) -> "bool | None":
    val = row.get(key)
    if val is None or val == "":
        return None
    return str(val).strip().lower() == "true"


# ---------------------------------------------------------------------------
# Self-gating: distinct dates with p_yes_raw data
# ---------------------------------------------------------------------------

def distinct_dates_with_raw_data(candidates_csv: Path) -> set:
    """Return the set of UTC dates (YYYY-MM-DD) with >=1 non-NULL p_yes_raw row.

    Cheap existence check: stops reading a given day's file as soon as one
    qualifying row is found.
    """
    import re
    dates: set = set()
    date_re = re.compile(r"(\d{4}-\d{2}-\d{2})")
    for path in rotated_sources(candidates_csv):
        m = date_re.search(path.name)
        file_date = m.group(1) if m else None
        is_gz = path.suffix == ".gz"
        try:
            opener = (
                gzip.open(path, "rt", encoding="utf-8", newline="")
                if is_gz else open(path, "r", encoding="utf-8", newline="")
            )
            with opener as fh:
                for row in csv.DictReader(fh):
                    if _f(row, "p_yes_raw") is not None:
                        dates.add(file_date or (row.get("ts", "")[:10]))
                        break
        except OSError as exc:
            log.warning("[prob_cap_shadow] could not read %s: %s", path, exc)
    return dates


# ---------------------------------------------------------------------------
# Loading + normalizing settled candidates
# ---------------------------------------------------------------------------

def load_settled_candidates(settlements_csv: Path) -> list[dict]:
    """Load and numerically normalize every row from settlements.csv.

    Rows with no p_yes_raw (pre-#564 data) are kept but flagged so callers can
    exclude them from clamp-saturation stats without losing the row entirely.
    """
    out = []
    for row in _iter_csv_rows(settlements_csv):
        p_yes = _f(row, "p_yes")
        p_yes_raw = _f(row, "p_yes_raw")
        price = _f(row, "flagged_price")
        pnl = _f(row, "pnl_cents")
        if p_yes is None or price is None:
            continue
        out.append({
            "ts": row.get("ts", ""),
            "date": row.get("ts", "")[:10],
            "station": row.get("station", ""),
            "ticker": row.get("ticker", ""),
            "side": row.get("flagged_side", ""),
            "bracket_low": _f(row, "bracket_low"),
            "bracket_high": _f(row, "bracket_high"),
            "price_cents": price,
            "p_yes": p_yes,
            "p_yes_raw": p_yes_raw,
            "ev_no": _f(row, "ev_no"),
            "ev_no_raw": _f(row, "ev_no_raw"),
            "actual_high": _f(row, "actual_high"),
            "yes_won": _b(row, "yes_won"),
            "pnl_cents": pnl,
        })
    return out


def load_snapshots(snapshots_jsonl: Path) -> list[dict]:
    """Load every evaluated-bracket snapshot (gated or not) across rotation."""
    out = []
    for rec in iter_rotated_jsonl(snapshots_jsonl):
        raw = rec.get("raw_p_yes")
        no_ask = rec.get("no_ask")
        if raw is None or no_ask is None:
            continue
        out.append({
            "ts": rec.get("ts", ""),
            "date": str(rec.get("ts", ""))[:10],
            "station": rec.get("station", ""),
            "ticker": rec.get("ticker", ""),
            "bracket_low": rec.get("bracket_low"),
            "bracket_high": rec.get("bracket_high"),
            "no_ask": float(no_ask),
            "raw_p_yes": float(raw),
        })
    return out


# ---------------------------------------------------------------------------
# Cap-clamp math (mirrors src/strategy/scanner.py exactly)
# ---------------------------------------------------------------------------

def clamp_p_yes(raw_p_yes: float, cap: float) -> float:
    """Reproduce scanner.py's symmetric clamp, including the round() IEEE-creep guard."""
    floor = round(1.0 - cap, 10)
    return min(max(raw_p_yes, floor), cap)


def simulated_ev_no(p_yes_sim: float, price_cents: float) -> float:
    fee = estimate_fee_cents(int(round(price_cents)))
    return (1 - p_yes_sim) * 100 - price_cents - fee


# ---------------------------------------------------------------------------
# DB-backed loading (issue #682, 2026-07-10 diagnosis)
#
# logs/settlements.csv is dead and never carried p_yes_raw; meteoedge.db and
# analytics.db are the real data sources on the bot host. Every helper here
# is read-only (opened with SQLite's ?mode=ro URI -- this script must never
# write to production databases) and degrades to an empty/zero result rather
# than raising when a DB file is missing or a table doesn't exist yet, so a
# fresh checkout with no local data still produces an honest "no data"
# report instead of crashing (see src/tests for the empty-environment case).
# ---------------------------------------------------------------------------

def _connect_ro(db_path: "Path | None") -> "sqlite3.Connection | None":
    """Open *db_path* read-only. Returns None if missing or unopenable.

    Never creates or writes to the target file, unlike ``sqlite3.connect``
    on a plain path (which silently creates an empty DB -- the exact
    footgun that produced a false "zero rows" reading during the #682
    diagnosis).
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
        log.warning("[prob_cap_shadow] could not open %s read-only: %s", db_path, exc)
        return None


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def distinct_dates_with_raw_data_db(meteoedge_db: "Path | None",
                                     since_ts: "str | None" = None) -> set:
    """DB-backed self-gate: distinct UTC dates with >=1 settled trades.p_yes_raw row."""
    con = _connect_ro(meteoedge_db)
    if con is None:
        return set()
    try:
        if not _table_exists(con, "trades"):
            return set()
        sql = "SELECT DISTINCT substr(ts, 1, 10) AS d FROM trades WHERE p_yes_raw IS NOT NULL"
        params: tuple = ()
        if since_ts:
            sql += " AND ts >= ?"
            params = (since_ts,)
        cur = con.execute(sql, params)
        return {row["d"] for row in cur.fetchall() if row["d"]}
    except sqlite3.OperationalError as exc:
        log.warning("[prob_cap_shadow] distinct_dates_with_raw_data_db failed: %s", exc)
        return set()
    finally:
        con.close()


def load_settled_candidates_db(meteoedge_db: "Path | None", since_ts: str,
                                cap: float = MODEL_PROB_CAP) -> list[dict]:
    """DB-backed replacement for load_settled_candidates(): meteoedge.db::trades.

    Settlement is same-row (settled_at/outcome/pnl), so the join hit-rate to
    outcomes is 100% by construction (#682 diagnosis). The DB stores only the
    raw (pre-clamp) probability, never the capped one -- p_yes is
    reconstructed deterministically via clamp_p_yes(), exactly mirroring
    what scanner.py did live.

    "won" (whether the flagged side won money) is pnl > 0, per the PM's
    diagnosis-comment mapping. yes_won -- whether the *YES* outcome
    occurred, the flag every downstream stat here keys off -- is derived
    from that: a NO-side trade wins money exactly when YES did NOT happen.

    Issue #704: filters to ``is_next_day = 0`` -- this is exactly the
    settled-candidates population Amendment 1 (#682) was written to keep
    clean, and next-day shadow rows (different sigma/lead-time regime, #687)
    would otherwise contaminate it once NEXT_DAY_EVALUATION is on.
    """
    con = _connect_ro(meteoedge_db)
    if con is None:
        return []
    try:
        if not _table_exists(con, "trades"):
            return []
        cur = con.execute(
            "SELECT ts, station, ticker, bracket_low, bracket_high, side, "
            "p_yes_raw, actual_price, pnl FROM trades "
            "WHERE settled_at IS NOT NULL AND p_yes_raw IS NOT NULL AND ts >= ? "
            "AND is_next_day = 0",
            (since_ts,),
        )
        rows = cur.fetchall()
    except sqlite3.OperationalError as exc:
        log.warning("[prob_cap_shadow] load_settled_candidates_db failed: %s", exc)
        return []
    finally:
        con.close()

    out = []
    for r in rows:
        p_yes_raw = r["p_yes_raw"]
        price = r["actual_price"]
        if p_yes_raw is None or price is None:
            continue
        pnl = r["pnl"]
        side = r["side"]
        price_cents = float(price)
        p_yes = clamp_p_yes(p_yes_raw, cap)
        won = pnl is not None and pnl > 0
        if pnl is None:
            yes_won = None
        elif side == "NO":
            yes_won = not won
        elif side == "YES":
            yes_won = won
        else:
            yes_won = None
        out.append({
            "ts": r["ts"] or "",
            "date": (r["ts"] or "")[:10],
            "station": r["station"],
            "ticker": r["ticker"],
            "side": side,
            "bracket_low": r["bracket_low"],
            "bracket_high": r["bracket_high"],
            "price_cents": price_cents,
            "p_yes": p_yes,
            "p_yes_raw": p_yes_raw,
            "ev_no": simulated_ev_no(p_yes, price_cents),
            "ev_no_raw": simulated_ev_no(p_yes_raw, price_cents),
            # Not stored on trades -- see load_actual_high_lookup_db(), which
            # resolves newly-discovered (not already-settled) snapshot
            # candidates via observations daily highs instead.
            "actual_high": None,
            "yes_won": yes_won,
            "pnl_cents": pnl,
        })
    return out


def _saturation_bucket(rows: "list[tuple[str, float, float]]") -> dict:
    """Shared aggregation for the two population-saturation sources below.

    *rows* is an iterable of (station, raw_p_yes, capped_or_recomputed_p_yes).
    """
    by_station: dict = defaultdict(lambda: [0, 0])
    clamped_raw_values = []
    total_clamped = 0
    total = 0
    for station, raw, capped in rows:
        total += 1
        by_station[station][1] += 1
        if abs(raw - capped) > 1e-6:
            by_station[station][0] += 1
            total_clamped += 1
            clamped_raw_values.append(raw)
    return {
        "total": total,
        "total_clamped": total_clamped,
        "by_station": dict(by_station),
        "clamped_raw_values": clamped_raw_values,
    }


def population_saturation_from_archive(analytics_db: "Path | None",
                                        since_ts: str) -> dict:
    """Population-level clamp saturation from analytics.db::snapshot_archive.

    Every evaluated bracket (gated or not) has both raw_p_yes and
    capped_p_yes recorded here -- no per-row "side" though (evaluation
    happens before any entry gate), so this complements rather than
    replaces the settled-trades, side-aware saturation table.
    """
    con = _connect_ro(analytics_db)
    if con is None:
        return _saturation_bucket([])
    try:
        if not _table_exists(con, "snapshot_archive"):
            return _saturation_bucket([])
        cur = con.execute(
            "SELECT station, raw_p_yes, capped_p_yes FROM snapshot_archive "
            "WHERE ts >= ? AND raw_p_yes IS NOT NULL AND capped_p_yes IS NOT NULL",
            (since_ts,),
        )
        rows = [(r["station"], r["raw_p_yes"], r["capped_p_yes"]) for r in cur.fetchall()]
    except sqlite3.OperationalError as exc:
        log.warning("[prob_cap_shadow] population_saturation_from_archive failed: %s", exc)
        return _saturation_bucket([])
    finally:
        con.close()
    return _saturation_bucket(rows)


def latest_archived_snapshot_ts(analytics_db: "Path | None") -> "str | None":
    con = _connect_ro(analytics_db)
    if con is None:
        return None
    try:
        if not _table_exists(con, "snapshot_archive"):
            return None
        row = con.execute("SELECT MAX(ts) AS m FROM snapshot_archive").fetchone()
        return row["m"] if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


def population_saturation_from_recent_jsonl(snapshots_jsonl: Path, since_ts: str,
                                             after_ts: "str | None" = None,
                                             cap: float = MODEL_PROB_CAP) -> dict:
    """Gap-fill population saturation for the archiver's ~24h ingest lag.

    Reads the live, not-yet-archived logs/snapshots.<date>.jsonl. Only rows
    strictly after *after_ts* (the archive's high-water mark) are counted,
    so this never double-counts rows population_saturation_from_archive()
    already has.
    """
    rows = []
    for s in load_snapshots(snapshots_jsonl):
        if s["ts"] < since_ts:
            continue
        if after_ts is not None and s["ts"] <= after_ts:
            continue
        capped = clamp_p_yes(s["raw_p_yes"], cap)
        rows.append((s["station"], s["raw_p_yes"], capped))
    return _saturation_bucket(rows)


def merge_saturation_dicts(a: dict, b: dict) -> dict:
    by_station: dict = defaultdict(lambda: [0, 0])
    for src in (a, b):
        for station, (clamped, tot) in src.get("by_station", {}).items():
            by_station[station][0] += clamped
            by_station[station][1] += tot
    return {
        "total": a.get("total", 0) + b.get("total", 0),
        "total_clamped": a.get("total_clamped", 0) + b.get("total_clamped", 0),
        "by_station": dict(by_station),
        "clamped_raw_values": a.get("clamped_raw_values", []) + b.get("clamped_raw_values", []),
    }


def guardrail_cap_applied_count(meteoedge_db: "Path | None", since_ts: str) -> "int | None":
    """Cross-check count for population saturation: guardrail_events.cap_applied.

    Population-level saturation is computed from snapshot_archive above;
    this is only a sanity cross-check (the #682 diagnosis found the two
    agree to within a few rows/day), so a mismatch is not itself an error.
    """
    con = _connect_ro(meteoedge_db)
    if con is None:
        return None
    try:
        if not _table_exists(con, "guardrail_events"):
            return None
        row = con.execute(
            "SELECT COUNT(*) AS n FROM guardrail_events "
            "WHERE event_type='cap_applied' AND ts >= ?",
            (since_ts,),
        ).fetchone()
        return row["n"] if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


def population_saturation_stats_db(analytics_db: "Path | None", snapshots_jsonl: Path,
                                    since_ts: str,
                                    meteoedge_db: "Path | None" = None,
                                    cap: float = MODEL_PROB_CAP) -> dict:
    """Combine the archive + live-jsonl population saturation sources.

    Returns the merged dict plus a `guardrail_cap_applied` cross-check count
    (None if guardrail_events is unavailable).
    """
    archive = population_saturation_from_archive(analytics_db, since_ts)
    cutoff_ts = latest_archived_snapshot_ts(analytics_db)
    recent = population_saturation_from_recent_jsonl(
        snapshots_jsonl, since_ts=since_ts, after_ts=cutoff_ts, cap=cap,
    )
    merged = merge_saturation_dicts(archive, recent)
    merged["guardrail_cap_applied"] = guardrail_cap_applied_count(meteoedge_db, since_ts)
    return merged


def load_actual_high_lookup_db(meteoedge_db: "Path | None",
                                stations_dates: "set[tuple[str, str]]") -> dict:
    """Resolve (station, date) -> daily actual high via observations.

    The DB trades table has no bracket-outcome column, so newly-discovered
    snapshot candidates (not already a settled trade) can't be resolved
    from it directly. Per the #682 diagnosis step 3 ("optionally label
    snapshots with outcomes from observations daily highs"), reuse the same
    station-local-day / canonical-feed / training-eligibility logic settle.py
    itself uses (``Database.get_daily_obs_high``) rather than reimplementing
    it here. Skips entirely (no Database() construction, so no schema writes)
    if the observations table doesn't exist -- keeps this safe against an
    empty/temp DB used to exercise the degrade-gracefully path.
    """
    if not stations_dates:
        return {}
    con = _connect_ro(meteoedge_db)
    if con is None:
        return {}
    try:
        has_obs = _table_exists(con, "observations")
    finally:
        con.close()
    if not has_obs:
        return {}

    from src.data.db import Database  # local import: only needed in DB mode

    db = Database(str(meteoedge_db))
    try:
        out = {}
        for station, date in stations_dates:
            try:
                high = db.get_daily_obs_high(station, date)
            except Exception as exc:  # defensive: one bad row must not crash the report
                log.warning(
                    "[prob_cap_shadow] get_daily_obs_high(%s, %s) failed: %s",
                    station, date, exc,
                )
                high = None
            if high is not None:
                out[(station, date)] = high
        return out
    finally:
        db.close()


def no_admission_gates_pass(p_yes_sim: float, ev_no_sim: float, price_cents: float,
                             fixed_gate: float = MAX_CONFIDENCE_YES_FOR_NO,
                             min_edge: float = MIN_EDGE_CENTS,
                             max_edge: float = MAX_EDGE_CENTS,
                             min_price: float = MIN_PRICE_CENTS) -> bool:
    """Replicates the NO-entry branch of scan_markets(): confidence, edge, price gates."""
    return (
        p_yes_sim <= fixed_gate
        and min_edge <= ev_no_sim <= max_edge
        and price_cents >= min_price
    )


# ---------------------------------------------------------------------------
# Clamp saturation + distribution
# ---------------------------------------------------------------------------

def clamp_saturation_stats(rows: list[dict]) -> dict:
    """Share of candidates where p_yes_raw != p_yes, overall / per side / per station."""
    rows = [r for r in rows if r["p_yes_raw"] is not None]
    by_side: dict = defaultdict(lambda: [0, 0])
    by_station: dict = defaultdict(lambda: [0, 0])
    clamped_raw_values = []
    for r in rows:
        is_clamped = abs(r["p_yes_raw"] - r["p_yes"]) > 1e-6
        by_side[r["side"]][1] += 1
        by_station[r["station"]][1] += 1
        if is_clamped:
            by_side[r["side"]][0] += 1
            by_station[r["station"]][0] += 1
            clamped_raw_values.append(r["p_yes_raw"])
    return {
        "total": len(rows),
        "total_clamped": sum(v[0] for v in by_side.values()),
        "by_side": dict(by_side),
        "by_station": dict(by_station),
        "clamped_raw_values": clamped_raw_values,
    }


def distribution_stats(values: list[float]) -> dict:
    if not values:
        return {"count": 0}
    values_sorted = sorted(values)
    stats = {
        "count": len(values),
        "min": values_sorted[0],
        "max": values_sorted[-1],
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
    }
    if len(values) >= 4:
        q = statistics.quantiles(values, n=4)
        stats["p25"], stats["p75"] = q[0], q[2]
    return stats


# ---------------------------------------------------------------------------
# Cap simulation, including the two-channel breakout
# ---------------------------------------------------------------------------

def classify_new_admission_channel(gate_pass_actual: bool, gate_pass_sim: bool) -> str:
    """Attribute a newly-admitted NO candidate to one of two coupled channels.

    - "gate_headroom": the fixed confidence gate (p_yes <= MAX_CONFIDENCE_YES_FOR_NO)
      newly passes under the simulated cap when it did NOT pass under the deployed
      cap. This can only happen if the simulated floor (1 - cap_sim) exceeds the
      fixed gate value -- i.e. cap_sim < 1 - MAX_CONFIDENCE_YES_FOR_NO. For any
      cap_sim in [0.95, 1) with the gate fixed at 0.05, the floor is always
      <= 0.05, so this channel is mathematically 0 for the caps this script
      simulates (0.95/0.97/0.98). The classifier is still evaluated per-row
      (not hardcoded) so the report reflects reality if that assumption ever
      breaks (e.g. a future cap proposal below 0.95).
    - "edge": the gate already passed under the deployed cap; the new admission
      is purely because less-aggressive floor clamping raised the computed
      NO edge (ev_no) past MIN_EDGE_CENTS.
    """
    if not gate_pass_actual and gate_pass_sim:
        return "gate_headroom"
    return "edge"


def simulate_cap_values(settled_rows: list[dict], snapshot_rows: list[dict],
                         cap_values: "tuple[float, ...]" = DEFAULT_CAP_VALUES,
                         deployed_cap: float = MODEL_PROB_CAP,
                         extra_actual_high_lookup: "dict | None" = None) -> dict:
    """Simulate NO-side trade count / win rate / PnL at each cap value.

    Two populations are combined per cap:
      1. Already-admitted NO rows from settlements.csv (settled outcomes known).
         Membership in this population never changes with cap -- see module
         docstring / classify_new_admission_channel -- only ev_no is recomputed
         for transparency.
      2. Newly-admitted candidates discovered from snapshots.jsonl: brackets
         that were evaluated but never cleared MIN_EDGE_CENTS under the
         deployed cap, and would newly clear the (recomputed) edge and/or the
         fixed confidence gate under cap_sim. Resolved via the per
         (station, date) actual_high already recorded in settlements.csv
         (or supplied via *extra_actual_high_lookup* -- e.g. from
         observations daily highs in the DB-backed path, since the DB
         `trades` table has no bracket-outcome column of its own, see
         load_actual_high_lookup_db()); rows with no matching outcome are
         reported separately as "unresolved" and excluded from win-rate/PnL.
    """
    no_rows = [r for r in settled_rows if r["side"] == "NO" and r["p_yes_raw"] is not None]
    admitted_tickers = {r["ticker"] for r in settled_rows}

    # actual_high per (station, date), needed to resolve newly-discovered candidates.
    # extra_actual_high_lookup seeds the table first; settled_rows' own actual_high
    # (when present, e.g. the legacy settlements.csv path) takes precedence per-key.
    actual_high_lookup: dict = dict(extra_actual_high_lookup) if extra_actual_high_lookup else {}
    for r in settled_rows:
        if r["actual_high"] is not None:
            actual_high_lookup[(r["station"], r["date"])] = r["actual_high"]

    # One snapshot per not-yet-admitted ticker: the most mature (latest ts) observation.
    latest_by_ticker: dict = {}
    for s in snapshot_rows:
        if s["ticker"] in admitted_tickers:
            continue
        prev = latest_by_ticker.get(s["ticker"])
        if prev is None or s["ts"] > prev["ts"]:
            latest_by_ticker[s["ticker"]] = s

    results = {}
    for cap in cap_values:
        already_admitted = []
        for r in no_rows:
            p_yes_sim = clamp_p_yes(r["p_yes_raw"], cap)
            ev_no_sim = simulated_ev_no(p_yes_sim, r["price_cents"])
            already_admitted.append({**r, "p_yes_sim": p_yes_sim, "ev_no_sim": ev_no_sim})

        newly_admitted = []
        unresolved_new = 0
        gate_headroom_count = 0
        edge_count = 0
        for s in latest_by_ticker.values():
            p_yes_actual = clamp_p_yes(s["raw_p_yes"], deployed_cap)
            ev_no_actual = simulated_ev_no(p_yes_actual, s["no_ask"])
            gate_pass_actual = p_yes_actual <= MAX_CONFIDENCE_YES_FOR_NO
            admitted_actual = no_admission_gates_pass(p_yes_actual, ev_no_actual, s["no_ask"])

            p_yes_sim = clamp_p_yes(s["raw_p_yes"], cap)
            ev_no_sim = simulated_ev_no(p_yes_sim, s["no_ask"])
            gate_pass_sim = p_yes_sim <= MAX_CONFIDENCE_YES_FOR_NO
            admitted_sim = no_admission_gates_pass(p_yes_sim, ev_no_sim, s["no_ask"])

            if not (admitted_sim and not admitted_actual):
                continue

            channel = classify_new_admission_channel(gate_pass_actual, gate_pass_sim)
            if channel == "gate_headroom":
                gate_headroom_count += 1
            else:
                edge_count += 1

            actual_high = actual_high_lookup.get((s["station"], s["date"]))
            if actual_high is None or s["bracket_low"] is None or s["bracket_high"] is None:
                unresolved_new += 1
                continue
            yes_won = s["bracket_low"] <= actual_high <= s["bracket_high"]
            no_won = not yes_won
            pnl_cents = (100 - s["no_ask"]) if no_won else -s["no_ask"]
            newly_admitted.append({
                "ticker": s["ticker"], "station": s["station"], "date": s["date"],
                "p_yes_sim": p_yes_sim, "ev_no_sim": ev_no_sim, "channel": channel,
                "yes_won": yes_won, "pnl_cents": pnl_cents,
            })

        resolved_baseline = [r for r in already_admitted if r["yes_won"] is not None]
        trade_count = len(already_admitted) + len(newly_admitted)
        wins = sum(1 for r in resolved_baseline if r["yes_won"] is False) + \
            sum(1 for n in newly_admitted if not n["yes_won"])
        resolved_count = len(resolved_baseline) + len(newly_admitted)
        win_rate = (wins / resolved_count) if resolved_count else None
        total_pnl = sum(r["pnl_cents"] for r in resolved_baseline) + \
            sum(n["pnl_cents"] for n in newly_admitted)

        results[cap] = {
            "trade_count": trade_count,
            "already_admitted_count": len(already_admitted),
            "newly_admitted_count": len(newly_admitted),
            "unresolved_new_count": unresolved_new,
            "resolved_count": resolved_count,
            "win_rate": win_rate,
            "total_pnl_cents": total_pnl,
            "edge_channel_count": edge_count,
            "gate_headroom_channel_count": gate_headroom_count,
        }
    return results


# ---------------------------------------------------------------------------
# RANK_ON_RAW_PROB simulated ordering effect
# ---------------------------------------------------------------------------

def simulate_rank_on_raw_prob(settled_rows: list[dict]) -> dict:
    """Compare the scan-order pick vs the raw-edge-ranked pick within each poll.

    Candidates sharing an exact ``ts`` came from the same poll iteration
    (``src/scripts/run.py`` sets ``ts`` once per poll before iterating
    ``scan_markets()`` output). Where a poll produced >1 NO candidate, the
    default (RANK_ON_RAW_PROB=False) execution preference is file/scan order;
    the simulated (RANK_ON_RAW_PROB=True) preference is descending ev_no_raw.
    """
    by_ts: dict = defaultdict(list)
    for r in settled_rows:
        if r["side"] == "NO" and r["ev_no_raw"] is not None and r["yes_won"] is not None:
            by_ts[r["ts"]].append(r)

    divergent = 0
    pnl_delta_sum = 0.0
    raw_pick_wins = 0
    capped_pick_wins = 0
    for ts, group in by_ts.items():
        if len(group) < 2:
            continue
        capped_pick = group[0]  # scan order preserved
        raw_pick = max(group, key=lambda r: r["ev_no_raw"])
        if raw_pick["ticker"] == capped_pick["ticker"]:
            continue
        divergent += 1
        raw_pnl = raw_pick["pnl_cents"] or 0.0
        capped_pnl = capped_pick["pnl_cents"] or 0.0
        pnl_delta_sum += raw_pnl - capped_pnl
        raw_pick_wins += 1 if raw_pick["yes_won"] is False else 0
        capped_pick_wins += 1 if capped_pick["yes_won"] is False else 0

    return {
        "polls_with_multiple_no_candidates": sum(1 for g in by_ts.values() if len(g) >= 2),
        "divergent_picks": divergent,
        "pnl_delta_cents": pnl_delta_sum,
        "raw_pick_wins": raw_pick_wins,
        "capped_pick_wins": capped_pick_wins,
    }


# ---------------------------------------------------------------------------
# Recommendation heuristic
# ---------------------------------------------------------------------------

def recommend(cap_results: dict, baseline_cap: float = MODEL_PROB_CAP,
              min_new_sample: int = 5) -> str:
    baseline = cap_results.get(baseline_cap)
    if baseline is None or baseline["win_rate"] is None:
        return "HOLD -- insufficient settled baseline data to compare cap values."

    lines = []
    for cap, res in cap_results.items():
        if cap == baseline_cap:
            continue
        new_sample = res["newly_admitted_count"]
        if new_sample < min_new_sample:
            lines.append(
                f"cap={cap}: only {new_sample} newly-simulated resolved candidate(s) "
                f"(<{min_new_sample}) -- EXTEND WINDOW before deciding."
            )
            continue
        if res["win_rate"] is None:
            lines.append(f"cap={cap}: no resolved trades -- EXTEND WINDOW.")
            continue
        pnl_delta = res["total_pnl_cents"] - baseline["total_pnl_cents"]
        wr_delta = res["win_rate"] - baseline["win_rate"]
        if pnl_delta > 0 and wr_delta >= -0.05 and res["gate_headroom_channel_count"] == 0:
            lines.append(
                f"cap={cap}: PnL +{pnl_delta:.1f}c, win-rate delta {wr_delta:+.1%}, "
                f"gate-headroom channel=0 (safe) -- CANDIDATE TO RAISE CAP."
            )
        else:
            lines.append(
                f"cap={cap}: PnL delta {pnl_delta:+.1f}c, win-rate delta {wr_delta:+.1%}, "
                f"gate-headroom channel={res['gate_headroom_channel_count']} -- HOLD at current cap."
            )
    return "\n".join(lines) if lines else "HOLD -- no comparison caps evaluated."


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def render_report(report_date: str, n_days: int, saturation: dict,
                   raw_distribution: dict, cap_results: dict,
                   rank_sim: dict, recommendation: str,
                   cap_values: "tuple[float, ...]",
                   population_saturation: "dict | None" = None) -> str:
    lines = []
    lines.append(f"# Prob-cap shadow report -- {report_date}")
    lines.append("")
    lines.append(f"Window: {n_days} distinct date(s) with p_yes_raw data since PR #564 deploy.")
    lines.append(f"Deployed cap: {MODEL_PROB_CAP} | Fixed NO-entry gate "
                 f"(MAX_CONFIDENCE_YES_FOR_NO, held constant across all simulations): "
                 f"{MAX_CONFIDENCE_YES_FOR_NO}")
    lines.append("")

    lines.append("## Clamp saturation")
    lines.append("")
    total = saturation["total"] or 1
    lines.append(f"- Overall: {saturation['total_clamped']}/{saturation['total']} "
                 f"({saturation['total_clamped'] / total:.1%}) candidates were clamped "
                 f"(p_yes_raw != p_yes).")
    lines.append("")
    lines.append("| Side | Clamped | Total | Rate |")
    lines.append("|---|---|---|---|")
    for side, (clamped, tot) in sorted(saturation["by_side"].items()):
        rate = clamped / tot if tot else 0.0
        lines.append(f"| {side} | {clamped} | {tot} | {rate:.1%} |")
    lines.append("")
    lines.append("| Station | Clamped | Total | Rate |")
    lines.append("|---|---|---|---|")
    for station, (clamped, tot) in sorted(saturation["by_station"].items()):
        rate = clamped / tot if tot else 0.0
        lines.append(f"| {station} | {clamped} | {tot} | {rate:.1%} |")
    lines.append("")

    if population_saturation is not None:
        lines.append("## Population-level clamp saturation (all evaluated brackets)")
        lines.append("")
        lines.append(
            "Source: `analytics.db::snapshot_archive`, gap-filled with the live "
            "(not-yet-archived) `logs/snapshots.<date>.jsonl` for the most recent "
            "~24h the archiver hasn't ingested. Every evaluated bracket, gated or "
            "not -- much larger than, and complementary to, the settled-trades "
            "table above (no per-row trade side is recorded at evaluation time)."
        )
        lines.append("")
        pop_total = population_saturation["total"] or 1
        lines.append(
            f"- Overall: {population_saturation['total_clamped']}/{population_saturation['total']} "
            f"({population_saturation['total_clamped'] / pop_total:.1%}) evaluated brackets clamped."
        )
        guardrail_n = population_saturation.get("guardrail_cap_applied")
        if guardrail_n is not None:
            lines.append(
                f"- Cross-check: `guardrail_events` recorded {guardrail_n} `cap_applied` "
                f"events over the same window (population count above should be close)."
            )
        lines.append("")
        if population_saturation["by_station"]:
            lines.append("| Station | Clamped | Total | Rate |")
            lines.append("|---|---|---|---|")
            for station, (clamped, tot) in sorted(population_saturation["by_station"].items()):
                rate = clamped / tot if tot else 0.0
                lines.append(f"| {station} | {clamped} | {tot} | {rate:.1%} |")
        else:
            lines.append("No population-level data available in this window.")
        lines.append("")

    lines.append("## Distribution of p_yes_raw within the clamped population")
    lines.append("")
    if raw_distribution["count"] == 0:
        lines.append("No clamped candidates in this window.")
    else:
        lines.append(f"count={raw_distribution['count']} "
                     f"min={raw_distribution['min']:.4f} "
                     f"p25={raw_distribution.get('p25', float('nan')):.4f} "
                     f"median={raw_distribution['median']:.4f} "
                     f"p75={raw_distribution.get('p75', float('nan')):.4f} "
                     f"max={raw_distribution['max']:.4f} "
                     f"mean={raw_distribution['mean']:.4f}")
    lines.append("")

    lines.append("## Cap simulation (NO side only -- protected side)")
    lines.append("")
    lines.append("| Cap | Trades | Already-admitted | Newly-admitted | Unresolved-new "
                 "| Win rate | Total PnL (c) | Edge-channel | Gate-headroom-channel |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for cap in cap_values:
        r = cap_results[cap]
        wr = f"{r['win_rate']:.1%}" if r["win_rate"] is not None else "n/a"
        lines.append(
            f"| {cap} | {r['trade_count']} | {r['already_admitted_count']} | "
            f"{r['newly_admitted_count']} | {r['unresolved_new_count']} | {wr} | "
            f"{r['total_pnl_cents']:.1f} | {r['edge_channel_count']} | "
            f"{r['gate_headroom_channel_count']} |"
        )
    lines.append("")
    lines.append(
        "Per the binding PM spec addition (2026-07-02): the confidence gate "
        f"(MAX_CONFIDENCE_YES_FOR_NO={MAX_CONFIDENCE_YES_FOR_NO}) is held fixed across all "
        "three simulated caps above. \"Edge-channel\" counts NO admissions gained purely "
        "because a lower clamp floor raised the computed edge past MIN_EDGE_CENTS for "
        "candidates that already satisfied the (unchanged) confidence gate. "
        "\"Gate-headroom-channel\" counts admissions gained because the fixed gate itself "
        "newly passed -- mathematically 0 for any cap in [0.95, 1) held against a 0.05 gate, "
        "confirmed empirically above."
    )
    lines.append("")
    if population_saturation is not None:
        lines.append(
            "**Unit caveat (DB path, issue #682):** \"Total PnL\" mixes two different scales "
            "here. Already-admitted rows carry `trades.pnl`, the real account-currency PnL of "
            "the actual position (scaled by that trade's `size_eur`); newly-admitted rows "
            "(discovered from snapshots, resolved via observations daily highs) use the "
            "per-$1-notional synthetic PnL `(100 - price) if won else -price` that this "
            "simulation has always used. A cap's \"Total PnL\" is therefore not an "
            "apples-to-apples number once it has any newly-admitted rows -- read the win-rate "
            "delta and the gate-headroom-channel count as the primary signals, and treat PnL "
            "deltas as directional, not literal, until this is reconciled."
        )
        lines.append("")

    lines.append("## RANK_ON_RAW_PROB=true simulated ordering effect")
    lines.append("")
    lines.append(f"- Polls with >=2 simultaneous NO candidates: "
                 f"{rank_sim['polls_with_multiple_no_candidates']}")
    lines.append(f"- Polls where raw-edge ranking would have preferred a different "
                 f"candidate than scan order: {rank_sim['divergent_picks']}")
    lines.append(f"- Realized PnL delta if raw-edge pick had been taken instead "
                 f"(sum over divergent polls): {rank_sim['pnl_delta_cents']:.1f}c")
    lines.append(f"- Wins: raw-pick={rank_sim['raw_pick_wins']} "
                 f"capped-pick={rank_sim['capped_pick_wins']}")
    lines.append("")

    lines.append("## Recommendation")
    lines.append("")
    lines.append(recommendation)
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def run_report(candidates_csv: Path, settlements_csv: Path, snapshots_jsonl: Path,
               out_dir: Path, min_days: int, dry_run: bool,
               cap_values: "tuple[float, ...]" = DEFAULT_CAP_VALUES,
               report_date: "str | None" = None,
               meteoedge_db: "Path | None" = None,
               analytics_db: "Path | None" = None,
               since_ts: "str | None" = None) -> int:
    """Generate the report.

    Two data paths:
      * DB path (issue #682): used when *meteoedge_db* is given and the file
        exists -- settled candidates come from `trades`, population
        saturation from `snapshot_archive` + live jsonl gap-fill, newly-
        discovered candidate outcomes from `observations` daily highs.
      * Legacy CSV path (issue #570): used otherwise, unchanged from the
        original implementation -- kept so a fresh checkout with no local
        DB (or an explicit CSV-only invocation) still degrades to an honest
        "insufficient data" report rather than crashing.
    """
    since_ts = since_ts or DEFAULT_SINCE_TS
    use_db = meteoedge_db is not None and Path(meteoedge_db).exists()

    if use_db:
        dates = distinct_dates_with_raw_data_db(meteoedge_db, since_ts)
    else:
        dates = distinct_dates_with_raw_data(candidates_csv)
    n_days = len(dates)
    if n_days < min_days:
        log.info(
            "[prob_cap_shadow] only %d distinct date(s) with p_yes_raw data "
            "(< --min-days=%d) -- skipping report, safe to re-run any day.",
            n_days, min_days,
        )
        return 0

    if MODEL_PROB_CAP not in cap_values:
        # Baseline comparison requires the deployed cap to be one of the
        # simulated points; guard against a --cap-values override that omits it.
        cap_values = (MODEL_PROB_CAP,) + tuple(cap_values)

    if use_db:
        settled_rows = load_settled_candidates_db(meteoedge_db, since_ts)
    else:
        settled_rows = load_settled_candidates(settlements_csv)
    snapshot_rows = load_snapshots(snapshots_jsonl)

    saturation = clamp_saturation_stats(settled_rows)
    raw_distribution = distribution_stats(saturation["clamped_raw_values"])

    population_saturation = None
    extra_actual_high_lookup = None
    if use_db:
        population_saturation = population_saturation_stats_db(
            analytics_db, snapshots_jsonl, since_ts, meteoedge_db=meteoedge_db,
        )
        admitted_tickers = {r["ticker"] for r in settled_rows}
        pairs = {
            (s["station"], s["date"]) for s in snapshot_rows
            if s["ticker"] not in admitted_tickers
        }
        extra_actual_high_lookup = load_actual_high_lookup_db(meteoedge_db, pairs)

    cap_results = simulate_cap_values(
        settled_rows, snapshot_rows, cap_values,
        extra_actual_high_lookup=extra_actual_high_lookup,
    )
    rank_sim = simulate_rank_on_raw_prob(settled_rows)
    recommendation = recommend(cap_results)

    report_date = report_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report = render_report(
        report_date, n_days, saturation, raw_distribution, cap_results,
        rank_sim, recommendation, cap_values,
        population_saturation=population_saturation,
    )

    if dry_run:
        print(report)
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"prob_cap_shadow_{report_date}.md"
    out_path.write_text(report)
    log.info("[prob_cap_shadow] wrote %s (%d distinct date(s) of data)", out_path, n_days)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-days", type=int, default=7,
                        help="Minimum distinct dates of p_yes_raw data required (default 7)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the report to stdout instead of writing a file")
    parser.add_argument("--out-dir", default="backtest_results")
    parser.add_argument("--candidates-csv", default=str(CANDIDATES_CSV))
    parser.add_argument("--settlements-csv", default=str(SETTLEMENTS_CSV))
    parser.add_argument("--snapshots-jsonl", default=str(SNAPSHOTS_JSONL))
    parser.add_argument("--cap-values", default=",".join(str(c) for c in DEFAULT_CAP_VALUES),
                        help="Comma-separated cap values to simulate (default 0.95,0.97,0.98)")
    parser.add_argument("--report-date", default=None,
                        help="Override the report filename date (default: today UTC)")
    parser.add_argument("--meteoedge-db", default=str(DEFAULT_METEOEDGE_DB),
                        help="Path to meteoedge.db (issue #682 DB-backed path); "
                             "used automatically when the file exists")
    parser.add_argument("--analytics-db", default=str(DEFAULT_ANALYTICS_DB),
                        help="Path to analytics.db (snapshot_archive population saturation)")
    parser.add_argument("--since", default=DEFAULT_SINCE_TS,
                        help="ISO date/timestamp floor for the DB-backed path "
                             f"(default {DEFAULT_SINCE_TS}, first day of full "
                             "p_yes_raw coverage per the #682 diagnosis)")
    parser.add_argument("--no-db", action="store_true",
                        help="Force the legacy CSV-based path even if --meteoedge-db exists")
    args = parser.parse_args()

    cap_values = tuple(float(c) for c in args.cap_values.split(","))

    return run_report(
        candidates_csv=Path(args.candidates_csv),
        settlements_csv=Path(args.settlements_csv),
        snapshots_jsonl=Path(args.snapshots_jsonl),
        out_dir=Path(args.out_dir),
        min_days=args.min_days,
        dry_run=args.dry_run,
        cap_values=cap_values,
        report_date=args.report_date,
        meteoedge_db=None if args.no_db else Path(args.meteoedge_db),
        analytics_db=Path(args.analytics_db),
        since_ts=args.since,
    )


if __name__ == "__main__":
    sys.exit(main())

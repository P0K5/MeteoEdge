"""One-off backfill for issue #609: settle live held-to-expiry trades that
were stranded in the DB since ~2026-06-20 when settle_live_trades() started
silently rewriting a frozen ``live_trades.jsonl`` instead of settling the
``trades`` table (log rotation had already moved live writes to dated files,
e.g. ``live_trades.2026-06-20.jsonl``, weeks earlier).

Background:
    settle_live_trades() now settles directly from the DB (see #609). This
    script drives that SAME function once per stranded date so backfill and
    nightly settlement logic can never diverge -- it only has to supply a
    truth source, since fetch_daily_climate_high() (live METAR, hours=48)
    cannot look back further than ~2 days and is therefore useless here.

Truth source (in order of preference, all considered, first usable wins):
    1. The DB-resident ``observations`` table, populated continuously by the
       collector threads in the run loop. MAX(temp_f) is computed per the
       station's LOCAL calendar day (via STATION_TZ + pytz), matching how
       settlement is supposed to work -- NOT src.data.db.get_daily_obs_high(),
       which groups by the observation's raw (UTC) DATE(ts) and would
       misattribute observations near local midnight for non-UTC stations.
    2. Nothing else is currently available in this codebase for dates this
       old: the ``settlements`` table is only populated as a SIDE EFFECT of
       a successful live settlement (chicken-and-egg for exactly the rows
       we're trying to backfill), and there is no NWS Daily Climate Report
       API integration in this repo (fetch_daily_climate_high is METAR-only).
    A (date, station) with no observations on record is SKIPPED and reported
    -- this script never guesses a truth value.

Usage (run against the production DB by the operator; this script is not
run in CI -- the production DB is not present in this checkout):

    # Default range: 2026-06-20 (first stranded date, see #609) -> yesterday
    python -m src.scripts.backfill_live_settlements

    # Preview only -- runs against a throwaway copy of the DB, the real
    # database file is never opened for writing.
    python -m src.scripts.backfill_live_settlements --dry-run

    # Explicit range
    python -m src.scripts.backfill_live_settlements --from 2026-06-20 --to 2026-07-03

    # Against a non-default DB path (defaults to $DB_PATH or data/meteoedge.db)
    python -m src.scripts.backfill_live_settlements --db-path /path/to/meteoedge.db

Safe to run more than once: settle_live_trades() only ever selects
``settled_at IS NULL`` rows, so a rerun after a successful pass finds nothing
left to do for dates it already covered.
"""
from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytz
from dateutil import parser as dtparse

from src.config import STATION_TZ
from src.data.db import Database
from src.scripts.settle import resolve_trade_date, settle_live_trades

# First date with confirmed stranded live trades per issue #609.
_DEFAULT_FROM = date(2026, 6, 20)


def _observed_daily_high(obs_by_station: dict[str, list[dict]], station: str, target: date) -> "float | None":
    """MAX(temp_f) among *station*'s DB observations that fall on *target* in
    the station's LOCAL calendar day (STATION_TZ). None if there are no
    observations on record for that station/day, or the station has no known
    timezone -- callers must treat None as "no trustworthy truth", not zero.
    """
    if station not in STATION_TZ:
        return None
    tz = pytz.timezone(STATION_TZ[station])
    best = None
    for r in obs_by_station.get(station, []):
        ts, temp_f = r.get("ts"), r.get("temp_f")
        if ts is None or temp_f is None:
            continue
        try:
            t = dtparse.parse(ts)
            if t.tzinfo is None:
                t = t.replace(tzinfo=pytz.UTC)
            if t.astimezone(tz).date() != target:
                continue
        except (ValueError, OverflowError):
            continue
        temp_f = float(temp_f)
        if best is None or temp_f > best:
            best = temp_f
    return best


def _pending_by_date(db: Database, date_from: date, date_to: date) -> dict[date, set[str]]:
    """Return {date: {station, ...}} for unsettled live trades in [date_from, date_to]."""
    pending: dict[date, set[str]] = {}
    for row in db.get_unsettled_live_trades():
        d = resolve_trade_date(row)
        if d is None or d < date_from or d > date_to:
            continue
        pending.setdefault(d, set()).add(row.get("station") or "")
    return pending


def run_backfill(db_path: Path, date_from: date, date_to: date, dry_run: bool = False) -> dict:
    """Settle stranded live trades in [date_from, date_to] via settle_live_trades().

    Returns a summary dict: {"exit_code", "settled", "skipped_station_days"}.

    In --dry-run mode this operates on a throwaway copy of the DB file so the
    real database is opened read-only (copied, never written) -- the preview
    is a real settlement run against identical data, not a guess, but nothing
    it writes is ever visible outside the temp copy.
    """
    if not db_path.exists():
        print(f"[backfill] DB not found: {db_path}")
        return {"exit_code": 1, "settled": 0, "skipped_station_days": 0}

    work_path = db_path
    tmp_copy: "Path | None" = None
    if dry_run:
        fd, tmp_name = tempfile.mkstemp(suffix=".db", prefix="meteoedge_backfill_dryrun_")
        os.close(fd)
        tmp_copy = Path(tmp_name)
        shutil.copyfile(db_path, tmp_copy)
        work_path = tmp_copy
        print(f"[backfill] --dry-run: operating on a throwaway copy of the DB ({tmp_copy}); "
              "the real database will not be modified")

    db = Database(path=str(work_path))
    try:
        pending = _pending_by_date(db, date_from, date_to)
        if not pending:
            print(f"[backfill] no unsettled live trades found in [{date_from}, {date_to}]")
            return {"exit_code": 0, "settled": 0, "skipped_station_days": 0}

        # Fetch each pending station's observations once for the whole range
        # rather than re-querying per day.
        stations = sorted({s for day_stations in pending.values() for s in day_stations if s})
        since = (date_from - timedelta(days=1)).isoformat()
        obs_by_station = {s: db.get_observations(s, since) for s in stations}

        total_settled = 0
        total_skipped_station_days = 0
        for d in sorted(pending):
            truth: dict[str, float] = {}
            missing: list[str] = []
            for station in sorted(pending[d]):
                high = _observed_daily_high(obs_by_station, station, d)
                if high is None:
                    missing.append(station)
                else:
                    truth[station] = high

            if missing:
                print(f"[backfill] {d}: no trustworthy observed high for {missing} "
                      "-- skipping (not guessing)")
                total_skipped_station_days += len(missing)

            if not truth:
                continue

            before = len(db.get_unsettled_live_trades())
            settle_live_trades(d, truth, db=db)
            after = len(db.get_unsettled_live_trades())
            n = before - after
            total_settled += n
            print(f"[backfill] {d}: settled {n} live trade(s) from truth={truth}")

        print(
            f"\n[backfill] Summary: settled {total_settled} trade(s) total, "
            f"{total_skipped_station_days} station-day(s) skipped for lack of truth"
            + (" (dry-run -- no changes were made to the real DB)" if dry_run else "")
        )
        return {
            "exit_code": 0,
            "settled": total_settled,
            "skipped_station_days": total_skipped_station_days,
        }
    finally:
        db.close()
        if tmp_copy is not None:
            tmp_copy.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill live held-to-expiry settlements stranded since issue #609 "
            "(settle_live_trades() rewrote a frozen JSONL file instead of the DB)."
        )
    )
    yesterday = datetime.utcnow().date() - timedelta(days=1)
    parser.add_argument(
        "--from", dest="date_from", type=date.fromisoformat, default=_DEFAULT_FROM,
        help=f"First date to backfill, YYYY-MM-DD (default: {_DEFAULT_FROM.isoformat()})",
    )
    parser.add_argument(
        "--to", dest="date_to", type=date.fromisoformat, default=yesterday,
        help="Last date to backfill, YYYY-MM-DD (default: yesterday)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Preview only -- runs against a throwaway copy of the DB",
    )
    parser.add_argument(
        "--db-path", type=Path, default=Path(os.getenv("DB_PATH", "data/meteoedge.db")),
        help="Path to the SQLite DB (default: $DB_PATH or data/meteoedge.db)",
    )
    args = parser.parse_args()
    result = run_backfill(args.db_path, args.date_from, args.date_to, dry_run=args.dry_run)
    raise SystemExit(result["exit_code"])


if __name__ == "__main__":
    main()

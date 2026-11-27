"""One-off backfill for ``trades.market_date`` (issue #1543).

Background: ``trades.is_next_day`` is a UTC-day label, not the market's own
settlement date. For western-hemisphere stations, a market that is already
"same-day" by UTC (``is_next_day=0``) can still settle on the station's
*next* local calendar day when entered in the evening (16:00-22:00 local) --
see issue #1543 for the audited magnitude. This script fills in the ground
truth retroactively; it never touches ``is_next_day``, any gate, or any
probability.

Fill order (first usable source wins, per row):
    1. ``scan_decisions.date`` joined on ``(station, ticker)`` -- one ticker
       is one market is one date, so any matching row's ``date`` is
       authoritative and free (no HTTP call).
    2. Polymarket Gamma API, batched ~40 condition IDs per request (repeated
       ``condition_ids=`` query keys, never comma-joined -- verified unsafe,
       see ``src/data/polymarket.py``'s ``_fetch_resolution_batch_once``),
       parsing the settlement date from the market's ``question`` text
       (e.g. "... on November 16?") rather than trusting ``endDate``'s
       calendar day, which can roll into the next UTC day for an evening
       close -- exactly the ambiguity this issue exists to resolve. The
       response's own ``endDate`` still supplies the year (question text is
       usually "Month Day" only), with a December/January wraparound check.

Idempotent: only ever touches rows where ``market_date IS NULL``. Safe to
rerun -- a rerun after a successful pass has nothing left to do for rows it
already filled.

Usage::

    python -m src.scripts.backfill_trade_market_date            # real run
    python -m src.scripts.backfill_trade_market_date --dry-run  # counts only
    python -m src.scripts.backfill_trade_market_date --db-path /path/to/meteoedge.db
"""
from __future__ import annotations

import argparse
import logging
import re
from datetime import date

from src.config import HTTP_TIMEOUT_SECONDS, POLYMARKET_GAMMA_API
from src.data.db import Database
from src.http_client import fetch

log = logging.getLogger(__name__)

#: Conservative batch size for the Gamma fallback lookup, matching the
#: issue's suggestion and the same conservative-first posture as
#: DEFAULT_RESOLUTION_BATCH_SIZE in src/data/polymarket.py.
GAMMA_BATCH_SIZE = 40

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

#: Matches "... on November 16?" / "...on Nov 16..." style Polymarket
#: weather-market question text.
_QUESTION_DATE_RE = re.compile(r"on\s+([A-Za-z]+)\s+(\d{1,2})\b", re.IGNORECASE)


def _parse_question_date(question: str, end_dt: "date | None") -> "date | None":
    """Extract the settlement date from a market question like
    "Highest temperature in Chicago on November 16?".

    The question text rarely carries a year, so the year is taken from
    *end_dt* (the market's Gamma ``endDate``, calendar-day-only -- never
    trusted for which *day* the market is, only which *year*), with a
    December/January wraparound correction when the two disagree by more
    than one month (e.g. question "December 31" but endDate already rolled
    into January UTC).
    """
    if not question:
        return None
    m = _QUESTION_DATE_RE.search(question)
    if not m:
        return None
    month = _MONTHS.get(m.group(1).lower())
    if month is None:
        return None
    try:
        day = int(m.group(2))
    except ValueError:
        return None

    year = end_dt.year if end_dt is not None else date.today().year
    if end_dt is not None and abs(month - end_dt.month) >= 6:
        year = year - 1 if month > end_dt.month else year + 1

    try:
        return date(year, month, day)
    except ValueError:
        return None


def _fetch_gamma_dates_batch(tickers: "list[str]") -> "dict[str, date | None]":
    """Resolve the settlement date for each ticker (0x condition ID) via the
    Gamma API, batched. A ticker absent from its batch's response, or whose
    question text can't be parsed, maps to ``None`` -- never guessed.
    """
    from dateutil import parser as dtparse

    result: "dict[str, date | None]" = {}
    unique = list(dict.fromkeys(tickers))

    for i in range(0, len(unique), GAMMA_BATCH_SIZE):
        chunk = unique[i:i + GAMMA_BATCH_SIZE]
        # Repeated condition_ids= keys -- a comma-joined list silently
        # returns an empty response (verified, see
        # src/data/polymarket.py::_fetch_resolution_batch_once).
        query = "&".join(f"condition_ids={cid}" for cid in chunk)
        url = f"{POLYMARKET_GAMMA_API}/markets?{query}&closed=true"
        try:
            r = fetch(url, timeout=HTTP_TIMEOUT_SECONDS)
            r.raise_for_status()
            markets = r.json()
        except Exception as e:
            log.warning(
                "[backfill_market_date] Gamma batch of %s failed: %s", len(chunk), e,
            )
            for cid in chunk:
                result.setdefault(cid, None)
            continue

        if not isinstance(markets, list):
            markets = []
        by_cid = {}
        for mkt in markets:
            cid = mkt.get("conditionId") or mkt.get("condition_id")
            if cid:
                by_cid[str(cid).lower()] = mkt

        for cid in chunk:
            mkt = by_cid.get(str(cid).lower())
            if mkt is None:
                result[cid] = None
                continue
            end_dt = None
            raw_end = mkt.get("endDate")
            if raw_end:
                try:
                    end_dt = dtparse.parse(str(raw_end)).date()
                except (ValueError, OverflowError):
                    end_dt = None
            result[cid] = _parse_question_date(mkt.get("question") or "", end_dt)

    return result


def run_backfill(db: Database, dry_run: bool = False) -> dict:
    """Backfill ``trades.market_date`` for every row where it is NULL.

    Returns ``{"total", "from_scan_decisions", "from_gamma", "unresolved"}``.
    In ``--dry-run`` mode no write happens -- the same lookups run, so the
    counts are a real preview, not a guess.
    """
    rows = db._conn.execute(
        "SELECT id, station, ticker FROM trades WHERE market_date IS NULL"
    ).fetchall()
    total = len(rows)
    if total == 0:
        return {"total": 0, "from_scan_decisions": 0, "from_gamma": 0, "unresolved": 0}

    from_scan_decisions = 0
    from_gamma = 0
    unresolved = 0
    updates: "list[tuple[str, int]]" = []
    needs_gamma: "list[tuple[int, str]]" = []

    for row_id, station, ticker in rows:
        sd_row = db._conn.execute(
            "SELECT date FROM scan_decisions WHERE station=? AND ticker=? "
            "AND date IS NOT NULL LIMIT 1",
            (station, ticker),
        ).fetchone()
        if sd_row is not None and sd_row[0]:
            updates.append((sd_row[0], row_id))
            from_scan_decisions += 1
        else:
            needs_gamma.append((row_id, ticker))

    if needs_gamma:
        gamma_dates = _fetch_gamma_dates_batch([t for _, t in needs_gamma])
        for row_id, ticker in needs_gamma:
            d = gamma_dates.get(ticker)
            if d is not None:
                updates.append((d.isoformat(), row_id))
                from_gamma += 1
            else:
                unresolved += 1

    if not dry_run and updates:
        with db._lock:
            db._conn.executemany(
                "UPDATE trades SET market_date=? WHERE id=?", updates,
            )
            db._conn.commit()

    return {
        "total": total,
        "from_scan_decisions": from_scan_decisions,
        "from_gamma": from_gamma,
        "unresolved": unresolved,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Report counts without writing any changes.",
    )
    parser.add_argument(
        "--db-path", default=None,
        help="Path to the SQLite DB (default: Database()'s own default, "
             "$DB_PATH or data/meteoedge.db).",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    db = Database(path=args.db_path) if args.db_path else Database()
    try:
        summary = run_backfill(db, dry_run=args.dry_run)
    finally:
        db.close()

    verb = "Would fill" if args.dry_run else "Filled"
    print(
        f"[backfill] {verb} {summary['from_scan_decisions']} from scan_decisions, "
        f"{summary['from_gamma']} from Gamma, {summary['unresolved']} unresolved "
        f"(of {summary['total']} NULL market_date row(s) total)"
        + (" -- dry-run, no changes were made" if args.dry_run else "")
    )


if __name__ == "__main__":
    main()

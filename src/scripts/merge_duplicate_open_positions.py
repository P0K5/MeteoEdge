"""One-shot merge of duplicate open_positions rows per token_id (issue #611).

Background:
    Before the run.py entry gate (issue #611), the bot could re-enter a bracket
    it already held on every poll: candidate evaluation had no "already holding
    this token today" check, and reconciliation leaned on the frozen
    live_trades.jsonl (root cause #609). Each duplicate live BUY inserted its
    own open_positions row for the SAME token_id -- on 2026-07-03 one WMKK
    token held three separate rows.

    Duplicate rows are exit/display machinery only: every sell path
    (check_take_profit_exits, _check_stop_loss_exits, _check_forced_exits,
    manual_sell_position) already groups fills by token_id, sells the summed
    share total in one order, and deletes all rows via
    close_positions_by_token(). Merging the rows changes no exit behaviour --
    it just makes open_positions reflect reality: one row per held token.

    The extra trades-table rows for the stacked orders are deliberately left
    untouched: settlement (#609) handles those rows independently and their
    PnL history must stay accurate.

Merge semantics (per token_id with more than one row):
    - shares:       summed across all rows
    - entry_price:  share-weighted average, rounded to the nearest int cent
    - entry_ts:     earliest across all rows
    - id/trade_id/order_id: kept from the FIRST row (earliest entry_ts,
      tie-broken by lowest id; the unique index is on order_id)
    - stop_loss_cents:   most protective (highest non-NULL -- exits earliest)
    - take_profit_cents: most protective (lowest non-NULL -- locks in earliest)
      A warning is printed whenever the duplicate rows disagreed.
    - all other rows for the token are deleted

Zero-share phantom rows (issue #977):
    A row can have shares<=0 -- an order that was registered as a position by
    reconciliation despite never (or not yet, per that row) having a matching
    fill. Unlike the pre-#611 stacking bug, there is nothing to merge here:
    the zero-share row contributes nothing to the weighted average, and
    settle.py can never join it to a settlement (Database.close_position()
    only deletes by order_id, and a never-filled order has no fill to close
    against). For each token with both a zero-share row and one or more
    positive-share rows:
      - the zero-share row(s) are DROPPED outright (not merged), and
      - if a surviving positive-share row still carries the synthetic
        ``<STATION>-order-<id>`` ticker placeholder (pre-#977) while a
        dropped zero-share row carried the real condition-id ticker
        (reconciliation reads it from the correct field), the real ticker is
        copied onto the surviving row (open_positions AND its trades row).
    Remaining positive-share duplicates (if more than one survives) are then
    merged as above. A token with ONLY zero-share rows is left untouched and
    reported as a mismatch -- that needs a human to confirm the order truly
    never filled before removing the position tracking that's protecting it.

Safety:
    - Verify-before-touch: all rows for a token must share the same station,
      side, and ticker prefix pattern; a mismatch is skipped with a loud
      warning and the script exits non-zero.
    - Idempotent: a second run finds no duplicate tokens and exits 0 without
      touching anything.
    - --dry-run previews every merge without modifying the DB.

Usage (run against the production DB by the operator; not run in CI):
    python -m src.scripts.merge_duplicate_open_positions            # live run
    python -m src.scripts.merge_duplicate_open_positions --dry-run  # preview only
    python -m src.scripts.merge_duplicate_open_positions --db-path /path/to/meteoedge.db

Runbook: resolving the #977 KORD 2026-08-11 incident (open_positions id 435/436)
    This is the exact, reviewable procedure for the live rows named in issue
    #977 -- both on token_id
    101370671429372224220389749572993685064650155630486212084462717077923353473008:
    id 435 (trade_id 2161, order_id 0x418b18, real fill, shares=6.67, synthetic
    ticker "KORD-order-0x418b18") and id 436 (trade_id 2160, order_id
    0x6f780a, phantom, shares=0.0, real ticker from reconciliation).

    A dry-run transcript against a fixture reproducing this exact row shape
    (same token_id, tickers, shares, order_ids) is in the PR body for #977 --
    that is the reviewable evidence of what this invocation does before it
    touches anything. NOT run against production by the PR author; execution
    against the live DB is a post-merge step sequenced by the Tech Lead PM,
    because it's an OPEN position (KORD had not settled as of the incident) --
    it must not be touched while other automation (take-profit exits, stop-
    loss, settlement) may still be reading it mid-poll.

    1. Preview (no DB write):
        python -m src.scripts.merge_duplicate_open_positions --dry-run \
            --token-id 101370671429372224220389749572993685064650155630486212084462717077923353473008 \
            --db-path /path/to/meteoedge.db
       Confirm the output shows exactly one token with one zero-share phantom
       row dropped (order_id 0x6f780a...) and one ticker repair
       ('KORD-order-0x418b18' -> the real condition id read off the phantom
       row) -- and nothing else. --token-id scoping ensures only this
       incident's token is touched.
    2. Apply (same command without --dry-run):
        python -m src.scripts.merge_duplicate_open_positions \
            --token-id 101370671429372224220389749572993685064650155630486212084462717077923353473008 \
            --db-path /path/to/meteoedge.db
    3. Verify:
        sqlite3 /path/to/meteoedge.db \
            "SELECT id, trade_id, order_id, ticker, shares FROM open_positions \
             WHERE token_id='101370671429372224220389749572993685064650155630486212084462717077923353473008'"
       Expect exactly one row: trade_id 2161, ticker no longer
       'KORD-order-0x418b18', shares 6.67. trade_id 2160's trades row is left
       in place (settlement history); only its open_positions row is gone.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
from pathlib import Path

_DEFAULT_DB_PATH = Path(os.getenv("DB_PATH", "data/meteoedge.db"))


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _is_synthetic_ticker(ticker: str, station: str) -> bool:
    """True if *ticker* is the pre-#977 ``<STATION>-order-<id>`` placeholder."""
    return bool(ticker) and bool(re.match(rf"^{re.escape(station)}-order-[0-9a-fA-Fx]+$", ticker))


def _most_protective(values: list, *, prefer_high: bool) -> "int | None":
    """Return the most protective of the non-NULL cent values (None if all NULL).

    prefer_high=True  -> highest wins (stop_loss: a higher stop exits earlier).
    prefer_high=False -> lowest wins (take_profit: a lower target locks in earlier).
    """
    non_null = [int(v) for v in values if v is not None]
    if not non_null:
        return None
    return max(non_null) if prefer_high else min(non_null)


def merge_duplicates(db_path: Path, dry_run: bool = False, token_ids: list[str] | None = None) -> int:
    """Merge duplicate open_positions rows per token_id. Returns exit code.

    Args:
        db_path: Path to the SQLite database.
        dry_run: If True, preview changes without modifying the DB.
        token_ids: Optional list of specific token_ids to process. If None, process all duplicates.
    """
    if not db_path.exists():
        print(f"[merge] DB not found: {db_path}")
        return 1

    conn = _connect(db_path)

    # Build query to find duplicate tokens, optionally filtered by token_ids list
    if token_ids:
        placeholders = ",".join("?" * len(token_ids))
        query = (
            "SELECT token_id FROM open_positions "
            f"WHERE token_id IN ({placeholders}) "
            "GROUP BY token_id HAVING COUNT(*) > 1"
        )
        dup_tokens = [
            r["token_id"]
            for r in conn.execute(query, token_ids).fetchall()
        ]
    else:
        dup_tokens = [
            r["token_id"]
            for r in conn.execute(
                "SELECT token_id FROM open_positions "
                "GROUP BY token_id HAVING COUNT(*) > 1"
            ).fetchall()
        ]

    if not dup_tokens:
        if token_ids:
            print(f"[merge] no duplicate token_id rows found for specified token(s) -- nothing to do")
        else:
            print("[merge] no duplicate token_id rows found -- nothing to do")
        conn.close()
        return 0

    n_merged = 0
    n_deleted = 0
    n_mismatched = 0
    n_phantoms_dropped = 0
    n_tickers_repaired = 0

    for token_id in dup_tokens:
        rows = conn.execute(
            "SELECT * FROM open_positions WHERE token_id=? "
            "ORDER BY entry_ts ASC, id ASC",
            (token_id,),
        ).fetchall()

        stations = {r["station"] for r in rows}
        sides = {r["side"] for r in rows}
        if len(stations) > 1 or len(sides) > 1:
            print(
                f"[merge] token {token_id[:14]}...: MISMATCH -- rows span "
                f"stations={sorted(stations)} sides={sorted(sides)}. "
                "Refusing to merge; investigate manually."
            )
            n_mismatched += 1
            continue

        # --- Zero-share phantom rows (issue #977): drop, don't merge -------
        zero_rows = [r for r in rows if not (float(r["shares"]) > 0)]
        real_rows = [r for r in rows if float(r["shares"]) > 0]
        if zero_rows and real_rows:
            station = real_rows[0]["station"]
            # Repair a surviving row's synthetic ticker from a dropped
            # phantom's real ticker, if one is available and needed.
            real_ticker = next(
                (r["ticker"] for r in real_rows if not _is_synthetic_ticker(r["ticker"], station)),
                None,
            )
            phantom_ticker = next(
                (r["ticker"] for r in zero_rows if not _is_synthetic_ticker(r["ticker"], station)),
                None,
            )
            repair_ticker = real_ticker or phantom_ticker
            for rr in real_rows:
                if repair_ticker and _is_synthetic_ticker(rr["ticker"], station):
                    action = "[dry-run] would repair" if dry_run else "repairing"
                    print(
                        f"[merge] token {token_id[:14]}...: {action} synthetic ticker "
                        f"'{rr['ticker']}' -> '{repair_ticker}' on open_positions id={rr['id']} "
                        f"(trade_id={rr['trade_id']})"
                    )
                    if not dry_run:
                        conn.execute(
                            "UPDATE open_positions SET ticker=? WHERE id=?",
                            (repair_ticker, rr["id"]),
                        )
                        conn.execute(
                            "UPDATE trades SET ticker=? WHERE id=?",
                            (repair_ticker, rr["trade_id"]),
                        )
                    n_tickers_repaired += 1

            phantom_ids = [r["id"] for r in zero_rows]
            action = "[dry-run] would drop" if dry_run else "dropping"
            print(
                f"[merge] token {token_id[:14]}... ({station}): {action} "
                f"{len(phantom_ids)} zero-share phantom row(s) id(s) {phantom_ids} "
                f"(order_id(s) {[r['order_id'][:16] for r in zero_rows]}); "
                f"{len(real_rows)} positive-share row(s) kept -- the trades row(s) for "
                "the phantom's order_id(s) are left untouched (settlement history)."
            )
            if not dry_run:
                conn.execute(
                    f"DELETE FROM open_positions WHERE id IN ({','.join('?' * len(phantom_ids))})",
                    phantom_ids,
                )
            n_phantoms_dropped += len(phantom_ids)
            rows = real_rows

        if len(rows) < 2:
            # Nothing left to merge for this token (either it only had a
            # single real row plus phantom(s) now dropped, or -- in dry-run,
            # where the drop above was only previewed -- we still don't want
            # to also run merge logic against a phantom row).
            continue

        bad_rows = [
            r["id"] for r in rows
            if not (float(r["shares"]) > 0 and 1 <= int(r["entry_price"]) <= 99)
        ]
        if bad_rows:
            print(
                f"[merge] token {token_id[:14]}...: MISMATCH -- row id(s) "
                f"{bad_rows} have non-positive shares or entry_price outside "
                "1-99c. Refusing to merge; investigate manually."
            )
            n_mismatched += 1
            continue

        keeper = rows[0]  # earliest entry_ts (tie: lowest id)
        total_shares = sum(float(r["shares"]) for r in rows)
        weighted_price = int(round(
            sum(float(r["shares"]) * int(r["entry_price"]) for r in rows) / total_shares
        ))
        earliest_ts = min(r["entry_ts"] for r in rows)

        stop_values = [r["stop_loss_cents"] for r in rows]
        tp_values = [r["take_profit_cents"] for r in rows]
        stop_loss = _most_protective(stop_values, prefer_high=True)
        take_profit = _most_protective(tp_values, prefer_high=False)
        if len({v for v in stop_values if v is not None}) > 1:
            print(
                f"[merge] token {token_id[:14]}...: WARNING -- rows disagree on "
                f"stop_loss_cents {stop_values}; keeping most protective {stop_loss}"
            )
        if len({v for v in tp_values if v is not None}) > 1:
            print(
                f"[merge] token {token_id[:14]}...: WARNING -- rows disagree on "
                f"take_profit_cents {tp_values}; keeping most protective {take_profit}"
            )

        delete_ids = [r["id"] for r in rows[1:]]
        action = "[dry-run] would merge" if dry_run else "merging"
        print(
            f"[merge] token {token_id[:14]}... ({keeper['station']} {keeper['side']}): "
            f"{action} {len(rows)} rows into id={keeper['id']} "
            f"(order_id={keeper['order_id'][:16]}...) -- shares={total_shares:.4f}, "
            f"entry_price={weighted_price}c (weighted), entry_ts={earliest_ts}; "
            f"deleting row id(s) {delete_ids}"
        )
        if not dry_run:
            conn.execute(
                "UPDATE open_positions SET shares=?, entry_price=?, entry_ts=?, "
                "stop_loss_cents=?, take_profit_cents=? WHERE id=?",
                (total_shares, weighted_price, earliest_ts,
                 stop_loss, take_profit, keeper["id"]),
            )
            conn.execute(
                "DELETE FROM open_positions WHERE token_id=? AND id!=?",
                (token_id, keeper["id"]),
            )
        n_merged += 1
        n_deleted += len(delete_ids)

    if not dry_run:
        conn.commit()
    conn.close()

    print(
        f"\n[merge] Summary: {n_merged} token(s) {'would be ' if dry_run else ''}merged, "
        f"{n_deleted} duplicate row(s) {'would be ' if dry_run else ''}deleted, "
        f"{n_phantoms_dropped} zero-share phantom row(s) {'would be ' if dry_run else ''}dropped, "
        f"{n_tickers_repaired} synthetic ticker(s) {'would be ' if dry_run else ''}repaired, "
        f"{n_mismatched} mismatched"
    )
    if n_mismatched:
        print("[merge] FAILED: mismatched token(s) found -- investigate before re-running")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Merge duplicate open_positions rows per token_id left behind by "
            "the pre-#611 bracket-stacking bug (summed shares, share-weighted "
            "entry price, earliest entry_ts). Also drops zero-share phantom "
            "rows left by the pre-#977 reconciliation bug and repairs any "
            "surviving row's synthetic '<STATION>-order-<id>' ticker from a "
            "dropped phantom's real ticker."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview what would change without modifying the DB",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=_DEFAULT_DB_PATH,
        help=f"Path to the SQLite DB (default: {_DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--token-id",
        action="append",
        dest="token_ids",
        help="Restrict merging to the specified token_id(s) (repeatable, or comma-separated)",
    )
    args = parser.parse_args()

    # Handle comma-separated token_ids: flatten the list if any comma-separated values exist
    token_ids = None
    if args.token_ids:
        token_ids = []
        for token_id_arg in args.token_ids:
            token_ids.extend(token_id_arg.split(","))

    raise SystemExit(merge_duplicates(args.db_path, dry_run=args.dry_run, token_ids=token_ids))


if __name__ == "__main__":
    main()

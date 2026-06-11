#!/usr/bin/env python3
"""One-time repair of data/meteoedge.db from logs/live_trades.jsonl.

Fixes the historical damage left by the pre-#198 pipeline bugs:
1. settlements rows written with actual_high_f=0.0 (broken backfill field names)
2. duplicate trades rows (double insert at order placement + at fill)
3. trades.pnl / settled_at / outcome never written back by settle.py
4. risk_state.daily_pnl stuck at 0.0
5. stale open_positions rows for settled / sold / timed-out trades

Idempotent — safe to re-run. Defaults to a DRY RUN; pass --apply to commit.

Usage (on the server, from the repo root):
    python scripts/repair_db.py            # dry run, prints what would change
    python scripts/repair_db.py --apply    # write the changes
"""
import argparse
import json
import os
import sqlite3
from pathlib import Path

DB_PATH = os.getenv("DB_PATH", "data/meteoedge.db")
LIVE_TRADES = Path("logs/live_trades.jsonl")


def load_records(path: Path) -> list[dict]:
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def dedupe_trades(conn: sqlite3.Connection) -> int:
    """Collapse duplicate trades rows sharing an order_id.

    Keeps the lowest-id row (the one open_positions.trade_id points at),
    merging ticker / outcome / pnl / capital_after / settled_at from the
    duplicates when the kept row has them empty. Prefers a 0x… market-hash
    ticker over a synthetic '{STATION}-order-…' one.
    """
    cur = conn.execute(
        "SELECT order_id FROM trades "
        "WHERE order_id IS NOT NULL AND order_id != '' "
        "GROUP BY order_id HAVING COUNT(*) > 1"
    )
    dup_order_ids = [r[0] for r in cur.fetchall()]
    removed = 0
    for order_id in dup_order_ids:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM trades WHERE order_id=? ORDER BY id ASC", (order_id,)
            )
        ]
        keep, dupes = rows[0], rows[1:]

        merged = {}
        candidates = [keep] + dupes
        hash_ticker = next(
            (r["ticker"] for r in candidates if str(r["ticker"] or "").startswith("0x")),
            None,
        )
        if hash_ticker and keep["ticker"] != hash_ticker:
            merged["ticker"] = hash_ticker
        for col in ("outcome", "pnl", "capital_after", "settled_at"):
            if keep[col] is None:
                val = next((r[col] for r in dupes if r[col] is not None), None)
                if val is not None:
                    merged[col] = val

        if merged:
            set_clause = ", ".join(f"{c}=?" for c in merged)
            conn.execute(
                f"UPDATE trades SET {set_clause} WHERE id=?",
                (*merged.values(), keep["id"]),
            )
        dupe_ids = [r["id"] for r in dupes]
        ph = ",".join("?" * len(dupe_ids))
        conn.execute(
            f"UPDATE open_positions SET trade_id=? WHERE trade_id IN ({ph})",
            (keep["id"], *dupe_ids),
        )
        conn.execute(f"DELETE FROM trades WHERE id IN ({ph})", dupe_ids)
        removed += len(dupe_ids)
    return removed


def _buy_order_for_sell(conn: sqlite3.Connection, rec: dict) -> "str | None":
    """Resolve the BUY order_id behind a 'sold' JSONL record.

    The sold record carries the SELL order_id; the BUY order is recovered
    from the synthetic ticker '{STATION}-order-{buy_order[:8]}', falling
    back to the open_positions token_id mapping.
    """
    ticker = str(rec.get("ticker", ""))
    if "-order-" in ticker:
        prefix = ticker.split("-order-")[-1]
        rows = conn.execute(
            "SELECT order_id FROM trades WHERE order_id LIKE ?", (prefix + "%",)
        ).fetchall()
        if len(rows) == 1:
            return rows[0][0]
    token_id = rec.get("no_token_id", "")
    if token_id:
        rows = conn.execute(
            "SELECT order_id FROM open_positions WHERE token_id=?", (token_id,)
        ).fetchall()
        if len(rows) == 1:
            return rows[0][0]
    return None


def backfill_trade_pnl(conn: sqlite3.Connection, records: list[dict]) -> tuple[int, int, int]:
    """Write pnl / settled_at / outcome onto trades rows from JSONL records.

    Only touches rows where pnl IS NULL, so re-runs are no-ops.
    Returns (filled_updated, sold_updated, timeout_updated).
    """
    n_filled = n_sold = n_timeout = 0
    for rec in records:
        outcome = rec.get("outcome")
        order_id = rec.get("order_id") or ""

        if outcome == "filled" and "pnl" in rec:
            cur = conn.execute(
                "UPDATE trades SET pnl=?, settled_at=? "
                "WHERE order_id=? AND pnl IS NULL",
                (rec["pnl"], rec.get("end_date", ""), order_id),
            )
            n_filled += cur.rowcount

        elif outcome == "sold":
            buy_order = _buy_order_for_sell(conn, rec)
            if buy_order is None:
                continue
            cur = conn.execute(
                "UPDATE trades SET outcome='sold', pnl=?, settled_at=? "
                "WHERE order_id=? AND pnl IS NULL",
                (rec["pnl"], rec.get("ts", ""), buy_order),
            )
            n_sold += cur.rowcount

        elif outcome == "timeout":
            cur = conn.execute(
                "UPDATE trades SET outcome='timeout' "
                "WHERE order_id=? AND outcome IS NULL",
                (order_id,),
            )
            n_timeout += cur.rowcount
    return n_filled, n_sold, n_timeout


def rebuild_settlements(conn: sqlite3.Connection, records: list[dict]) -> tuple[int, int]:
    """Drop garbage settlement rows and rebuild from settled JSONL records.

    Returns (garbage_deleted, settlements_written).
    """
    cur = conn.execute("DELETE FROM settlements WHERE actual_high_f = 0.0")
    deleted = cur.rowcount

    hash_by_token = {
        r["no_token_id"]: r["ticker"]
        for r in records
        if r.get("no_token_id") and str(r.get("ticker", "")).startswith("0x")
    }
    seen: set[str] = set()
    written = 0
    for rec in records:
        actual = rec.get("actual_high")
        lo, hi = rec.get("bracket_low"), rec.get("bracket_high")
        station = rec.get("station", "")
        if actual is None or lo is None or hi is None or not station:
            continue
        ticker = str(rec.get("ticker", ""))
        if ticker.startswith("0x"):
            market_key = ticker
        else:
            market_key = hash_by_token.get(rec.get("no_token_id", "")) or rec.get("no_token_id", "")
        if not market_key or market_key in seen:
            continue
        seen.add(market_key)
        conn.execute(
            "INSERT OR REPLACE INTO settlements"
            "(ts,station,ticker,bracket_low,bracket_high,actual_high_f,resolved_yes,source) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                rec.get("end_date", ""), station, market_key,
                float(lo), float(hi), float(actual),
                int(float(lo) <= float(actual) <= float(hi)), "polymarket",
            ),
        )
        written += 1
    return deleted, written


def rebuild_risk_state(conn: sqlite3.Connection) -> int:
    """Recompute risk_state.daily_pnl from settled trades, keyed by settlement
    date. Preserves the open_positions column. Returns rows touched."""
    conn.execute("UPDATE risk_state SET daily_pnl = 0.0")
    rows = conn.execute(
        "SELECT DATE(settled_at) AS d, SUM(pnl) AS total FROM trades "
        "WHERE pnl IS NOT NULL AND settled_at IS NOT NULL AND settled_at != '' "
        "GROUP BY DATE(settled_at)"
    ).fetchall()
    for d, total in rows:
        if d is None:
            continue
        conn.execute(
            "INSERT INTO risk_state(trade_date,daily_pnl,open_positions,updated_at) "
            "VALUES(?,?,0,datetime('now')) "
            "ON CONFLICT(trade_date) DO UPDATE SET "
            "daily_pnl=excluded.daily_pnl, updated_at=excluded.updated_at",
            (d, round(total, 4)),
        )
    return len(rows)


def clean_open_positions(conn: sqlite3.Connection) -> int:
    """Delete open_positions rows whose trade is already settled, sold,
    timed out, or cancelled. Returns rows deleted."""
    cur = conn.execute(
        "DELETE FROM open_positions WHERE trade_id IN ("
        "  SELECT id FROM trades "
        "  WHERE pnl IS NOT NULL OR outcome IN ('sold','timeout','cancelled')"
        ")"
    )
    return cur.rowcount


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default is a dry run)")
    ap.add_argument("--db", default=DB_PATH, help=f"DB path (default: {DB_PATH})")
    ap.add_argument("--jsonl", default=str(LIVE_TRADES),
                    help=f"live trades log (default: {LIVE_TRADES})")
    args = ap.parse_args()

    jsonl = Path(args.jsonl)
    if not Path(args.db).exists():
        raise SystemExit(f"[repair] DB not found: {args.db}")
    if not jsonl.exists():
        raise SystemExit(f"[repair] JSONL not found: {jsonl}")

    records = load_records(jsonl)
    print(f"[repair] {len(records)} JSONL record(s) loaded from {jsonl}")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("BEGIN")
        removed = dedupe_trades(conn)
        n_filled, n_sold, n_timeout = backfill_trade_pnl(conn, records)
        s_deleted, s_written = rebuild_settlements(conn, records)
        risk_days = rebuild_risk_state(conn)
        pos_cleaned = clean_open_positions(conn)

        print(f"[repair] duplicate trades removed:        {removed}")
        print(f"[repair] trades pnl backfilled (filled):  {n_filled}")
        print(f"[repair] trades pnl backfilled (sold):    {n_sold}")
        print(f"[repair] trades outcome set (timeout):    {n_timeout}")
        print(f"[repair] garbage settlements deleted:     {s_deleted}")
        print(f"[repair] settlements rebuilt:             {s_written}")
        print(f"[repair] risk_state days recomputed:      {risk_days}")
        print(f"[repair] stale open_positions cleaned:    {pos_cleaned}")

        if args.apply:
            conn.commit()
            print("[repair] changes COMMITTED")
        else:
            conn.rollback()
            print("[repair] DRY RUN — nothing written (use --apply to commit)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

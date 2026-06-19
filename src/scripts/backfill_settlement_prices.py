#!/usr/bin/env python3
"""Backfill market_final_price for existing settlements where the column is NULL.

Idempotent and safe to re-run: only rows with market_final_price IS NULL are
processed. Rows that already have a value are left untouched.

Usage:
    python -m src.scripts.backfill_settlement_prices
    # or from repo root:
    python src/scripts/backfill_settlement_prices.py

Requires network access to the Polymarket Gamma API.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.data.db import Database
from src.data.polymarket import fetch_market_final_price


def main() -> None:
    db = Database()
    conn = db._conn

    rows = conn.execute(
        "SELECT id, ticker FROM settlements WHERE market_final_price IS NULL"
    ).fetchall()

    if not rows:
        print("[backfill_prices] No settlements with NULL market_final_price — nothing to do.")
        return

    print(f"[backfill_prices] Found {len(rows)} settlement(s) with NULL market_final_price.")
    updated = 0
    skipped = 0

    for row in rows:
        row_id = row["id"]
        ticker = row["ticker"]

        # Only 0x tickers are Polymarket condition IDs we can look up via Gamma.
        if not ticker.startswith("0x"):
            skipped += 1
            continue

        price = fetch_market_final_price(ticker)
        if price is None:
            print(f"[backfill_prices]   {ticker[:16]}... — no price returned (market may be open or API error)")
            skipped += 1
            continue

        conn.execute(
            "UPDATE settlements SET market_final_price = ? WHERE id = ?",
            (price, row_id),
        )
        conn.commit()
        updated += 1
        print(f"[backfill_prices]   {ticker[:16]}... -> {price}c")

    print(
        f"[backfill_prices] Done: updated {updated}, skipped {skipped} "
        f"(non-0x tickers or no price available)."
    )


if __name__ == "__main__":
    main()

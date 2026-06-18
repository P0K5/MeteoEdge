#!/usr/bin/env python3
"""Backfill actual_fee_cents for legacy live trade rows where it is NULL.

Idempotent — re-running only updates rows that still have actual_fee_cents IS NULL.

    python scripts/backfill_trade_costs.py [--db-path PATH] [--dry-run]

Uses estimate_fee_cents(actual_price) as a proxy for the sell-time fee.
For old rows the sell price is not stored, so the BUY actual_price gives a
reasonable approximation (fees are small and proportional to price level).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import os
from src.data.db import Database
from src.strategy.fee import estimate_fee_cents


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=os.environ.get("DB_PATH", "meteoedge.db"))
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be updated without writing")
    args = parser.parse_args()

    db = Database(args.db_path)
    rows = db.get_trades_missing_fee_costs()

    if not rows:
        print("[backfill_trade_costs] nothing to backfill — all live closed trades have actual_fee_cents")
        return

    print(f"[backfill_trade_costs] found {len(rows)} rows to backfill")
    updated = 0
    for row in rows:
        fee = estimate_fee_cents(int(row["actual_price"]))
        if args.dry_run:
            print(
                f"  DRY-RUN trade_id={row['id']} order_id={str(row['order_id'])[:12]}..."
                f" actual_price={row['actual_price']}c → fee={fee}c"
            )
        else:
            db.update_trade_costs(row["order_id"], actual_fee_cents=fee)
            updated += 1

    if args.dry_run:
        print(f"[backfill_trade_costs] dry-run complete — {len(rows)} rows would be updated")
    else:
        print(f"[backfill_trade_costs] done — updated {updated}/{len(rows)} rows")


if __name__ == "__main__":
    main()

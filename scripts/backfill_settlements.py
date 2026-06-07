#!/usr/bin/env python3
"""Backfill settlements table from logs/live_trades.jsonl.

Run once after Epic #76 Issues A and F PRs merge:
    python scripts/backfill_settlements.py

Reads settled trade records (pnl != 0) and writes them to the settlements
table. This seeds the DEB (Epic #69) and EMOS (Epic #71) training data.
"""
import json
import sys
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.data.db import Database
from src.data.settlements import SettlementWriter

LIVE_TRADES = Path("logs/live_trades.jsonl")


def main() -> None:
    db = Database()
    writer = SettlementWriter(db)
    count = 0
    skipped = 0

    if not LIVE_TRADES.exists():
        print(f"[backfill] {LIVE_TRADES} not found — nothing to backfill")
        return

    with open(LIVE_TRADES) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue

            pnl = record.get("pnl")
            if pnl is None or float(pnl) == 0.0:
                skipped += 1
                continue  # skip unsettled or flat trades

            ticker = record.get("ticker", "")
            station = record.get("station", "")
            bracket_low = record.get("bracket_low")
            bracket_high = record.get("bracket_high")
            actual_daily_high = record.get("actual_daily_high")

            if not all([ticker, station, bracket_low is not None, bracket_high is not None]):
                skipped += 1
                continue

            side = record.get("side", "NO")
            pnl_positive = float(pnl) > 0
            # resolved_yes: YES won if side=YES and pnl>0, or side=NO and pnl<0
            resolved_yes = (side == "YES" and pnl_positive) or (side == "NO" and not pnl_positive)

            writer.record_settlement(
                ticker=ticker,
                station=station,
                bracket_low=float(bracket_low),
                bracket_high=float(bracket_high),
                actual_high_f=float(actual_daily_high) if actual_daily_high is not None else 0.0,
                resolved_yes=resolved_yes,
                market_final_price=record.get("actual_price"),
            )
            count += 1

    print(f"[backfill] inserted {count} settlement(s), skipped {skipped} record(s)")


if __name__ == "__main__":
    main()

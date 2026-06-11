#!/usr/bin/env python3
"""Backfill settlements table from logs/live_trades.jsonl.

Idempotent (settlements upsert on ticker) — safe to re-run:
    python scripts/backfill_settlements.py

Reads settled trade records (actual_high present) and writes one settlement
row per resolved market. Seeds DEB (Epic #69) and EMOS (Epic #71) training data.
"""
import json
import sys
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.data.db import Database
from src.data.settlements import SettlementWriter

LIVE_TRADES = Path("logs/live_trades.jsonl")


def load_records() -> list[dict]:
    records = []
    with open(LIVE_TRADES) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def main() -> None:
    if not LIVE_TRADES.exists():
        print(f"[backfill] {LIVE_TRADES} not found — nothing to backfill")
        return

    db = Database()
    writer = SettlementWriter(db)
    count = 0
    skipped = 0

    records = load_records()

    # Some records carry a synthetic '{STATION}-order-…' ticker; map them back
    # to the 0x market hash via no_token_id when another record has it.
    hash_by_token = {
        r["no_token_id"]: r["ticker"]
        for r in records
        if r.get("no_token_id") and str(r.get("ticker", "")).startswith("0x")
    }

    seen: set[str] = set()
    for record in records:
        ticker = str(record.get("ticker", ""))
        station = record.get("station", "")
        bracket_low = record.get("bracket_low")
        bracket_high = record.get("bracket_high")
        # actual_high is only written by settle.py at settlement; 'sold'
        # (stop-loss) records lack it and cannot tell us the market outcome.
        actual_high = record.get("actual_high")

        if actual_high is None or not all(
            [ticker, station, bracket_low is not None, bracket_high is not None]
        ):
            skipped += 1
            continue

        if ticker.startswith("0x"):
            market_key = ticker
        else:
            market_key = hash_by_token.get(record.get("no_token_id", "")) or record.get("no_token_id", "")
        if not market_key or market_key in seen:
            skipped += 1
            continue
        seen.add(market_key)

        lo, hi = float(bracket_low), float(bracket_high)
        actual = float(actual_high)
        writer.record_settlement(
            ticker=market_key,
            station=station,
            bracket_low=lo,
            bracket_high=hi,
            actual_high_f=actual,
            resolved_yes=lo <= actual <= hi,
        )
        count += 1

    print(f"[backfill] inserted {count} settlement(s), skipped {skipped} record(s)")


if __name__ == "__main__":
    main()

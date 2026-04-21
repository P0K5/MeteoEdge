"""Summarize hit rate and P&L after N days. Run any time after settlements accumulate."""
import csv
import statistics
from config import SETTLEMENTS_CSV


def main():
    if not SETTLEMENTS_CSV.exists():
        print("No settlements yet.")
        return

    rows = list(csv.DictReader(open(SETTLEMENTS_CSV)))
    # Deduplicate: if the same ticker was flagged multiple times, take first flag of the day
    seen = set()
    unique = []
    for r in rows:
        key = (r["ts"][:10], r["ticker"], r["flagged_side"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)

    n = len(unique)
    if n == 0:
        print("Zero unique flagged candidates.")
        return

    wins = sum(1 for r in unique if r["candidate_won"] in ("True", "true", True))
    hit_rate = wins / n
    pnls = [float(r["pnl_cents"]) for r in unique]
    total_pnl = sum(pnls)
    avg_pnl = statistics.mean(pnls)
    stdev_pnl = statistics.pstdev(pnls) if n > 1 else 0.0

    by_station = {}
    for r in unique:
        by_station.setdefault(r["station"], []).append(r)

    print(f"\n=== MeteoEdge Spike Report ===")
    print(f"Unique flagged candidates: {n}")
    print(f"Wins: {wins}  Hit rate: {hit_rate:.2%}")
    print(f"Total P&L (cents, pre-fee): {total_pnl:+.1f}")
    print(f"Avg P&L per trade: {avg_pnl:+.2f}¢  stdev: {stdev_pnl:.2f}¢")
    print(f"\nBy station:")
    for station, items in sorted(by_station.items()):
        w = sum(1 for r in items if r["candidate_won"] in ("True", "true", True))
        print(f"  {station}: {len(items)} flagged, {w} won ({w/len(items):.1%})")

    print(f"\n{'='*40}")
    if hit_rate >= 0.55 and n >= 30:
        print(f"GREEN LIGHT: hit rate {hit_rate:.2%} >= 55% on {n} candidates. Proceed to full build.")
    elif hit_rate >= 0.55:
        print(f"PROVISIONAL GREEN: hit rate OK but n={n} is below 30. Run more days.")
    else:
        print(f"RED LIGHT: hit rate {hit_rate:.2%} < 55%. Do not proceed. Revisit spec.")


if __name__ == "__main__":
    main()

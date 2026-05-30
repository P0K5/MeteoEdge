"""Backdated simulation of shadow-tracked stations vs live model.

Strategy:
1. For every ticker observed in shadow snapshots.jsonl, derive the settled
   daily high as max(current_high) across all polls for that ticker.
2. Trust the estimate only when polling continued to within an hour of the
   market's close (min_mins_to_settlement <= 60). Otherwise the day was
   still in progress when polling stopped and the peak may not have been
   captured.
3. For each flagged candidate row in candidates.csv, take the first
   flag per ticker (the live bot enters once and holds), and resolve
   YES/NO based on whether the settled high lands inside the bracket.
4. PnL per trade (cents): win = (100 - price) - fee; loss = -price - fee.
   Fee uses the same heuristic as the shadow loop.
5. Aggregate per station: trades, win-rate, total PnL, ROI on capital
   risked (sum of prices paid), avg edge at flag time.
"""
import csv
import json
from collections import defaultdict
from pathlib import Path

SHADOW_DIR = Path("shadow-logs")
SNAPSHOTS = SHADOW_DIR / "snapshots.jsonl"
CANDIDATES = SHADOW_DIR / "candidates.csv"

LIVE_STATIONS = {"KORD", "KMIA", "KLAX", "KATL", "KHOU"}

# Settlement-confidence: a ticker counts as "settled" only when (a) the bot
# polled within ~5h of close AND (b) the last observed temperature is clearly
# below the peak (descending from the daily high). Either condition alone is
# noisy, both together are robust evidence the day's max was captured.
MAX_MINS_TO_SETTLE_AT_LAST_POLL = 300
MIN_TEMP_DROP_FROM_PEAK = 1.0  # in native unit (°F or °C)


def fee_cents(price_cents: float) -> float:
    p = price_cents / 100.0
    return max(1.0, 7.0 * p * (1 - p))


def load_ticker_settlement() -> dict[str, dict]:
    """Per-ticker rollup from snapshots."""
    out: dict[str, dict] = {}
    with open(SNAPSHOTS, "r", encoding="utf-8") as f:
        for line in f:
            s = json.loads(line)
            t = s["ticker"]
            if t not in out:
                out[t] = {
                    "station": s["station"],
                    "unit": s["unit"],
                    "region": s.get("region", "?"),
                    "city": s.get("city", "?"),
                    "low": s["bracket_low"],
                    "high": s["bracket_high"],
                    "max_high": s["current_high"],
                    "last_ts": s["ts"],
                    "last_temp": s["latest_temp"],
                    "min_mins_left": s["minutes_to_settlement"],
                    "n_polls": 1,
                }
                continue
            rec = out[t]
            rec["max_high"] = max(rec["max_high"], s["current_high"])
            rec["min_mins_left"] = min(rec["min_mins_left"], s["minutes_to_settlement"])
            rec["n_polls"] += 1
            if s["ts"] > rec["last_ts"]:
                rec["last_ts"] = s["ts"]
                rec["last_temp"] = s["latest_temp"]
    return out


def main() -> None:
    tickers = load_ticker_settlement()
    print(f"Loaded {len(tickers)} unique tickers across snapshots")

    def is_settled(rec: dict) -> bool:
        if rec["min_mins_left"] > MAX_MINS_TO_SETTLE_AT_LAST_POLL:
            return False
        return (rec["max_high"] - rec["last_temp"]) >= MIN_TEMP_DROP_FROM_PEAK

    # Per-station: tickers we can plausibly settle
    complete_by_station: dict[str, int] = defaultdict(int)
    total_by_station: dict[str, int] = defaultdict(int)
    for t, rec in tickers.items():
        total_by_station[rec["station"]] += 1
        if is_settled(rec):
            complete_by_station[rec["station"]] += 1

    stats = defaultdict(lambda: {
        "trades": 0, "wins": 0, "losses": 0,
        "pnl_cents": 0.0, "capital_cents": 0.0,
        "edges": [], "by_side": defaultdict(lambda: {"n": 0, "w": 0}),
        "unit": "?", "region": "?",
    })

    seen_tickers: set[str] = set()  # entry once per ticker
    skipped_no_settle = 0
    skipped_dup = 0
    skipped_unknown = 0

    with open(CANDIDATES, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    # Sort by timestamp so first-flag dedup is deterministic and chronological.
    rows.sort(key=lambda r: r["ts"])

    for r in rows:
        t = r["ticker"]
        if t in seen_tickers:
            skipped_dup += 1
            continue
        rec = tickers.get(t)
        if not rec:
            skipped_unknown += 1
            continue
        if not is_settled(rec):
            skipped_no_settle += 1
            continue
        seen_tickers.add(t)

        low = float(r["bracket_low"])
        high = float(r["bracket_high"])
        settled_yes = (low <= rec["max_high"] <= high)

        side = r["flagged_side"]
        price = float(r["flagged_price"])
        f_cents = fee_cents(price)
        won = (side == "YES") == settled_yes
        pnl = (100 - price - f_cents) if won else (-price - f_cents)

        station = r["station"]
        s = stats[station]
        s["unit"] = rec["unit"]
        s["region"] = rec["region"]
        s["trades"] += 1
        s["capital_cents"] += price
        s["pnl_cents"] += pnl
        s["edges"].append(float(r["flagged_edge"]))
        s["by_side"][side]["n"] += 1
        if won:
            s["wins"] += 1
            s["by_side"][side]["w"] += 1
        else:
            s["losses"] += 1

    # ---- Report ----
    print("\n=== Polling coverage (tickers polled to within 1h of close) ===")
    print(f"{'Station':6} {'Region':8} {'Total':>5} {'Settled':>7}")
    for st in sorted(total_by_station):
        live = "  [LIVE]" if st in LIVE_STATIONS else ""
        print(f"{st:6} {'':8} {total_by_station[st]:>5} "
              f"{complete_by_station.get(st,0):>7}{live}")

    print("\n=== Per-station backtest (first flag per ticker, hold to settlement) ===")
    print(f"Filters: ticker polled within {MAX_MINS_TO_SETTLE_AT_LAST_POLL}min of close "
          f"AND last temp >= {MIN_TEMP_DROP_FROM_PEAK}° below peak (descending)")
    print(f"Skipped: {skipped_dup} duplicate flags, "
          f"{skipped_no_settle} tickers without complete settlement, "
          f"{skipped_unknown} unknown tickers")
    print()
    header = (f"{'Station':6} {'Reg':5} {'Live?':5} {'Trades':>6} {'Win%':>6} "
              f"{'PnL¢':>8} {'Cap¢':>8} {'ROI%':>7} {'AvgEdge':>8} "
              f"{'NO win/n':>10} {'YES win/n':>10}")
    print(header)
    print("-" * len(header))

    rows_summary = []
    for st in sorted(stats, key=lambda s: -stats[s]["pnl_cents"]):
        s = stats[st]
        n = s["trades"]
        wr = (s["wins"] / n * 100) if n else 0.0
        roi = (s["pnl_cents"] / s["capital_cents"] * 100) if s["capital_cents"] else 0.0
        avg_edge = sum(s["edges"]) / len(s["edges"]) if s["edges"] else 0.0
        live = "YES" if st in LIVE_STATIONS else "no"
        no_w = s["by_side"]["NO"]["w"]; no_n = s["by_side"]["NO"]["n"]
        yes_w = s["by_side"]["YES"]["w"]; yes_n = s["by_side"]["YES"]["n"]
        print(f"{st:6} {s['region'][:4]:5} {live:5} {n:>6} {wr:5.1f}% "
              f"{s['pnl_cents']:>8.1f} {s['capital_cents']:>8.0f} {roi:>6.1f}% "
              f"{avg_edge:>7.1f}¢ {no_w:>3}/{no_n:<5} {yes_w:>3}/{yes_n:<5}")
        rows_summary.append({
            "station": st, "region": s["region"], "live": st in LIVE_STATIONS,
            "trades": n, "win_rate_pct": round(wr, 1),
            "pnl_cents": round(s["pnl_cents"], 1),
            "capital_cents": round(s["capital_cents"], 1),
            "roi_pct": round(roi, 2), "avg_edge_cents": round(avg_edge, 1),
            "no_wins": no_w, "no_n": no_n, "yes_wins": yes_w, "yes_n": yes_n,
        })

    # Save the summary
    out_path = SHADOW_DIR / "backtest_summary.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(rows_summary, f, indent=2)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()

"""Per-station calibration: does the flagged confidence match realized win rate?"""
import csv
import json
from collections import defaultdict
from pathlib import Path

SHADOW_DIR = Path("shadow-logs")
SNAPSHOTS = SHADOW_DIR / "snapshots.jsonl"
CANDIDATES = SHADOW_DIR / "candidates.csv"

LIVE_STATIONS = {"KORD", "KMIA", "KLAX", "KATL", "KHOU"}
MAX_MINS_TO_SETTLE = 300
MIN_TEMP_DROP_FROM_PEAK = 1.0


def load_tickers() -> dict[str, dict]:
    out: dict[str, dict] = {}
    with open(SNAPSHOTS, "r", encoding="utf-8") as f:
        for line in f:
            s = json.loads(line)
            t = s["ticker"]
            if t not in out:
                out[t] = {
                    "station": s["station"], "unit": s["unit"],
                    "low": s["bracket_low"], "high": s["bracket_high"],
                    "max_high": s["current_high"], "last_ts": s["ts"],
                    "last_temp": s["latest_temp"],
                    "min_mins_left": s["minutes_to_settlement"],
                }
                continue
            r = out[t]
            r["max_high"] = max(r["max_high"], s["current_high"])
            r["min_mins_left"] = min(r["min_mins_left"], s["minutes_to_settlement"])
            if s["ts"] > r["last_ts"]:
                r["last_ts"] = s["ts"]; r["last_temp"] = s["latest_temp"]
    return out


def is_settled(rec: dict) -> bool:
    return (rec["min_mins_left"] <= MAX_MINS_TO_SETTLE
            and (rec["max_high"] - rec["last_temp"]) >= MIN_TEMP_DROP_FROM_PEAK)


def main() -> None:
    tickers = load_tickers()

    # Per-day, per-station: collect first-flag-per-ticker trades
    per_day = defaultdict(lambda: defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0}))
    per_station_total = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0,
                                              "conf_sum": 0.0, "edges": []})
    seen = set()

    with open(CANDIDATES, "r", encoding="utf-8") as f:
        rows = sorted(csv.DictReader(f), key=lambda r: r["ts"])
    for r in rows:
        t = r["ticker"]
        if t in seen:
            continue
        rec = tickers.get(t)
        if not rec or not is_settled(rec):
            continue
        seen.add(t)

        low = float(r["bracket_low"]); high = float(r["bracket_high"])
        settled_yes = low <= rec["max_high"] <= high
        side = r["flagged_side"]; price = float(r["flagged_price"])
        p = price / 100; fee = max(1.0, 7.0 * p * (1 - p))
        won = (side == "YES") == settled_yes
        pnl = (100 - price - fee) if won else (-price - fee)

        day = r["ts"][:10]
        st = r["station"]
        per_day[st][day]["n"] += 1; per_day[st][day]["pnl"] += pnl
        if won: per_day[st][day]["w"] += 1
        s = per_station_total[st]
        s["n"] += 1; s["pnl"] += pnl
        s["conf_sum"] += float(r["flagged_confidence"])
        s["edges"].append(float(r["flagged_edge"]))
        if won: s["w"] += 1

    print("=== Calibration: expected vs realized win rate per station ===")
    print(f"{'Station':6} {'Live?':5} {'n':>4} {'Pred%':>6} {'Real%':>6} {'Diff%':>6} "
          f"{'PnL¢':>8} {'AvgEdge':>8}")
    print("-" * 70)
    for st in sorted(per_station_total, key=lambda s: -per_station_total[s]["pnl"]):
        s = per_station_total[st]
        n = s["n"]
        pred = (s["conf_sum"] / n * 100) if n else 0
        real = (s["w"] / n * 100) if n else 0
        diff = real - pred
        live = "YES" if st in LIVE_STATIONS else ""
        avg_edge = sum(s["edges"]) / len(s["edges"])
        flag = " <-- LIVE" if st in LIVE_STATIONS else ""
        # candidate flag for promotion
        promotable = ""
        if st not in LIVE_STATIONS and n >= 2 and real >= 80 and s["pnl"] > 0:
            promotable = " *"
        print(f"{st:6} {live:5} {n:>4} {pred:5.1f}% {real:5.1f}% {diff:+5.1f}% "
              f"{s['pnl']:>8.1f} {avg_edge:>7.1f}¢{flag}{promotable}")

    print("\n=== Per-day stability (only live + interesting non-live) ===")
    interesting = LIVE_STATIONS | {"RKSI", "WMKK", "CYYZ", "LTFM", "RCSS", "LTAC",
                                    "KBKF", "MPMG", "EGLC", "ZGGG"}
    days = sorted({d for stats in per_day.values() for d in stats})
    header = f"{'Station':6} " + " ".join(f"{d[-5:]:>10}" for d in days) + " " + f"{'Total':>10}"
    print(header)
    for st in sorted(interesting & set(per_day.keys())):
        cells = []
        total_pnl = 0; total_n = 0; total_w = 0
        for d in days:
            x = per_day[st].get(d, {"n": 0, "w": 0, "pnl": 0.0})
            if x["n"] > 0:
                cells.append(f"{x['w']}/{x['n']}({x['pnl']:+.0f})")
            else:
                cells.append("-")
            total_pnl += x["pnl"]; total_n += x["n"]; total_w += x["w"]
        live = " LIVE" if st in LIVE_STATIONS else ""
        print(f"{st:6} " + " ".join(f"{c:>10}" for c in cells)
              + f" {total_w}/{total_n}({total_pnl:+.0f}){live}")


if __name__ == "__main__":
    main()

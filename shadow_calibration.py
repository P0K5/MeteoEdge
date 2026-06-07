"""Per-station calibration: does the flagged confidence match realized win rate?

Data sources (tried in order):
- DB observations and candidates tables (when --db flag is set or DB has data)
- File fallback: shadow-logs/snapshots.jsonl + shadow-logs/candidates.csv
"""
import argparse
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


def load_tickers_from_file() -> dict[str, dict]:
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


def load_tickers_from_db(db, since: str = "2000-01-01") -> dict[str, dict]:
    """Per-station rollup from DB observations table."""
    out: dict[str, dict] = {}
    from src.config import STATIONS as _STATIONS
    for cfg in _STATIONS:
        station = cfg[0]
        unit = cfg[5] if len(cfg) > 5 else "F"
        rows = db.get_observations(station=station, since=since)
        if not rows:
            continue
        for row in rows:
            key = station
            if key not in out:
                out[key] = {
                    "station": station,
                    "unit": unit,
                    "max_high": row.get("current_high") or row["temp_f"],
                    "last_ts": row["ts"],
                    "last_temp": row["temp_f"],
                    "min_mins_left": 9999,
                }
            else:
                rec = out[key]
                h = row.get("current_high") or row["temp_f"]
                rec["max_high"] = max(rec["max_high"], h)
                if row["ts"] > rec["last_ts"]:
                    rec["last_ts"] = row["ts"]
                    rec["last_temp"] = row["temp_f"]
    return out


def load_tickers(db=None, since: str = "2000-01-01") -> dict[str, dict]:
    """Load ticker settlement data, preferring DB when available."""
    if db is not None:
        try:
            result = load_tickers_from_db(db, since=since)
            if result:
                print(f"[calibration] Loaded tickers from DB ({len(result)} stations)")
                return result
        except Exception as e:
            print(f"[calibration] DB ticker load failed: {e} — falling back to file")

    if SNAPSHOTS.exists():
        result = load_tickers_from_file()
        print(f"[calibration] Loaded {len(result)} tickers from file")
        return result

    raise FileNotFoundError(
        f"No data source available: DB empty/unavailable and {SNAPSHOTS} not found"
    )


def load_candidates_from_file() -> list[dict]:
    with open(CANDIDATES, "r", encoding="utf-8") as f:
        return sorted(csv.DictReader(f), key=lambda r: r["ts"])


def load_candidates_from_db(db, since: str = "2000-01-01") -> list[dict]:
    """Load candidates from DB, normalised to CSV field names."""
    cur = db._conn.execute(
        "SELECT * FROM candidates WHERE ts >= ? ORDER BY ts ASC",
        (since,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    normalised = []
    for r in rows:
        normalised.append({
            "ts": r["ts"],
            "station": r["station"],
            "ticker": r["ticker"],
            "bracket_low": r["bracket_low"],
            "bracket_high": r["bracket_high"],
            "flagged_side": r["side"],
            "flagged_price": r["predicted_price"],
            "flagged_edge": r["predicted_edge"],
            "flagged_confidence": r["confidence"],
            "minutes_to_settlement": r["minutes_to_settlement"],
            "flagged_first": r.get("flagged_first", 1),
        })
    return normalised


def load_candidates(db=None, since: str = "2000-01-01") -> list[dict]:
    """Load candidate rows, preferring DB when available."""
    if db is not None:
        try:
            rows = load_candidates_from_db(db, since=since)
            if rows:
                print(f"[calibration] Loaded {len(rows)} candidates from DB")
                return rows
        except Exception as e:
            print(f"[calibration] DB candidate load failed: {e} — falling back to file")

    if CANDIDATES.exists():
        rows = load_candidates_from_file()
        print(f"[calibration] Loaded {len(rows)} candidates from file")
        return rows

    raise FileNotFoundError(
        f"No data source available: DB empty/unavailable and {CANDIDATES} not found"
    )


def is_settled(rec: dict) -> bool:
    return (rec["min_mins_left"] <= MAX_MINS_TO_SETTLE
            and (rec["max_high"] - rec["last_temp"]) >= MIN_TEMP_DROP_FROM_PEAK)


def main() -> None:
    parser = argparse.ArgumentParser(description="Calibration — expected vs realized win rate per station")
    parser.add_argument("--db", action="store_true", default=False,
                        help="Force DB as data source (default: auto-detect, file fallback)")
    parser.add_argument("--since", default="2000-01-01",
                        help="Only include data at or after this date (YYYY-MM-DD)")
    args = parser.parse_args()

    db = None
    if args.db:
        try:
            from src.data.db import Database
            db = Database()
            print("[calibration] Using DB as primary data source")
        except Exception as e:
            print(f"[calibration] Failed to open DB: {e} — falling back to files")

    tickers = load_tickers(db=db, since=args.since)

    # Per-day, per-station: collect first-flag-per-ticker trades
    per_day = defaultdict(lambda: defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0}))
    per_station_total = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0,
                                              "conf_sum": 0.0, "edges": []})
    seen = set()

    candidate_rows = load_candidates(db=db, since=args.since)
    for r in candidate_rows:
        t = r["ticker"]
        if t in seen:
            continue
        # Match by ticker first, then fall back to station for DB-sourced rollups
        rec = tickers.get(t) or tickers.get(r.get("station", ""))
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

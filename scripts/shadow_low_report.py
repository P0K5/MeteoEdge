#!/usr/bin/env python3
"""Daily shadow-low report: summarise low-side shadow candidates for each station.

Reads candidates with direction='low' and shadow=1 from today's log, then
appends one summary line per station to logs/shadow_low_<station>.log.

Usage:
    python scripts/shadow_low_report.py [--db-path PATH] [--date YYYY-MM-DD]
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# Epic-C shadow-rollout stations (issue #457)
SHADOW_LOW_STATIONS = ("EGLC", "LFPB", "RJTT", "RKSI", "ZSPD", "KMIA")


def _load_candidates(db, target_date: str) -> list[dict]:
    """Return low-side shadow candidates for *target_date*.

    Queries candidates with direction='low' (requires #455+#456 merged).
    Returns empty list gracefully when the direction column does not yet exist.
    """
    try:
        cur = db._conn.execute(
            "SELECT station, ticker, side, predicted_price, predicted_edge, confidence, ts "
            "FROM candidates "
            "WHERE direction='low' AND substr(ts,1,10)=?",
            (target_date,),
        )
    except Exception as exc:
        # direction column not yet present (pre-#456 DB) — report zeros
        log.warning("[shadow_low] candidates.direction column unavailable: %s", exc)
        return []
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _write_report(station: str, rows: list[dict], target_date: str, log_dir: Path) -> None:
    """Append one summary line to logs/shadow_low_<station>.log."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"shadow_low_{station}.log"

    n_no = sum(1 for r in rows if r["side"] == "NO")
    n_yes = sum(1 for r in rows if r["side"] == "YES")
    avg_edge = (
        sum(r["predicted_edge"] for r in rows) / len(rows) if rows else 0.0
    )
    avg_conf = (
        sum(r["confidence"] for r in rows) / len(rows) if rows else 0.0
    )

    line = (
        f"{target_date}\t"
        f"total={len(rows)}\t"
        f"no={n_no}\t"
        f"yes={n_yes}\t"
        f"avg_edge={avg_edge:.2f}\t"
        f"avg_conf={avg_conf:.3f}\n"
    )
    with open(log_path, "a") as f:
        f.write(line)
    log.info("[shadow_low] %s: %s candidates (NO=%d YES=%d) → %s",
             station, len(rows), n_no, n_yes, log_path)


def _run(db, target_date: str, log_dir: Path) -> None:
    all_rows = _load_candidates(db, target_date)
    by_station: dict[str, list[dict]] = {s: [] for s in SHADOW_LOW_STATIONS}
    for row in all_rows:
        if row["station"] in by_station:
            by_station[row["station"]].append(row)

    for station, rows in by_station.items():
        _write_report(station, rows, target_date, log_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily shadow-low candidate report")
    parser.add_argument("--db-path", default=os.getenv("DB_PATH", "data/meteoedge.db"))
    parser.add_argument(
        "--date",
        default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        help="Date to report on (YYYY-MM-DD, default: today UTC)",
    )
    parser.add_argument(
        "--log-dir",
        default="logs",
        help="Directory for shadow_low_*.log files (default: logs/)",
    )
    args = parser.parse_args()

    from src.data.db import Database
    db = Database(path=args.db_path)

    log_dir = Path(args.log_dir)
    log.info("[shadow_low] reporting for date=%s stations=%s", args.date, SHADOW_LOW_STATIONS)
    _run(db, args.date, log_dir)


if __name__ == "__main__":
    main()

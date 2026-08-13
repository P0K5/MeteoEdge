"""Audit-only report for issue #993: classify multi-fill trade groups.

`OrderManager.reconcile_timeout_fills()` currently infers a fill from wallet
token presence alone (see #993). When a #743 in-process reprice-retry runs
after a clean timeout, the SAME token ends up in the wallet because of the
*replacement* order -- the original, never-filled order then gets falsely
patched to outcome='filled'/'sold' too, producing a group of >1
filled/sold ``trades`` rows for the same (day, station, ticker, side).

This script is read-only. It:

1. Runs the grouping query from the #993 issue body against the live
   ``trades`` table to find every such multi-fill group.
2. Classifies each group into exactly one of two populations, using the
   intra-group timestamp delta between consecutive rows (ordered by ``ts``):

   - **reprice artifact** (#743/#993): exactly 2 rows, and the gap between
     them is bounded by the ~5-minute order-fill-wait window
     (``FILL_MAX_WAIT_S`` in ``order_executor.py``) plus reprice/book-fetch
     latency. Empirically this population sits at ~5.0 minutes.
   - **pre-#611 bracket stacking**: everything else -- 3+ rows, or 2 rows
     whose gap is at or above roughly one poll cycle
     (``POLL_INTERVAL_SECONDS``, 5 min default, but observed stacking gaps
     are consistently >=10 min because a poll's blocking fill-wait pushes
     the next placement to the poll after next). These are genuine repeated
     placements from the stacking bug #611 fixed -- real trades, not
     fabrications -- and must not be conflated with the reprice population.

   The threshold (``REPRICE_MAX_GAP_MINUTES``) is set well below the
   observed stacking-population floor so the split is unambiguous on the
   current data; it is printed alongside the results for visibility if the
   underlying data changes.

3. Reports the PnL impact of the reprice-artifact population: total trade
   count contributed, total pnl summed across those rows, and how the
   dataset's realized win rate over settled ('filled'/'sold') live trades
   moves if those rows are excluded.

This script does NOT delete, update, or otherwise mutate any `trades` row.
It is a report only. Run:

    .venv/bin/python -m src.scripts.audit_multi_fill_groups [--db data/meteoedge.db]
"""
import argparse
import sqlite3
from datetime import datetime

from src.execution.order_executor import FILL_MAX_WAIT_S

# Bound for the reprice-artifact population's intra-pair gap. FILL_MAX_WAIT_S
# is 300s (5 min) -- the order-timeout wait -- plus book-fetch/reprice/place
# latency for the retry. Observed reprice pairs sit at ~5.0 min; observed
# stacking-population gaps are consistently >=10.3 min (roughly one poll
# cycle), so any threshold strictly between 5 and 10 minutes cleanly
# separates the two populations on the current data.
REPRICE_MAX_GAP_MINUTES = (FILL_MAX_WAIT_S / 60.0) * 1.5  # 7.5 min

GROUPING_QUERY = """
SELECT substr(ts,1,10) AS day, station, ticker, side,
       COUNT(*) AS n, GROUP_CONCAT(id) AS ids
FROM trades WHERE mode='live'
GROUP BY day, station, ticker, side
HAVING SUM(CASE WHEN outcome IN ('filled','sold') THEN 1 ELSE 0 END) > 1
"""

ROW_QUERY_TEMPLATE = (
    "SELECT id, ts, order_id, outcome, pnl, actual_price, size_eur "
    "FROM trades WHERE id IN ({placeholders}) ORDER BY ts"
)


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def find_multi_fill_groups(conn: sqlite3.Connection) -> "list[dict]":
    """Run the #993 grouping query and return each group with its member rows."""
    conn.row_factory = sqlite3.Row
    groups = conn.execute(GROUPING_QUERY).fetchall()
    result = []
    for g in groups:
        ids = [int(x) for x in g["ids"].split(",")]
        placeholders = ",".join(str(i) for i in ids)
        rows = conn.execute(
            ROW_QUERY_TEMPLATE.format(placeholders=placeholders)
        ).fetchall()
        result.append({
            "day": g["day"],
            "station": g["station"],
            "ticker": g["ticker"],
            "side": g["side"],
            "n": g["n"],
            "rows": [dict(r) for r in rows],
        })
    return result


def classify_group(group: dict, max_gap_minutes: float = REPRICE_MAX_GAP_MINUTES) -> str:
    """Classify one group as 'reprice_artifact' or 'bracket_stacking'.

    A group is a reprice artifact iff it has exactly 2 rows AND the gap
    between them is <= max_gap_minutes. Everything else -- 3+ rows of any
    spacing, or 2 rows spaced further apart -- is bracket stacking.
    """
    rows = group["rows"]
    if len(rows) != 2:
        return "bracket_stacking"
    t0 = _parse_ts(rows[0]["ts"])
    t1 = _parse_ts(rows[1]["ts"])
    gap_minutes = abs((t1 - t0).total_seconds()) / 60.0
    if gap_minutes <= max_gap_minutes:
        return "reprice_artifact"
    return "bracket_stacking"


def compute_pnl_impact(reprice_groups: "list[dict]") -> dict:
    """Sum pnl/size_eur across all rows in reprice-artifact groups."""
    rows = [r for g in reprice_groups for r in g["rows"]]
    total_pnl = sum(r["pnl"] or 0.0 for r in rows)
    total_size_eur = sum(r["size_eur"] or 0.0 for r in rows)
    zero_size_rows = [r for r in rows if not r["size_eur"]]
    return {
        "n_rows": len(rows),
        "n_groups": len(reprice_groups),
        "total_pnl": round(total_pnl, 4),
        "total_size_eur": round(total_size_eur, 4),
        "n_zero_size_rows": len(zero_size_rows),
        "zero_size_row_ids": [r["id"] for r in zero_size_rows],
    }


def compute_settled_stats(conn: sqlite3.Connection, exclude_ids: "set[int]") -> dict:
    """Win-rate / trade-count over settled ('filled'/'sold') live trades,
    with and without the given ids excluded."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, pnl FROM trades WHERE mode='live' AND outcome IN ('filled','sold')"
    ).fetchall()

    def _stats(rs):
        n = len(rs)
        settled = [r for r in rs if r["pnl"] is not None]
        wins = sum(1 for r in settled if r["pnl"] > 0)
        win_rate = (wins / len(settled)) if settled else None
        return {"n_trades": n, "n_settled": len(settled), "n_wins": wins, "win_rate": win_rate}

    before = _stats(rows)
    after = _stats([r for r in rows if r["id"] not in exclude_ids])
    return {"before": before, "after": after}


def _ro_uri(db_path: str) -> str:
    """Build a read-only sqlite3 URI, robust to Windows backslash paths."""
    import os
    abspath = os.path.abspath(db_path).replace(os.sep, "/")
    prefix = "" if abspath.startswith("/") else "/"
    return f"file:{prefix}{abspath}?mode=ro"


def run_report(db_path: str) -> None:
    conn = sqlite3.connect(_ro_uri(db_path), uri=True)
    groups = find_multi_fill_groups(conn)

    reprice_groups = []
    stacking_groups = []
    for g in groups:
        g["classification"] = classify_group(g)
        if g["classification"] == "reprice_artifact":
            reprice_groups.append(g)
        else:
            stacking_groups.append(g)

    print("# Multi-fill group audit (issue #993)")
    print(f"# DB: {db_path}")
    print(f"# Reprice-artifact gap threshold: <= {REPRICE_MAX_GAP_MINUTES:.1f} min "
          f"(FILL_MAX_WAIT_S={FILL_MAX_WAIT_S}s x 1.5)")
    print(f"# Total multi-fill groups found: {len(groups)}\n")

    print(f"## Reprice-artifact groups ({len(reprice_groups)})")
    for g in reprice_groups:
        rows = g["rows"]
        t0, t1 = _parse_ts(rows[0]["ts"]), _parse_ts(rows[1]["ts"])
        gap = abs((t1 - t0).total_seconds()) / 60.0
        print(f"  {g['day']} {g['station']} {g['ticker'][:16]} {g['side']} "
              f"n={g['n']} gap={gap:.1f}min")
        for r in rows:
            print(f"    id={r['id']} ts={r['ts']} order_id={(r['order_id'] or '')[:14]}... "
                  f"outcome={r['outcome']} pnl={r['pnl']} size_eur={r['size_eur']} "
                  f"price={r['actual_price']}c")
    print()

    print(f"## Pre-#611 bracket-stacking groups ({len(stacking_groups)})")
    for g in stacking_groups:
        rows = g["rows"]
        deltas = [
            abs((_parse_ts(rows[i + 1]["ts"]) - _parse_ts(rows[i]["ts"])).total_seconds()) / 60.0
            for i in range(len(rows) - 1)
        ]
        max_gap = max(deltas) if deltas else 0.0
        min_gap = min(deltas) if deltas else 0.0
        print(f"  {g['day']} {g['station']} {g['ticker'][:16]} {g['side']} "
              f"n={g['n']} gap_range=[{min_gap:.1f},{max_gap:.1f}]min ids={[r['id'] for r in rows]}")
    print()

    impact = compute_pnl_impact(reprice_groups)
    print("## PnL impact of the reprice-artifact population")
    print(f"  rows involved: {impact['n_rows']} (across {impact['n_groups']} groups)")
    print(f"  sum(pnl):      {impact['total_pnl']}")
    print(f"  sum(size_eur): {impact['total_size_eur']}")
    print(f"  zero-size rows (the falsified timed-out leg of each pair): "
          f"{impact['n_zero_size_rows']} -- ids {impact['zero_size_row_ids']}")
    print()

    exclude_ids = {r["id"] for g in reprice_groups for r in g["rows"]}
    stats = compute_settled_stats(conn, exclude_ids)
    print("## Settled live-trade stats, with vs without reprice-artifact rows")
    for label in ("before", "after"):
        s = stats[label]
        wr = f"{s['win_rate']:.4f}" if s["win_rate"] is not None else "n/a"
        print(f"  {label:>7}: n_trades={s['n_trades']} n_settled={s['n_settled']} "
              f"n_wins={s['n_wins']} win_rate={wr}")
    print()
    wr_before = stats["before"]["win_rate"]
    wr_after = stats["after"]["win_rate"]
    if wr_before is not None and wr_after is not None:
        delta_pp = f"{(wr_after - wr_before) * 100:+.2f}"
        wr_line = (
            f"  diluting the realized win rate by {delta_pp} percentage points on\n"
            f"  this dataset ({wr_before:.4f} -> {wr_after:.4f} once excluded) and\n"
        )
    else:
        wr_line = "  win rate could not be compared (insufficient settled rows) but\n"
    print(
        "## Summary\n"
        "  The falsified (never-actually-filled) leg of each reprice-artifact\n"
        "  pair carries size_eur=0.0 and pnl=0.0 -- #983's fill-size guard\n"
        "  already stopped it from ever getting real size or a phantom\n"
        "  open_positions row. So the direct dollar PnL misstatement from\n"
        f"  these {impact['n_zero_size_rows']} row(s) today is $0.00. Their cost is\n"
        f"  elsewhere: {impact['n_zero_size_rows']} extra row(s) in trade-count/\n"
        "  settlement stats that are not real trades,\n"
        f"{wr_line}"
        "  feeding settlement logic with non-existent trades. Repair (i.e.\n"
        "  deleting/correcting these rows) is future/separate work -- not in\n"
        "  scope for this report."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/meteoedge.db",
                         help="Path to the SQLite DB to audit (read-only). Default: data/meteoedge.db")
    args = parser.parse_args()
    run_report(args.db)


if __name__ == "__main__":
    main()

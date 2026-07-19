"""One-off backfill for the shadow NO-side price-semantics bug (issue #737).

Background
==========
Until #737, ``src/scripts/run.py`` stored ``actual_price=yes_ask_cents`` on
EVERY shadow row regardless of side. ``settle_shadow_trades()`` prices
``actual_price`` as the cost of the side actually bought (win pays
``100 - cost``, loss pays ``-cost``). So for NO shadow rows the stored value
was the YES ask (~22c) while the true NO cost was ~78c, inflating every
settled NO win by ~+0.56 and under-charging every loss by ~+0.56 — the shadow
NO book read ~14x too profitable.

The writer is fixed going forward (NO rows now store the NO ask). This script
migrates the historical rows written under the old convention:

  * For ``mode='shadow'`` ``side='NO'`` rows with ``ts < --deploy-ts``:
      - flip ``actual_price`` -> ``100 - actual_price`` (YES ask -> NO cost);
      - if the row is already settled (``settled_at`` and ``pnl`` set), recompute
        ``pnl`` and ``capital_after`` from the corrected cost. The win/loss
        outcome is read from the SIGN of the existing pnl (old-convention win
        pnl > 0, loss pnl < 0), which is preserved by the flip.
      - rows that are settled-but-``pnl IS NULL`` (quarantined/excluded, e.g.
        issue #610) are marked done WITHOUT a price flip — they are excluded
        from all stats and must not be resurrected.

Idempotency & guard
===================
  * A marker column ``trades.price_semantics_fixed`` (added idempotently) is set
    to 1 on every processed row; already-marked rows are skipped, so re-running
    the script never double-flips a price.
  * ``--deploy-ts`` bounds the migration to rows written BEFORE the #737 writer
    fix deployed. Rows written afterwards already carry the correct NO cost and
    must not be touched. Pass the actual deploy timestamp; it defaults to "now"
    for the common case of running immediately at deploy.

Coordination with #742
======================
#742 makes the settler resolve the 0x-ticker LOW backlog. Whichever runs first:
  * settle-then-backfill: the newly-settled rows are still ``ts < deploy-ts`` and
    unmarked, so this script flips their price AND recomputes their pnl once.
  * backfill-then-settle: this script flips the (still unsettled) price and marks
    it; the later settle computes pnl from the already-corrected cost.
Either way each row is corrected exactly once (marker guarantees no re-flip).

Usage (operator, against the production DB; not run in CI):
    python -m src.scripts.backfill_shadow_no_price_737 --dry-run
    python -m src.scripts.backfill_shadow_no_price_737 --deploy-ts 2026-07-20T00:00:00+00:00
    python -m src.scripts.backfill_shadow_no_price_737 --db-path /path/to/meteoedge.db
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_DB_PATH = Path(os.getenv("DB_PATH", "data/meteoedge.db"))


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_marker_column(conn: sqlite3.Connection) -> None:
    """Add the idempotency marker column if it does not already exist."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
    if "price_semantics_fixed" not in cols:
        conn.execute(
            "ALTER TABLE trades ADD COLUMN price_semantics_fixed INTEGER DEFAULT 0"
        )
        conn.commit()


def backfill(db_path: Path, deploy_ts: str, dry_run: bool = False) -> int:
    """Correct historical shadow NO prices/pnl. Returns a process exit code."""
    if not db_path.exists():
        print(f"[backfill-737] DB not found: {db_path}")
        return 1

    conn = _connect(db_path)
    _ensure_marker_column(conn)

    rows = conn.execute(
        "SELECT id, actual_price, pnl, settled_at, capital_before "
        "FROM trades "
        "WHERE mode='shadow' AND side='NO' AND ts < ? "
        "AND (price_semantics_fixed IS NULL OR price_semantics_fixed = 0)",
        (deploy_ts,),
    ).fetchall()

    n_unsettled = 0   # price flipped, pnl left NULL for later settlement
    n_settled = 0     # price flipped + pnl/capital recomputed
    n_quarantined = 0 # settled with NULL pnl (excluded) -> marked, not flipped
    n_bad = 0

    for r in rows:
        rid = r["id"]
        old_price = r["actual_price"]
        if old_price is None or not (0 <= int(old_price) <= 100):
            print(f"[backfill-737] id={rid}: implausible actual_price={old_price} -- skipping")
            n_bad += 1
            continue
        new_price = 100 - int(old_price)

        settled = r["settled_at"] is not None
        has_pnl = r["pnl"] is not None

        if settled and not has_pnl:
            # Quarantined / excluded row (e.g. #610): do not resurrect its pnl.
            action = "[dry-run] would mark" if dry_run else "marking"
            print(f"[backfill-737] id={rid}: excluded (settled, pnl NULL) -- {action} done, no price flip")
            if not dry_run:
                conn.execute(
                    "UPDATE trades SET price_semantics_fixed=1 WHERE id=?", (rid,)
                )
            n_quarantined += 1
            continue

        if settled and has_pnl:
            won = float(r["pnl"]) > 0  # old-convention: win pnl > 0, loss pnl < 0
            new_pnl = (100 - new_price) / 100 if won else -new_price / 100
            cap_before = float(r["capital_before"] or 0.0)
            new_capital_after = round(cap_before + new_pnl, 6)
            action = "[dry-run] would set" if dry_run else "setting"
            print(
                f"[backfill-737] id={rid}: settled {('WIN' if won else 'LOSS')} "
                f"actual_price {old_price}->{new_price}, pnl->{round(new_pnl, 6)} "
                f"({action})"
            )
            if not dry_run:
                conn.execute(
                    "UPDATE trades SET actual_price=?, pnl=?, capital_after=?, "
                    "price_semantics_fixed=1 WHERE id=?",
                    (new_price, round(new_pnl, 6), new_capital_after, rid),
                )
            n_settled += 1
        else:
            # Unsettled: flip the price only; settlement will price it correctly.
            action = "[dry-run] would flip" if dry_run else "flipping"
            print(f"[backfill-737] id={rid}: unsettled actual_price {old_price}->{new_price} ({action})")
            if not dry_run:
                conn.execute(
                    "UPDATE trades SET actual_price=?, price_semantics_fixed=1 WHERE id=?",
                    (new_price, rid),
                )
            n_unsettled += 1

    if not dry_run:
        conn.commit()
    conn.close()

    verb = "would correct" if dry_run else "corrected"
    print(
        f"\n[backfill-737] Summary (deploy-ts={deploy_ts}): {verb} "
        f"{n_settled} settled + {n_unsettled} unsettled NO row(s); "
        f"{n_quarantined} excluded-row(s) marked; {n_bad} skipped (bad price)."
    )
    if n_bad:
        print("[backfill-737] WARNING: rows with implausible actual_price were skipped -- investigate.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill historical shadow NO rows to the #737 bought-side cost "
            "convention (flip actual_price, recompute settled pnl)."
        )
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Preview what would change without modifying the DB",
    )
    parser.add_argument(
        "--deploy-ts", type=str, default=datetime.now(timezone.utc).isoformat(),
        help=(
            "ISO timestamp of the #737 writer deploy. Only rows with ts before "
            "this are migrated (rows written after already carry the NO cost). "
            "Defaults to now."
        ),
    )
    parser.add_argument(
        "--db-path", type=Path, default=_DEFAULT_DB_PATH,
        help=f"Path to the SQLite DB (default: {_DEFAULT_DB_PATH})",
    )
    args = parser.parse_args()
    raise SystemExit(backfill(args.db_path, args.deploy_ts, dry_run=args.dry_run))


if __name__ == "__main__":
    main()

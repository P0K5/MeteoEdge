"""DRY-RUN report: recompute copy_live_positions fill costs and settled P&L
for rows written before issue #1336 (full fills never recorded their actual
USD cost in ``filled_stake_usd``, so settlement booked them at the intended
``stake_usd``).

**This script is read-only and never writes to any database.** It opens the
SQLite file with ``mode=ro`` and has no write path at all. Applying the
corrections to a live database is a SEPARATE operator step that needs
explicit approval -- it is deliberately not implemented here. The output is
a before/after report per row plus the total drift impact, to be reviewed
before anything is changed.

Per row (scope: ``status IN ('filled','partial','settled')`` with
``filled_stake_usd IS NULL`` and an ``order_id``):

- ``cost_after`` = the CLOB fill record's matched shares x placed
  ``fill_price`` (read via a raw read-only CLOB ``get_order`` GET). A
  present-but-zero matched size keeps ``stake_usd`` and is reported as
  ``no_fill_record``; an empty or failed response is reported as
  ``lookup_error`` and also keeps ``stake_usd``.
- For ``settled`` rows: ``pnl_after`` = ``compute_realized_pnl_usd`` with
  ``cost_after``, using the market's resolution re-fetched read-only via
  ``fetch_market_resolution``. ``pnl_delta = pnl_after - stored
  settled_pnl_usd``.
- Drift impact: ``expected_balance = capital - committed + realized``
  (copy_live_settle.check_wallet_balance_drift). A correction changes it by
  ``sum(pnl_delta over settled) - sum(cost_delta over open rows)``. When
  ``--capital`` and ``--actual-balance`` are given, the drift before and
  after the corrections are printed too.

Usage (read-only)::

    python -m src.scripts.copy_live_fill_cost_backfill --db data/meteoedge.db \\
        --capital 40 --actual-balance 21.53
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.data.copy_pnl import compute_realized_pnl_usd, effective_stake_usd  # noqa: E402

_SCOPE_SQL = (
    "SELECT id, address, market, outcome_index, status, order_id, fill_price, "
    "stake_usd, filled_stake_usd, settled_pnl_usd FROM copy_live_positions "
    "WHERE status IN ('filled','partial','settled') ORDER BY id"
)


# Same field precedence as LiveTrader.get_order_fill_size, but WITHOUT its
# swallow-and-return-0.0 behaviour: an empty/failed response must surface as
# a lookup_error, never masquerade as "no fill record".
_FILL_FIELDS = ("size_matched", "matched_amount", "filled_size", "size_filled")


def fill_shares_from_order(order: "dict | None") -> float:
    """Return matched shares from a raw CLOB order dict, or raise on an empty
    response. A present-but-zero matched size returns 0.0 (a real no-fill)."""
    if not order:
        raise ValueError("empty CLOB order response")
    for field in _FILL_FIELDS:
        val = order.get(field)
        if val is not None:
            return float(val)
    return 0.0


def _r(x: "float | None", nd: int = 4):
    return None if x is None else round(x, nd)


def build_report(
    conn: sqlite3.Connection,
    *,
    get_fill_shares: Callable[[str], float],
    get_resolution: Callable[[str], "bool | None"],
    capital_usd: "float | None" = None,
    actual_balance_usd: "float | None" = None,
) -> dict:
    """Compute the dry-run report. Only SELECTs are issued on *conn*.

    *get_fill_shares* and *get_resolution* are injected so the report can be
    unit-tested without network access. A lookup that raises is recorded as
    ``lookup_error`` on that row and leaves its cost unchanged.
    """
    rows = [dict(r) for r in conn.execute(_SCOPE_SQL).fetchall()]
    resolution_cache: dict = {}
    fill_cache: dict = {}

    def _fill(order_id: str) -> "tuple[float | None, str | None]":
        if order_id not in fill_cache:
            try:
                fill_cache[order_id] = (float(get_fill_shares(order_id) or 0.0), None)
            except Exception as e:  # report, never abort the whole run
                fill_cache[order_id] = (None, f"lookup_error: {e}")
        return fill_cache[order_id]

    def _resolution(market: str) -> "bool | None":
        if market not in resolution_cache:
            try:
                resolution_cache[market] = get_resolution(market)
            except Exception:
                resolution_cache[market] = None
        return resolution_cache[market]

    out_rows = []
    realized_before = 0.0  # all settled rows, stored values
    committed_before = 0.0  # all open rows, effective cost before
    realized_delta = 0.0
    committed_delta = 0.0
    n_changed = 0
    n_no_record = 0
    n_unresolved = 0
    n_error = 0
    n_stored_mismatch = 0

    n_in_scope = 0
    n_already_recorded = 0
    for r in rows:
        stake = float(r["stake_usd"])
        cost_before = effective_stake_usd(r["filled_stake_usd"], stake)
        is_settled = r["status"] == "settled"
        if is_settled:
            if r["settled_pnl_usd"] is not None:
                realized_before += float(r["settled_pnl_usd"])
        else:
            committed_before += cost_before
        if r["filled_stake_usd"] is not None:
            # Already carries a recorded fill cost (post-#1336 or a partial):
            # out of scope, never re-priced. Still counted in the "before"
            # totals above so the drift arithmetic stays complete.
            n_already_recorded += 1
            continue
        n_in_scope += 1

        needs_lookup = bool(r["order_id"] and r["fill_price"])
        note = None
        cost_after = cost_before
        if needs_lookup:
            shares, err = _fill(r["order_id"])
            if err:
                n_error += 1
                note = err
            elif shares and shares > 0:
                cost_after = round(shares * float(r["fill_price"]), 6)
            else:
                n_no_record += 1
                note = "no_fill_record"

        if not is_settled:
            committed_delta += cost_after - cost_before
            if cost_after != cost_before:
                n_changed += 1
            out_rows.append({
                "id": r["id"], "status": r["status"], "fill_price": _r(r["fill_price"]),
                "stake_usd": _r(stake, 2), "cost_before": _r(cost_before, 6),
                "cost_after": _r(cost_after, 6), "cost_delta": _r(cost_after - cost_before, 6),
                "pnl_stored": None, "pnl_after": None, "pnl_delta": None,
                "note": note,
            })
            continue

        stored = None if r["settled_pnl_usd"] is None else float(r["settled_pnl_usd"])
        yes_won = _resolution(r["market"])
        pnl_after = None
        if yes_won is None:
            n_unresolved += 1
            note = (note + "; " if note else "") + "market_unresolved"
        else:
            pnl_after = round(compute_realized_pnl_usd(
                entry_price=float(r["fill_price"]), stake_usd=cost_after,
                outcome_index=int(r["outcome_index"]), yes_won=yes_won,
            ), 6)
            recomputed_before = compute_realized_pnl_usd(
                entry_price=float(r["fill_price"]), stake_usd=cost_before,
                outcome_index=int(r["outcome_index"]), yes_won=yes_won,
            )
            if stored is None or abs(stored - recomputed_before) > 0.005:
                n_stored_mismatch += 1
                note = (note + "; " if note else "") + "stored_pnl_differs_from_recompute"
        pnl_delta = None
        if pnl_after is not None:
            pnl_delta = round(pnl_after - (stored or 0.0), 6)
            realized_delta += pnl_delta
        if cost_after != cost_before or (pnl_delta or 0.0) != 0.0:
            n_changed += 1
        out_rows.append({
            "id": r["id"], "status": r["status"], "fill_price": _r(r["fill_price"]),
            "stake_usd": _r(stake, 2), "cost_before": _r(cost_before, 6),
            "cost_after": _r(cost_after, 6), "cost_delta": _r(cost_after - cost_before, 6),
            "pnl_stored": _r(stored, 6), "pnl_after": _r(pnl_after, 6),
            "pnl_delta": _r(pnl_delta, 6), "note": note,
        })

    # Only settled rows contribute to realized P&L and only open rows to
    # committed exposure; the impact on expected balance is the net of both.
    expected_delta = realized_delta - committed_delta
    summary = {
        "rows_in_scope": n_in_scope,
        "rows_already_recorded_untouched": n_already_recorded,
        "rows_changed": n_changed,
        "no_fill_record": n_no_record,
        "lookup_errors": n_error,
        "settled_unresolved": n_unresolved,
        "stored_pnl_mismatch": n_stored_mismatch,
        "realized_pnl_delta_usd": _r(realized_delta, 6),
        "committed_delta_usd": _r(committed_delta, 6),
        "expected_balance_delta_usd": _r(expected_delta, 6),
    }
    if capital_usd is not None:
        expected_before = capital_usd - committed_before + realized_before
        summary["expected_balance_before_usd"] = _r(expected_before, 6)
        summary["expected_balance_after_usd"] = _r(expected_before + expected_delta, 6)
        if actual_balance_usd is not None:
            summary["actual_balance_usd"] = _r(actual_balance_usd, 6)
            summary["drift_before_usd"] = _r(actual_balance_usd - expected_before, 6)
            summary["drift_after_usd"] = _r(actual_balance_usd - (expected_before + expected_delta), 6)
    return {"dry_run": True, "rows": out_rows, "summary": summary}


def format_report(report: dict) -> str:
    lines = [
        "DRY RUN -- nothing has been written. Corrections are NOT applied.",
        f"{'id':>5} {'status':<8} {'px':>6} {'stake':>7} {'cost_b':>8} {'cost_a':>8} "
        f"{'pnl_st':>9} {'pnl_a':>9} {'pnl_d':>9}  note",
    ]
    for r in report["rows"]:
        if r["cost_delta"] == 0 and (r["pnl_delta"] in (None, 0.0)) and not r["note"]:
            continue  # unchanged, no-note rows are omitted from the table
        lines.append(
            f"{r['id']:>5} {r['status']:<8} {str(r['fill_price']):>6} {str(r['stake_usd']):>7} "
            f"{str(r['cost_before']):>8} {str(r['cost_after']):>8} "
            f"{str(r['pnl_stored']):>9} {str(r['pnl_after']):>9} {str(r['pnl_delta']):>9}  "
            f"{r['note'] or ''}"
        )
    lines.append("")
    lines.append("SUMMARY")
    for k, v in report["summary"].items():
        lines.append(f"  {k}: {v}")
    if report["summary"]["lookup_errors"]:
        lines.append(
            f"WARNING: {report['summary']['lookup_errors']} fill lookup(s) failed -- those rows "
            "kept their stake_usd. This report is NOT valid evidence for any correction; "
            "re-run where CLOB credentials return order records."
        )
    return "\n".join(lines)


def _open_readonly(path: str) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: "list[str] | None" = None) -> int:
    import os

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=os.getenv("DB_PATH", "data/meteoedge.db"))
    parser.add_argument("--capital", type=float, default=None,
                        help="COPY_LIVE_CAPITAL_USD at ledger start (enables drift before/after)")
    parser.add_argument("--actual-balance", type=float, default=None,
                        help="Exchange USDC balance observed now (enables drift before/after)")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    args = parser.parse_args(argv)

    from src.execution.auth import get_clob_client  # noqa: PLC0415
    from src.data.polymarket import fetch_market_resolution  # noqa: PLC0415

    # Preflight: refuse to run without a CLOB client rather than print a
    # misleading all-zero report.
    try:
        client = get_clob_client()
    except Exception as e:  # e.g. KeyError on a missing POLYMARKET_* env var
        client = None
        reason = str(e)
    else:
        reason = "get_clob_client() returned None"
    if client is None:
        print(f"ABORTED: no CLOB client ({reason}) -- the dry-run needs fill "
              "records and will not report without them.", file=sys.stderr)
        return 2

    conn = _open_readonly(args.db)
    try:
        report = build_report(
            conn,
            get_fill_shares=lambda order_id: fill_shares_from_order(client.get_order(order_id)),
            get_resolution=fetch_market_resolution,
            capital_usd=args.capital,
            actual_balance_usd=args.actual_balance,
        )
    finally:
        conn.close()
    print(json.dumps(report, indent=2) if args.json else format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

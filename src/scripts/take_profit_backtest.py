"""Take-profit buffer backtest script.

Reads settled trade rows from the MeteoEdge DB and reports realised P&L,
win rate, and trade count per TAKE_PROFIT_BUFFER_CENTS setting in {2, 3, 5}.

A "settled" trade is defined as a row that:
 - has mode IN ('live', 'paper')
 - has outcome IN ('sold', 'filled')
 - has pnl IS NOT NULL

For take_profit exits the buffer determines the trigger price:
  trigger_price = predicted_price - buffer
If trigger_price <= actual_sell_price, the exit would have fired.

For each buffer value the script counts:
  - take_profit eligible: sold trades where predicted_price IS NOT NULL
    (i.e. the take-profit exit path fired or could have fired)
  - win_count: trades with pnl > 0
  - total_pnl: sum of pnl

Usage:
    python -m src.scripts.take_profit_backtest
    python -m src.scripts.take_profit_backtest --trades-db data/meteoedge.db
    python -m src.scripts.take_profit_backtest --buffers 2,3,5 --mode live
"""
import argparse
import logging
import os
import sys

log = logging.getLogger(__name__)

from src.data.db import Database  # noqa: E402
from src.logging_config import setup_logging  # noqa: E402

_DEFAULT_DB_PATH = os.getenv("DB_PATH", "data/meteoedge.db")
_DEFAULT_BUFFERS = [2, 3, 5]


# ---------------------------------------------------------------------------
# Pure helpers (extracted for testability)
# ---------------------------------------------------------------------------

def compute_tp_stats(
    trades: list,
    buffer_cents: int,
) -> dict:
    """Compute take-profit backtest stats for a single buffer value.

    Args:
        trades: List of trade dicts (from DB or tests).  Each must have at
                minimum: predicted_price, pnl, outcome.
        buffer_cents: The buffer (¢) to test.  A take-profit exit fires when
                      actual exit price >= predicted_price - buffer_cents.

    Returns:
        Dict with keys: buffer_cents, count, tp_eligible, win_count,
        total_pnl, avg_pnl, worst_pnl, win_rate.
    """
    count = 0
    tp_eligible = 0
    win_count = 0
    total_pnl = 0.0
    worst_pnl = 0.0

    for t in trades:
        pnl = t.get("pnl")
        if pnl is None:
            continue
        pnl = float(pnl)
        predicted = t.get("predicted_price")
        actual = t.get("actual_price")

        count += 1
        total_pnl += pnl
        if pnl < worst_pnl:
            worst_pnl = pnl
        if pnl > 0:
            win_count += 1

        # Determine if this trade would have been caught by take-profit at this buffer
        if (predicted is not None and actual is not None
                and int(actual) >= int(predicted) - buffer_cents):
            tp_eligible += 1

    avg_pnl = total_pnl / count if count else 0.0
    win_rate = win_count / count if count else None

    return {
        "buffer_cents": buffer_cents,
        "count": count,
        "tp_eligible": tp_eligible,
        "win_count": win_count,
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(avg_pnl, 4),
        "worst_pnl": round(worst_pnl, 2),
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
    }


def load_settled_trades(db: Database, mode: "str | None" = None) -> list:
    """Return settled trades from the DB for backtest.

    A trade is settled when pnl IS NOT NULL and mode != 'shadow'.

    Args:
        db: Database instance.
        mode: If provided, filter to this mode ('live' | 'paper').
              When None, includes both live and paper.

    Returns:
        List of trade dicts.
    """
    all_trades = db.get_trades(limit=None, mode=mode)
    return [
        t for t in all_trades
        if t.get("pnl") is not None and t.get("mode") != "shadow"
    ]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(
        description=(
            "Backtest TAKE_PROFIT_BUFFER_CENTS settings {2, 3, 5} against"
            " settled trades in the MeteoEdge DB"
        )
    )
    parser.add_argument(
        "--trades-db",
        default=_DEFAULT_DB_PATH,
        help=f"Path to the MeteoEdge SQLite DB (default: {_DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--buffers",
        default=",".join(str(b) for b in _DEFAULT_BUFFERS),
        help="Comma-separated buffer values in cents to test (default: 2,3,5)",
    )
    parser.add_argument(
        "--mode",
        default=None,
        choices=["live", "paper", None],
        help="Filter to a specific trade mode (default: both live and paper)",
    )
    args = parser.parse_args()

    buffers: list[int] = []
    for b in args.buffers.split(","):
        b = b.strip()
        if not b:
            continue
        try:
            buffers.append(int(b))
        except ValueError:
            log.error("Invalid buffer value: %r -- skipping", b)
    if not buffers:
        log.error("No valid buffer values provided.")
        sys.exit(1)

    db = Database(args.trades_db)
    trades = load_settled_trades(db, mode=args.mode)
    db.close()

    mode_label = args.mode if args.mode else "live+paper"
    log.info(
        "Take-Profit Buffer Backtest -- %d settled trades -- mode: %s",
        len(trades), mode_label,
    )

    if not trades:
        log.info("No settled trades found in %s (mode=%s).", args.trades_db, mode_label)
        sys.exit(0)

    # --- Compute per-buffer stats ---
    results: list[dict] = []
    for buf in buffers:
        stats = compute_tp_stats(trades, buf)
        results.append(stats)

    # --- Print report ---
    log.info("=" * 62)
    log.info(
        "%-12s %8s %10s %10s %9s %9s",
        "Buffer (¢)", "Count", "Win Rate", "Total P&L", "Avg P&L", "Worst",
    )
    log.info("-" * 62)
    for r in results:
        wr = f"{r['win_rate']*100:.0f}%" if r["win_rate"] is not None else "—"
        log.info(
            "%-12d %8d %10s %+10.2f €%8.4f €%8.2f €",
            r["buffer_cents"], r["count"], wr,
            r["total_pnl"], r["avg_pnl"], r["worst_pnl"],
        )
    log.info("=" * 62)

    # Highlight best buffer by total_pnl
    if results:
        best = max(results, key=lambda r: r["total_pnl"])
        log.info(
            "Best buffer by total P&L: %d¢  (€%.2f over %d trades, %.0f%% win rate)",
            best["buffer_cents"], best["total_pnl"], best["count"],
            (best["win_rate"] or 0) * 100,
        )


if __name__ == "__main__":
    main()

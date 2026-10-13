"""Operator-only: abandon ONE stuck copy-trading paper position (issue #1301).

For a genuinely-dead market (trading ended at an ambiguous price, UMA
resolution stalled/disputed past hope). This is the ONLY path into the
``abandoned`` status. It is never imported by, scheduled with, or reachable
from ``copy_settle.py`` or any timer/heuristic/price path -- a human runs it
per position, and it records who, when and why.

An abandoned position is excluded from realized P&L, ROI and win-rate (no
``settled_pnl_usd`` is written) and no longer counts toward open exposure.
``--observed-price`` is recorded for audit only and never used in any P&L.

Usage::

    python -m src.scripts.copy_abandon --position-id 404 \\
        --operator andre --reason "UMA dispute stalled, Gamma price 0.55 closed"
        [--observed-price 0.55]

For a verified 50/50 void (closed, UMA resolved, both tokens 0.5) use
``--settle-at-price 0.5`` instead of abandoning: the position settles with
``stake * (0.5/entry - 1)`` so the loss stays in ROI. Use abandon only when
there is no trustworthy payout price. Both are idempotent and operator-only.

Idempotent: a second call on the same position changes nothing and exits 1.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def abandon(db, position_id: int, operator: str, reason: str,
            observed_price: "float | None" = None) -> bool:
    """Abandon one open position. Returns True if this call did it."""
    if observed_price is not None and not (0.0 <= observed_price <= 1.0):
        raise ValueError("observed_price must be within [0, 1]")
    return db.abandon_copy_position(
        position_id, operator, reason,
        datetime.now(timezone.utc).isoformat(), observed_price,
    )


def settle_at_price(db, position_id: int, operator: str, reason: str,
                    yes_price: float) -> bool:
    """Settle one open position at an operator-observed final YES price."""
    return db.settle_copy_position_at_price(
        position_id, operator, reason, yes_price,
        datetime.now(timezone.utc).isoformat(),
    )


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--position-id", type=int, required=True)
    ap.add_argument("--operator", required=True)
    ap.add_argument("--reason", required=True)
    ap.add_argument("--observed-price", type=float, default=None)
    ap.add_argument("--settle-at-price", type=float, default=None,
                    help="settle at this final YES price (e.g. 0.5) instead of abandoning")
    args = ap.parse_args(argv)
    from src.data.db import Database
    if args.settle_at_price is not None:
        done = settle_at_price(Database(), args.position_id, args.operator,
                               args.reason, args.settle_at_price)
        print(f"position {args.position_id}: "
              + ("settled" if done else "NOT changed (not open / unknown id)"))
        return 0 if done else 1
    done = abandon(Database(), args.position_id, args.operator, args.reason,
                   args.observed_price)
    print(f"position {args.position_id}: "
          + ("abandoned" if done else "NOT changed (not open / unknown id)"))
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())

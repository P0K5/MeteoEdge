"""Data-fix script for stuck trades from 2026-06-07.

Five trades have outcome IS NULL and no matching open_positions row.
This script attempts to settle them based on settlements or price data,
or flags them for manual review if outcome cannot be determined automatically.

Idempotent: runs safely multiple times without side effects.
"""
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def _open_db():
    """Return a Database handle, or None if unavailable."""
    try:
        from src.data.db import Database
        return Database()
    except Exception as e:
        log.error("[fix_stuck_trades] DB unavailable: %s", e)
        return None


def fix_stuck_trades():
    """Settle the five stuck trades from 2026-06-07 by attempting automatic resolution."""
    db = _open_db()
    if db is None:
        log.error("[fix_stuck_trades] Cannot open database. Aborting.")
        return

    # Query stuck trades: outcome IS NULL, mode='live', from 2026-06-07, no open_position
    stuck = db._conn.execute(
        """
        SELECT t.id, t.ts, t.station, t.ticker, t.side, t.bracket_low, t.bracket_high,
               t.actual_price, t.capital_before
        FROM trades t
        WHERE t.mode = 'live'
          AND t.outcome IS NULL
          AND DATE(t.ts) = '2026-06-07'
          AND NOT EXISTS (SELECT 1 FROM open_positions p WHERE p.trade_id = t.id)
        ORDER BY t.ts ASC
        """
    ).fetchall()

    if not stuck:
        log.info("[fix_stuck_trades] No stuck trades found for 2026-06-07")
        return

    log.info("[fix_stuck_trades] Found %d stuck trade(s)", len(stuck))
    updated = []
    manual_review = []

    for trade in stuck:
        trade_id = trade['id']
        ticker = trade['ticker']
        station = trade['station']
        side = trade['side']
        bracket_low = float(trade['bracket_low'])
        bracket_high = float(trade['bracket_high'])

        log.debug("[fix_stuck_trades] Processing trade id=%s, ticker=%s, station=%s",
                  trade_id, ticker, station)

        # Try to determine outcome from settlements table
        outcome = None
        pnl = None

        settlement = db._conn.execute(
            "SELECT actual_high_f, resolved_yes FROM settlements WHERE ticker=?",
            (ticker,)
        ).fetchone()

        if settlement:
            actual_high = float(settlement['actual_high_f'])
            resolved_yes = bool(settlement['resolved_yes'])

            # Determine if the trade won
            if side == "YES":
                won = resolved_yes  # YES wins if resolved_yes=1
            else:  # side == "NO"
                won = not resolved_yes  # NO wins if resolved_yes=0

            # Calculate P&L
            actual_price = float(trade['actual_price'])
            if won:
                pnl = (100 - actual_price) / 100
                outcome = "filled"
            else:
                pnl = -actual_price / 100
                outcome = "filled"

            log.info(
                "[fix_stuck_trades] id=%s ticker=%s side=%s actual_high=%.1f "
                "resolved_yes=%s won=%s pnl=%.4f",
                trade_id, ticker, side, actual_high, resolved_yes, won, pnl
            )
        else:
            # No settlement found; flag for manual review
            log.warning(
                "[fix_stuck_trades] id=%s ticker=%s: no settlement found — requires manual review",
                trade_id, ticker
            )
            manual_review.append({
                'trade_id': trade_id,
                'ticker': ticker,
                'station': station,
                'side': side,
                'reason': 'no_settlement_found',
            })
            continue

        # Update the trade (only if outcome was determined)
        if outcome and pnl is not None:
            try:
                settled_at = datetime.now(timezone.utc).isoformat()
                capital_after = float(trade['capital_before']) + pnl
                db.update_trade_by_id(
                    trade_id,
                    outcome=outcome,
                    pnl=round(pnl, 6),
                    capital_after=round(capital_after, 6),
                    settled_at=settled_at,
                )
                updated.append({
                    'trade_id': trade_id,
                    'ticker': ticker,
                    'outcome': outcome,
                    'pnl': pnl,
                })
                log.info("[fix_stuck_trades] Updated id=%s with outcome=%s pnl=%.4f",
                         trade_id, outcome, pnl)
            except Exception as e:
                log.error("[fix_stuck_trades] Failed to update id=%s: %s", trade_id, e)
                manual_review.append({
                    'trade_id': trade_id,
                    'ticker': ticker,
                    'station': station,
                    'side': side,
                    'reason': 'update_failed',
                    'error': str(e),
                })

    # Summary
    log.info("[fix_stuck_trades] ===== SUMMARY =====")
    log.info("[fix_stuck_trades] Updated: %d trade(s)", len(updated))
    for u in updated:
        log.info("[fix_stuck_trades]   id=%s ticker=%s outcome=%s pnl=%.4f",
                 u['trade_id'], u['ticker'], u['outcome'], u['pnl'])

    if manual_review:
        log.warning("[fix_stuck_trades] Requires manual review: %d trade(s)", len(manual_review))
        for m in manual_review:
            log.warning("[fix_stuck_trades]   id=%s ticker=%s reason=%s",
                        m['trade_id'], m['ticker'], m.get('reason', 'unknown'))


if __name__ == "__main__":
    import sys
    from src.logging_config import setup_logging
    setup_logging()
    log.info("[fix_stuck_trades] Starting settlement of 2026-06-07 stuck trades")
    fix_stuck_trades()
    log.info("[fix_stuck_trades] Complete")

"""One-shot settlement job for copy-trading positions (epic #1102 story C2,
issue #1132).

Structured like ``src/scripts/settle.py``'s one-shot shape (fetch unsettled
-> resolve -> compute -> persist -> log summary) -- **not** a persistent
loop like ``copy_signal_loop.py`` (that's story B3's job). This runs once,
does a single pass over ``copy_positions``, and exits. Scheduled hourly by
``deploy/systemd/meteoedge-copy-settle.timer`` -- see docs/OPERATIONS.md
for the "why hourly" rationale.

For every ``status='open'`` row from ``db.get_open_copy_positions()``:

1. Resolve its market via ``fetch_market_resolution()``
   (``src/data/polymarket.py``) -- the **only** truth source for a
   copy-trading position. Polymarket condition IDs are the only truth
   these markets have; METAR or any other weather-derived truth is never
   substituted here (unlike the weather strategy's own settlement, which
   does fall back to METAR for legacy synthetic tickers). Open positions
   are grouped by distinct ``market`` first, so a market with several open
   positions spends exactly one ``fetch_market_resolution()`` call, not one
   per position -- see the architecture doc's "Isolation" section on the
   shared ``data-api``/``gamma-api`` rate-limit budget.
2. ``None`` (not resolved yet) -- the row stays ``open`` and is retried on
   the next run.
3. Resolved -- compute this position's realized P&L via
   ``src.data.copy_pnl.compute_realized_pnl_usd`` and persist it via
   ``Database.settle_copy_position``. A single bad row (e.g. a raised
   exception) is caught and logged, never aborting the run -- mirrors
   ``settle.py``'s ``settle_live_trades``/``settle_shadow_trades`` per-row
   try/except.

Usage::

    python -m src.scripts.copy_settle
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.data.copy_pnl import compute_realized_pnl_usd  # noqa: E402
from src.data.copy_stuck import LEVEL_LABELS, PAST_END_TRADING, STUCK_HOURS, age_hours, classify_market  # noqa: E402
from src.data.polymarket import fetch_market_resolution, fetch_market_state, market_seen_closed  # noqa: E402

log = logging.getLogger(__name__)

_STATE_ERROR = object()


def _open_db():
    """Return a Database handle, or None if the DB cannot be opened."""
    try:
        from src.data.db import Database
        return Database()
    except Exception as e:
        log.warning("[copy_settle] DB unavailable: %s -- skipping settlement", e)
        return None


def run_once(db=None, escalations: "list | None" = None) -> dict:
    """Run a single settlement pass over open ``copy_positions``.

    Returns a summary dict ``{'settled': int, 'pending': int, 'errors': int}``
    -- counts, not rows, so callers (and the systemd log) get a cheap summary
    without holding settled data in memory.

    If *db* is not given, opens (and owns -- callers never need to close it
    themselves) a real ``Database()`` handle. Passing *db* explicitly is how
    tests inject a seeded / mocked database.

    If *escalations* is a list, each newly-emitted stuck-position record
    (issue #1301) is appended to it. Escalation is once per position per
    level (48h, 7d from endDate, or immediately for a verified ambiguous close): the level is persisted in
    ``copy_positions.stuck_level``, so repeated runs over the same stuck
    position emit nothing new. Chosen over "newly stuck + stable count"
    because the per-position record is what an operator can act on, and the
    daily health report carries the full stable listing. Escalation is
    bookkeeping only: nothing here can move a position out of ``open``.
    """
    if db is None:
        db = _open_db()
    if db is None:
        return {"settled": 0, "pending": 0, "errors": 0}

    rows = db.get_open_copy_positions()
    if not rows:
        log.info("[copy_settle] no open copy positions to settle")
        return {"settled": 0, "pending": 0, "errors": 0}

    # Group by distinct market first so a market with multiple open
    # positions spends one fetch_market_resolution() call, not one per
    # position (shared data-api/gamma-api rate-limit budget).
    markets = sorted({r["market"] for r in rows})
    resolutions: dict = {}
    for market in markets:
        resolutions[market] = fetch_market_resolution(market)

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    n_settled = 0
    n_pending = 0
    n_errors = 0
    states: dict = {}
    n_stale = 0  # escalated positions
    for r in rows:
        market = r["market"]
        yes_won = resolutions.get(market)
        if yes_won is None:
            n_pending += 1
            try:
                # #1387: a state lookup is "due" once the position is >48h old OR
                # the resolution poll we just made already saw closed=true for
                # this market (free signal, no extra call). That lets a verified
                # 50/50 void (closed + UMA resolved + ambiguous price) be reported
                # at once instead of after 48h. classify_market still decides.
                if age_hours(r["entry_ts"], now) > STUCK_HOURS or market_seen_closed(market):
                    if market not in states:
                        # One state lookup per distinct market (batching contract).
                        try:
                            states[market] = fetch_market_state(market)
                        except RuntimeError as e:
                            log.warning("[copy_settle] market state unavailable for %s...: %s -- not escalating this run", str(market)[:14], e)
                            states[market] = _STATE_ERROR
                    st = states[market]
                    if st is not _STATE_ERROR:
                        category, lvl = classify_market(st, r["entry_ts"], now)
                        if lvl > 0:
                            n_stale += 1
                        # #1388: de-escalate / tag a past-endDate market that is
                        # still trading (level resets to 0; if it stops trading
                        # the 48h/7d steps alert again). Persist category only
                        # when it changes.
                        want_cat = PAST_END_TRADING if category == PAST_END_TRADING else None
                        cur_cat = r.get("stuck_category")
                        if lvl == 0 and (
                            (want_cat and (cur_cat != want_cat or int(r.get("stuck_level") or 0) > 0))
                            or (want_cat is None and cur_cat == PAST_END_TRADING)
                        ):
                            db.reset_copy_position_stuck(r["id"], want_cat, now_iso if want_cat else None)
                        if lvl > int(r.get("stuck_level") or 0):
                            rec = {
                                "position_id": r["id"],
                                "wallet": r.get("address"),
                                "market": market,
                                "age_hours": round(age_hours(r["entry_ts"], now), 1),
                                "entry_price": r["entry_price"],
                                "stake_usd": r["stake_usd"],
                                "level": lvl,
                                "step": LEVEL_LABELS[lvl],
                                "category": category,
                            }
                            db.set_copy_position_stuck_level(r["id"], lvl, now_iso, category)
                            if escalations is not None:
                                escalations.append(rec)
                            log.warning(
                                "[copy_settle] STUCK %s/%s position=%s wallet=%s market=%s "
                                "age_h=%s entry_price=%s stake_usd=%s -- unresolved, stays open; "
                                "operator action: copy_abandon.py (abandon or --settle-at-price)",
                                category, rec["step"], rec["position_id"], rec["wallet"],
                                rec["market"], rec["age_hours"], rec["entry_price"],
                                rec["stake_usd"], extra={"stuck_record": rec},
                            )
            except (ValueError, KeyError) as e:
                log.debug("[copy_settle] position %s has malformed entry_ts, skipping stale tracking: %s", r.get("id"), e)
            except Exception as e:  # noqa: BLE001 -- a DB hiccup must not abort the run
                log.warning("[copy_settle] stuck tracking skipped for position %s: %s", r.get("id"), e)
            log.debug(
                "[copy_settle] market %s... not resolved yet -- position %s stays open",
                str(market)[:14], r["id"],
            )
            continue

        try:
            pnl = compute_realized_pnl_usd(
                entry_price=float(r["entry_price"]),
                stake_usd=float(r["stake_usd"]),
                outcome_index=int(r["outcome_index"]),
                yes_won=yes_won,
            )
            db.settle_copy_position(r["id"], round(pnl, 6), now_iso)
            n_settled += 1
            log.debug(
                "[copy_settle] settled position %s address=%s market=%s... "
                "outcome_index=%s yes_won=%s pnl=%.4f",
                r["id"], r.get("address"), str(market)[:14], r["outcome_index"],
                yes_won, pnl,
            )
        except Exception as e:
            n_errors += 1
            log.warning("[copy_settle] settlement failed for position %s: %s", r["id"], e)

    log.info(
        "[copy_settle] run complete: settled=%s pending=%s errors=%s (of %s open position(s))",
        n_settled, n_pending, n_errors, len(rows),
    )

    # The per-position STUCK records above replace the old identical
    # aggregate WARNING (issue #1301). Only a stable count at INFO remains.
    if n_stale > 0:
        log.info("[copy_settle] %s open position(s) in an escalated stuck category", n_stale)

    return {"settled": n_settled, "pending": n_pending, "errors": n_errors}


def main() -> None:
    from src.logging_config import setup_logging
    setup_logging()
    run_once()


if __name__ == "__main__":
    main()

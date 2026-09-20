"""Copy-trading signal-detection and flat-stake paper-execution loop
(epic #1101 story B3, issue #1123).

Persistent process, structured like ``src/scripts/run.py``'s own poll
loop (sleep-between-cycles, graceful Ctrl-C shutdown, per-cycle exception
isolation so one bad cycle never kills the process) -- **not** a systemd
oneshot (that's story #B4's job, see ``deploy/systemd``).

**Paper mode only.** No path to ``src/execution/live_trader.py`` anywhere
in this module. Does **not** call
``src.paper_trader.PaperTrader.execute_trade()`` -- that writes to the
shared ``trades`` table (forbidden by the #1100 isolation decision --
copy-trading never touches the weather-strategy tables) and settles
win/loss *synchronously* from a known outcome, which a freshly detected
copy-trading signal doesn't have (the market resolves later). This module
only ever creates an **open, unsettled** ``copy_positions`` row; settling
it (computing ``settled_pnl_usd``) is epic C's job (#1102), not this one.

Per cycle, for each ``active``-status followed wallet
(``db.get_followed_wallets(status="active")``):

1. Fetch trades newer than the wallet's ``last_seen_trade_ts`` via
   ``get_wallet_trades_since()`` (see
   ``src/data/polymarket_traders.py``'s docstring for the pagination-order
   research finding this relies on -- the live API returns newest-first
   by default, not oldest-first as previously (incorrectly) documented).
2. For each new **BUY** trade (SELLs are never copied), decide
   execute-or-skip: market already resolved, wallet exposure limit, total
   exposure limit, else execute at a slippage-adjusted fill price sized
   at the wallet's flat ``stake_per_trade``.
3. Insert a ``copy_signals`` row for every detected BUY, executed or
   skipped, always with a specific ``skip_reason`` when not executed.
4. Advance the wallet's ``last_seen_trade_ts`` past whatever was
   processed this cycle, so already-seen trades are never re-signaled.

**Kill switch, checked every cycle, not just at startup.**
``COPY_TRADING_ENABLED`` is live-read via ``get_live_config(db)`` at the
top of every cycle -- an operator can flip it off mid-run and have it
take effect on the very next cycle, not just at process start.

**Startup sanity check, fail loud.** If
``COPY_MAX_TOTAL_EXPOSURE_USD > COPY_TRADING_CAPITAL_USD`` (both from
``src/config.py``), the process refuses to start. These two values are
inconsistent by default (100 vs. 250) -- harmless today since
``COPY_TRADING_ENABLED`` defaults ``False``, but a real footgun once an
operator turns it on without noticing: it would let the total-exposure
cap approve more open paper positions than the configured capital pool
can actually cover.

Usage::

    python -m src.scripts.copy_signal_loop            # persistent loop
    python -m src.scripts.copy_signal_loop --once      # single cycle then exit
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import (  # noqa: E402
    CONFIG_DEFAULTS, COPY_TRADING_CAPITAL_USD, get_live_config, seed_config,
)
from src.data.db import Database  # noqa: E402
from src.data.polymarket import fetch_market_resolution  # noqa: E402
from src.data.polymarket_traders import get_wallet_trades_since, normalize_trade  # noqa: E402
from src.scripts.copy_trade_backtest import DEFAULT_SLIPPAGE_BPS, apply_slippage  # noqa: E402

log = logging.getLogger(__name__)


def startup_sanity_check(live_config: dict) -> "str | None":
    """Return an error message if it is unsafe to start, else ``None``.

    ``COPY_MAX_TOTAL_EXPOSURE_USD`` (live-editable) must never exceed
    ``COPY_TRADING_CAPITAL_USD`` (a fixed module constant, not
    live-editable -- see its own comment in src/config.py): otherwise the
    total-exposure gate would keep approving new paper positions past the
    point the configured capital pool could actually fund.
    """
    max_total = live_config["COPY_MAX_TOTAL_EXPOSURE_USD"]
    if max_total > COPY_TRADING_CAPITAL_USD:
        return (
            f"COPY_MAX_TOTAL_EXPOSURE_USD (${max_total:.2f}) exceeds "
            f"COPY_TRADING_CAPITAL_USD (${COPY_TRADING_CAPITAL_USD:.2f}) -- "
            "refusing to start. Fix the config values (dashboard config "
            "tab or env vars) before restarting."
        )
    return None


def _raw_trade_timestamp(raw: dict) -> "int | None":
    try:
        return int(raw.get("timestamp"))
    except (TypeError, ValueError):
        return None


def _handle_buy_trade(
    db, wallet: dict, trade: dict, stake: float, live_config: dict, now_iso: str,
) -> None:
    """Decide execute-or-skip for one normalized BUY trade and persist the
    outcome (always a copy_signals row; a copy_positions row too if
    executed).

    **Atomicity (AI review #1128, BLOCK item 1):** the exposure reads
    (``get_open_copy_positions``) and the resulting insert(s) are wrapped
    in ``db._lock`` -- the same ``threading.RLock`` every ``Database``
    mutator already acquires (see ``src/data/db.py``). Without this, two
    signals evaluated close together (a future concurrent refactor of
    this loop, or two processes sharing one ``Database`` instance) could
    both read the exposure total *before* either insert commits and both
    pass the same limit, jointly exceeding
    ``COPY_MAX_EXPOSURE_PER_WALLET_USD`` / ``COPY_MAX_TOTAL_EXPOSURE_USD``
    -- a trading-safety guardrail, not just a data-quality one. The
    current loop is single-threaded and processes trades strictly
    sequentially, so this isn't reachable today, but the fix is cheap and
    closes the gap for any future caller. ``db._lock`` is reentrant, so
    the nested acquisitions inside ``insert_copy_signal`` /
    ``insert_copy_position`` / ``link_copy_signal_to_position`` (each of
    which takes the same lock internally) are safe.
    """
    address = wallet["address"]
    market = trade["market"]
    outcome_index = trade.get("outcome_index")
    source_price = trade["price"]
    source_trade_id = trade.get("source_trade_id")

    with db._lock:
        # Crash-recovery guard: if a prior cycle inserted a signal for
        # this exact fill but crashed before advancing
        # last_seen_trade_ts, the next cycle would otherwise re-fetch and
        # re-signal (and re-open a position for) the same trade.
        if db.copy_signal_exists_for_trade(
            address=address, market=market, source_price=source_price,
            source_trade_id=source_trade_id,
        ):
            return

        skip_reason = None
        if fetch_market_resolution(market) is not None:
            skip_reason = "market_resolved"
        elif outcome_index is None:
            # Data-integrity guard, not part of the acceptance criteria's
            # 3-way skip ladder: copy_positions.outcome_index is NOT
            # NULL, so a trade normalize_trade() couldn't confidently
            # assign a positional side to can never be executed -- only
            # signal-logged.
            skip_reason = "missing_outcome_index"
        else:
            wallet_exposure = sum(p["stake_usd"] for p in db.get_open_copy_positions(address))
            if wallet_exposure + stake > live_config["COPY_MAX_EXPOSURE_PER_WALLET_USD"]:
                skip_reason = "wallet_exposure_limit"
            else:
                total_exposure = sum(p["stake_usd"] for p in db.get_open_copy_positions())
                if total_exposure + stake > live_config["COPY_MAX_TOTAL_EXPOSURE_USD"]:
                    skip_reason = "total_exposure_limit"

        if skip_reason is not None:
            db.insert_copy_signal(
                address=address, market=market, outcome_index=outcome_index,
                source_price=source_price, source_trade_id=source_trade_id,
                detected_at=now_iso, order_placed=0, skip_reason=skip_reason,
            )
            return

        fill_price = apply_slippage(source_price, "BUY", DEFAULT_SLIPPAGE_BPS)
        signal_id = db.insert_copy_signal(
            address=address, market=market, outcome_index=outcome_index,
            source_price=source_price, source_trade_id=source_trade_id,
            detected_at=now_iso, order_placed=1, fill_price=fill_price, size_usd=stake,
        )
        position_id = db.insert_copy_position(
            signal_id=signal_id, address=address, market=market,
            outcome_index=outcome_index, entry_price=fill_price,
            stake_usd=stake, entry_ts=now_iso,
        )
        db.link_copy_signal_to_position(signal_id, position_id)


def _process_wallet(db, wallet: dict, live_config: dict, now_iso: str) -> None:
    """Fetch, detect, and act on one followed wallet's new trades, then
    advance its high-water mark past whatever was actually processed.

    **Fail-safe watermark advancement (AI review #1128, BLOCK item 2):**
    ``last_seen_trade_ts`` is advanced incrementally, trade by trade, as
    each one finishes processing -- never in one shot after the whole
    batch. If ``_handle_buy_trade`` raises partway through a batch (a
    transient DB/network error, not the fail-soft cases that are already
    handled internally), the exception propagates out of this function
    (caught by ``run_cycle``'s per-wallet handler, matching the
    acceptance criteria's per-wallet isolation), but the watermark has
    already been persisted up to the last trade that *did* finish --
    never past the one that failed. The next cycle retries from exactly
    that point: the already-processed trades are skipped by
    ``since_ts``, and any trade that got far enough to have a
    ``copy_signals`` row before failing is additionally caught by
    ``copy_signal_exists_for_trade``'s crash-recovery guard.
    """
    address = wallet["address"]
    since_ts = wallet.get("last_seen_trade_ts") or 0
    stake = wallet["stake_per_trade"]

    raw_trades = get_wallet_trades_since(address, since_ts)
    if not raw_trades:
        return

    # get_wallet_trades_since() returns server order (newest-first);
    # process oldest-to-newest so signals land in the order they actually
    # happened.
    dated = [(r, _raw_trade_timestamp(r)) for r in raw_trades]
    dated = [(r, ts) for r, ts in dated if ts is not None]
    dated.sort(key=lambda pair: pair[1])
    if not dated:
        return

    max_ts_seen = since_ts
    for raw, ts in dated:
        trade = normalize_trade(raw)
        if trade is not None and trade["side"] == "BUY":
            _handle_buy_trade(db, wallet, trade, stake, live_config, now_iso)

        # Only reached once this trade (BUY, SELL, or unnormalizable) has
        # fully finished processing without raising -- see the fail-safe
        # note above.
        max_ts_seen = ts
        db.update_followed_wallet_last_seen(address, int(max_ts_seen))


def run_cycle(db) -> None:
    """One poll cycle: kill switch, then per-wallet fetch/detect/execute,
    with per-wallet failure isolation."""
    live_config = get_live_config(db)
    if not live_config["COPY_TRADING_ENABLED"]:
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    for wallet in db.get_followed_wallets(status="active"):
        try:
            _process_wallet(db, wallet, live_config, now_iso)
        except Exception as exc:
            log.warning(
                "[copy-signal] %s...: cycle processing failed, skipping to "
                "next wallet: %s",
                str(wallet.get("address", "?"))[:10], exc,
            )


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--once", action="store_true", default=False,
        help="Run a single cycle then exit",
    )
    args = ap.parse_args(argv)

    db = Database()
    seed_config(db)

    live_config = get_live_config(db)
    error = startup_sanity_check(live_config)
    if error:
        log.error("[copy-signal] %s", error)
        return 1

    if args.once:
        run_cycle(db)
        log.info("[copy-signal] --once mode: exiting after single cycle.")
        return 0

    log.info("[copy-signal] Starting copy-trading signal loop. Press Ctrl-C to stop.")
    while True:
        try:
            run_cycle(db)
        except KeyboardInterrupt:
            log.info("[copy-signal] Stopping.")
            break
        except Exception as e:
            log.error("[copy-signal] Unhandled error in cycle: %s", e, exc_info=True)
        interval = get_live_config(db).get(
            "COPY_SIGNAL_POLL_INTERVAL_SECONDS",
            CONFIG_DEFAULTS["COPY_SIGNAL_POLL_INTERVAL_SECONDS"],
        )
        time.sleep(interval)
    return 0


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    raise SystemExit(main())

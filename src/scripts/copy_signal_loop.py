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
   execute-or-skip: wallet newly followed (first-ever poll -- see below),
   market already resolved, wallet exposure limit, total exposure limit,
   else execute at a slippage-adjusted fill price sized at the wallet's
   flat ``stake_per_trade``.
3. Insert a ``copy_signals`` row for every detected BUY, executed or
   skipped, always with a specific ``skip_reason`` when not executed.
4. Advance the wallet's ``last_seen_trade_ts`` past whatever was
   processed this cycle, so already-seen trades are never re-signaled.

**Kill switch, checked every cycle, not just at startup.**
``COPY_TRADING_ENABLED`` is live-read via ``get_live_config(db)`` at the
top of every cycle -- an operator can flip it off mid-run and have it
take effect on the very next cycle, not just at process start.

**Realized-P&L circuit breaker (issue #1139), checked once per cycle,
right after the kill switch.** ``src.risk.copy_risk_manager.allow_copy_signal``
gates *new* signal execution once realized copy-trading P&L breaches a
configured daily-loss limit or drawdown-from-capital threshold (both
DB-derived, not in-memory -- survives this loop's own restarts). Unlike
the kill switch, a tripped breaker does not skip polling: every detected
BUY this cycle still gets a ``copy_signals`` row, with
``skip_reason="circuit_breaker_daily_loss"`` or
``"circuit_breaker_drawdown"`` -- the activity feed shows *why* nothing
executed. Never touches settlement (``copy_settle.py``) -- see
``src/risk/copy_risk_manager.py``'s module docstring for the full
rationale, including why it does not import ``src/risk/manager.py``
(the *weather* strategy's risk manager, isolated per issue #1100).

**Bootstrap guard for a newly-followed wallet's first-ever poll.** A
wallet's ``last_seen_trade_ts`` is ``NULL`` until its first poll
completes -- with no reference point, "new since last check" would
otherwise mean its *entire* fetched trade history, including BUYs on
markets that are still open today, priced at whatever the trade cost
months or years ago rather than a currently-tradable price. Every BUY in
that first-poll backlog still gets a ``copy_signals`` row (never silently
dropped), but is always skipped with ``skip_reason="wallet_newly_followed"``
-- regardless of what the market-resolution or exposure checks would
otherwise have said -- and never even spends a ``fetch_market_resolution()``
call on it. The watermark still advances normally, so the *second* poll
onward only ever sees genuinely new activity (decided in PR #1128 review,
2026-09-20).

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
from src.risk.copy_risk_manager import allow_copy_signal  # noqa: E402
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
    first_poll: bool = False, breaker_reason: "str | None" = None,
) -> None:
    """Decide execute-or-skip for one normalized BUY trade and persist the
    outcome (always a copy_signals row; a copy_positions row too if
    executed).

    **Circuit breaker short-circuit (issue #1139).** When *breaker_reason*
    is not ``None`` (the realized-P&L circuit breaker -- daily-loss or
    drawdown -- tripped this cycle, decided once in ``run_cycle`` via
    ``allow_copy_signal``, the same global answer for every signal this
    cycle), this trade is unconditionally skipped with
    ``skip_reason=breaker_reason`` before any other check -- market
    resolution, exposure limits, even the ``first_poll`` bootstrap guard
    below never run, mirroring the bootstrap guard's own "no point
    spending a fetch_market_resolution() call on a trade that will be
    skipped unconditionally regardless of its answer" rationale. The
    circuit breaker never touches settlement -- it only ever changes
    what happens here, in the execute-or-skip decision for a brand new
    signal.

    **Bootstrap guard (PR #1128 review, 2026-09-20):** when *first_poll*
    is ``True`` (the wallet's ``last_seen_trade_ts`` was ``NULL`` entering
    this cycle), the trade is unconditionally skipped with
    ``skip_reason="wallet_newly_followed"`` -- market-resolution and
    exposure checks are never even evaluated, and
    ``fetch_market_resolution()`` is not called at all (saving the
    network call on a potentially large backlog). See the module
    docstring's "Bootstrap guard" section for the full rationale.

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

    **No network I/O under the lock (AI review #1128 round 2, BLOCK item
    2):** ``fetch_market_resolution()`` is a blocking HTTP call (up to
    ``HTTP_TIMEOUT_SECONDS``). It runs *before* ``db._lock`` is acquired
    -- ``run.py``'s dashboard shares one ``Database`` instance across the
    poll thread and the FastAPI request-handling threads
    (``_dashboard_module.set_db(db)``), so holding the lock across a
    slow/hanging resolution call would stall every other DB operation in
    the process (including unrelated dashboard reads) for the duration
    of the timeout. Only the DB-only decision logic (dedup check,
    exposure reads, inserts) needs the lock for atomicity.
    """
    address = wallet["address"]
    market = trade["market"]
    outcome_index = trade.get("outcome_index")
    source_price = trade["price"]
    source_trade_id = trade.get("source_trade_id")

    # Breaker and bootstrap guards short-circuit before the network call --
    # no point spending a fetch_market_resolution() request on a trade
    # that will be skipped unconditionally regardless of its answer.
    market_resolved = (
        None if (breaker_reason is not None or first_poll)
        else fetch_market_resolution(market) is not None
    )

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
        if breaker_reason is not None:
            skip_reason = breaker_reason
        elif first_poll:
            skip_reason = "wallet_newly_followed"
        elif market_resolved:
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


def _process_wallet(
    db, wallet: dict, live_config: dict, now_iso: str,
    breaker_reason: "str | None" = None,
) -> None:
    """Fetch, detect, and act on one followed wallet's new trades, then
    advance its high-water mark past whatever was actually processed.

    *breaker_reason* (issue #1139) is threaded straight through to every
    ``_handle_buy_trade`` call this cycle -- decided once in ``run_cycle``,
    it is the same global answer for every wallet and every signal in
    this cycle, so it is never re-derived per wallet or per trade here.

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

    **Unnormalizable trades ARE deliberately watermark-advanced past,
    permanently (AI review #1128 round 2, BLOCK item 1).** A trade record
    is on-chain history -- immutable once fetched -- so a record
    ``normalize_trade()`` can't parse today will still fail to parse on
    an identical re-fetch tomorrow; retrying gains nothing and would
    otherwise wedge this wallet on the same bad record forever. This
    mirrors ``normalize_trade()``'s own documented contract ("an
    unnormalizable record is dropped, never guessed into a value"). The
    risk this trades away is a genuine *software* bug (e.g. an API
    schema change ``normalize_trade()`` doesn't yet handle) permanently
    losing a trade that a later code fix could have parsed -- mitigated,
    not eliminated, by the ``log.warning`` below giving an operator
    visibility to notice and backfill out-of-band if it ever happens at
    volume.
    """
    address = wallet["address"]
    first_poll = wallet.get("last_seen_trade_ts") is None
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
        if trade is None:
            log.warning(
                "[copy-signal] %s...: unparseable trade record at ts=%s -- "
                "dropped and watermark advanced past it permanently (see "
                "_process_wallet's docstring for why retrying is pointless "
                "for on-chain history; this warning is the visibility "
                "trade-off if it's actually a normalize_trade() bug).",
                str(address)[:10], ts,
            )
        elif trade["side"] == "BUY":
            _handle_buy_trade(
                db, wallet, trade, stake, live_config, now_iso, first_poll=first_poll,
                breaker_reason=breaker_reason,
            )

        # Only reached once this trade (BUY, SELL, or unnormalizable) has
        # fully finished processing without raising -- see the fail-safe
        # note above.
        max_ts_seen = ts
        db.update_followed_wallet_last_seen(address, int(max_ts_seen))


def run_cycle(db) -> None:
    """One poll cycle: kill switch, then the realized-P&L circuit breaker,
    then per-wallet fetch/detect/execute, with per-wallet failure isolation.

    **Circuit breaker (issue #1139), checked once per cycle -- same place
    as the kill switch, and for the same reason: it is one global answer
    for every signal this cycle, not a per-wallet or per-signal decision.**
    Unlike the kill switch (which skips polling entirely, silently), a
    tripped breaker still runs the full per-wallet loop below -- every
    detected BUY still gets fetched, normalized, and logged with a
    ``copy_signals`` row (``breaker_reason`` as its ``skip_reason``,
    threaded through ``_process_wallet``/``_handle_buy_trade``) so the
    activity feed shows *why* nothing executed, rather than nothing
    appearing at all.
    """
    live_config = get_live_config(db)
    if not live_config["COPY_TRADING_ENABLED"]:
        return

    breaker_ok, breaker_skip_reason = allow_copy_signal(db, live_config)
    breaker_reason = None if breaker_ok else breaker_skip_reason

    now_iso = datetime.now(timezone.utc).isoformat()
    for wallet in db.get_followed_wallets(status="active"):
        try:
            _process_wallet(db, wallet, live_config, now_iso, breaker_reason=breaker_reason)
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

"""Copy-trading signal-detection, flat-stake paper-execution, and (issue
#1167, epic H #1159) live-execution loop.

Persistent process, structured like ``src/scripts/run.py``'s own poll
loop (sleep-between-cycles, graceful Ctrl-C shutdown, per-cycle exception
isolation so one bad cycle never kills the process) -- **not** a systemd
oneshot (that's story #B4's job, see ``deploy/systemd``).

**Paper execution is unconditional and independent of live mode.** Does
**not** call ``src.paper_trader.PaperTrader.execute_trade()`` -- that
writes to the shared ``trades`` table (forbidden by the #1100 isolation
decision -- copy-trading never touches the weather-strategy tables) and
settles win/loss *synchronously* from a known outcome, which a freshly
detected copy-trading signal doesn't have (the market resolves later).
This module only ever creates an **open, unsettled** ``copy_positions``
row for paper; settling it (computing ``settled_pnl_usd``) is epic C's
job (#1102), not this one.

**Live execution (issue #1167), layered strictly on top of paper, never
instead of it.** When a signal passes every paper gate (market-resolution,
exposure, circuit breaker -- see below) AND ``COPY_LIVE_TRADING_ENABLED``
is ``True`` AND Epic G's live-specific exposure/capital gates also pass
(``COPY_LIVE_MAX_EXPOSURE_PER_WALLET_USD`` / ``COPY_LIVE_MAX_TOTAL_EXPOSURE_USD``
/ ``live_startup_sanity_check()``), a real order is submitted via
``src.execution.copy_live_executor.execute_live_copy_order`` -- which
reuses ``src.execution.live_trader.LiveTrader``'s CLOB submission/fill/
cancel primitives (constructed with ``db=None`` there, so its own
weather-specific ``trades``/``open_positions`` writes never fire; see
that module's docstring for the full isolation rationale) -- and the
result is written into ``copy_live_positions`` (#1166), never into
``copy_positions``/``open_positions``/``trades``. A signal can be
paper-executed and live-executed together, or paper-only when live is
off, but **never live-only** -- live only ever runs downstream of an
already-committed paper execution in ``_handle_buy_trade``, so live can
never fire on a signal paper itself skipped.

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

**Live variant of the same sanity check (issue #1163 / epic G #1158,
wired into the live path by #1167).** :func:`live_startup_sanity_check`
mirrors :func:`startup_sanity_check` exactly, one layer up, for
``COPY_LIVE_MAX_TOTAL_EXPOSURE_USD`` vs. ``COPY_LIVE_CAPITAL_USD``. It is
still not wired into ``main()`` (this process is allowed to *start* even
if live config is inconsistent, exactly like paper's own ``main()``-only
``startup_sanity_check``) -- instead it is re-evaluated once per
``run_cycle()``, same cadence as the kill switch and the circuit breaker,
so a live-editable config change that violates the invariant mid-run
blocks new live orders on the very next cycle rather than only at process
start. A non-``None`` result is threaded through exactly like
``breaker_reason`` -- every live attempt this cycle is skipped with that
message as ``copy_live_positions.rejected_reason``, but paper execution
and polling are both completely unaffected.

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
    CONFIG_DEFAULTS, COPY_LIVE_CAPITAL_USD, COPY_TRADING_CAPITAL_USD,
    get_live_config, seed_config,
)
from src.data.db import Database  # noqa: E402
from src.data.polymarket import fetch_market_resolution  # noqa: E402
from src.data.polymarket_traders import get_wallet_trades_since, normalize_trade  # noqa: E402
from src.execution.copy_live_executor import execute_live_copy_order  # noqa: E402
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


def live_startup_sanity_check(live_config: dict) -> "str | None":
    """Return an error message if it is unsafe to start LIVE, else ``None``.

    Mirrors :func:`startup_sanity_check` exactly, one layer up (issue
    #1163): ``COPY_LIVE_MAX_TOTAL_EXPOSURE_USD`` (live-editable) must never
    exceed ``COPY_LIVE_CAPITAL_USD`` (a fixed module constant, not
    live-editable -- see its own comment in src/config.py), for the same
    reason -- otherwise the live total-exposure gate would keep approving
    new live positions past the point the configured live capital pool
    could actually fund.

    Not called anywhere yet -- no live loop exists to call it (epic H's
    job, #1159). Provided now so the isolation/config layer is ready ahead
    of that loop being built.
    """
    max_total = live_config["COPY_LIVE_MAX_TOTAL_EXPOSURE_USD"]
    if max_total > COPY_LIVE_CAPITAL_USD:
        return (
            f"COPY_LIVE_MAX_TOTAL_EXPOSURE_USD (${max_total:.2f}) exceeds "
            f"COPY_LIVE_CAPITAL_USD (${COPY_LIVE_CAPITAL_USD:.2f}) -- "
            "refusing to start. Fix the config values (dashboard config "
            "tab or env vars) before restarting."
        )
    return None


def _raw_trade_timestamp(raw: dict) -> "int | None":
    try:
        return int(raw.get("timestamp"))
    except (TypeError, ValueError):
        return None


def _handle_live_order(
    db, *, address: str, market: str, outcome_index: int, signal_id: int,
    stake: float, fill_price: float, token_id: "str | None", side_label: str,
    live_config: dict, live_gate_reason: "str | None", clob_client_factory,
    now_iso: str,
) -> None:
    """Independent live-execution layer on top of an already paper-executed
    signal (issue #1167). Only ever called from the branch of
    ``_handle_buy_trade`` where paper execution just happened -- paper's
    own gate ladder (market resolution, exposure, circuit breaker,
    first-poll bootstrap guard) has therefore already passed
    unconditionally for this signal, satisfying the acceptance criteria's
    "the full gate ladder applies to live orders exactly as it already
    does to paper" requirement without re-implementing any of it here.

    **The kill switch -- checked FIRST, before anything else, including
    before ``db._lock`` is ever acquired.** When
    ``COPY_LIVE_TRADING_ENABLED`` is falsy, this function returns
    immediately: no DB read, no DB write, no CLOB call of any kind. This
    is the single most load-bearing line in this module for issue #1167 --
    it is what the "zero live orders when disabled" test asserts against.

    **Live-specific gates, atomic with the insert (mirrors the paper
    exposure-check pattern in ``_handle_buy_trade``).** ``live_gate_reason``
    (the once-per-cycle ``live_startup_sanity_check()`` result, threaded
    through exactly like ``breaker_reason``) is checked first; then the
    live wallet/total exposure caps against ``get_open_copy_live_positions``,
    reading and inserting under the same ``db._lock`` for the same
    TOCTOU-safety reason AI review #1128 already established for paper.

    **A ``copy_live_positions`` row is written for every live attempt,
    executed or gate-rejected** -- mirrors ``copy_signals``' own
    "always log something, never silently drop" contract, one layer up,
    scoped to the live sub-decision. The two-step insert
    (``status='pending'``) then transition (``update_copy_live_position_status``)
    mirrors the table's documented lifecycle exactly (see
    ``Database.insert_copy_live_position``'s docstring: a row "rejected
    before submission may never get an order_id").

    **Order placement happens OUTSIDE ``db._lock``.** Exactly like
    ``fetch_market_resolution()`` above in ``_handle_buy_trade``, a live
    order's fill-wait can block for minutes (``copy_live_executor.
    FILL_MAX_WAIT_S``, plus a possible reprice-retry) -- holding the
    shared ``Database`` lock across that would stall every other DB
    consumer (the dashboard's request-handling threads) for the duration.

    Never raises -- a live-side failure must never affect the
    already-committed paper signal/position rows, and must never abort
    the wallet's whole processing batch (mirrors this module's fail-soft
    philosophy for network/DB blips elsewhere, e.g. the per-wallet
    isolation in ``run_cycle``).
    """
    if not live_config["COPY_LIVE_TRADING_ENABLED"]:
        return

    with db._lock:
        if live_gate_reason is not None:
            skip_reason = live_gate_reason
        elif not token_id:
            # Data-integrity guard, mirrors _handle_buy_trade's own
            # missing_outcome_index guard: a copied trade whose raw record
            # never carried an asset/token_id can be paper-signal-logged
            # and paper-executed (paper needs no token_id), but can never
            # be placed on the CLOB.
            skip_reason = "live_missing_token_id"
        else:
            wallet_live_exposure = sum(
                p["stake_usd"] for p in db.get_open_copy_live_positions(address)
            )
            if wallet_live_exposure + stake > live_config["COPY_LIVE_MAX_EXPOSURE_PER_WALLET_USD"]:
                skip_reason = "live_wallet_exposure_limit"
            else:
                total_live_exposure = sum(
                    p["stake_usd"] for p in db.get_open_copy_live_positions()
                )
                skip_reason = (
                    "live_total_exposure_limit"
                    if total_live_exposure + stake > live_config["COPY_LIVE_MAX_TOTAL_EXPOSURE_USD"]
                    else None
                )

        position_id = db.insert_copy_live_position(
            signal_id=signal_id, address=address, market=market,
            outcome_index=outcome_index, stake_usd=stake, entry_ts=now_iso,
        )

        if skip_reason is not None:
            db.update_copy_live_position_status(
                position_id, status="rejected", rejected_reason=skip_reason,
            )
            return

    try:
        result = execute_live_copy_order(
            clob_client_factory,
            token_id=token_id,
            market=market,
            side_label=side_label,
            price=fill_price,
            stake_usd=stake,
        )
    except Exception as exc:
        # execute_live_copy_order() is documented to never raise (every
        # failure mode it knows about comes back as status="rejected") --
        # this is a genuinely unexpected exception, but no CLOB order can
        # have been left live on the exchange from HERE (the function
        # itself already handles/logs its own place/cancel failures before
        # ever returning or raising), so a plain "rejected" write is safe.
        log.error(
            "[copy-signal] %s...: live order execution raised unexpectedly: %s",
            str(address)[:10], exc, exc_info=True,
        )
        _record_live_outcome(
            db, position_id, status="rejected",
            rejected_reason=f"unexpected_error:{exc}"[:200], address=address,
        )
        return

    if result["status"] == "rejected":
        _record_live_outcome(
            db, position_id, status="rejected", order_id=result.get("order_id"),
            rejected_reason=result.get("rejected_reason"), address=address,
        )
    else:
        _record_live_outcome(
            db, position_id, status=result["status"], order_id=result.get("order_id"),
            fill_price=result.get("fill_price"), address=address,
        )


def _record_live_outcome(
    db, position_id: int, *, status: str, address: str,
    order_id: "str | None" = None, fill_price: "float | None" = None,
    rejected_reason: "str | None" = None,
) -> None:
    """Persist a ``copy_live_positions`` status transition, escalating to a
    CRITICAL log (never raising further) if the write itself fails.

    This mirrors ``LiveTrader.place_order``'s own precedent exactly (see
    that function's ``except Exception as db_err: logging.critical(...)``
    block): by the time this is called, a REAL order may already have been
    submitted (or a CLOB call already made and settled to a terminal
    outcome) -- a DB write failure at this point must never look like an
    ordinary, quietly-retried DB blip. It has to be loud, because our local
    bookkeeping and the exchange's actual state can now silently diverge
    (e.g. an order genuinely filled on the exchange but ``copy_live_positions``
    never learned about it), which is exactly the "never silently dropped"
    guarantee the acceptance criteria calls out for fills/rejections.
    """
    try:
        with db._lock:
            db.update_copy_live_position_status(
                position_id, status=status, order_id=order_id,
                fill_price=fill_price, rejected_reason=rejected_reason,
            )
    except Exception as exc:
        log.critical(
            "[copy-signal] %s...: CRITICAL: live order outcome (status=%s, "
            "order_id=%s) could not be recorded into copy_live_positions "
            "(row id=%s): %s. The exchange's actual state and this DB's "
            "view of it may now be out of sync -- manual reconciliation "
            "may be required.",
            str(address)[:10], status, order_id, position_id, exc, exc_info=True,
        )


def _handle_buy_trade(
    db, wallet: dict, trade: dict, stake: float, live_config: dict, now_iso: str,
    first_poll: bool = False, breaker_reason: "str | None" = None,
    live_gate_reason: "str | None" = None, clob_client_factory=None,
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
    # The copied wallet's own trade record already carries the CLOB token id
    # it traded (see normalize_trade()'s docstring) -- issue #1167's live
    # path reuses it directly rather than re-deriving a token_id from
    # market+outcome_index. May be None for older/partial records; guarded
    # in _handle_live_order (live_missing_token_id), never blocks paper.
    token_id = trade.get("asset")

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

    # Paper is now fully committed and this function returns unconditionally
    # after this point -- live execution (issue #1167) is a pure addition on
    # top, never a precondition paper waits on. Deliberately OUTSIDE the
    # db._lock block above: _handle_live_order's own order-placement path
    # can block for minutes (CLOB fill-wait + a possible reprice-retry), and
    # must never hold the shared Database lock while doing so (same "no
    # network I/O under the lock" rule as fetch_market_resolution() above).
    side_label = "YES" if outcome_index == 0 else "NO"
    _handle_live_order(
        db, address=address, market=market, outcome_index=outcome_index,
        signal_id=signal_id, stake=stake, fill_price=fill_price,
        token_id=token_id, side_label=side_label, live_config=live_config,
        live_gate_reason=live_gate_reason, clob_client_factory=clob_client_factory,
        now_iso=now_iso,
    )


def _process_wallet(
    db, wallet: dict, live_config: dict, now_iso: str,
    breaker_reason: "str | None" = None,
    live_gate_reason: "str | None" = None, clob_client_factory=None,
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
                breaker_reason=breaker_reason, live_gate_reason=live_gate_reason,
                clob_client_factory=clob_client_factory,
            )

        # Only reached once this trade (BUY, SELL, or unnormalizable) has
        # fully finished processing without raising -- see the fail-safe
        # note above.
        max_ts_seen = ts
        db.update_followed_wallet_last_seen(address, int(max_ts_seen))


def run_cycle(db, clob_client_factory=None) -> None:
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

    **Live gate (issue #1167), also checked once per cycle, same cadence
    as the circuit breaker.** ``COPY_LIVE_TRADING_ENABLED`` itself is
    checked later, per-signal, in ``_handle_live_order`` (it is the one
    gate that must be re-read as close to the actual CLOB call as
    possible -- see that function's docstring); what IS decided once here
    is ``live_startup_sanity_check(live_config)`` (Epic G's
    ``COPY_LIVE_MAX_TOTAL_EXPOSURE_USD`` vs. ``COPY_LIVE_CAPITAL_USD``
    invariant), threaded through as ``live_gate_reason`` exactly like
    ``breaker_reason``.

    *clob_client_factory* defaults to ``None``; a zero-arg factory
    (``src.execution.auth.get_clob_client`` in production) is lazily
    imported and resolved here **only when live trading is actually
    enabled this cycle** -- paper-only runs (the default, kill-switch-off
    posture ahead of the phase-7 go/no-go gate) never import or touch the
    CLOB auth module at all. Callers (tests) may pass an explicit factory
    to bypass the lazy import entirely.

    **Wallet auto-pause (issue #1177, verified, no new gate needed).**
    ``for wallet in db.get_followed_wallets(status="active")`` below is the
    ONE snapshot both the paper path (``_process_wallet`` /
    ``_handle_buy_trade``) and the live path (``_handle_live_order``) are
    nested inside -- a wallet ``copy_wallet_health.py`` (a separate daily
    03:15 UTC job, see that module's docstring) auto-pauses is absent from
    this list entirely, halting live execution for it exactly as it already
    halts paper, with no separate live-specific check. A wallet paused by
    that job *after* this cycle already took its snapshot but *before* the
    loop reaches it would still be processed once more this cycle --
    audited and judged not worth guarding against given the two jobs'
    actual cadence (this loop's cycles complete in low single-digit seconds
    against a 300s poll interval; the health job runs once a day and does
    no network I/O), which bounds the exposure to at most one already
    in-flight cycle, self-corrected by the very next one. See
    ``copy_wallet_health.py``'s docstring for the full audit writeup.
    """
    live_config = get_live_config(db)
    if not live_config["COPY_TRADING_ENABLED"]:
        return

    breaker_ok, breaker_skip_reason = allow_copy_signal(db, live_config)
    breaker_reason = None if breaker_ok else breaker_skip_reason

    live_gate_reason = None
    if live_config["COPY_LIVE_TRADING_ENABLED"]:
        live_gate_reason = live_startup_sanity_check(live_config)
        if clob_client_factory is None:
            from src.execution.auth import get_clob_client  # noqa: PLC0415
            clob_client_factory = get_clob_client

    now_iso = datetime.now(timezone.utc).isoformat()
    for wallet in db.get_followed_wallets(status="active"):
        try:
            _process_wallet(
                db, wallet, live_config, now_iso, breaker_reason=breaker_reason,
                live_gate_reason=live_gate_reason, clob_client_factory=clob_client_factory,
            )
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

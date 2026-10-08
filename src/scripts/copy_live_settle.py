"""One-shot settlement, wallet-balance reconciliation, and ghost-order
recovery job for REAL (live) copy-trading positions (epic I #1160, issue
#1174). One layer up from ``src/scripts/copy_settle.py`` -- same one-shot
shape (fetch -> resolve -> compute -> persist -> log summary, not a
persistent loop) -- for ``copy_live_positions`` instead of ``copy_positions``.
Scheduled hourly by ``deploy/systemd/meteoedge-copy-live-settle.timer``,
mirroring ``meteoedge-copy-settle.timer``'s own "why hourly" rationale --
see docs/OPERATIONS.md.

**"Reconciliation" is the operative word in this epic's name -- this is not
just copy_settle.py's simulated-P&L math ported one layer up.** A single
run does three independent things, each isolated so a failure in one never
blocks the others:

1. **Settlement** (``_settle_live_positions``): for every ``status IN
   ('filled','partial')`` row, resolve its market via
   ``fetch_market_resolution`` (grouped by distinct market, same
   rate-limit-conscious pattern as ``copy_settle.py``), compute realized
   P&L via ``src.data.copy_pnl.compute_realized_pnl_usd``, and persist via
   ``Database.settle_copy_live_position``. A ``'partial'`` row's P&L is
   computed from its actual filled stake (``filled_stake_usd``, issue
   #1171 item 3), never the full originally-intended ``stake_usd``.
2. **Ghost-order recovery** (``recover_ghost_orders``, issue #1171 item 1):
   periodically re-checks ``copy_live_positions`` rows left in an ambiguous
   state after a failed GTC cancel (``rejected_reason='cancel_failed_ghost'``,
   written by ``src.execution.copy_live_executor``) via
   ``LiveTrader.check_fill``/``get_order_fill_size``/``get_order_fill_cost_usd``
   (issue #1341), and trues them up to a correct terminal status instead of
   leaving a real fill permanently mis-recorded as a dead rejection.
3. **Wallet-balance reconciliation** (``check_wallet_balance_drift``): the
   actual "reconciliation" this epic is named for. Compares the real CLOB
   USDC balance (``LiveTrader.get_usdc_balance()``) against an expected
   balance computed from a **full on-chain + Data API reconciliation of the
   deposit wallet's entire transaction history** (issue #1345; rebuilt from
   the original static-``COPY_LIVE_CAPITAL_USD``-ledger model, which broke
   the moment the wallet saw a manual trade or deposit outside the bot's
   own bookkeeping -- see ``src.data.wallet_reconciliation`` and
   ``check_wallet_balance_drift()``'s own docstring), and logs a loud
   ``log.critical`` (never a silent log line) when drift exceeds a
   configurable tolerance -- mirrors ``_record_live_outcome``'s existing
   "local bookkeeping and exchange state may have diverged" precedent. Every
   computed verdict is also persisted to ``bot_config`` (issue #1189, epic J
   #1161) so the dashboard can surface it outside of ``logs/bot.log`` --
   see ``get_wallet_balance_drift_status()``'s docstring.

**Isolation (#1100), unchanged from every other module in this epic set.**
This script never reads or writes ``copy_positions``/``open_positions``/
``trades`` -- only ``copy_live_positions``. It is a completely separate
process/systemd unit from ``copy_settle.py``, with its own timer.

Usage::

    python -m src.scripts.copy_live_settle
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import COPY_LIVE_CAPITAL_USD, get_live_config  # noqa: E402
from src.data.copy_pnl import compute_realized_pnl_usd, effective_stake_usd  # noqa: E402
from src.data.polymarket import fetch_market_resolution  # noqa: E402
from src.data.wallet_reconciliation import compute_wallet_reconciliation  # noqa: E402
from src.execution.live_trader import LiveTrader  # noqa: E402

log = logging.getLogger(__name__)

_EMPTY_SETTLE_SUMMARY = {"settled": 0, "pending": 0, "errors": 0}

# bot_config key the wallet-balance-drift verdict is persisted under (issue
# #1189) -- reuses the existing key/value store (Database.get_config/
# set_config) exactly like COPY_LIVE_TRADING_ENABLED / capture_staleness's
# own threshold key, rather than a new table: this is a single computed
# status blob, not a user-editable parameter, so it is deliberately NOT in
# CONFIG_DEFAULTS (src/config.py) and never surfaced by GET /api/config --
# both seed_config()/get_live_config() and the dashboard's config endpoints
# only ever iterate CONFIG_DEFAULTS's own keys, so this extra row is inert
# to them. Value is a JSON blob (not a plain scalar, unlike other
# bot_config rows) because the verdict is a small structured record, not a
# single value -- see get_wallet_balance_drift_status()'s docstring for the
# shape.
_BALANCE_DRIFT_STATUS_KEY = "COPY_LIVE_WALLET_BALANCE_DRIFT_CHECK"
_EMPTY_GHOST_SUMMARY = {"recovered": 0, "confirmed_dead": 0, "still_ambiguous": 0}


def _open_db():
    """Return a Database handle, or None if the DB cannot be opened."""
    try:
        from src.data.db import Database
        return Database()
    except Exception as e:
        log.warning("[copy-live-settle] DB unavailable: %s -- skipping run", e)
        return None


def _settle_live_positions(db) -> dict:
    """Settle every ``status IN ('filled','partial')`` real position whose
    market has resolved. Mirrors ``copy_settle.run_once``'s body exactly,
    one layer up, with two differences: the source query
    (``get_unsettled_copy_live_positions`` instead of
    ``get_open_copy_positions``) and the effective stake used for P&L --
    ``filled_stake_usd`` when a fill recorded one (issue #1171 item 3 for
    partials; issue #1336 for full fills), falling back to the row's own
    ``stake_usd`` otherwise (legacy ``'filled'`` rows whose fill record was
    never captured, and any row written before this column existed). This is
    ``COALESCE(filled_stake_usd, stake_usd)`` -- see ``effective_stake_usd``.
    """
    rows = db.get_unsettled_copy_live_positions()
    if not rows:
        log.info("[copy-live-settle] no unsettled real positions to settle")
        return dict(_EMPTY_SETTLE_SUMMARY)

    # Group by distinct market first -- same shared data-api/gamma-api
    # rate-limit budget as copy_settle.py; a market with several unsettled
    # real positions spends one fetch_market_resolution() call, not one per
    # position.
    markets = sorted({r["market"] for r in rows})
    resolutions: dict = {}
    for market in markets:
        resolutions[market] = fetch_market_resolution(market)

    now_iso = datetime.now(timezone.utc).isoformat()
    n_settled = 0
    n_pending = 0
    n_errors = 0
    for r in rows:
        market = r["market"]
        yes_won = resolutions.get(market)
        if yes_won is None:
            n_pending += 1
            log.debug(
                "[copy-live-settle] market %s... not resolved yet -- position %s stays %s",
                str(market)[:14], r["id"], r["status"],
            )
            continue

        try:
            # COALESCE(filled_stake_usd, stake_usd) -- issue #1336.
            effective_stake = effective_stake_usd(r.get("filled_stake_usd"), r["stake_usd"])
            pnl = compute_realized_pnl_usd(
                entry_price=float(r["fill_price"]),
                stake_usd=float(effective_stake),
                outcome_index=int(r["outcome_index"]),
                yes_won=yes_won,
            )
            db.settle_copy_live_position(r["id"], round(pnl, 6), now_iso)
            n_settled += 1
            log.debug(
                "[copy-live-settle] settled position %s address=%s market=%s... "
                "status=%s outcome_index=%s yes_won=%s stake=%.4f pnl=%.4f",
                r["id"], r.get("address"), str(market)[:14], r["status"],
                r["outcome_index"], yes_won, effective_stake, pnl,
            )
        except Exception as e:
            n_errors += 1
            log.warning(
                "[copy-live-settle] settlement failed for position %s: %s", r["id"], e,
            )

    log.info(
        "[copy-live-settle] settlement pass complete: settled=%s pending=%s errors=%s "
        "(of %s unsettled real position(s))",
        n_settled, n_pending, n_errors, len(rows),
    )
    return {"settled": n_settled, "pending": n_pending, "errors": n_errors}


def recover_ghost_orders(db, clob_client_factory) -> dict:
    """Re-check every ``copy_live_positions`` row left ambiguous by a failed
    GTC cancel (issue #1171 item 1 / #1174), and true it up to a correct
    terminal status.

    For each row returned by ``Database.get_ghost_order_positions()``
    (``status='rejected'``, ``rejected_reason='cancel_failed_ghost'``):

    - A confirmed non-zero fill (``LiveTrader.get_order_fill_size() > 0``)
      is trued up to ``'filled'`` (if the confirmed shares cover the
      originally-intended contract count) or ``'partial'`` otherwise,
      recording ``filled_stake_usd`` -- exactly like a normal fill/partial
      would have been recorded had the cancel not failed. Escalated via
      ``log.critical`` (never a quiet log line): local bookkeeping and the
      exchange's actual state were out of sync until this recheck.
    - A confirmed zero fill with ``check_fill() == 'cancelled'`` is a
      genuine, now-unambiguous rejection -- rewritten with
      ``rejected_reason='cancel_confirmed_zero_fill'`` so it is never
      re-queried by a future run (distinct from the still-open case below).
    - Anything else (``check_fill() == 'open'``, still resting on the
      exchange, or the recheck call itself failed) is left exactly as-is
      for the next hourly run -- still genuinely ambiguous, not something
      this pass can safely resolve.

    Never raises for an individual row -- one row's recheck failing must
    never block the rest, mirroring ``_settle_live_positions``'s per-row
    isolation.
    """
    rows = db.get_ghost_order_positions()
    if not rows:
        return dict(_EMPTY_GHOST_SUMMARY)

    trader = LiveTrader(clob_client_factory(), db=None)  # isolation (#1100), mirrors copy_live_executor
    n_recovered = 0
    n_confirmed_dead = 0
    n_still_ambiguous = 0

    for r in rows:
        order_id = r["order_id"]
        try:
            fill_status = trader.check_fill(order_id)
            filled_shares = trader.get_order_fill_size(order_id)
        except Exception as e:
            log.warning(
                "[copy-live-settle] ghost recheck failed for position %s order=%s...: %s",
                r["id"], str(order_id)[:12], e,
            )
            n_still_ambiguous += 1
            continue

        if filled_shares and filled_shares > 0:
            fill_price = r.get("fill_price")
            if not fill_price:
                # Should not happen for a ghost row written after this fix
                # (execute_live_copy_order always captures the placed price
                # even on a ghost rejection) -- but never guess a price.
                log.critical(
                    "[copy-live-settle] CRITICAL: ghost order %s... (position %s) "
                    "confirmed %.4f shares filled but has no captured fill_price to "
                    "value it at -- cannot true up automatically, manual "
                    "reconciliation required.",
                    str(order_id)[:12], r["id"], filled_shares,
                )
                n_still_ambiguous += 1
                continue

            intended_shares = round(r["stake_usd"] / fill_price, 2)
            new_status = "filled" if filled_shares >= intended_shares - 1e-6 else "partial"
            # Issue #1341: prefer the order's own confirmed-trade cost over
            # filled_shares * placed fill_price -- the latter is only an
            # upper bound (see LiveTrader.get_order_fill_cost_usd's
            # docstring), the exact same bug #1336 shipped for the normal
            # fill/partial paths in copy_live_executor.py, which this ghost
            # recovery path must not repeat.
            try:
                actual_cost = trader.get_order_fill_cost_usd(order_id)
            except Exception as e:
                log.warning(
                    "[copy-live-settle] fill-cost lookup failed for ghost order "
                    "%s... (position %s): %s", str(order_id)[:12], r["id"], e,
                )
                actual_cost = 0.0
            filled_stake_usd = (
                round(actual_cost, 6) if actual_cost and actual_cost > 0
                else round(filled_shares * fill_price, 6)
            )
            db.update_copy_live_position_status(
                r["id"], status=new_status, filled_stake_usd=filled_stake_usd,
            )
            log.critical(
                "[copy-live-settle] CRITICAL: ghost order %s... (position %s) recovered "
                "as %s -- %.4f of ~%.4f intended shares filled at %.4f. Local "
                "bookkeeping was out of sync with the exchange until this recheck.",
                str(order_id)[:12], r["id"], new_status, filled_shares, intended_shares,
                fill_price,
            )
            n_recovered += 1
        elif fill_status == "cancelled":
            db.update_copy_live_position_status(
                r["id"], status="rejected", rejected_reason="cancel_confirmed_zero_fill",
            )
            n_confirmed_dead += 1
        else:
            # 'open' (still resting) or check_fill's own error-fallback
            # ('open') -- genuinely still ambiguous, retry next run.
            n_still_ambiguous += 1

    log.info(
        "[copy-live-settle] ghost-order recovery complete: recovered=%s confirmed_dead=%s "
        "still_ambiguous=%s (of %s ghost row(s))",
        n_recovered, n_confirmed_dead, n_still_ambiguous, len(rows),
    )
    return {
        "recovered": n_recovered,
        "confirmed_dead": n_confirmed_dead,
        "still_ambiguous": n_still_ambiguous,
    }


def check_wallet_balance_drift(db, clob_client_factory, *, now: "datetime | None" = None) -> "dict | None":
    """Compare the real CLOB USDC balance against an expected balance
    computed as the **full on-chain + Data API cash-accounting
    reconciliation** of the deposit wallet's entire transaction history
    (issue #1345), and flag drift beyond
    ``COPY_LIVE_BALANCE_DRIFT_TOLERANCE_USD`` -- the actual "reconciliation"
    this epic (#1160) is named for.

    **Rebuilt 2026-10-08 (issue #1345) -- replaces the old static-capital
    model.** The previous formula (``COPY_LIVE_CAPITAL_USD - committed +
    realized``, derived purely from local ``copy_live_positions`` rows) was
    wrong by construction the moment the wallet saw anything outside the
    bot's own bookkeeping -- confirmed in production: a real ~$20 external
    top-up, and ongoing operator manual trades on the same wallet, neither
    of which ``copy_live_positions`` has any row for. The new formula (see
    ``src.data.wallet_reconciliation.compute_wallet_reconciliation``) is a
    cash-accounting identity over the wallet's *entire* real history, not a
    delta from a capital constant::

        expected_balance = sum(TRADE/REDEEM cash flow, Data API activity)
            + sum(external deposits, on-chain pUSD transfers)
            - sum(external withdrawals, on-chain pUSD transfers)

    This covers every dollar that ever moved the wallet's balance --
    bot-placed trades, manual trades, redemptions, deposits, withdrawals --
    regardless of which local table (if any) recorded it, which is exactly
    why it stays correct where the old model didn't.

    **Persistence (issue #1189, interface unchanged by #1345):** every time
    this function actually computes a verdict (i.e. does not return
    ``None``), it persists that exact ``result`` dict plus a ``checked_at``
    timestamp to ``_BALANCE_DRIFT_STATUS_KEY`` via ``db.set_config`` --
    readable by the dashboard through ``get_wallet_balance_drift_status()``
    below, without needing a live CLOB call of its own. A later
    within-tolerance run overwrites the same key, which is what makes a
    resolved drift stop showing as a warning (only the LATEST verdict is
    ever kept -- no history). The persisted dict keeps its original five
    keys (``expected_balance_usd``, ``actual_balance_usd``, ``drift_usd``,
    ``within_tolerance``, ``checked_at``) exactly as #1189 shaped them, plus
    new additive-only keys from the #1345 rebuild (``external_deposits_usd``,
    ``external_withdrawals_usd``, ``transfers_unverified``,
    ``unresolved_usd``, ``unresolved_count``) -- ``CopyLiveBalanceDriftOut``
    (``src/dashboard/api.py``) only reads the original five, so this is
    forward-compatible, not a breaking change to #1189's contract. The
    ``None``-return paths (no capital allocated, no deposit wallet
    configured, Data API unreachable, CLOB unreachable) deliberately do NOT
    touch the persisted row: a stale timestamp on the dashboard is the
    intended signal that the last real check is old, rather than silently
    erasing the last known verdict. *now* overrides "current time" for the
    persisted timestamp (testing only); defaults to UTC now.

    **Degraded mode -- missing/invalid ``ETHERSCAN_API_KEY`` (issue #1345
    acceptance criteria).** The on-chain-transfer dimension is the only part
    that key gates -- Data API trade/redeem cash flow has no such
    dependency. When the key is absent (or the fetch otherwise fails), this
    function does NOT skip the whole check: it still computes
    ``expected_balance`` from Data API activity alone (external transfers
    contribute $0 that run) and persists ``transfers_unverified=True`` so a
    real external deposit/withdrawal since the last verified run shows up
    as reported-unverified-risk, not as a silently-assumed "zero external
    activity" fact nor a crash.

    **Unresolved transfers never get folded into "drift".** A transfer that
    looks like trade settlement (by its on-chain method selector) but is
    absent from the Data API activity feed -- the known Data-API trade gap,
    confirmed in #1342 -- is reported separately (``unresolved_usd``,
    ``unresolved_count``), not guessed into a deposit/withdrawal and not
    silently absorbed into ``drift_usd`` with no explanation. See
    ``src.data.wallet_reconciliation.classify_onchain_transfers``.

    Returns ``None`` (skips entirely, no CLOB call made) when
    ``COPY_LIVE_CAPITAL_USD<=0`` -- the same "operator hasn't allocated real
    capital yet" safety-default precedent ``live_startup_sanity_check`` and
    ``COPY_LIVE_TRADING_ENABLED`` already establish elsewhere in this epic
    set. **Carried over unchanged from the pre-#1345 model, deliberately --
    flagged as a design question for PR review**, not an oversight: this
    gate is now orthogonal to the expected-balance formula (which no longer
    references ``COPY_LIVE_CAPITAL_USD`` at all), so it currently serves
    only as a cheap "is live copy-trading in use at all" toggle to avoid
    spending Data-API/Etherscan calls when the feature isn't allocated any
    capital. Also returns ``None`` when ``POLYMARKET_DEPOSIT_WALLET`` is
    unset (nothing to reconcile), when the Data API activity feed itself is
    unreachable (no cash-flow signal at all to build an expected balance
    from), or when the CLOB balance call fails (network/auth issue; logged
    as a warning, not a drift verdict this run cannot actually make).
    """
    if COPY_LIVE_CAPITAL_USD <= 0:
        log.debug(
            "[copy-live-settle] COPY_LIVE_CAPITAL_USD<=0 -- skipping wallet-balance "
            "reconciliation (no real capital allocated)",
        )
        return None

    wallet_address = os.environ.get("POLYMARKET_DEPOSIT_WALLET")
    if not wallet_address:
        log.warning(
            "[copy-live-settle] POLYMARKET_DEPOSIT_WALLET not set -- skipping "
            "wallet-balance reconciliation (nothing to reconcile against)",
        )
        return None

    live_config = get_live_config(db)
    tolerance = live_config["COPY_LIVE_BALANCE_DRIFT_TOLERANCE_USD"]

    reconciliation = compute_wallet_reconciliation(wallet_address)
    if not reconciliation.activity_available:
        log.warning(
            "[copy-live-settle] wallet-balance check failed (Data API activity feed "
            "unreachable for %s...) -- no cash-flow signal to build an expected "
            "balance from this run", wallet_address[:10],
        )
        return None

    try:
        actual_balance = LiveTrader(clob_client_factory(), db=None).get_usdc_balance()
    except Exception as e:
        log.warning(
            "[copy-live-settle] wallet-balance check failed (CLOB unreachable?): %s", e,
        )
        return None

    expected_balance = reconciliation.expected_balance_usd
    drift = actual_balance - expected_balance
    within_tolerance = abs(drift) <= tolerance
    result = {
        "expected_balance_usd": expected_balance,
        "actual_balance_usd": actual_balance,
        "drift_usd": drift,
        "within_tolerance": within_tolerance,
        "external_deposits_usd": reconciliation.external_deposits_usd,
        "external_withdrawals_usd": reconciliation.external_withdrawals_usd,
        "transfers_unverified": reconciliation.transfers_unverified,
        "transfers_unverified_reason": reconciliation.transfers_unverified_reason,
        "unresolved_usd": reconciliation.unresolved_usd,
        "unresolved_count": len(reconciliation.unresolved),
    }
    if not within_tolerance:
        log.critical(
            "[copy-live-settle] CRITICAL: WALLET BALANCE DRIFT of $%.4f exceeds tolerance "
            "$%.2f -- expected $%.4f (activity_cash_flow=$%.4f + external_deposits=$%.4f "
            "- external_withdrawals=$%.4f) vs actual exchange balance $%.4f. "
            "transfers_unverified=%s unresolved_usd=$%.4f (%s transfer(s)). Local "
            "bookkeeping and exchange state may have diverged -- manual reconciliation "
            "required.",
            drift, tolerance, expected_balance, reconciliation.activity_cash_flow_usd,
            reconciliation.external_deposits_usd, reconciliation.external_withdrawals_usd,
            actual_balance, reconciliation.transfers_unverified, reconciliation.unresolved_usd,
            len(reconciliation.unresolved),
        )
    elif reconciliation.transfers_unverified:
        log.warning(
            "[copy-live-settle] wallet balance within tolerance ($%.4f drift) but "
            "on-chain external-transfer verification was UNVERIFIED this run (%s) -- "
            "expected $%.4f reflects Data API trade/redeem activity only",
            drift, reconciliation.transfers_unverified_reason, expected_balance,
        )
    else:
        log.info(
            "[copy-live-settle] wallet balance OK: expected $%.4f, actual $%.4f, "
            "drift $%.4f (tolerance $%.2f)",
            expected_balance, actual_balance, drift, tolerance,
        )

    checked_at = (now or datetime.now(timezone.utc)).isoformat()
    try:
        db.set_config(_BALANCE_DRIFT_STATUS_KEY, json.dumps({**result, "checked_at": checked_at}))
    except Exception as e:
        # Persistence is a best-effort convenience for the dashboard -- a
        # write failure here must never turn an otherwise-successful check
        # (already logged above, loudly if in drift) into a crashed run.
        log.warning("[copy-live-settle] failed to persist wallet-balance-drift status: %s", e)

    return result


_EMPTY_BALANCE_DRIFT_STATUS = {
    "checked_at": None,
    "within_tolerance": None,
    "drift_usd": None,
    "expected_balance_usd": None,
    "actual_balance_usd": None,
}


def get_wallet_balance_drift_status(db) -> dict:
    """Read-only accessor for the LATEST persisted ``check_wallet_balance_drift()``
    verdict (issue #1189) -- exposed to the dashboard via
    ``GET /api/copy-trading/balance-drift`` (``src/dashboard/api.py``),
    mirroring ``src.monitoring.capture_staleness.get_capture_health()``'s own
    "module owns both the check-and-log function and a read-only getter"
    split.

    Returns a dict shaped exactly like ``_EMPTY_BALANCE_DRIFT_STATUS``:
    ``checked_at`` (ISO timestamp string or None), ``within_tolerance``
    (bool or None), ``drift_usd``/``expected_balance_usd``/
    ``actual_balance_usd`` (float or None). All fields are ``None`` when no
    check has ever successfully persisted a result yet (real capital never
    allocated, or every run so far hit a CLOB failure) -- never guess a
    verdict when the source of truth is unavailable, same conservative-
    default rule this epic uses throughout (``execution_mode`` fallback,
    ``COPY_LIVE_TRADING_ENABLED`` inert-by-default). Never raises: a
    missing DB, missing config row, or corrupt JSON blob all degrade to the
    same all-``None`` "unknown" shape rather than a 500.
    """
    if db is None:
        return dict(_EMPTY_BALANCE_DRIFT_STATUS)
    try:
        raw = db.get_config(_BALANCE_DRIFT_STATUS_KEY)
    except Exception as e:
        log.warning("[copy-live-settle] wallet-balance-drift status read failed: %s", e)
        return dict(_EMPTY_BALANCE_DRIFT_STATUS)
    if raw is None:
        return dict(_EMPTY_BALANCE_DRIFT_STATUS)
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise TypeError(f"expected dict, got {type(payload).__name__}")
    except (ValueError, TypeError) as e:
        log.warning("[copy-live-settle] wallet-balance-drift status row unparseable: %r (%s)", raw, e)
        return dict(_EMPTY_BALANCE_DRIFT_STATUS)
    return {**_EMPTY_BALANCE_DRIFT_STATUS, **payload}


def run_once(db=None, clob_client_factory=None) -> dict:
    """Run one full pass: settlement, then ghost-order recovery, then
    wallet-balance reconciliation. Each is independent -- a failure/skip in
    one never blocks the others.

    *clob_client_factory* defaults to ``None``; a zero-arg factory
    (``src.execution.auth.get_clob_client`` in production) is lazily
    imported and resolved here **only when there is something that needs
    the CLOB** (a ghost row to recheck, or real capital allocated to
    reconcile) -- mirrors ``copy_signal_loop.run_cycle``'s own "never
    import/touch the CLOB auth module when live trading isn't actually in
    play" laziness rule. Callers (tests) may pass an explicit factory to
    bypass the lazy import entirely.

    If *db* is not given, opens (and owns) a real ``Database()`` handle.
    """
    if db is None:
        db = _open_db()
    if db is None:
        return {
            "settle": dict(_EMPTY_SETTLE_SUMMARY),
            "ghost_recovery": dict(_EMPTY_GHOST_SUMMARY),
            "balance_check": None,
        }

    settle_summary = _settle_live_positions(db)

    ghost_rows_exist = bool(db.get_ghost_order_positions())
    needs_clob = ghost_rows_exist or COPY_LIVE_CAPITAL_USD > 0
    if needs_clob and clob_client_factory is None:
        from src.execution.auth import get_clob_client  # noqa: PLC0415
        clob_client_factory = get_clob_client

    if ghost_rows_exist:
        ghost_summary = recover_ghost_orders(db, clob_client_factory)
    else:
        ghost_summary = dict(_EMPTY_GHOST_SUMMARY)

    balance_summary = (
        check_wallet_balance_drift(db, clob_client_factory) if needs_clob else None
    )

    log.info(
        "[copy-live-settle] run complete: settle=%s ghost_recovery=%s balance_check=%s",
        settle_summary, ghost_summary, balance_summary,
    )
    return {
        "settle": settle_summary,
        "ghost_recovery": ghost_summary,
        "balance_check": balance_summary,
    }


def main() -> None:
    from src.logging_config import setup_logging
    setup_logging()
    run_once()


if __name__ == "__main__":
    main()

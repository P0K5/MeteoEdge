"""Live order execution for copy-trading signals (issue #1167, epic H #1159).

One layer up from ``src/execution/order_executor.py``'s ``_execute_live()``:
reuses the exact same CLOB submission/fill/cancel primitives
(``LiveTrader.place_order`` / ``check_fill`` / ``cancel_order``) and mirrors
its GTC-timeout + one-shot reprice-retry pattern (``_attempt`` /
``_reprice_from_book`` there), adapted for a copy-trading signal instead of
a weather-strategy ``ScanCandidate``.

**Isolation (#1100), the load-bearing design decision in this module.**
``LiveTrader`` is always constructed here with ``db=None``. This is
deliberate, not an oversight: ``LiveTrader.place_order()``'s own
``if self._db is not None:`` branch writes to the weather-strategy-specific
``trades`` / ``open_positions`` tables (via ``insert_trade`` /
``open_position``), and ``cancel_order()``'s own ``if cancelled and
self._db is not None:`` branch calls ``close_position()`` -- both are
``live_trader.py``'s own position-tracking/state, which the #1167 issue
(and the #1100 isolation decision it cites) explicitly forbids touching or
importing. Passing ``db=None`` makes both branches structurally
unreachable from this module, so the exact same order-submission /
fill-check / cancel code path can be reused without ever writing to those
tables. This module performs **no DB writes of its own at all** -- it
returns a plain result dict; the caller (``src/scripts/copy_signal_loop.py``)
is solely responsible for persisting the outcome into ``copy_live_positions``
(#1166).

**No candidate.station/bracket/EV concepts here.** Unlike the weather
scanner's reprice-retry, a copy-trading signal has no edge/confidence gate
to re-validate against an unchanged threshold -- the flat stake and
slippage-adjusted price were already decided by the paper gate ladder
before this module is ever called. The only thing that can meaningfully
change between the original attempt and a retry is whether the *source*
market has resolved in the interim (``fetch_market_resolution``), so that
is the only re-check ``_reprice_from_book`` performs here.

**Partial fills on the BUY side -- new versus the weather mirror.** Unlike
``order_executor._attempt()`` (which only ever treats a post-timeout
cancel as "zero filled", relying on ``order_manager.reconcile_timeout_fills``
to patch a ghost fill later out-of-band), this module actively checks
``LiveTrader.get_order_fill_size()`` after a timeout+cancel, mirroring the
exact "wallet-token presence alone is NOT sufficient, `get_fill_size` is
the authority" pattern already documented for issue #993 on the SELL side
in ``order_manager.py``. This is necessary because the acceptance criteria
for #1167 explicitly requires partial-fill handling, which the weather BUY
path does not have. A confirmed partial fill short-circuits the
reprice-retry (retrying while some of the original stake already landed
risks doubling exposure on the same signal) and is reported as
``status="partial"`` for the caller to record.
"""
from __future__ import annotations

import logging
import time

from src.data.polymarket import fetch_market_resolution, get_orderbook
from src.execution.live_trader import LiveTrader
from src.execution.order_manager import order_manager as _order_manager

log = logging.getLogger(__name__)

# Mirrors order_executor.py's own constants exactly -- same GTC fill-wait
# budget for the same underlying exchange behavior.
FILL_POLL_INTERVAL_S = 30
FILL_MAX_WAIT_S = 300  # 5 minutes, 10 attempts


def _price_to_cents(price: float) -> int:
    """Clamp a 0-1 probability price to a valid 1-99 cent CLOB tick."""
    return max(1, min(99, round(price * 100)))


def _reprice_from_book(token_id: str, market: str) -> "float | None":
    """Re-validate a copy-trade candidate at the current best ask.

    Returns the new 0-1 price to retry at, or ``None`` (do not retry) when
    the source market has resolved in the interim or there is no ask
    liquidity. Unlike ``order_executor._reprice_from_book``, there are no
    EV/confidence gates to re-check here -- see the module docstring.
    """
    if fetch_market_resolution(market) is not None:
        log.info("  [copy-live] reprice: market %s... resolved -- not retrying", str(market)[:12])
        return None
    try:
        book = get_orderbook(token_id)
    except Exception as e:
        log.info("  [copy-live] reprice: book fetch failed (%s) -- not retrying", e)
        return None
    asks = (book or {}).get("asks") or []
    prices = [float(a["price"]) for a in asks if a.get("price") is not None]
    if not prices:
        log.info("  [copy-live] reprice: no asks on book -- not retrying")
        return None
    return min(prices)


def execute_live_copy_order(
    clob_client_factory,
    *,
    token_id: str,
    market: str,
    side_label: str,
    price: float,
    stake_usd: float,
    retry_enabled: bool = True,
) -> dict:
    """Place one real GTC BUY order mirroring a copied wallet's trade.

    Args:
        clob_client_factory: zero-arg callable returning a fresh
            ``ClobClient`` (``src.execution.auth.get_clob_client`` in
            production) -- mirrors ``order_executor._execute_live``'s own
            per-call client construction.
        token_id: the CLOB token id to buy (the copied trade's own
            ``asset``/``token_id`` field -- see
            ``src.data.polymarket_traders.normalize_trade``).
        market: the market's condition id, used only for the
            reprice-retry's resolution re-check and as ``LiveTrader.
            place_order``'s required (but here purely informational, since
            ``db=None``) ``ticker`` argument.
        side_label: ``"YES"`` or ``"NO"`` -- informational only (the CLOB
            order itself is always a BUY on ``token_id``, exactly like
            ``LiveTrader.place_order``'s own ``side`` argument).
        price: the slippage-adjusted target price in ``[0, 1]``
            (``copy_signal_loop.py``'s already-computed ``fill_price``).
        stake_usd: USDC to spend, at most (``size_usdc`` in
            ``LiveTrader.place_order`` terms).
        retry_enabled: mirrors ``LIVE_TIMEOUT_REPRICE_RETRY`` -- passed in
            by the caller so this module has no direct config dependency.

    Returns:
        One of:
          ``{"status": "filled", "order_id": str, "fill_price": float}``
          ``{"status": "partial", "order_id": str, "fill_price": float}``
          ``{"status": "rejected", "order_id": str | None,
              "rejected_reason": str}``

        ``fill_price`` on a fill/partial is the *placed* limit price
        (converted back from cents), not a re-queried exchange-matched
        price -- mirrors the same simplification ``order_executor.py`` /
        ``_append_live_trade`` already make for the weather strategy (a
        GTC BUY limit order fills at its limit price or better; price
        improvement, if any, is not captured here or there).

    Never raises -- every failure mode (placement rejected, cancel
    failure, unexpected exception) is caught and returned as
    ``status="rejected"`` with a description, mirroring copy_signals'
    "always log something, never silently drop" contract one layer up.
    """
    trader = LiveTrader(clob_client_factory(), db=None)  # see module docstring: isolation

    def _attempt(price_cents: int) -> "tuple[str | None, str, bool]":
        """Place one GTC order at *price_cents*, wait for fill/timeout/cancel.

        Returns (order_id, outcome, cancel_ok). outcome is one of
        'filled' | 'timeout' | 'cancelled' | 'place_failed'.
        """
        with _order_manager._order_lock:  # Serialize CLOB placements, mirrors order_executor.py
            try:
                order_id = trader.place_order(
                    token_id=token_id,
                    side=side_label,
                    price_cents=price_cents,
                    size_usdc=stake_usd,
                    ticker=market,  # required non-empty; never persisted (db=None)
                )
            except Exception as e:
                log.error("  [copy-live] place_order failed: %s", e, exc_info=True)
                return None, "place_failed", True

        log.info("  [copy-live] placed %s... %s @ %sc", order_id[:12], side_label, price_cents)

        deadline = time.monotonic() + FILL_MAX_WAIT_S
        outcome = "timeout"
        while time.monotonic() < deadline:
            time.sleep(FILL_POLL_INTERVAL_S)
            status = trader.check_fill(order_id)
            if status == "filled":
                outcome = "filled"
                break
            if status == "cancelled":
                outcome = "cancelled"
                break

        cancel_ok = True
        if outcome == "timeout":
            try:
                trader.cancel_order(order_id)
            except Exception as e:
                cancel_ok = False
                log.error(
                    "  [copy-live] CRITICAL: failed to cancel GTC order %s after timeout: %s. "
                    "Order is still live on the exchange and may fill as a ghost trade.",
                    order_id[:12], e, exc_info=True,
                )

        log.info("  [copy-live] %s %s...", outcome, order_id[:12])
        return order_id, outcome, cancel_ok

    price_cents = _price_to_cents(price)
    order_id, outcome, cancel_ok = _attempt(price_cents)

    if outcome == "place_failed":
        return {"status": "rejected", "order_id": None, "rejected_reason": "place_failed"}

    # A timeout/cancellation may still have partially filled before the
    # cancel took effect -- new versus the weather BUY-side mirror, see the
    # module docstring's "Partial fills" section. This is checked BEFORE
    # deciding whether to reprice-retry: retrying on top of a confirmed
    # partial fill would risk buying more than the intended stake for this
    # signal, so a confirmed partial short-circuits the retry entirely.
    if outcome in ("timeout", "cancelled") and cancel_ok:
        filled_shares = trader.get_order_fill_size(order_id)
        if filled_shares and filled_shares > 0:
            return {
                "status": "partial",
                "order_id": order_id,
                "fill_price": round(price_cents / 100, 4),
            }

    # Issue #743-style one-shot reprice-retry: only after a CLEANLY
    # cancelled timeout (no confirmed fill) with retry enabled. Never
    # retried when the cancel itself failed (cancel_ok=False) -- the
    # original order may still be live on the exchange, and placing a
    # second order on top of it would risk double exposure on one signal.
    if outcome == "timeout" and cancel_ok and retry_enabled:
        new_price = _reprice_from_book(token_id, market)
        if new_price is not None:
            new_cents = _price_to_cents(new_price)
            log.info(
                "  [copy-live] reprice-retry: re-placing %s... at %sc (was %sc)",
                str(market)[:12], new_cents, price_cents,
            )
            order_id2, outcome2, cancel_ok2 = _attempt(new_cents)
            if outcome2 != "place_failed":
                order_id, outcome, price_cents = order_id2, outcome2, new_cents
                if outcome in ("timeout", "cancelled") and cancel_ok2:
                    filled_shares = trader.get_order_fill_size(order_id)
                    if filled_shares and filled_shares > 0:
                        return {
                            "status": "partial",
                            "order_id": order_id,
                            "fill_price": round(price_cents / 100, 4),
                        }

    if outcome == "filled":
        return {
            "status": "filled",
            "order_id": order_id,
            "fill_price": round(price_cents / 100, 4),
        }

    # 'timeout' (no confirmed fill, retry not applicable/exhausted) or
    # 'cancelled' (no confirmed fill) both end up here: never submitted a
    # working position, but an order_id may exist for audit purposes.
    return {
        "status": "rejected",
        "order_id": order_id,
        "rejected_reason": outcome,
    }

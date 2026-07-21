"""Single-order execution lifecycle for live trading.

_execute_live is extracted from src.scripts.run (issue #210) to keep run.py
focused on the top-level polling loop.  It is imported back into run.py so
poll_once() can call it with no change to callers.

Issue #743: after a GTC fill timeout + successful cancel, one in-process
reprice-retry is attempted -- re-fetch the book, re-validate the candidate at
the new price against the UNCHANGED entry gates, and place one fresh GTC. The
retry is in-process only, so it never weakens the same-day entry guard that
blocks re-entry on later polls.
"""
import logging
import time

from src.config import (
    POSITION_SIZE_EUR, SIZING_MODE, get_live_config, CONFIG_DEFAULTS,
    MIN_EDGE_CENTS, MAX_EDGE_CENTS, MIN_PRICE_CENTS, MIN_CONFIDENCE_YES,
    MAX_CONFIDENCE_YES_FOR_NO, LIVE_TIMEOUT_REPRICE_RETRY,
)
from src.data.polymarket import get_orderbook
from src.execution.live_trader import LiveTrader
from src.execution.order_manager import order_manager as _order_manager
from src.monitoring.alerts import AlertManager
from src.strategy.fee import estimate_fee_cents
from src.strategy.sizing import compute_position_size

log = logging.getLogger(__name__)

FILL_POLL_INTERVAL_S = 30
FILL_MAX_WAIT_S = 300  # 5 minutes, 10 attempts


def _reprice_from_book(candidate, token_id, db) -> "tuple[int, float] | None":
    """Re-validate the candidate at the current best ask (issue #743).

    Fetches the live book for the flagged side's token, takes the best ask as
    the new price, and re-checks the SAME entry gates the scanner uses
    (MIN/MAX_EDGE_CENTS, MIN_PRICE_CENTS, and the side's confidence gate), read
    live from config so they match production exactly -- gates are never
    loosened here. Returns ``(new_price_cents, new_edge_cents)`` if the
    candidate still qualifies at the new price, else ``None`` (do not retry).
    """
    try:
        book = get_orderbook(token_id)
    except Exception as e:
        log.info("  [live] reprice: book fetch failed (%s) -- not retrying", e)
        return None
    asks = (book or {}).get("asks") or []
    prices = [float(a["price"]) for a in asks if a.get("price") is not None]
    if not prices:
        log.info("  [live] reprice: no asks on book -- not retrying")
        return None
    new_price = max(1, min(99, round(min(prices) * 100)))
    fee = estimate_fee_cents(new_price)

    cfg = get_live_config(db) if db is not None else {}
    min_edge = float(cfg.get("MIN_EDGE_CENTS", MIN_EDGE_CENTS))
    max_edge = float(cfg.get("MAX_EDGE_CENTS", MAX_EDGE_CENTS))
    min_price = int(cfg.get("MIN_PRICE_CENTS", MIN_PRICE_CENTS))

    if candidate.side == "NO":
        new_ev = (1 - candidate.p_yes) * 100 - new_price - fee
        max_conf = float(cfg.get("MAX_CONFIDENCE_YES_FOR_NO", MAX_CONFIDENCE_YES_FOR_NO))
        passes = (
            new_ev >= min_edge and new_ev <= max_edge
            and new_price >= min_price and candidate.p_yes <= max_conf
        )
    else:  # YES
        new_ev = candidate.p_yes * 100 - new_price - fee
        min_conf = float(cfg.get("MIN_CONFIDENCE_YES", MIN_CONFIDENCE_YES))
        passes = (
            new_ev >= min_edge and new_ev <= max_edge
            and new_price >= min_price and candidate.p_yes >= min_conf
        )

    if not passes:
        log.info(
            "  [live] reprice: new %s price %sc (ev=%.2f¢) fails unchanged gates -- not retrying",
            candidate.side, new_price, new_ev,
        )
        return None
    return new_price, round(new_ev, 2)


def _execute_live(
    candidate,
    clob_client_factory,
    risk_manager,
    ts: str,
    db=None,
    bankroll: float = 0.0,
) -> "str | None":
    """Place one order and wait for fill/timeout. open_position() already called by caller.

    Each thread creates its own LiveTrader/ClobClient to avoid HTTP/2 stream
    collisions when multiple orders are placed concurrently.

    Returns:
        The final per-attempt outcome string ('filled' | 'timeout' | 'cancelled'
        | 'place_failed'), or a short sentinel ('no_token_id' | 'already_open')
        when execution never reached ``_attempt``. Callers use this to resolve
        the scan_decisions verdict seam (issue #756) -- run.py maps 'filled' to
        'traded_live' and everything else to 'timeout_today' (with the raw
        outcome carried in gate_detail for diagnostics).
    """
    from src.scripts.run import _append_live_trade  # noqa: PLC0415

    trader = LiveTrader(clob_client_factory(), db)

    token_id = (
        candidate.bracket.yes_token_id if candidate.side == "YES"
        else candidate.bracket.no_token_id
    )
    if not token_id:
        log.info("  [live] no token_id for %s..., skipping", candidate.bracket.ticker[:16])
        risk_manager.close_position()
        return "no_token_id"

    order_key = token_id
    with _order_manager._open_orders_lock:
        if order_key in _order_manager._open_orders:
            log.info("  [live] skip %s %s... -- GTC order already open on exchange", candidate.side, candidate.bracket.ticker[:16])
            risk_manager.close_position()
            return "already_open"
        _order_manager._open_orders.add(order_key)

    predicted_price = round(candidate.confidence * 100)

    end_date = (candidate.market.get("endDate") or candidate.market.get("end_date_iso") or "")[:10]

    # Read sizing params live from DB so dashboard config changes take effect
    # without a restart. Fall back to module-level env-var constants if db is None
    # or if the DB returns a value that cannot be coerced to the expected type.
    retry_enabled = LIVE_TIMEOUT_REPRICE_RETRY
    if db is not None:
        live_cfg = get_live_config(db)
        try:
            live_position_size = float(live_cfg["POSITION_SIZE_EUR"])
            if live_position_size <= 0:
                raise ValueError("POSITION_SIZE_EUR must be positive")
        except (KeyError, TypeError, ValueError):
            live_position_size = POSITION_SIZE_EUR
        raw_mode = live_cfg.get("SIZING_MODE")
        live_sizing_mode = raw_mode if raw_mode in ("flat", "kelly") else SIZING_MODE
        retry_enabled = bool(live_cfg.get(
            "LIVE_TIMEOUT_REPRICE_RETRY", CONFIG_DEFAULTS["LIVE_TIMEOUT_REPRICE_RETRY"]
        ))
    else:
        live_position_size = POSITION_SIZE_EUR
        live_sizing_mode = SIZING_MODE

    fee_cents = estimate_fee_cents(candidate.price_cents)
    size_eur = compute_position_size(
        p_win=candidate.confidence,
        price_cents=float(candidate.price_cents),
        fee_cents=fee_cents,
        bankroll=bankroll,
        sizing_mode=live_sizing_mode,
        flat_size=live_position_size,
    )
    log.info(
        "  [sizing] mode=%s p_win=%.3f price=%sc fee=%.2fc bankroll=%.2f -> size=%.2f EUR",
        live_sizing_mode, candidate.confidence, candidate.price_cents, fee_cents, bankroll, size_eur,
    )

    def _attempt(price_cents: int, edge_cents: float) -> "tuple[str | None, str, bool]":
        """Place one GTC order at *price_cents*, wait for fill/timeout/cancel,
        cancel on timeout, and record the row. Returns (order_id, outcome, cancel_ok).
        outcome is one of 'filled' | 'timeout' | 'cancelled' | 'place_failed'."""
        with _order_manager._order_lock:  # Serialize HTTP/2 placements; fill-monitoring remains parallel
            try:
                order_id = trader.place_order(
                    token_id=token_id,
                    side=candidate.side,
                    price_cents=price_cents,
                    size_usdc=size_eur,
                    station=candidate.station,
                    bracket_low=candidate.bracket.low_f,
                    bracket_high=candidate.bracket.high_f,
                    predicted_price=predicted_price,
                    predicted_edge=round(edge_cents, 2),
                    p_yes_raw=candidate.p_yes_raw,
                    end_date=end_date,
                )
            except Exception as e:
                log.error("  [live] place_order failed: %s", e, exc_info=True)
                return None, "place_failed", True

        log.info("  [live] placed %s... %s @ %sc", order_id[:12], candidate.side, price_cents)

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
                    "  [live] CRITICAL: Failed to cancel GTC order %s after timeout: %s. "
                    "Order is still live on exchange and may fill as a ghost trade.",
                    order_id[:12], e, exc_info=True,
                )
                alert_manager = AlertManager()
                alert_manager._fire(
                    alert_key="gtc_cancel_failure",
                    subject=f"CRITICAL: GTC order cancellation failed for {order_id[:12]}",
                    body=(
                        f"Order {order_id} reached timeout but cancellation failed with error: {e}\n"
                        f"This order is still live on the Polymarket exchange and may fill "
                        f"as a ghost trade long after we stop tracking it.\n"
                        "Immediate investigation required."
                    ),
                )

        _append_live_trade({
            "ts": ts,
            "order_id": order_id,
            "station": candidate.station,
            "question": candidate.market.get("question") or candidate.market.get("groupItemTitle") or "",
            "end_date": end_date,
            "ticker": candidate.bracket.ticker,
            "asset_id": token_id,
            "no_token_id": token_id if candidate.side == "NO" else candidate.bracket.no_token_id,
            "bracket_low": candidate.bracket.low_f,
            "bracket_high": candidate.bracket.high_f,
            "side": candidate.side,
            "price_cents": price_cents,
            "predicted_price": predicted_price,
            "size_eur": size_eur,
            "sizing_mode": SIZING_MODE,
            "edge_cents": round(edge_cents, 2),
            "p_yes_raw": candidate.p_yes_raw,
            "outcome": outcome,
        }, db=db)
        log.info("  [live] %s %s...", outcome, order_id[:12])
        return order_id, outcome, cancel_ok

    try:
        order_id, outcome, cancel_ok = _attempt(candidate.price_cents, candidate.edge_cents)
        if outcome == "place_failed":
            risk_manager.close_position()
            return outcome

        # Issue #743: exactly one reprice-retry after a timeout that was cleanly
        # cancelled. In-process only -- the same-day entry guard (has_live_trade_today)
        # is not touched, so later polls still see the timeout row and stay blocked.
        if outcome == "timeout" and cancel_ok and retry_enabled:
            reprice = _reprice_from_book(candidate, token_id, db)
            if reprice is not None:
                new_price, new_edge = reprice
                log.info(
                    "  [live] reprice-retry: re-placing %s %s... at %sc (was %sc)",
                    candidate.side, candidate.bracket.ticker[:16], new_price, candidate.price_cents,
                )
                _oid2, outcome2, _cancel_ok2 = _attempt(new_price, new_edge)
                if outcome2 != "place_failed":
                    outcome = outcome2  # the retry is the final outcome for this bracket

        risk_manager.close_position()
        if outcome == "filled":
            risk_manager.record_pnl(0.0)  # Actual PnL resolved at settlement
        return outcome
    finally:
        with _order_manager._open_orders_lock:
            _order_manager._open_orders.discard(order_key)

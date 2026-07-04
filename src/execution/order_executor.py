"""Single-order execution lifecycle for live trading.

_execute_live is extracted from src.scripts.run (issue #210) to keep run.py
focused on the top-level polling loop.  It is imported back into run.py so
poll_once() can call it with no change to callers.
"""
import logging
import time

from src.config import POSITION_SIZE_EUR, SIZING_MODE
from src.execution.live_trader import LiveTrader
from src.execution.order_manager import order_manager as _order_manager
from src.monitoring.alerts import AlertManager
from src.strategy.fee import estimate_fee_cents
from src.strategy.sizing import compute_position_size

log = logging.getLogger(__name__)

FILL_POLL_INTERVAL_S = 30
FILL_MAX_WAIT_S = 300  # 5 minutes, 10 attempts


def _execute_live(
    candidate,
    clob_client_factory,
    risk_manager,
    ts: str,
    db=None,
    bankroll: float = 0.0,
) -> None:
    """Place one order and wait for fill/timeout. open_position() already called by caller.

    Each thread creates its own LiveTrader/ClobClient to avoid HTTP/2 stream
    collisions when multiple orders are placed concurrently.
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
        return

    order_key = token_id
    with _order_manager._open_orders_lock:
        if order_key in _order_manager._open_orders:
            log.info("  [live] skip %s %s... -- GTC order already open on exchange", candidate.side, candidate.bracket.ticker[:16])
            risk_manager.close_position()
            return
        _order_manager._open_orders.add(order_key)

    predicted_price = round(candidate.confidence * 100)

    end_date = (candidate.market.get("endDate") or candidate.market.get("end_date_iso") or "")[:10]

    fee_cents = estimate_fee_cents(candidate.price_cents)
    size_eur = compute_position_size(
        p_win=candidate.confidence,
        price_cents=float(candidate.price_cents),
        fee_cents=fee_cents,
        bankroll=bankroll,
        sizing_mode=SIZING_MODE,
        flat_size=POSITION_SIZE_EUR,
    )
    log.info(
        "  [sizing] mode=%s p_win=%.3f price=%sc fee=%.2fc bankroll=%.2f -> size=%.2f EUR",
        SIZING_MODE, candidate.confidence, candidate.price_cents, fee_cents, bankroll, size_eur,
    )

    try:
        with _order_manager._order_lock:  # Serialize HTTP/2 placements; fill-monitoring remains parallel
            try:
                order_id = trader.place_order(
                    token_id=token_id,
                    side=candidate.side,
                    price_cents=candidate.price_cents,
                    size_usdc=size_eur,
                    station=candidate.station,
                    bracket_low=candidate.bracket.low_f,
                    bracket_high=candidate.bracket.high_f,
                    predicted_price=predicted_price,
                    predicted_edge=round(candidate.edge_cents, 2),
                    p_yes_raw=candidate.p_yes_raw,
                    end_date=end_date,
                )
            except Exception as e:
                log.error("  [live] place_order failed: %s", e, exc_info=True)
                risk_manager.close_position()
                return

        log.info("  [live] placed %s... %s @ %sc", order_id[:12], candidate.side, candidate.price_cents)

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

        if outcome == "timeout":
            try:
                trader.cancel_order(order_id)
            except Exception as e:
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

        risk_manager.close_position()
        if outcome == "filled":
            risk_manager.record_pnl(0.0)  # Actual PnL resolved at settlement

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
            "price_cents": candidate.price_cents,
            "predicted_price": predicted_price,
            "size_eur": size_eur,
            "sizing_mode": SIZING_MODE,
            "edge_cents": round(candidate.edge_cents, 2),
            "p_yes_raw": candidate.p_yes_raw,
            "outcome": outcome,
        }, db=db)
        log.info("  [live] %s %s...", outcome, order_id[:12])
    finally:
        with _order_manager._open_orders_lock:
            _order_manager._open_orders.discard(order_key)

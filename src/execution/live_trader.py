"""Live order execution via Polymarket CLOB."""
import logging
import math
from datetime import datetime

log = logging.getLogger(__name__)
from typing import Literal

from py_clob_client_v2 import ClobClient
from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams, CreateOrderOptions, OrderArgs, OrderPayload

from src.data.polymarket import get_orderbook


class LiveTrader:
    def __init__(self, client: ClobClient, db=None):
        self.client = client
        self._db = db

    def get_usdc_balance(self) -> float:
        """Return available USDC in the CLOB (internal balance, not on-chain)."""
        bal = self.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        return int(bal["balance"]) / 1e6

    def place_order(
        self,
        token_id: str,
        side: str,          # "YES" or "NO" -- we always BUY the token
        price_cents: int,   # e.g. 72 -> 0.72 USDC per contract
        size_usdc: float,   # e.g. 5.0 -> spend up to 5 (denominated in USDC)
        *,
        station: str = "",
        bracket_low: float = 0.0,
        bracket_high: float = 0.0,
        predicted_price: int = 0,
        predicted_edge: float = 0.0,
    ) -> str:
        """Place a GTC limit order. Returns order_id string."""
        price = round(price_cents / 100, 4)
        size = round(size_usdc / price, 2)  # contracts = USDC / price_per_contract
        args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side="BUY",  # Always BUY YES or NO tokens -- never short
        )
        # Weather markets on Polymarket are consistently neg_risk=True, tick_size=0.01
        options = CreateOrderOptions(tick_size="0.01", neg_risk=True)
        resp = self.client.create_and_post_order(args, options)
        order_id = resp.get("orderID") or resp.get("id")
        if not order_id:
            raise RuntimeError(f"Order placement failed: {resp}")

        if self._db is not None:
            try:
                now_utc = datetime.utcnow().isoformat() + "Z"
                trade_id = self._db.insert_trade(
                    ts=now_utc,
                    station=station,
                    ticker=f"{station}-order-{order_id[:8]}",
                    bracket_low=bracket_low,
                    bracket_high=bracket_high,
                    side=side,
                    predicted_price=predicted_price,
                    actual_price=price_cents,
                    predicted_edge=predicted_edge,
                    mode="live",
                    order_id=order_id,
                    capital_before=size_usdc,
                )
                self._db.open_position(
                    trade_id=trade_id,
                    station=station,
                    ticker=f"{station}-order-{order_id[:8]}",
                    token_id=token_id,
                    side=side,
                    order_id=order_id,
                    entry_price=price_cents,
                    shares=size,
                    entry_ts=now_utc,
                )
            except Exception as db_err:
                logging.critical(
                    "[live] CRITICAL: CLOB order %s placed but DB write failed: %s. "
                    "Position details: token_id=%s side=%s price=%sc size=%s",
                    order_id, db_err, token_id, side, price_cents, size,
                )

        return order_id

    def sell_position(self, token_id: str, shares: float) -> tuple[str, int]:
        """Sell NO tokens at the current best bid price.

        Used for METAR-triggered stop-loss exits when the running daily high
        has moved inside the bracket. Raises RuntimeError if no bids exist.
        Returns (order_id, sell_price_cents).
        """
        ob = get_orderbook(token_id)
        bids = ob.get("bids") or []
        if not bids:
            raise RuntimeError(f"No bids for token {token_id[:14]}... -- cannot sell")
        best_bid = max(float(b["price"]) for b in bids)
        sell_price_cents = max(1, min(99, round(best_bid * 100)))
        # Floor (don't round) so submitted size never exceeds available balance.
        # round(6.325713, 2) == 6.33 > wallet 6.325713 -> "invalid maker amount".
        # Floor to 6.32 stays safely under wallet balance.
        size_floored = math.floor(shares * 100) / 100
        args = OrderArgs(
            token_id=token_id,
            price=round(sell_price_cents / 100, 4),
            size=size_floored,
            side="SELL",
        )
        options = CreateOrderOptions(tick_size="0.01", neg_risk=True)
        resp = self.client.create_and_post_order(args, options)
        order_id = resp.get("orderID") or resp.get("id")
        if not order_id:
            raise RuntimeError(f"Sell order failed: {resp}")
        return order_id, sell_price_cents

    def get_order_fill_size(self, order_id: str) -> float:
        """Return the shares matched/filled for *order_id* so far (0.0 on error).

        Used after a cancelled IOC sell to detect partial fills before the
        cancel completed.  Never raises.
        """
        try:
            order = self.client.get_order(order_id)
            for field in ("size_matched", "matched_amount", "filled_size", "size_filled"):
                val = order.get(field)
                if val is not None:
                    return float(val)
            return 0.0
        except Exception as e:
            log.warning("[live] get_order_fill_size %s... error: %s", order_id[:12], e)
            return 0.0

    def sell_position_immediate(
        self, token_id: str, shares: float, aggression_cents: int = 2,
    ) -> "tuple[str, int] | tuple[None, str | None]":
        """Sell NO tokens immediately or not at all — never leaves a resting order.

        Prices the limit *through* the best bid by `aggression_cents` so the
        order still crosses if the top of book ticks down between the orderbook
        fetch and the post (it fills at the resting bids' prices, the limit is
        only a floor). If the order does not match, it is cancelled so a
        falling market can't strand us behind an unfillable resting sell.

        Returns:
          (order_id, limit_price_cents) on fill — actual proceeds are at least
          the limit price.
          (None, order_id) when the order was cancelled (may have partially
          filled) — caller should query get_order_fill_size(order_id) for
          partial fill tracking, then retry on a later poll.
          (None, None) when no order_id is available.
        """
        ob = get_orderbook(token_id)
        bids = ob.get("bids") or []
        if not bids:
            raise RuntimeError(f"No bids for token {token_id[:14]}... -- cannot sell")
        best_bid = max(float(b["price"]) for b in bids)
        best_bid_cents = max(1, min(99, round(best_bid * 100)))
        limit_cents = max(1, best_bid_cents - aggression_cents)
        # See sell_position note: floor to never exceed available balance.
        size_floored = math.floor(shares * 100) / 100
        args = OrderArgs(
            token_id=token_id,
            price=round(limit_cents / 100, 4),
            size=size_floored,
            side="SELL",
        )
        options = CreateOrderOptions(tick_size="0.01", neg_risk=True)
        resp = self.client.create_and_post_order(args, options)
        order_id = resp.get("orderID") or resp.get("id")
        if not order_id:
            raise RuntimeError(f"Sell order failed: {resp}")
        status = (resp.get("status") or "").lower()
        if status in ("matched", "filled") or self.check_fill(order_id) == "filled":
            return order_id, limit_cents
        if self.cancel_order(order_id):
            log.info("[live] sell %s... not matched at %sc -- cancelled, will retry",
                     order_id[:12], limit_cents)
            return None, order_id
        # cancel_order() returned False — ambiguous: either the cancel API errored
        # (network blip, order still resting) or the order matched in-flight and the
        # exchange refused the cancel.  Verify via check_fill() before recording sold.
        fill_status = self.check_fill(order_id)
        if fill_status == "filled":
            return order_id, limit_cents
        log.warning(
            "[live] sell %s... cancel returned False but order not confirmed filled "
            "(status=%s) -- leaving position intact for next poll",
            order_id[:12], fill_status,
        )
        return None, order_id

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True if cancelled.

        Uses py_clob_client_v2's cancel_order API which raises exceptions
        on failure (network errors, unauthorized, etc.).
        """
        payload = OrderPayload(orderID=order_id)
        resp = self.client.cancel_order(payload)
        # If cancel_order() doesn't raise, treat as successful
        # Response may be None or a dict depending on exchange response
        cancelled = resp is not None and not resp.get("error")
        if cancelled and self._db is not None:
            self._db.close_position(order_id)
        return cancelled

    def check_fill(self, order_id: str) -> Literal["open", "filled", "cancelled"]:
        """Return current status of an order. Never raises."""
        try:
            order = self.client.get_order(order_id)
            status = (order.get("status") or "").lower()
            if status in ("matched", "filled"):
                return "filled"
            if status in ("cancelled", "canceled"):
                return "cancelled"
            return "open"
        except Exception as e:
            log.warning("[live] check_fill %s... error: %s", order_id[:12], e)
            return "open"  # Assume still open on error -- will retry

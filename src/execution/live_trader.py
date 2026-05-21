"""Live order execution via Polymarket CLOB."""
from typing import Literal

from py_clob_client_v2 import ClobClient
from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams, CreateOrderOptions, OrderArgs

from src.data.polymarket import get_orderbook


class LiveTrader:
    def __init__(self, client: ClobClient):
        self.client = client

    def get_usdc_balance(self) -> float:
        """Return available USDC in the CLOB (internal balance, not on-chain)."""
        bal = self.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        return int(bal["balance"]) / 1e6

    def place_order(
        self,
        token_id: str,
        side: str,          # "YES" or "NO" — we always BUY the token
        price_cents: int,   # e.g. 72 → 0.72 USDC per contract
        size_usdc: float,   # e.g. 5.0 → spend up to €5 (denominated in USDC)
    ) -> str:
        """Place a GTC limit order. Returns order_id string."""
        price = round(price_cents / 100, 4)
        size = round(size_usdc / price, 2)  # contracts = USDC / price_per_contract
        args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side="BUY",  # Always BUY YES or NO tokens — never short
        )
        # Weather markets on Polymarket are consistently neg_risk=True, tick_size=0.01
        options = CreateOrderOptions(tick_size="0.01", neg_risk=True)
        resp = self.client.create_and_post_order(args, options)
        order_id = resp.get("orderID") or resp.get("id")
        if not order_id:
            raise RuntimeError(f"Order placement failed: {resp}")
        return order_id

    def sell_position(self, token_id: str, shares: float) -> str:
        """Sell NO tokens at the current best bid price.

        Used for METAR-triggered stop-loss exits when the running daily high
        has moved inside the bracket. Raises RuntimeError if no bids exist.
        """
        ob = get_orderbook(token_id)
        bids = ob.get("bids") or []
        if not bids:
            raise RuntimeError(f"No bids for token {token_id[:14]}… — cannot sell")
        best_bid = max(float(b["price"]) for b in bids)
        args = OrderArgs(
            token_id=token_id,
            price=round(best_bid, 4),
            size=round(shares, 2),
            side="SELL",
        )
        options = CreateOrderOptions(tick_size="0.01", neg_risk=True)
        resp = self.client.create_and_post_order(args, options)
        order_id = resp.get("orderID") or resp.get("id")
        if not order_id:
            raise RuntimeError(f"Sell order failed: {resp}")
        return order_id

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True if cancelled."""
        try:
            resp = self.client.cancel(order_id)
            return resp.get("canceled") == [order_id]
        except Exception as e:
            print(f"[live] cancel {order_id[:12]}… error: {e}")
            return False

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
            print(f"[live] check_fill {order_id[:12]}… error: {e}")
            return "open"  # Assume still open on error — will retry

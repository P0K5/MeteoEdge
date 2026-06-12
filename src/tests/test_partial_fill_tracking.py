"""Tests for stop-loss IOC partial fill tracking (#204)."""
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch, call

import pytest

# Stub py_clob_client_v2 for the test environment.
_clob_stub = ModuleType("py_clob_client_v2")
_clob_stub.ClobClient = MagicMock  # type: ignore[attr-defined]
_clob_types_stub = ModuleType("py_clob_client_v2.clob_types")
for _name in ("AssetType", "BalanceAllowanceParams", "CreateOrderOptions", "OrderArgs"):
    setattr(_clob_types_stub, _name, MagicMock)
sys.modules.setdefault("py_clob_client_v2", _clob_stub)
sys.modules.setdefault("py_clob_client_v2.clob_types", _clob_types_stub)

from src.execution.live_trader import LiveTrader


def _make_trader():
    mock_client = MagicMock()
    return LiveTrader(mock_client, db=None)


def _mock_orderbook():
    return {"bids": [{"price": "0.70", "size": "100"}]}


class TestGetOrderFillSize:
    """LiveTrader.get_order_fill_size() should return filled shares, never raise."""

    def test_returns_size_matched_field(self):
        trader = _make_trader()
        trader.client.get_order.return_value = {"status": "live", "size_matched": "3.5"}
        assert trader.get_order_fill_size("ord-001") == pytest.approx(3.5)

    def test_returns_zero_on_api_error(self):
        trader = _make_trader()
        trader.client.get_order.side_effect = Exception("timeout")
        assert trader.get_order_fill_size("ord-002") == 0.0

    def test_returns_zero_when_no_fill_field(self):
        trader = _make_trader()
        trader.client.get_order.return_value = {"status": "live"}
        assert trader.get_order_fill_size("ord-003") == 0.0


class TestSellPositionImmediateCancelReturnsOrderId:
    """sell_position_immediate() should return (None, order_id) on cancel so caller can track partial fills."""

    def test_clean_cancel_returns_none_and_order_id(self):
        trader = _make_trader()
        trader.client.create_and_post_order.return_value = {
            "orderID": "sell-ord-cancel", "status": "live"
        }
        trader.client.cancel.return_value = {"canceled": ["sell-ord-cancel"]}

        with patch("src.execution.live_trader.get_orderbook", return_value=_mock_orderbook()):
            result = trader.sell_position_immediate("tok-x", shares=10.0)

        sell_id, extra = result
        assert sell_id is None
        assert extra == "sell-ord-cancel"

    def test_fill_returns_order_id_and_price(self):
        trader = _make_trader()
        trader.client.create_and_post_order.return_value = {
            "orderID": "sell-ord-fill", "status": "matched"
        }

        with patch("src.execution.live_trader.get_orderbook", return_value=_mock_orderbook()):
            result = trader.sell_position_immediate("tok-y", shares=10.0)

        sell_id, price = result
        assert sell_id == "sell-ord-fill"
        assert isinstance(price, int)


class TestCheckStopLossPartialFillTracking:
    """_check_stop_loss_exits should track partial fills and sell only the remainder."""

    def _make_position_state(self, token_id="tok-001", fair=30, bid=65, depth=50,
                              price_cents=70, size_eur=5.0):
        fills = [{
            "station": "KORD",
            "bracket_low": 75.0,
            "bracket_high": 77.0,
            "price_cents": price_cents,
            "size_eur": size_eur,
            "question": "Will KORD hit 75-77?",
            "ticker": "KORD-HIGH-75-77",
            "order_id": "buy-ord-001",
            "end_date": "2026-06-12",
        }]
        snap = {
            "fair_value_now": fair,
            "no_best_bid": bid,
            "no_best_bid_size": depth,
        }
        return {"token_id": token_id, "fills": fills, "snap": snap}

    def test_partial_fill_then_retry_sells_remainder(self):
        """After 5-share partial fill, retry should sell only (total - 5) shares."""
        import src.scripts.run as run_mod
        run_mod.order_manager._stop_loss_strikes.clear()
        run_mod.order_manager._partial_fill_shares.clear()
        run_mod.order_manager._sold_positions.clear()

        from src.config import STOP_LOSS_CONSECUTIVE_POLLS
        token_id = "tok-partial"

        # Prime strikes to trigger level
        run_mod.order_manager._stop_loss_strikes[token_id] = STOP_LOSS_CONSECUTIVE_POLLS

        ps = self._make_position_state(token_id=token_id, fair=30, bid=65, depth=50,
                                       price_cents=70, size_eur=5.0)
        total_shares = ps["fills"][0]["size_eur"] / (ps["fills"][0]["price_cents"] / 100)
        # ~7.14 shares

        trader = MagicMock()
        # First call: partial fill (cancel returns order_id, fill_size=5.0)
        trader.sell_position_immediate.return_value = (None, "sell-ord-partial")
        trader.get_order_fill_size.return_value = 5.0

        with patch("src.scripts.run.STOP_LOSS_MIN_LOT_SHARES", 0.5, create=True):
            run_mod._check_stop_loss_exits(trader, "2026-06-12T10:00:00Z", [ps])

        # Partial fill tracked
        assert run_mod.order_manager._partial_fill_shares.get(token_id, 0.0) == pytest.approx(5.0)
        # Position NOT marked sold yet
        assert token_id not in run_mod.order_manager._sold_positions

        # Second call: sell the remainder
        remaining = total_shares - 5.0
        trader.sell_position_immediate.return_value = ("sell-ord-fill", 65)
        run_mod.order_manager._stop_loss_strikes[token_id] = STOP_LOSS_CONSECUTIVE_POLLS

        with patch("src.scripts.run.STOP_LOSS_MIN_LOT_SHARES", 0.5, create=True), \
             patch("src.scripts.run._append_live_trade"), \
             patch("src.scripts.run._record_sell_in_db"):
            run_mod._check_stop_loss_exits(trader, "2026-06-12T10:01:00Z", [ps])

        # Should have called sell with remaining shares (not the full total)
        last_call = trader.sell_position_immediate.call_args
        assert last_call[0][1] == pytest.approx(remaining, abs=0.01)
        assert token_id in run_mod.order_manager._sold_positions

    def test_remaining_below_min_lot_skips_dust_sell(self):
        """Remaining shares below min lot after partial fill → skip, mark sold."""
        import src.scripts.run as run_mod
        run_mod.order_manager._stop_loss_strikes.clear()
        run_mod.order_manager._partial_fill_shares.clear()
        run_mod.order_manager._sold_positions.clear()

        from src.config import STOP_LOSS_CONSECUTIVE_POLLS
        token_id = "tok-dust"

        run_mod.order_manager._stop_loss_strikes[token_id] = STOP_LOSS_CONSECUTIVE_POLLS
        # Simulate 7.0 of 7.14 shares already sold via partial fill
        run_mod.order_manager._partial_fill_shares[token_id] = 7.0

        ps = self._make_position_state(token_id=token_id, fair=30, bid=65, depth=50,
                                       price_cents=70, size_eur=5.0)
        # remaining = 7.14 - 7.0 = 0.14, below min lot of 0.5

        trader = MagicMock()
        db_mock = MagicMock()
        db_mock.close_positions_by_token.return_value = 1

        run_mod._check_stop_loss_exits(trader, "2026-06-12T10:00:00Z", [ps], db=db_mock)

        # sell_position_immediate should NOT be called for dust
        trader.sell_position_immediate.assert_not_called()
        # Position should be marked sold (dust cleared)
        assert token_id in run_mod.order_manager._sold_positions
        assert token_id not in run_mod.order_manager._partial_fill_shares

    def test_full_fill_path_unaffected(self):
        """Full fill on first attempt: single clean record, _partial_fill_shares not touched."""
        import src.scripts.run as run_mod
        run_mod.order_manager._stop_loss_strikes.clear()
        run_mod.order_manager._partial_fill_shares.clear()
        run_mod.order_manager._sold_positions.clear()

        from src.config import STOP_LOSS_CONSECUTIVE_POLLS
        token_id = "tok-full-fill"
        run_mod.order_manager._stop_loss_strikes[token_id] = STOP_LOSS_CONSECUTIVE_POLLS

        ps = self._make_position_state(token_id=token_id, fair=30, bid=65, depth=50)
        trader = MagicMock()
        trader.sell_position_immediate.return_value = ("sell-ord-full", 65)

        with patch("src.scripts.run._append_live_trade"), \
             patch("src.scripts.run._record_sell_in_db"):
            run_mod._check_stop_loss_exits(trader, "2026-06-12T10:00:00Z", [ps])

        assert token_id in run_mod.order_manager._sold_positions
        # No partial fill tracking for a clean fill
        assert token_id not in run_mod.order_manager._partial_fill_shares

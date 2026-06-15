"""Tests for sell_position_immediate cancel-error vs cancel-refused distinction (#203)."""
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# Stub py_clob_client_v2 for the test environment.
_clob_stub = ModuleType("py_clob_client_v2")
_clob_stub.ClobClient = MagicMock  # type: ignore[attr-defined]
_clob_types_stub = ModuleType("py_clob_client_v2.clob_types")
for _name in ("AssetType", "BalanceAllowanceParams", "CreateOrderOptions", "OrderArgs", "OrderPayload"):
    setattr(_clob_types_stub, _name, MagicMock)
sys.modules.setdefault("py_clob_client_v2", _clob_stub)
sys.modules.setdefault("py_clob_client_v2.clob_types", _clob_types_stub)

from src.execution.live_trader import LiveTrader


def _make_trader():
    mock_client = MagicMock()
    return LiveTrader(mock_client, db=None)


def _mock_orderbook():
    return {"bids": [{"price": "0.70", "size": "100"}]}


class TestSellPositionImmediateCancelPaths:
    """#203 — cancel_order False must not mark position sold without fill confirmation."""

    def test_cancel_api_errors_order_still_live_raises(self):
        """cancel_order() raises (network error) → exception propagates to caller."""
        trader = _make_trader()
        # Order placed OK
        trader.client.create_and_post_order.return_value = {
            "orderID": "sell-ord-001", "status": "live"
        }
        # v2 API: cancel_order() now raises on errors (no try-catch in live_trader.py)
        trader.client.cancel_order.side_effect = Exception("connection reset")

        with patch("src.execution.live_trader.get_orderbook", return_value=_mock_orderbook()):
            with pytest.raises(Exception, match="connection reset"):
                trader.sell_position_immediate("tok-001", shares=10.0)

    def test_cancel_returns_false_check_fill_confirms_filled_returns_order_id(self):
        """cancel_order() returns False (error response) + check_fill confirms filled → returns (order_id, price)."""
        trader = _make_trader()
        trader.client.create_and_post_order.return_value = {
            "orderID": "sell-ord-002", "status": "live"
        }
        # v2 API: cancel_order returns False when response has error field
        trader.client.cancel_order.return_value = {"error": "order already filled"}
        # check_fill confirms it is filled
        trader.client.get_order.return_value = {"status": "matched"}

        with patch("src.execution.live_trader.get_orderbook", return_value=_mock_orderbook()):
            result = trader.sell_position_immediate("tok-002", shares=10.0)

        assert result is not None
        order_id, price_cents = result
        assert order_id == "sell-ord-002"
        assert isinstance(price_cents, int)

    def test_cancel_returns_false_check_fill_open_returns_none(self):
        """cancel_order() returns False + check_fill shows open → sell_id is None, not sold."""
        trader = _make_trader()
        trader.client.create_and_post_order.return_value = {
            "orderID": "sell-ord-003", "status": "live"
        }
        # v2 API: cancel_order returns False when response has error field
        trader.client.cancel_order.return_value = {"error": "cancel failed"}
        trader.client.get_order.return_value = {"status": "open"}

        with patch("src.execution.live_trader.get_orderbook", return_value=_mock_orderbook()):
            result = trader.sell_position_immediate("tok-003", shares=10.0)

        sell_id, _ = result
        assert sell_id is None

    def test_happy_path_immediate_fill_no_cancel_attempted(self):
        """Happy path: order fills immediately → returns (order_id, price), cancel never called."""
        trader = _make_trader()
        trader.client.create_and_post_order.return_value = {
            "orderID": "sell-ord-004", "status": "matched"
        }

        with patch("src.execution.live_trader.get_orderbook", return_value=_mock_orderbook()):
            result = trader.sell_position_immediate("tok-004", shares=10.0)

        assert result is not None
        order_id, price_cents = result
        assert order_id == "sell-ord-004"
        # v2 API: cancel was never called since status=matched was detected from resp
        trader.client.cancel_order.assert_not_called()

    def test_cancel_succeeds_returns_none_for_retry(self):
        """Happy path: cancel succeeds → returns None for caller to retry next poll."""
        trader = _make_trader()
        trader.client.create_and_post_order.return_value = {
            "orderID": "sell-ord-005", "status": "live"
        }
        # v2 API: cancel_order returns empty/success response on success
        trader.client.cancel_order.return_value = {}

        with patch("src.execution.live_trader.get_orderbook", return_value=_mock_orderbook()):
            result = trader.sell_position_immediate("tok-005", shares=10.0)

        sell_id, cancelled_order_id = result
        assert sell_id is None
        assert cancelled_order_id == "sell-ord-005"

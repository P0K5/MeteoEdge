"""Tests for sell_position_immediate cancel-error vs cancel-refused distinction (#203)."""
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

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


class TestSellPositionImmediateCancelPaths:
    """#203 — cancel_order False must not mark position sold without fill confirmation."""

    def test_cancel_api_errors_order_still_live_returns_none(self):
        """cancel_order() raises (network error) → check_fill shows 'open' → returns None."""
        trader = _make_trader()
        # Order placed OK
        trader.client.create_and_post_order.return_value = {
            "orderID": "sell-ord-001", "status": "live"
        }
        # cancel raises an exception (network blip) → cancel_order() catches and returns False
        trader.client.cancel.side_effect = Exception("connection reset")
        # check_fill shows the order is still open (not filled)
        trader.client.get_order.return_value = {"status": "live"}

        with patch("src.execution.live_trader.get_orderbook", return_value=_mock_orderbook()):
            result = trader.sell_position_immediate("tok-001", shares=10.0)

        assert result is None, (
            "When cancel errors AND order is not filled, position must NOT be marked sold"
        )

    def test_cancel_returns_false_check_fill_confirms_filled_returns_order_id(self):
        """cancel_order() returns False + check_fill confirms filled → returns (order_id, price)."""
        trader = _make_trader()
        trader.client.create_and_post_order.return_value = {
            "orderID": "sell-ord-002", "status": "live"
        }
        # cancel returns False (exchange refused cancel — order matched in-flight)
        trader.client.cancel.return_value = {"canceled": []}  # empty → False
        # check_fill confirms it is filled
        trader.client.get_order.return_value = {"status": "matched"}

        with patch("src.execution.live_trader.get_orderbook", return_value=_mock_orderbook()):
            result = trader.sell_position_immediate("tok-002", shares=10.0)

        assert result is not None
        order_id, price_cents = result
        assert order_id == "sell-ord-002"
        assert isinstance(price_cents, int)

    def test_cancel_returns_false_check_fill_open_returns_none(self):
        """cancel_order() returns False + check_fill shows open → returns None, not sold."""
        trader = _make_trader()
        trader.client.create_and_post_order.return_value = {
            "orderID": "sell-ord-003", "status": "live"
        }
        trader.client.cancel.return_value = {"canceled": []}
        trader.client.get_order.return_value = {"status": "open"}

        with patch("src.execution.live_trader.get_orderbook", return_value=_mock_orderbook()):
            result = trader.sell_position_immediate("tok-003", shares=10.0)

        assert result is None

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
        # cancel was never called since status=matched was detected from resp
        trader.client.cancel.assert_not_called()

    def test_cancel_succeeds_returns_none_for_retry(self):
        """Happy path: cancel succeeds → returns None for caller to retry next poll."""
        trader = _make_trader()
        trader.client.create_and_post_order.return_value = {
            "orderID": "sell-ord-005", "status": "live"
        }
        trader.client.cancel.return_value = {"canceled": ["sell-ord-005"]}

        with patch("src.execution.live_trader.get_orderbook", return_value=_mock_orderbook()):
            result = trader.sell_position_immediate("tok-005", shares=10.0)

        assert result is None
        # get_order called at most once (the pre-cancel status check on line 150),
        # not again for the cancel-false disambiguation path (which was not reached).
        assert trader.client.get_order.call_count <= 1

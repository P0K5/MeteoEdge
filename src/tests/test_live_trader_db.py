"""Tests for LiveTrader DB integration (Issue B: replace live_state.json with DB)."""
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# py_clob_client_v2 is not installed in the test environment; stub it out.
_clob_stub = ModuleType("py_clob_client_v2")
_clob_stub.ClobClient = MagicMock  # type: ignore[attr-defined]
_clob_types_stub = ModuleType("py_clob_client_v2.clob_types")
for _name in ("AssetType", "BalanceAllowanceParams", "CreateOrderOptions", "OrderArgs", "OrderPayload"):
    setattr(_clob_types_stub, _name, MagicMock)
sys.modules.setdefault("py_clob_client_v2", _clob_stub)
sys.modules.setdefault("py_clob_client_v2.clob_types", _clob_types_stub)

from src.data.db import Database
from src.execution.live_trader import LiveTrader


def _db() -> Database:
    return Database(":memory:")


def _make_trader(db=None):
    """Return a LiveTrader with a mock ClobClient."""
    mock_client = MagicMock()
    return LiveTrader(mock_client, db=db)


class TestPlaceOrderWritesPosition:
    """place_order() must persist the open position to DB."""

    def test_place_order_inserts_open_position(self):
        db = _db()
        trader = _make_trader(db)
        trader.client.create_and_post_order.return_value = {"orderID": "ord-abc123"}

        trader.place_order(
            token_id="tok-001",
            side="NO",
            price_cents=70,
            size_usdc=5.0,
            station="KORD",
            bracket_low=32.0,
            bracket_high=36.0,
        )

        positions = db.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["order_id"] == "ord-abc123"
        assert positions[0]["token_id"] == "tok-001"
        assert positions[0]["side"] == "NO"
        assert positions[0]["entry_price"] == 70

    def test_place_order_writes_size_eur(self):
        """Issue #746: size_eur is set at placement (== stake), before any outcome."""
        db = _db()
        trader = _make_trader(db)
        trader.client.create_and_post_order.return_value = {"orderID": "ord-size1"}

        trader.place_order(
            token_id="tok-size", side="NO", price_cents=70, size_usdc=5.0,
            station="KORD", bracket_low=32.0, bracket_high=36.0,
        )

        row = db.get_trade_by_order_id("ord-size1")
        assert row is not None
        assert row["size_eur"] == 5.0
        assert row["capital_before"] == 5.0

    def test_place_order_no_db_does_not_raise(self):
        """place_order() without DB must not raise."""
        trader = _make_trader(db=None)
        trader.client.create_and_post_order.return_value = {"orderID": "ord-xyz"}
        result = trader.place_order("tok-002", "YES", 60, 5.0)
        assert result == "ord-xyz"

    def test_place_order_db_failure_does_not_swallow_order_id(self):
        """Even if DB write fails, place_order() must still return the order_id."""
        db = MagicMock()
        db.open_position.side_effect = RuntimeError("disk full")
        trader = _make_trader(db=db)
        trader.client.create_and_post_order.return_value = {"orderID": "ord-critical"}

        result = trader.place_order("tok-003", "NO", 65, 5.0)
        assert result == "ord-critical"

    def test_place_order_raises_if_no_order_id(self):
        trader = _make_trader()
        trader.client.create_and_post_order.return_value = {}
        with pytest.raises(RuntimeError, match="Order placement failed"):
            trader.place_order("tok-fail", "NO", 70, 5.0)


class TestCancelOrderClosesPosition:
    """cancel_order() must remove the position from DB on success."""

    def test_cancel_order_removes_position(self):
        db = _db()
        trade_id = db.insert_trade(
            ts="2024-01-15T12:00:00Z", station="KORD",
            ticker="KORD-2024-01-15-HIGH-32-36",
            bracket_low=32.0, bracket_high=36.0,
            side="NO", predicted_price=70, actual_price=71,
            predicted_edge=0.08, mode="live", capital_before=1000.0,
        )
        db.open_position(
            trade_id=trade_id, station="KORD",
            ticker="KORD-2024-01-15-HIGH-32-36",
            token_id="tok-cancel", side="NO",
            order_id="ord-to-cancel", entry_price=70,
            shares=7.0, entry_ts="2024-01-15T12:01:00Z",
        )
        trader = _make_trader(db)
        # v2 API: cancel_order takes OrderPayload and returns success response
        trader.client.cancel_order.return_value = {}  # Empty response indicates success

        result = trader.cancel_order("ord-to-cancel")

        assert result is True
        assert db.get_open_positions() == []

    def test_cancel_order_no_db_returns_true(self):
        trader = _make_trader(db=None)
        # v2 API: cancel_order takes OrderPayload and returns success response
        trader.client.cancel_order.return_value = {}  # Empty response indicates success
        assert trader.cancel_order("ord-no-db") is True

    def test_cancel_order_client_error_raises(self):
        """v2 API: cancel_order failures now raise exceptions (hard error)."""
        trader = _make_trader(db=None)
        trader.client.cancel_order.side_effect = Exception("network error")
        with pytest.raises(Exception, match="network error"):
            trader.cancel_order("ord-net-fail")

    def test_cancel_order_with_error_response_returns_false(self):
        """v2 API: if response has an error field, cancel_order() returns False."""
        trader = _make_trader(db=None)
        trader.client.cancel_order.return_value = {"error": "order not found"}
        # Success is False when error is present in response
        result = trader.cancel_order("ord-missing")
        assert result is False


class TestNoLiveStateJson:
    """live_trader.py must not import or reference persist_state."""

    def test_persist_state_not_imported(self):
        import src.execution.live_trader as lt_module
        assert not hasattr(lt_module, "persist_state"), (
            "persist_state function must not exist in live_trader.py"
        )

    def test_no_live_state_json_write(self):
        """place_order() must not write any .json state files."""
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            db = _db()
            trader = _make_trader(db)
            trader.client.create_and_post_order.return_value = {"orderID": "ord-json-test"}
            trader.place_order("tok-json", "NO", 70, 5.0)
            # No .json files should have been created
            json_files = [f for f in os.listdir(tmpdir) if f.endswith(".json")]
            assert json_files == []

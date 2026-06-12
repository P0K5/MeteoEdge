"""Tests for the model-confidence stop-loss (issue #177).

Covers:
- LiveTrader.sell_position_immediate(): immediate-or-cancel semantics — never
  leaves a resting sell order on the book.
- run._check_stop_loss_exits(): strike counting, bid floor, depth guard, and
  retry-on-unfilled behaviour.
"""
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# py_clob_client_v2 is not installed in the test environment; stub it out.
_clob_stub = ModuleType("py_clob_client_v2")
_clob_stub.ClobClient = MagicMock  # type: ignore[attr-defined]
_clob_types_stub = ModuleType("py_clob_client_v2.clob_types")
for _name in ("AssetType", "BalanceAllowanceParams", "CreateOrderOptions", "OrderArgs"):
    setattr(_clob_types_stub, _name, MagicMock)
sys.modules.setdefault("py_clob_client_v2", _clob_stub)
sys.modules.setdefault("py_clob_client_v2.clob_types", _clob_types_stub)

from src.execution.live_trader import LiveTrader  # noqa: E402
import src.scripts.run as run  # noqa: E402


# ---------------------------------------------------------------------------
# LiveTrader.sell_position_immediate
# ---------------------------------------------------------------------------

def _trader() -> LiveTrader:
    return LiveTrader(MagicMock())


def _orderbook(bid_price: str, bid_size: str = "100"):
    return {"bids": [{"price": bid_price, "size": bid_size}], "asks": []}


class TestSellPositionImmediate:
    def test_matched_immediately_returns_order_and_limit(self):
        """Limit is priced through the bid (70c - 2c = 68c) and fill is reported."""
        trader = _trader()
        trader.client.create_and_post_order.return_value = {"orderID": "s-1", "status": "matched"}
        with patch("src.execution.live_trader.get_orderbook", return_value=_orderbook("0.70")):
            result = trader.sell_position_immediate("tok-a", 6.25, aggression_cents=2)
        assert result == ("s-1", 68)

    def test_unmatched_order_is_cancelled_and_returns_none(self):
        """If the order does not cross it must be cancelled — never left resting."""
        trader = _trader()
        trader.client.create_and_post_order.return_value = {"orderID": "s-2", "status": "live"}
        trader.client.get_order.return_value = {"status": "live"}
        trader.client.cancel.return_value = {"canceled": ["s-2"]}
        with patch("src.execution.live_trader.get_orderbook", return_value=_orderbook("0.70")):
            result = trader.sell_position_immediate("tok-b", 6.25)
        assert result is None
        trader.client.cancel.assert_called_once_with("s-2")

    def test_cancel_refused_means_filled_in_flight(self):
        """Cancel refused + check_fill confirms filled → treat as filled."""
        trader = _trader()
        trader.client.create_and_post_order.return_value = {"orderID": "s-3", "status": "live"}
        # check_fill called twice: pre-cancel returns open, post-cancel-refused returns matched
        trader.client.get_order.side_effect = [
            {"status": "live"},    # pre-cancel check → open, proceed to cancel
            {"status": "matched"}, # post-cancel-refused check → confirmed filled
        ]
        trader.client.cancel.return_value = {"canceled": []}
        with patch("src.execution.live_trader.get_orderbook", return_value=_orderbook("0.50")):
            result = trader.sell_position_immediate("tok-c", 6.25)
        assert result == ("s-3", 48)

    def test_limit_price_floors_at_one_cent(self):
        """Aggression below the 1c tick floor clamps to 1c, never 0 or negative."""
        trader = _trader()
        trader.client.create_and_post_order.return_value = {"orderID": "s-4", "status": "matched"}
        with patch("src.execution.live_trader.get_orderbook", return_value=_orderbook("0.02")):
            result = trader.sell_position_immediate("tok-d", 6.25, aggression_cents=5)
        assert result == ("s-4", 1)

    def test_no_bids_raises(self):
        trader = _trader()
        with patch("src.execution.live_trader.get_orderbook", return_value={"bids": [], "asks": []}):
            with pytest.raises(RuntimeError, match="No bids"):
                trader.sell_position_immediate("tok-e", 6.25)


# ---------------------------------------------------------------------------
# run._check_stop_loss_exits
# ---------------------------------------------------------------------------

TOKEN = "tok-sl-1"


def _position_state(fair: int | None, bid: int | None, depth: float | None,
                    entry_cents: int = 80) -> dict:
    fills = [{
        "station": "WSSS",
        "bracket_low": 86.0,
        "bracket_high": 87.8,
        "price_cents": entry_cents,
        "size_eur": 5.0,
        "order_id": "buy-1",
        "ticker": "WSSS-order-buy1",
        "question": "Will the highest temperature in Singapore be 30-31C?",
    }]
    snap = {"fair_value_now": fair, "no_best_bid": bid, "no_best_bid_size": depth}
    return {"token_id": TOKEN, "fills": fills, "snap": snap}


@pytest.fixture(autouse=True)
def _clean_state():
    run._stop_loss_strikes.clear()
    run._sold_positions.clear()
    yield
    run._stop_loss_strikes.clear()
    run._sold_positions.clear()


@pytest.fixture(autouse=True)
def _fixed_thresholds():
    with patch.object(run, "STOP_LOSS_MIN_BID_CENTS", 40), \
            patch.object(run, "STOP_LOSS_CONSECUTIVE_POLLS", 2), \
            patch.object(run, "STOP_LOSS_MIN_DEPTH_SHARES", 10.0), \
            patch.object(run, "STOP_LOSS_SELL_AGGRESSION_CENTS", 2):
        yield


@pytest.fixture()
def _no_side_effects():
    with patch.object(run, "_append_live_trade") as append_mock, \
            patch.object(run, "_record_sell_in_db") as record_mock:
        yield append_mock, record_mock


class TestCheckStopLossExits:
    def test_fair_above_entry_no_action_and_resets_strikes(self, _no_side_effects):
        run._stop_loss_strikes[TOKEN] = 1
        trader = MagicMock()
        run._check_stop_loss_exits(trader, "ts", [_position_state(fair=85, bid=82, depth=50.0)])
        trader.sell_position_immediate.assert_not_called()
        assert TOKEN not in run._stop_loss_strikes

    def test_first_strike_does_not_sell(self, _no_side_effects):
        trader = MagicMock()
        run._check_stop_loss_exits(trader, "ts", [_position_state(fair=70, bid=75, depth=50.0)])
        trader.sell_position_immediate.assert_not_called()
        assert run._stop_loss_strikes[TOKEN] == 1

    def test_strike_resets_on_recovery(self, _no_side_effects):
        trader = MagicMock()
        run._check_stop_loss_exits(trader, "ts", [_position_state(fair=70, bid=75, depth=50.0)])
        run._check_stop_loss_exits(trader, "ts", [_position_state(fair=85, bid=75, depth=50.0)])
        run._check_stop_loss_exits(trader, "ts", [_position_state(fair=70, bid=75, depth=50.0)])
        trader.sell_position_immediate.assert_not_called()
        assert run._stop_loss_strikes[TOKEN] == 1

    def test_consecutive_strikes_trigger_sell(self, _no_side_effects):
        append_mock, record_mock = _no_side_effects
        trader = MagicMock()
        trader.sell_position_immediate.return_value = ("sell-1", 68)
        risk = MagicMock()
        ps = _position_state(fair=70, bid=75, depth=50.0)
        run._check_stop_loss_exits(trader, "ts", [ps], risk_manager=risk)
        run._check_stop_loss_exits(trader, "ts", [ps], risk_manager=risk)
        trader.sell_position_immediate.assert_called_once_with(TOKEN, pytest.approx(6.25), 2)
        assert TOKEN in run._sold_positions
        assert TOKEN not in run._stop_loss_strikes
        # PnL: (68 - 80) / 100 * 6.25 shares = -0.75 EUR
        risk.record_pnl.assert_called_once_with(pytest.approx(-0.75))
        record_mock.assert_called_once()
        trade_row = append_mock.call_args[0][0]
        assert trade_row["outcome"] == "sold"
        assert trade_row["trigger"].startswith("stop_loss@75c_fair70c_entry80c")

    def test_bid_below_floor_holds(self, _no_side_effects):
        """Triggered but bid under STOP_LOSS_MIN_BID_CENTS — hold, keep strikes."""
        trader = MagicMock()
        ps = _position_state(fair=20, bid=35, depth=50.0)
        run._check_stop_loss_exits(trader, "ts", [ps])
        run._check_stop_loss_exits(trader, "ts", [ps])
        trader.sell_position_immediate.assert_not_called()
        assert run._stop_loss_strikes[TOKEN] == 2

    def test_thin_depth_skips_poll(self, _no_side_effects):
        """Triggered but best-bid depth too thin — skip this poll, keep strikes."""
        trader = MagicMock()
        ps = _position_state(fair=70, bid=75, depth=3.0)
        run._check_stop_loss_exits(trader, "ts", [ps])
        run._check_stop_loss_exits(trader, "ts", [ps])
        trader.sell_position_immediate.assert_not_called()
        assert run._stop_loss_strikes[TOKEN] == 2

    def test_unfilled_sell_retries_next_poll(self, _no_side_effects):
        """sell_position_immediate -> None (cancelled unfilled): no resting order,
        position stays open, strikes persist so the next poll retries."""
        append_mock, record_mock = _no_side_effects
        trader = MagicMock()
        trader.sell_position_immediate.return_value = None
        ps = _position_state(fair=70, bid=75, depth=50.0)
        run._check_stop_loss_exits(trader, "ts", [ps])
        run._check_stop_loss_exits(trader, "ts", [ps])
        assert TOKEN not in run._sold_positions
        assert run._stop_loss_strikes[TOKEN] == 2
        append_mock.assert_not_called()
        record_mock.assert_not_called()
        # Next poll retries the sell
        trader.sell_position_immediate.return_value = ("sell-2", 67)
        run._check_stop_loss_exits(trader, "ts", [ps])
        assert TOKEN in run._sold_positions

    def test_already_sold_token_is_skipped(self, _no_side_effects):
        run._sold_positions.add(TOKEN)
        trader = MagicMock()
        ps = _position_state(fair=70, bid=75, depth=50.0)
        run._check_stop_loss_exits(trader, "ts", [ps])
        run._check_stop_loss_exits(trader, "ts", [ps])
        trader.sell_position_immediate.assert_not_called()

    def test_missing_fair_value_is_skipped(self, _no_side_effects):
        """Model eval failed (fair None) — never strike or sell on missing data."""
        trader = MagicMock()
        run._check_stop_loss_exits(trader, "ts", [_position_state(fair=None, bid=75, depth=50.0)])
        trader.sell_position_immediate.assert_not_called()
        assert TOKEN not in run._stop_loss_strikes

    def test_balance_zero_error_closes_position(self, _no_side_effects):
        """'not enough balance' error means tokens are already gone — mark sold."""
        trader = MagicMock()
        trader.sell_position_immediate.side_effect = Exception("not enough balance: 0")
        db = MagicMock()
        db.close_positions_by_token.return_value = 1
        ps = _position_state(fair=70, bid=75, depth=50.0)
        run._check_stop_loss_exits(trader, "ts", [ps], db=db)
        run._check_stop_loss_exits(trader, "ts", [ps], db=db)
        assert TOKEN in run._sold_positions
        db.close_positions_by_token.assert_called_once_with(TOKEN)

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
        """If the order does not cross it must be cancelled — returns (None, order_id)."""
        trader = _trader()
        trader.client.create_and_post_order.return_value = {"orderID": "s-2", "status": "live"}
        trader.client.get_order.return_value = {"status": "live"}
        trader.client.cancel.return_value = {"canceled": ["s-2"]}
        with patch("src.execution.live_trader.get_orderbook", return_value=_orderbook("0.70")):
            result = trader.sell_position_immediate("tok-b", 6.25)
        sell_id, cancelled_order_id = result
        assert sell_id is None
        assert cancelled_order_id == "s-2"
        trader.client.cancel.assert_called_once_with("s-2")

    def test_cancel_refused_with_fill_confirmed_means_filled_in_flight(self):
        """Cancel refused + check_fill confirms filled → treat as sold."""
        trader = _trader()
        trader.client.create_and_post_order.return_value = {"orderID": "s-3", "status": "live"}
        # check_fill is called twice: once pre-cancel (returns open), once post-cancel-refused (returns filled)
        trader.client.get_order.side_effect = [
            {"status": "live"},    # pre-cancel check_fill → open
            {"status": "matched"}, # post-cancel-refused check_fill → filled
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
                    entry_cents: int = 80, *,
                    current_high: float | None = None,
                    forecast_nws: float | None = None,
                    forecast_secondary: float | None = None,
                    bracket_low: float = 86.0,
                    bracket_high: float = 87.8) -> dict:
    fills = [{
        "station": "WSSS",
        "bracket_low": bracket_low,
        "bracket_high": bracket_high,
        "price_cents": entry_cents,
        "size_eur": 5.0,
        "order_id": "buy-1",
        "ticker": "WSSS-order-buy1",
        "question": "Will the highest temperature in Singapore be 30-31C?",
    }]
    snap = {
        "fair_value_now": fair,
        "no_best_bid": bid,
        "no_best_bid_size": depth,
        "current_high": current_high,
        "forecast_nws": forecast_nws,
        "forecast_secondary": forecast_secondary,
    }
    return {"token_id": TOKEN, "fills": fills, "snap": snap}


@pytest.fixture(autouse=True)
def _clean_state():
    run.order_manager._stop_loss_strikes.clear()
    run.order_manager._sold_positions.clear()
    yield
    run.order_manager._stop_loss_strikes.clear()
    run.order_manager._sold_positions.clear()


@pytest.fixture(autouse=True)
def _fixed_thresholds():
    with patch.object(run, "STOP_LOSS_MIN_BID_CENTS", 40), \
            patch.object(run, "STOP_LOSS_CONSECUTIVE_POLLS", 2), \
            patch.object(run, "STOP_LOSS_MIN_DEPTH_SHARES", 10.0), \
            patch.object(run, "STOP_LOSS_SELL_AGGRESSION_CENTS", 2), \
            patch.object(run, "STOP_LOSS_MIN_BRACKET_PROXIMITY_F", 0.5), \
            patch.object(run, "STOP_LOSS_RESPECT_FORECAST_OVERSHOOT", True):
        yield


@pytest.fixture()
def _no_side_effects():
    with patch.object(run, "_append_live_trade") as append_mock, \
            patch.object(run, "_record_sell_in_db") as record_mock:
        yield append_mock, record_mock


class TestCheckStopLossExits:
    def test_fair_above_entry_no_action_and_resets_strikes(self, _no_side_effects):
        run.order_manager._stop_loss_strikes[TOKEN] = 1
        trader = MagicMock()
        run._check_stop_loss_exits(trader, "ts", [_position_state(fair=85, bid=82, depth=50.0)])
        trader.sell_position_immediate.assert_not_called()
        assert TOKEN not in run.order_manager._stop_loss_strikes

    def test_first_strike_does_not_sell(self, _no_side_effects):
        trader = MagicMock()
        run._check_stop_loss_exits(trader, "ts", [_position_state(fair=70, bid=75, depth=50.0)])
        trader.sell_position_immediate.assert_not_called()
        assert run.order_manager._stop_loss_strikes[TOKEN] == 1

    def test_strike_resets_on_recovery(self, _no_side_effects):
        trader = MagicMock()
        run._check_stop_loss_exits(trader, "ts", [_position_state(fair=70, bid=75, depth=50.0)])
        run._check_stop_loss_exits(trader, "ts", [_position_state(fair=85, bid=75, depth=50.0)])
        run._check_stop_loss_exits(trader, "ts", [_position_state(fair=70, bid=75, depth=50.0)])
        trader.sell_position_immediate.assert_not_called()
        assert run.order_manager._stop_loss_strikes[TOKEN] == 1

    def test_consecutive_strikes_trigger_sell(self, _no_side_effects):
        append_mock, record_mock = _no_side_effects
        trader = MagicMock()
        trader.sell_position_immediate.return_value = ("sell-1", 68)
        risk = MagicMock()
        ps = _position_state(fair=70, bid=75, depth=50.0)
        run._check_stop_loss_exits(trader, "ts", [ps], risk_manager=risk)
        run._check_stop_loss_exits(trader, "ts", [ps], risk_manager=risk)
        trader.sell_position_immediate.assert_called_once_with(TOKEN, pytest.approx(6.25), 2)
        assert TOKEN in run.order_manager._sold_positions
        assert TOKEN not in run.order_manager._stop_loss_strikes
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
        assert run.order_manager._stop_loss_strikes[TOKEN] == 2

    def test_thin_depth_skips_poll(self, _no_side_effects):
        """Triggered but best-bid depth too thin — skip this poll, keep strikes."""
        trader = MagicMock()
        ps = _position_state(fair=70, bid=75, depth=3.0)
        run._check_stop_loss_exits(trader, "ts", [ps])
        run._check_stop_loss_exits(trader, "ts", [ps])
        trader.sell_position_immediate.assert_not_called()
        assert run.order_manager._stop_loss_strikes[TOKEN] == 2

    def test_unfilled_sell_retries_next_poll(self, _no_side_effects):
        """sell_position_immediate -> None (cancelled unfilled): no resting order,
        position stays open, strikes persist so the next poll retries."""
        append_mock, record_mock = _no_side_effects
        trader = MagicMock()
        trader.sell_position_immediate.return_value = None
        ps = _position_state(fair=70, bid=75, depth=50.0)
        run._check_stop_loss_exits(trader, "ts", [ps])
        run._check_stop_loss_exits(trader, "ts", [ps])
        assert TOKEN not in run.order_manager._sold_positions
        assert run.order_manager._stop_loss_strikes[TOKEN] == 2
        append_mock.assert_not_called()
        record_mock.assert_not_called()
        # Next poll retries the sell
        trader.sell_position_immediate.return_value = ("sell-2", 67)
        run._check_stop_loss_exits(trader, "ts", [ps])
        assert TOKEN in run.order_manager._sold_positions

    def test_already_sold_token_is_skipped(self, _no_side_effects):
        run.order_manager._sold_positions.add(TOKEN)
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
        assert TOKEN not in run.order_manager._stop_loss_strikes

    def test_sell_exception_logs_warning_and_retries(self, _no_side_effects):
        """Generic sell exception → warning logged, position NOT marked sold (will retry next poll).

        The old error-string balance==0 reconciliation path was removed in #204
        in favour of explicit partial-fill tracking via _partial_fill_shares.
        """
        trader = MagicMock()
        trader.sell_position_immediate.side_effect = Exception("unexpected exchange error")
        db = MagicMock()
        ps = _position_state(fair=70, bid=75, depth=50.0)
        run._check_stop_loss_exits(trader, "ts", [ps], db=db)
        # Position is NOT marked sold — caller retries on next poll
        assert TOKEN not in run.order_manager._sold_positions


class TestStopLossSafetyGuards:
    """Hotfix guards added 2026-06-13 after 3 wins were cut in one day."""

    def test_proximity_guard_blocks_when_temp_far_below_bracket(self, _no_side_effects):
        """current_high 1.8F below bracket_low (>0.5F buffer) -- hold, keep strikes."""
        trader = MagicMock()
        ps = _position_state(
            fair=70, bid=75, depth=50.0,
            current_high=84.2,  # bracket_low=86.0 -- 1.8F below
        )
        run._check_stop_loss_exits(trader, "ts", [ps])
        run._check_stop_loss_exits(trader, "ts", [ps])
        trader.sell_position_immediate.assert_not_called()
        assert run.order_manager._stop_loss_strikes[TOKEN] == 2

    def test_proximity_guard_allows_when_temp_at_bracket(self, _no_side_effects):
        """current_high within 0.5F of bracket_low -- guard passes, sell fires."""
        trader = MagicMock()
        trader.sell_position_immediate.return_value = ("sell-1", 73)
        ps = _position_state(
            fair=70, bid=75, depth=50.0,
            current_high=85.6,  # bracket_low=86.0 -- 0.4F below, within buffer
        )
        run._check_stop_loss_exits(trader, "ts", [ps])
        run._check_stop_loss_exits(trader, "ts", [ps])
        trader.sell_position_immediate.assert_called_once()

    def test_proximity_guard_allows_when_temp_inside_bracket(self, _no_side_effects):
        """current_high above bracket_low -- definitely passes proximity guard."""
        trader = MagicMock()
        trader.sell_position_immediate.return_value = ("sell-1", 73)
        ps = _position_state(
            fair=70, bid=75, depth=50.0,
            current_high=86.5,  # inside bracket 86.0-87.8
        )
        run._check_stop_loss_exits(trader, "ts", [ps])
        run._check_stop_loss_exits(trader, "ts", [ps])
        trader.sell_position_immediate.assert_called_once()

    def test_proximity_guard_disabled_when_current_high_missing(self, _no_side_effects):
        """No current_high in snap -- can't apply proximity guard, fall through to sell."""
        trader = MagicMock()
        trader.sell_position_immediate.return_value = ("sell-1", 73)
        ps = _position_state(fair=70, bid=75, depth=50.0)  # no current_high
        run._check_stop_loss_exits(trader, "ts", [ps])
        run._check_stop_loss_exits(trader, "ts", [ps])
        trader.sell_position_immediate.assert_called_once()

    def test_overshoot_guard_blocks_when_forecast_above_bracket_high(self, _no_side_effects):
        """Forecast > bracket_high -- NO wins on overshoot, hold, keep strikes."""
        trader = MagicMock()
        ps = _position_state(
            fair=70, bid=75, depth=50.0,
            current_high=86.5,  # passes proximity guard
            forecast_nws=93.0,  # bracket_high=87.8 -- forecast predicts overshoot
        )
        run._check_stop_loss_exits(trader, "ts", [ps])
        run._check_stop_loss_exits(trader, "ts", [ps])
        trader.sell_position_immediate.assert_not_called()
        assert run.order_manager._stop_loss_strikes[TOKEN] == 2

    def test_overshoot_guard_uses_secondary_when_nws_missing(self, _no_side_effects):
        """Falls back to forecast_secondary when forecast_nws is None."""
        trader = MagicMock()
        ps = _position_state(
            fair=70, bid=75, depth=50.0,
            current_high=86.5,
            forecast_nws=None,
            forecast_secondary=93.0,
        )
        run._check_stop_loss_exits(trader, "ts", [ps])
        run._check_stop_loss_exits(trader, "ts", [ps])
        trader.sell_position_immediate.assert_not_called()

    def test_overshoot_guard_allows_when_forecast_inside_bracket(self, _no_side_effects):
        """Forecast within bracket -- bracket not expected to be overshot, sell fires."""
        trader = MagicMock()
        trader.sell_position_immediate.return_value = ("sell-1", 73)
        ps = _position_state(
            fair=70, bid=75, depth=50.0,
            current_high=86.5,
            forecast_nws=87.0,  # inside bracket 86.0-87.8
        )
        run._check_stop_loss_exits(trader, "ts", [ps])
        run._check_stop_loss_exits(trader, "ts", [ps])
        trader.sell_position_immediate.assert_called_once()

    def test_overshoot_guard_inactive_for_open_top_bracket(self, _no_side_effects):
        """'or above' bracket (high=200) -- overshoot impossible, guard skipped."""
        trader = MagicMock()
        trader.sell_position_immediate.return_value = ("sell-1", 73)
        ps = _position_state(
            fair=70, bid=75, depth=50.0,
            current_high=86.5,
            forecast_nws=95.0,
            bracket_low=86.0, bracket_high=200.0,
        )
        run._check_stop_loss_exits(trader, "ts", [ps])
        run._check_stop_loss_exits(trader, "ts", [ps])
        trader.sell_position_immediate.assert_called_once()

    def test_overshoot_guard_disabled_by_config(self, _no_side_effects):
        """STOP_LOSS_RESPECT_FORECAST_OVERSHOOT=False -- guard skipped."""
        trader = MagicMock()
        trader.sell_position_immediate.return_value = ("sell-1", 73)
        ps = _position_state(
            fair=70, bid=75, depth=50.0,
            current_high=86.5,
            forecast_nws=93.0,
        )
        with patch.object(run, "STOP_LOSS_RESPECT_FORECAST_OVERSHOOT", False):
            run._check_stop_loss_exits(trader, "ts", [ps])
            run._check_stop_loss_exits(trader, "ts", [ps])
        trader.sell_position_immediate.assert_called_once()

    def test_proximity_guard_disabled_by_negative_config(self, _no_side_effects):
        """STOP_LOSS_MIN_BRACKET_PROXIMITY_F <= 0 -- guard disabled."""
        trader = MagicMock()
        trader.sell_position_immediate.return_value = ("sell-1", 73)
        ps = _position_state(
            fair=70, bid=75, depth=50.0,
            current_high=70.0,  # very far from bracket, normally blocked
        )
        with patch.object(run, "STOP_LOSS_MIN_BRACKET_PROXIMITY_F", -1.0):
            run._check_stop_loss_exits(trader, "ts", [ps])
            run._check_stop_loss_exits(trader, "ts", [ps])
        trader.sell_position_immediate.assert_called_once()

    def test_today_jun13_scenario_wsss_blocked(self, _no_side_effects):
        """Reproduces today's WSSS 91.4-93.2F false fire (cur_high=89.6, gap=1.8F)."""
        trader = MagicMock()
        ps = _position_state(
            fair=67, bid=73, depth=50.0, entry_cents=79,
            current_high=89.6,
            bracket_low=91.4, bracket_high=93.2,
        )
        run._check_stop_loss_exits(trader, "ts", [ps])
        run._check_stop_loss_exits(trader, "ts", [ps])
        trader.sell_position_immediate.assert_not_called()  # proximity guard

    def test_today_jun13_scenario_katl_blocked(self, _no_side_effects):
        """Reproduces today's KATL 90-91F false fire (forecast 93 > bracket high 91)."""
        trader = MagicMock()
        ps = _position_state(
            fair=80, bid=82, depth=50.0, entry_cents=84,
            current_high=89.96,  # 0.04F below bracket -- passes proximity
            forecast_nws=93.0,   # overshoot expected
            bracket_low=90.0, bracket_high=91.0,
        )
        run._check_stop_loss_exits(trader, "ts", [ps])
        run._check_stop_loss_exits(trader, "ts", [ps])
        trader.sell_position_immediate.assert_not_called()  # overshoot guard

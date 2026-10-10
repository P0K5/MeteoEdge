"""Tests for stop-loss IOC partial fill tracking (#204)."""
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch, call

import pytest

# Stub py_clob_client_v2 for the test environment.
_clob_stub = ModuleType("py_clob_client_v2")
_clob_stub.ClobClient = MagicMock  # type: ignore[attr-defined]
_clob_types_stub = ModuleType("py_clob_client_v2.clob_types")
for _name in ("AssetType", "BalanceAllowanceParams", "CreateOrderOptions", "OrderArgs", "OrderPayload", "TradeParams"):
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


class TestGetOrderFillCostUsd:
    """LiveTrader.get_order_fill_cost_usd() (issue #1341) -- the actual-cost
    authority, replacing the `filled_shares * placed_price` approximation
    (#1336) which overstates cost whenever a GTC BUY crosses the spread at
    a better price, or part of an off-chain CLOB match fails on-chain
    settlement."""

    def test_sums_size_times_price_across_multiple_trades(self):
        """Multi-leg fill: an order matched via two separate trades, both
        as the taker (crossed the spread, took resting liquidity)."""
        trader = _make_trader()
        trader.client.get_order.return_value = {
            "status": "matched", "associate_trades": ["trade-1", "trade-2"],
        }

        def _get_trades(params, only_first_page=True):
            return {
                "trade-1": [{"id": "trade-1", "taker_order_id": "ord-full", "size": "5.0", "price": "0.22", "status": "CONFIRMED"}],
                "trade-2": [{"id": "trade-2", "taker_order_id": "ord-full", "size": "3.11", "price": "0.22", "status": "CONFIRMED"}],
            }[params.id]

        trader.client.get_trades.side_effect = _get_trades
        # 5.0*0.22 + 3.11*0.22 = 1.1 + 0.6842 = 1.7842
        assert trader.get_order_fill_cost_usd("ord-full") == pytest.approx(1.7842)

    def test_uses_actual_execution_price_not_placed_limit_price(self):
        """A single trade matched far better than the placed limit -- the
        whole point of #1341: this must reflect the trade's OWN price."""
        trader = _make_trader()
        trader.client.get_order.return_value = {
            "status": "matched", "associate_trades": ["trade-1"],
        }
        trader.client.get_trades.return_value = [
            {"id": "trade-1", "taker_order_id": "ord-x", "size": "8.11", "price": "0.22", "status": "MATCHED"},
        ]
        # Placed limit was 0.37 (not passed to this method at all) --
        # actual cost must be 8.11 * 0.22, never 8.11 * 0.37.
        assert trader.get_order_fill_cost_usd("ord-x") == pytest.approx(1.7842)

    def test_excludes_failed_trade_status(self):
        """A trade that matched off-chain but failed on-chain settlement
        (py_clob_client_v2's own FAILED_TRADE_STATUS) must not count."""
        trader = _make_trader()
        trader.client.get_order.return_value = {
            "status": "matched", "associate_trades": ["trade-ok", "trade-failed"],
        }

        def _get_trades(params, only_first_page=True):
            return {
                "trade-ok": [{"id": "trade-ok", "taker_order_id": "ord-y", "size": "4.0", "price": "0.5", "status": "CONFIRMED"}],
                "trade-failed": [{"id": "trade-failed", "taker_order_id": "ord-y", "size": "4.0", "price": "0.5", "status": "FAILED"}],
            }[params.id]

        trader.client.get_trades.side_effect = _get_trades
        assert trader.get_order_fill_cost_usd("ord-y") == pytest.approx(2.0)

    def test_returns_zero_when_no_associate_trades(self):
        trader = _make_trader()
        trader.client.get_order.return_value = {"status": "live", "associate_trades": []}
        assert trader.get_order_fill_cost_usd("ord-z") == 0.0
        trader.client.get_trades.assert_not_called()

    def test_returns_zero_when_associate_trades_field_missing(self):
        trader = _make_trader()
        trader.client.get_order.return_value = {"status": "live"}
        assert trader.get_order_fill_cost_usd("ord-missing") == 0.0

    def test_returns_zero_on_order_lookup_error(self):
        trader = _make_trader()
        trader.client.get_order.side_effect = Exception("network blip")
        assert trader.get_order_fill_cost_usd("ord-err") == 0.0

    def test_one_trade_lookup_failure_does_not_block_the_others(self):
        """A single trade-id lookup raising must not zero out the whole
        result -- the other, successfully-resolved legs still count."""
        trader = _make_trader()
        trader.client.get_order.return_value = {
            "status": "matched", "associate_trades": ["trade-good", "trade-bad"],
        }

        def _get_trades(params, only_first_page=True):
            if params.id == "trade-bad":
                raise Exception("timeout")
            return [{"id": "trade-good", "taker_order_id": "ord-partial-error", "size": "2.0", "price": "0.3", "status": "CONFIRMED"}]

        trader.client.get_trades.side_effect = _get_trades
        assert trader.get_order_fill_cost_usd("ord-partial-error") == pytest.approx(0.6)

    def test_mismatched_trade_id_in_response_is_skipped(self):
        """Defensive: only count a trade record whose own id matches what
        was requested (get_trades is a filtered list, but never trust it
        blindly)."""
        trader = _make_trader()
        trader.client.get_order.return_value = {
            "status": "matched", "associate_trades": ["trade-1"],
        }
        trader.client.get_trades.return_value = [
            {"id": "some-other-trade", "size": "99.0", "price": "0.99", "status": "CONFIRMED"},
        ]
        assert trader.get_order_fill_cost_usd("ord-mismatch") == 0.0

    def test_maker_leg_uses_own_price_not_taker_top_level_fields(self):
        """Issue #1347: when our order filled as a MAKER (resting liquidity
        hit by someone else's taker order), the trade's top-level
        size/price belong to the TAKER's own order on the COMPLEMENTARY
        outcome (e.g. YES @ 0.79 for a trade where we supplied NO @ 0.21 --
        prices sum to ~1, they are not interchangeable with ours). Must use
        our own `maker_orders[]` leg instead."""
        trader = _make_trader()
        trader.client.get_order.return_value = {
            "status": "matched", "associate_trades": ["trade-1"],
        }
        trader.client.get_trades.return_value = [
            {
                "id": "trade-1",
                "taker_order_id": "0xsomeone-elses-order",
                "size": "14",
                "price": "0.79",
                "status": "CONFIRMED",
                "trader_side": "MAKER",
                "maker_orders": [
                    {"order_id": "ord-maker", "matched_amount": "14", "price": "0.21"},
                ],
            },
        ]
        # Correct cost is OUR leg: 14 * 0.21 = 2.94 -- never 14 * 0.79 = 11.06.
        assert trader.get_order_fill_cost_usd("ord-maker") == pytest.approx(2.94)

    def test_multi_maker_match_picks_only_our_own_leg(self):
        """A single trade can have several maker_orders (true multi-maker
        match) -- only our own order's leg should count, never the other
        makers' or the taker's totals."""
        trader = _make_trader()
        trader.client.get_order.return_value = {
            "status": "matched", "associate_trades": ["trade-1"],
        }
        trader.client.get_trades.return_value = [
            {
                "id": "trade-1",
                "taker_order_id": "0xtaker",
                "size": "20",
                "price": "0.79",
                "status": "CONFIRMED",
                "trader_side": "MAKER",
                "maker_orders": [
                    {"order_id": "0xother-maker", "matched_amount": "6", "price": "0.21"},
                    {"order_id": "ord-maker-2", "matched_amount": "14", "price": "0.21"},
                ],
            },
        ]
        assert trader.get_order_fill_cost_usd("ord-maker-2") == pytest.approx(2.94)

    def test_corrupted_production_row_334_reproduces_true_cost(self):
        """Regression for issue #1347: real production evidence for
        position #334 -- a $3 stake filled as a MAKER across two separate
        trade events (14 shares and 0.29 shares, both @ 0.21), while each
        trade's top-level (taker-side) fields show the complementary
        outcome @ 0.79. The pre-fix formula summed the top-level fields and
        recorded $11.2891 (verified in the live DB and bug report); the
        true on-chain cost is $3.0009 (also independently verified against
        the Data API in the issue)."""
        trader = _make_trader()
        trader.client.get_order.return_value = {
            "status": "matched", "associate_trades": ["trade-a", "trade-b"],
        }

        def _get_trades(params, only_first_page=True):
            return {
                "trade-a": [{
                    "id": "trade-a", "taker_order_id": "0xtaker-a", "size": "14", "price": "0.79",
                    "status": "CONFIRMED", "trader_side": "MAKER",
                    "maker_orders": [{"order_id": "ord-334", "matched_amount": "14", "price": "0.21"}],
                }],
                "trade-b": [{
                    "id": "trade-b", "taker_order_id": "0xtaker-b", "size": "0.29", "price": "0.79",
                    "status": "CONFIRMED", "trader_side": "MAKER",
                    "maker_orders": [{"order_id": "ord-334", "matched_amount": "0.29", "price": "0.21"}],
                }],
            }[params.id]

        trader.client.get_trades.side_effect = _get_trades
        # 14*0.21 + 0.29*0.21 = 2.94 + 0.0609 = 3.0009 (true cost).
        # Pre-fix this returned 14*0.79 + 0.29*0.79 = 11.2891 (the bug).
        assert trader.get_order_fill_cost_usd("ord-334") == pytest.approx(3.0009)

    def test_order_neither_taker_nor_maker_leg_is_skipped_not_misattributed(self):
        """Defensive: if our order_id can't be found as the taker or in any
        maker leg of a resolved trade, skip it (and warn) rather than
        guessing with the taker's unrelated top-level fields."""
        trader = _make_trader()
        trader.client.get_order.return_value = {
            "status": "matched", "associate_trades": ["trade-1"],
        }
        trader.client.get_trades.return_value = [
            {
                "id": "trade-1",
                "taker_order_id": "0xsomeone-else",
                "size": "14",
                "price": "0.79",
                "status": "CONFIRMED",
                "maker_orders": [
                    {"order_id": "0xa-different-maker", "matched_amount": "14", "price": "0.21"},
                ],
            },
        ]
        assert trader.get_order_fill_cost_usd("ord-not-in-this-trade") == 0.0


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
             patch("src.execution.position_tracker._record_sell_in_db"):
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
             patch("src.execution.position_tracker._record_sell_in_db"):
            run_mod._check_stop_loss_exits(trader, "2026-06-12T10:00:00Z", [ps])

        assert token_id in run_mod.order_manager._sold_positions
        # No partial fill tracking for a clean fill
        assert token_id not in run_mod.order_manager._partial_fill_shares

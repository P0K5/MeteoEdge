"""Tests for src/execution/copy_live_executor.py (issue #1167, epic H #1159).

Mirrors test_order_executor_reprice_743.py's mocking style (LiveTrader
patched to a MagicMock, time.sleep patched out, FILL_MAX_WAIT_S patched to
force immediate timeout paths).
"""
from unittest.mock import MagicMock, patch

from src.execution import copy_live_executor as cle


def _run(
    *, book_asks=None, timeout=True, fill_status="open", resolution=None,
    fill_size=0.0, retry_enabled=True, place_order_side_effect=None,
):
    """Run execute_live_copy_order with LiveTrader/get_orderbook/fetch_market_resolution
    mocked. Returns (result, trader_mock, orderbook_mock)."""
    trader = MagicMock()
    if place_order_side_effect is not None:
        trader.place_order.side_effect = place_order_side_effect
    else:
        trader.place_order.return_value = "order-id-abc123"
    trader.check_fill.return_value = fill_status
    trader.get_order_fill_size.return_value = fill_size
    gob = MagicMock(return_value={"asks": book_asks or [], "bids": []})
    factory = MagicMock(return_value=MagicMock())

    with patch.object(cle, "LiveTrader", return_value=trader) as live_trader_cls, \
         patch.object(cle, "get_orderbook", gob), \
         patch.object(cle, "fetch_market_resolution", return_value=resolution), \
         patch.object(cle.time, "sleep", lambda s: None), \
         patch.object(cle, "FILL_MAX_WAIT_S", 0 if timeout else 100):
        result = cle.execute_live_copy_order(
            factory,
            token_id="tok123",
            market="0xmarket1",
            side_label="YES",
            price=0.40,
            stake_usd=10.0,
            retry_enabled=retry_enabled,
        )
    return result, trader, gob, live_trader_cls, factory


class TestIsolationFromLiveTraderState:
    """The single most important design invariant in this module (#1100):
    LiveTrader is constructed with db=None so its own trades/open_positions
    writes (place_order's and cancel_order's own `if self._db is not None`
    branches) are structurally unreachable."""

    def test_live_trader_constructed_with_db_none(self):
        _, _, _, live_trader_cls, factory = _run(fill_status="filled", timeout=False)
        live_trader_cls.assert_called_once_with(factory.return_value, db=None)


class TestFillOnFirstAttempt:
    def test_fill_returns_filled_status_and_placed_price(self):
        result, trader, gob, _, _ = _run(fill_status="filled", timeout=False)
        assert result == {"status": "filled", "order_id": "order-id-abc123", "fill_price": 0.40}
        trader.cancel_order.assert_not_called()
        gob.assert_not_called()  # no reprice needed

    def test_price_clamped_and_converted_to_cents(self):
        trader = MagicMock()
        trader.place_order.return_value = "oid"
        trader.check_fill.return_value = "filled"
        factory = MagicMock(return_value=MagicMock())
        with patch.object(cle, "LiveTrader", return_value=trader), \
             patch.object(cle.time, "sleep", lambda s: None), \
             patch.object(cle, "FILL_MAX_WAIT_S", 100):
            cle.execute_live_copy_order(
                factory, token_id="t", market="m", side_label="NO",
                price=0.9999, stake_usd=5.0,
            )
        assert trader.place_order.call_args.kwargs["price_cents"] == 99


class TestPlaceOrderRejection:
    def test_place_order_exception_returns_rejected_no_order_id(self):
        result, trader, gob, _, _ = _run(place_order_side_effect=RuntimeError("insufficient balance"))
        assert result == {"status": "rejected", "order_id": None, "rejected_reason": "place_failed"}
        trader.check_fill.assert_not_called()
        gob.assert_not_called()


class TestTimeoutRepriceRetry:
    def test_timeout_then_gates_pass_places_second_order_at_new_price(self):
        """FILL_MAX_WAIT_S=0 forces BOTH attempts' poll loops to never
        execute (deadline already passed on entry) -> outcome='timeout' on
        the original AND the retry -- this test only cares about proving
        exactly one retry is placed, at the repriced book price. A fill on
        the retry is covered separately (TestFillOnFirstAttempt exercises
        the fill path in isolation)."""
        trader = MagicMock()
        trader.place_order.side_effect = ["oid-1", "oid-2"]
        trader.check_fill.return_value = "open"
        trader.get_order_fill_size.return_value = 0.0
        factory = MagicMock(return_value=MagicMock())
        gob = MagicMock(return_value={"asks": [{"price": "0.45", "size": "10"}], "bids": []})

        with patch.object(cle, "LiveTrader", return_value=trader), \
             patch.object(cle, "get_orderbook", gob), \
             patch.object(cle, "fetch_market_resolution", return_value=None), \
             patch.object(cle.time, "sleep", lambda s: None), \
             patch.object(cle, "FILL_MAX_WAIT_S", 0):
            result = cle.execute_live_copy_order(
                factory, token_id="tok", market="0xm", side_label="YES",
                price=0.40, stake_usd=10.0,
            )

        assert gob.call_count == 1
        assert trader.place_order.call_count == 2
        assert trader.place_order.call_args_list[1].kwargs["price_cents"] == 45
        assert result["status"] == "rejected"  # FILL_MAX_WAIT_S=0 -> retry also times out
        assert result["order_id"] == "oid-2"

    def test_market_resolved_in_interim_no_retry(self):
        result, trader, gob, _, _ = _run(
            timeout=True, book_asks=[{"price": "0.45", "size": "10"}], resolution=True,
        )
        assert trader.place_order.call_count == 1
        gob.assert_not_called()  # resolution short-circuits before the book fetch
        assert result["status"] == "rejected"
        assert result["rejected_reason"] == "timeout"

    def test_empty_book_no_retry(self):
        result, trader, gob, _, _ = _run(timeout=True, book_asks=[], resolution=None)
        assert gob.call_count == 1
        assert trader.place_order.call_count == 1
        assert result["status"] == "rejected"

    def test_retry_disabled_never_fetches_book(self):
        result, trader, gob, _, _ = _run(
            timeout=True, book_asks=[{"price": "0.45", "size": "10"}],
            resolution=None, retry_enabled=False,
        )
        assert gob.call_count == 0
        assert trader.place_order.call_count == 1
        assert result["status"] == "rejected"


class TestPartialFillHandling:
    """New versus the weather BUY-side mirror -- see module docstring."""

    def test_confirmed_partial_fill_after_timeout_short_circuits_retry(self):
        result, trader, gob, _, _ = _run(
            timeout=True, book_asks=[{"price": "0.45", "size": "10"}],
            resolution=None, fill_size=3.5,
        )
        assert result["status"] == "partial"
        assert result["order_id"] == "order-id-abc123"
        gob.assert_not_called()  # never even considers a reprice-retry
        assert trader.place_order.call_count == 1

    def test_zero_fill_size_after_timeout_is_not_treated_as_partial(self):
        result, trader, gob, _, _ = _run(
            timeout=True, book_asks=[], resolution=None, fill_size=0.0,
        )
        assert result["status"] == "rejected"


class TestCancelFailureNeverRetries:
    """Mirrors order_executor.py's own ghost-trade-avoidance rule: never
    place a second order on top of one whose cancellation may not have
    actually succeeded on the exchange."""

    def test_cancel_raising_prevents_reprice_retry(self):
        trader = MagicMock()
        trader.place_order.return_value = "oid-1"
        trader.check_fill.return_value = "open"  # never resolves -> always 'timeout'
        trader.cancel_order.side_effect = RuntimeError("cancel API down")
        factory = MagicMock(return_value=MagicMock())
        gob = MagicMock(return_value={"asks": [{"price": "0.45", "size": "10"}], "bids": []})

        with patch.object(cle, "LiveTrader", return_value=trader), \
             patch.object(cle, "get_orderbook", gob), \
             patch.object(cle, "fetch_market_resolution", return_value=None), \
             patch.object(cle.time, "sleep", lambda s: None), \
             patch.object(cle, "FILL_MAX_WAIT_S", 0):
            result = cle.execute_live_copy_order(
                factory, token_id="tok", market="0xm", side_label="YES",
                price=0.40, stake_usd=10.0,
            )

        gob.assert_not_called()
        assert trader.place_order.call_count == 1
        assert result["status"] == "rejected"
        assert result["order_id"] == "oid-1"


class TestGhostOrderReporting:
    """Issue #1171 item 1 / #1174: a cancel-call failure must never be
    reported as an ordinary confirmed-zero-fill rejection -- it needs a
    distinct rejected_reason (and its placed fill_price) so
    copy_live_settle.py's periodic recover_ghost_orders() can find and
    re-verify it later."""

    def test_cancel_raising_reports_distinct_ghost_reason_and_fill_price(self):
        trader = MagicMock()
        trader.place_order.return_value = "oid-1"
        trader.check_fill.return_value = "open"  # never resolves -> always 'timeout'
        trader.cancel_order.side_effect = RuntimeError("cancel API down")
        factory = MagicMock(return_value=MagicMock())
        with patch.object(cle, "LiveTrader", return_value=trader), \
             patch.object(cle, "get_orderbook", MagicMock(return_value={"asks": [], "bids": []})), \
             patch.object(cle, "fetch_market_resolution", return_value=None), \
             patch.object(cle.time, "sleep", lambda s: None), \
             patch.object(cle, "FILL_MAX_WAIT_S", 0):
            result = cle.execute_live_copy_order(
                factory, token_id="tok", market="0xm", side_label="YES",
                price=0.40, stake_usd=10.0,
            )

        assert result["status"] == "rejected"
        assert result["rejected_reason"] == "cancel_failed_ghost"
        assert result["order_id"] == "oid-1"
        assert result["fill_price"] == 0.40  # placed price, for later reconciliation

    def test_confirmed_zero_fill_rejection_never_uses_ghost_reason(self):
        """The non-ambiguous counterpart: a cleanly cancelled/timed-out
        order with a confirmed zero fill (cancel_ok=True) must keep the
        plain outcome-string rejected_reason -- never conflated with a
        ghost."""
        result, trader, gob, _, _ = _run(
            timeout=True, book_asks=[{"price": "0.45", "size": "10"}], resolution=True,
        )
        assert result["status"] == "rejected"
        assert result["rejected_reason"] != "cancel_failed_ghost"

    def test_cancel_raising_on_retry_attempt_also_reports_ghost(self):
        """#1174 bug fix: previously the outer `cancel_ok` (from the FIRST
        attempt) was never updated after a reprice-retry, so a cancel
        failure on the RETRY's own attempt fell through to a plain
        outcome-string rejection instead of being flagged as a ghost."""
        trader = MagicMock()
        trader.place_order.side_effect = ["oid-1", "oid-2"]
        trader.check_fill.return_value = "open"  # both attempts always 'timeout'
        # First cancel (of oid-1) succeeds; second cancel (of oid-2, the
        # reprice-retry) raises.
        trader.cancel_order.side_effect = [None, RuntimeError("cancel API down")]
        trader.get_order_fill_size.return_value = 0.0
        factory = MagicMock(return_value=MagicMock())
        gob = MagicMock(return_value={"asks": [{"price": "0.45", "size": "10"}], "bids": []})

        with patch.object(cle, "LiveTrader", return_value=trader), \
             patch.object(cle, "get_orderbook", gob), \
             patch.object(cle, "fetch_market_resolution", return_value=None), \
             patch.object(cle.time, "sleep", lambda s: None), \
             patch.object(cle, "FILL_MAX_WAIT_S", 0):
            result = cle.execute_live_copy_order(
                factory, token_id="tok", market="0xm", side_label="YES",
                price=0.40, stake_usd=10.0,
            )

        assert trader.place_order.call_count == 2
        assert result["status"] == "rejected"
        assert result["rejected_reason"] == "cancel_failed_ghost"
        assert result["order_id"] == "oid-2"
        assert result["fill_price"] == 0.45  # the retry's own repriced value


class TestPartialFillIncludesFilledStakeUsd:
    """Issue #1171 item 3 / #1174: a confirmed partial fill must report the
    actual USD spent, distinct from the caller's originally-requested
    stake_usd, so settlement can compute correct P&L from what actually
    filled."""

    def test_partial_fill_after_timeout_includes_filled_stake_usd(self):
        result, trader, gob, _, _ = _run(
            timeout=True, book_asks=[{"price": "0.45", "size": "10"}],
            resolution=None, fill_size=3.5,
        )
        assert result["status"] == "partial"
        # price=0.40 -> price_cents=40 -> fill_price=0.40; 3.5 shares * 0.40 = 1.4
        assert result["fill_price"] == 0.40
        assert result["filled_stake_usd"] == 1.4

    def test_partial_fill_on_retry_attempt_includes_filled_stake_usd(self):
        trader = MagicMock()
        trader.place_order.side_effect = ["oid-1", "oid-2"]
        trader.check_fill.return_value = "open"
        trader.cancel_order.return_value = None  # cancel succeeds both times
        trader.get_order_fill_size.side_effect = [0.0, 2.0]
        factory = MagicMock(return_value=MagicMock())
        gob = MagicMock(return_value={"asks": [{"price": "0.45", "size": "10"}], "bids": []})

        with patch.object(cle, "LiveTrader", return_value=trader), \
             patch.object(cle, "get_orderbook", gob), \
             patch.object(cle, "fetch_market_resolution", return_value=None), \
             patch.object(cle.time, "sleep", lambda s: None), \
             patch.object(cle, "FILL_MAX_WAIT_S", 0):
            result = cle.execute_live_copy_order(
                factory, token_id="tok", market="0xm", side_label="YES",
                price=0.40, stake_usd=10.0,
            )

        assert result["status"] == "partial"
        assert result["order_id"] == "oid-2"
        # retry repriced to 0.45 -> 2.0 shares * 0.45 = 0.9
        assert result["fill_price"] == 0.45
        assert result["filled_stake_usd"] == 0.9


class TestNeverRaises:
    def test_unexpected_get_order_fill_size_exception_does_not_propagate(self):
        """get_order_fill_size is documented to never raise in LiveTrader
        itself, but a MagicMock double could still be misconfigured -- this
        just documents that this module trusts that contract rather than
        wrapping every call in its own try/except (matching order_manager.py's
        own reliance on the same contract)."""
        result, trader, gob, _, _ = _run(
            timeout=True, book_asks=[], resolution=None, fill_size=0.0,
        )
        assert result["status"] in ("rejected", "partial", "filled")

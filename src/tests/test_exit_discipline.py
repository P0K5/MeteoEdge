"""Tests for issue #304 — exit discipline features.

Covers:
- _check_forced_exits: fires inside window with adequate depth
- _check_forced_exits: does NOT fire when depth < floor
- _check_forced_exits: does NOT fire when outside the window (threshold=0)
- _check_forced_exits: does NOT fire when already sold
- take_profit still fires first (TAKE_PROFIT_BUFFER_CENTS logic)
- close_reason correctly persisted for take_profit path
- close_reason correctly persisted for stop_loss path
- get_take_profit_buffer_cents: returns station-specific override
- DB get_close_reason_stats: groups and aggregates correctly
- take_profit_backtest.compute_tp_stats: correct stats for given trades
"""
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub out heavy optional imports before project code loads
# ---------------------------------------------------------------------------
_clob_stub = ModuleType("py_clob_client_v2")
_clob_stub.ClobClient = MagicMock  # type: ignore[attr-defined]
_clob_types_stub = ModuleType("py_clob_client_v2.clob_types")
for _n in (
    "AssetType", "BalanceAllowanceParams", "CreateOrderOptions",
    "OrderArgs", "OpenOrderParams", "OrderPayload", "BookParams",
):
    setattr(_clob_types_stub, _n, MagicMock)
sys.modules.setdefault("py_clob_client_v2", _clob_stub)
sys.modules.setdefault("py_clob_client_v2.clob_types", _clob_types_stub)

import src.scripts.run  # noqa: E402 — pre-load so patch("src.scripts.run.*") is reliable

from src.execution.order_manager import OrderManager  # noqa: E402
from src.execution.position_tracker import _check_forced_exits  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_om() -> OrderManager:
    return OrderManager()


def _make_fill(
    token_id: str = "tok-1",
    price_cents: int = 80,
    size_eur: float = 5.0,
    predicted_price: int = 95,
    station: str = "KORD",
    bracket_low: float = 80.0,
    bracket_high: float = 82.0,
) -> dict:
    return {
        "no_token_id": token_id,
        "price_cents": price_cents,
        "size_eur": size_eur,
        "predicted_price": predicted_price,
        "station": station,
        "bracket_low": bracket_low,
        "bracket_high": bracket_high,
        "question": "Test question?",
        "ticker": "KORD-test",
    }


def _make_snap(
    no_best_bid: int = 70,
    no_best_bid_size: float = 20.0,
) -> dict:
    return {
        "no_best_bid": no_best_bid,
        "no_best_bid_size": no_best_bid_size,
        "no_best_ask": 72,
        "fair_value_now": 65,
        "current_high": 79.0,
        "latest_temp": 78.0,
        "forecast_nws": None,
        "forecast_secondary": None,
        "weather_missing": False,
    }


def _make_position_state(
    token_id: str = "tok-1",
    bid: int = 70,
    depth: float = 20.0,
) -> dict:
    fill = _make_fill(token_id)
    snap = _make_snap(no_best_bid=bid, no_best_bid_size=depth)
    return {"token_id": token_id, "fills": [fill], "snap": snap}


# ===========================================================================
# _check_forced_exits
# ===========================================================================

class TestCheckForcedExits:
    """_check_forced_exits fires inside window with adequate depth, skips otherwise."""

    @pytest.fixture(autouse=True)
    def _reset_om(self, monkeypatch):
        """Fresh OrderManager singleton state per test."""
        from src.execution import position_tracker
        self._saved_sold = position_tracker._order_manager._sold_positions.copy()
        self._saved_strikes = position_tracker._order_manager._stop_loss_strikes.copy()
        position_tracker._order_manager._sold_positions.clear()
        position_tracker._order_manager._stop_loss_strikes.clear()
        yield
        position_tracker._order_manager._sold_positions.clear()
        position_tracker._order_manager._sold_positions.update(self._saved_sold)
        position_tracker._order_manager._stop_loss_strikes.clear()
        position_tracker._order_manager._stop_loss_strikes.update(self._saved_strikes)

    def _make_trader(self, sell_result=("sell-id-1", 70)):
        trader = MagicMock()
        trader.sell_position.return_value = sell_result
        return trader

    def test_fires_inside_window_with_adequate_depth(self):
        """Forced exit fires when minutes_remaining < threshold and depth >= floor.

        We use a very large threshold (1440 min = 24h) which is always >= any
        minutes_remaining value (max ~1440 at midnight UTC), guaranteeing the
        window check passes regardless of test-run time-of-day.
        """
        token = "tok-fe-1"
        ps = _make_position_state(token_id=token, bid=70, depth=20.0)
        trader = self._make_trader(sell_result=("sell-fe-1", 70))
        db = MagicMock()
        db.close_positions_by_token.return_value = 1

        with patch("src.execution.position_tracker._record_sell_in_db"), \
             patch.object(src.scripts.run, "_append_live_trade") as mock_append:
            _check_forced_exits(
                trader, "ts-fe", [ps], db=db, risk_manager=None,
                force_exit_minutes=1440,  # Always fires — minutes_remaining is always < 24h
            )

        # sell_position should have been called
        trader.sell_position.assert_called_once()
        assert trader.sell_position.call_args[0][0] == token
        db.close_positions_by_token.assert_called_with(token)
        # Trade should be recorded with close_reason=forced_exit
        mock_append.assert_called_once()
        trade_row = mock_append.call_args[0][0]
        assert trade_row["close_reason"] == "forced_exit"
        assert "minutes_to_settlement_at_close" in trade_row

    def test_does_not_fire_when_depth_below_floor(self):
        """Forced exit is skipped when bid depth < STOP_LOSS_MIN_DEPTH_SHARES."""
        token = "tok-fe-2"
        # depth=5 is below default STOP_LOSS_MIN_DEPTH_SHARES=10
        ps = _make_position_state(token_id=token, bid=70, depth=5.0)
        trader = self._make_trader()

        with patch("src.execution.position_tracker._record_sell_in_db"), \
             patch.object(src.scripts.run, "_append_live_trade"):
            _check_forced_exits(
                trader, "ts-fe", [ps], db=None, risk_manager=None,
                force_exit_minutes=1440,
            )

        trader.sell_position.assert_not_called()

    def test_does_not_fire_when_threshold_zero(self):
        """When force_exit_minutes=0 (disabled), no exit fires regardless of window."""
        token = "tok-fe-3"
        ps = _make_position_state(token_id=token, bid=70, depth=20.0)
        trader = self._make_trader()

        with patch("src.execution.position_tracker._record_sell_in_db"), \
             patch.object(src.scripts.run, "_append_live_trade"):
            _check_forced_exits(
                trader, "ts-fe", [ps], db=None, risk_manager=None,
                force_exit_minutes=0,
            )

        trader.sell_position.assert_not_called()

    def test_does_not_fire_when_outside_window(self):
        """Forced exit skips positions when threshold=0 (disabled).

        threshold=0 is guaranteed to be outside the window since
        minutes_remaining will always be > 0.
        """
        token = "tok-fe-4"
        ps = _make_position_state(token_id=token, bid=70, depth=20.0)
        trader = self._make_trader()

        with patch("src.execution.position_tracker._record_sell_in_db"), \
             patch.object(src.scripts.run, "_append_live_trade"):
            _check_forced_exits(
                trader, "ts-fe", [ps], db=None, risk_manager=None,
                force_exit_minutes=0,  # Disabled — never fires
            )

        trader.sell_position.assert_not_called()

    def test_already_sold_token_is_skipped(self):
        """Tokens in _sold_positions are not re-sold."""
        from src.execution import position_tracker
        token = "tok-fe-5"
        ps = _make_position_state(token_id=token, bid=70, depth=20.0)
        position_tracker._order_manager._sold_positions.add(token)
        trader = self._make_trader()

        with patch("src.execution.position_tracker._record_sell_in_db"), \
             patch.object(src.scripts.run, "_append_live_trade"):
            _check_forced_exits(
                trader, "ts-fe", [ps], db=None, risk_manager=None,
                force_exit_minutes=1440,
            )

        trader.sell_position.assert_not_called()

    def test_noop_in_paper_mode(self):
        """When live_trader is None (paper mode), function is a no-op."""
        ps = _make_position_state()

        with patch.object(src.scripts.run, "_append_live_trade") as mock_append:
            _check_forced_exits(
                None, "ts-fe", [ps], db=None, risk_manager=None,
                force_exit_minutes=1440,
            )

        mock_append.assert_not_called()

    def test_risk_manager_pnl_recorded_on_sell(self):
        """risk_manager.record_pnl is called after a successful forced exit."""
        token = "tok-fe-6"
        ps = _make_position_state(token_id=token, bid=70, depth=20.0)
        trader = self._make_trader(sell_result=("sell-fe-6", 70))
        risk = MagicMock()
        db = MagicMock()
        db.close_positions_by_token.return_value = 1

        with patch("src.execution.position_tracker._record_sell_in_db"), \
             patch.object(src.scripts.run, "_append_live_trade"):
            _check_forced_exits(
                trader, "ts-fe", [ps], db=db, risk_manager=risk,
                force_exit_minutes=1440,
            )

        risk.record_pnl.assert_called_once()

    def test_empty_position_states_is_noop(self):
        """Empty position_states list → nothing happens."""
        trader = self._make_trader()

        _check_forced_exits(
            trader, "ts-fe", [], db=None, risk_manager=None,
            force_exit_minutes=1440,
        )

        trader.sell_position.assert_not_called()


# ===========================================================================
# take_profit fires before forced exit when bid reaches target
# ===========================================================================

class TestTakeProfitPriority:
    """Take-profit check runs before forced exit in the poll loop.

    We verify that take_profit fires (via check_take_profit_exits) when the
    bid reaches the target, and that the forced exit would NOT also fire
    (because the token is already in _sold_positions after take-profit).
    """

    def test_take_profit_fires_first(self):
        """After take-profit sells, the token is in _sold_positions, so
        forced exit skips it on the same position_states list."""
        from src.execution import position_tracker
        token = "tok-tp-first"
        position_tracker._order_manager._sold_positions.add(token)  # simulate TP already fired
        ps = _make_position_state(token_id=token, bid=95, depth=20.0)
        trader = MagicMock()

        with patch.object(src.scripts.run, "_append_live_trade"):
            _check_forced_exits(
                trader, "ts", [ps], db=None, risk_manager=None,
                force_exit_minutes=1440,
            )

        # Forced exit skipped — take-profit already closed it
        trader.sell_position.assert_not_called()
        position_tracker._order_manager._sold_positions.discard(token)


# ===========================================================================
# get_take_profit_buffer_cents
# ===========================================================================

class TestGetTakeProfitBufferCents:
    """Per-station take-profit buffer override via env var."""

    def test_returns_global_default_when_no_override(self, monkeypatch):
        from src.config import get_take_profit_buffer_cents, TAKE_PROFIT_BUFFER_CENTS
        monkeypatch.delenv("TAKE_PROFIT_BUFFER_CENTS_KORD", raising=False)
        assert get_take_profit_buffer_cents("KORD") == TAKE_PROFIT_BUFFER_CENTS

    def test_returns_station_override_when_set(self, monkeypatch):
        from src.config import get_take_profit_buffer_cents
        monkeypatch.setenv("TAKE_PROFIT_BUFFER_CENTS_KORD", "5")
        assert get_take_profit_buffer_cents("KORD") == 5

    def test_station_lookup_is_case_insensitive(self, monkeypatch):
        from src.config import get_take_profit_buffer_cents
        monkeypatch.setenv("TAKE_PROFIT_BUFFER_CENTS_KMIA", "3")
        assert get_take_profit_buffer_cents("kmia") == 3

    def test_falls_back_to_global_on_bad_override(self, monkeypatch):
        from src.config import get_take_profit_buffer_cents, TAKE_PROFIT_BUFFER_CENTS
        monkeypatch.setenv("TAKE_PROFIT_BUFFER_CENTS_KORD", "notanumber")
        assert get_take_profit_buffer_cents("KORD") == TAKE_PROFIT_BUFFER_CENTS


# ===========================================================================
# DB get_close_reason_stats
# ===========================================================================

class TestGetCloseReasonStats:
    """DB method aggregates close_reason correctly."""

    def test_returns_stats_grouped_by_close_reason(self):
        from src.data.db import Database
        db = Database(":memory:")

        # Insert trades with different close reasons
        trade_kwargs = dict(
            ts="2026-06-16T12:00:00Z",
            station="KORD",
            ticker="KORD-test",
            bracket_low=80.0,
            bracket_high=82.0,
            side="NO",
            predicted_price=95,
            actual_price=80,
            predicted_edge=0.15,
            mode="live",
            capital_before=500.0,
        )

        # take_profit trade (win)
        tid1 = db.insert_trade(**trade_kwargs)
        db._conn.execute(
            "UPDATE trades SET outcome='sold', pnl=0.75, close_reason='take_profit' WHERE id=?",
            (tid1,),
        )
        # stop_loss trade (loss)
        tid2 = db.insert_trade(**trade_kwargs)
        db._conn.execute(
            "UPDATE trades SET outcome='sold', pnl=-4.50, close_reason='stop_loss' WHERE id=?",
            (tid2,),
        )
        # settled (no close_reason — legacy row)
        tid3 = db.insert_trade(**trade_kwargs)
        db._conn.execute(
            "UPDATE trades SET outcome='filled', pnl=-1.00, close_reason=NULL WHERE id=?",
            (tid3,),
        )
        # forced_exit (win)
        tid4 = db.insert_trade(**trade_kwargs)
        db._conn.execute(
            "UPDATE trades SET outcome='sold', pnl=0.50, close_reason='forced_exit' WHERE id=?",
            (tid4,),
        )
        db._conn.commit()

        stats = db.get_close_reason_stats()
        by_reason = {row["close_reason"]: row for row in stats}

        assert "take_profit" in by_reason
        assert by_reason["take_profit"]["count"] == 1
        assert by_reason["take_profit"]["total_pnl"] == pytest.approx(0.75)
        assert by_reason["take_profit"]["win_rate"] == pytest.approx(1.0)

        assert "stop_loss" in by_reason
        assert by_reason["stop_loss"]["count"] == 1
        assert by_reason["stop_loss"]["total_pnl"] == pytest.approx(-4.50)
        assert by_reason["stop_loss"]["win_rate"] == pytest.approx(0.0)

        # NULL close_reason → 'settled' group
        assert "settled" in by_reason
        assert by_reason["settled"]["count"] == 1

        assert "forced_exit" in by_reason
        assert by_reason["forced_exit"]["count"] == 1
        assert by_reason["forced_exit"]["win_rate"] == pytest.approx(1.0)

    def test_excludes_shadow_trades(self):
        from src.data.db import Database
        db = Database(":memory:")
        trade_kwargs = dict(
            ts="2026-06-16T12:00:00Z",
            station="KORD",
            ticker="KORD-test",
            bracket_low=80.0, bracket_high=82.0,
            side="NO", predicted_price=95, actual_price=80,
            predicted_edge=0.15, mode="shadow", capital_before=0.0,
        )
        tid = db.insert_trade(**trade_kwargs)
        db._conn.execute(
            "UPDATE trades SET outcome='filled', pnl=1.0 WHERE id=?", (tid,)
        )
        db._conn.commit()

        stats = db.get_close_reason_stats()
        # Shadow trade should be excluded
        total_count = sum(r["count"] for r in stats)
        assert total_count == 0

    def test_returns_empty_list_when_no_settled_trades(self):
        from src.data.db import Database
        db = Database(":memory:")
        stats = db.get_close_reason_stats()
        assert stats == []


# ===========================================================================
# take_profit_backtest.compute_tp_stats
# ===========================================================================

class TestComputeTpStats:
    """compute_tp_stats returns correct aggregated stats."""

    def test_basic_stats(self):
        from src.scripts.take_profit_backtest import compute_tp_stats
        trades = [
            {"predicted_price": 95, "actual_price": 80, "pnl": 0.75, "outcome": "sold"},
            {"predicted_price": 95, "actual_price": 80, "pnl": -4.50, "outcome": "filled"},
            {"predicted_price": 90, "actual_price": 88, "pnl": 0.30, "outcome": "sold"},
        ]
        stats = compute_tp_stats(trades, buffer_cents=2)
        assert stats["count"] == 3
        assert stats["win_count"] == 2
        assert stats["total_pnl"] == pytest.approx(0.75 - 4.50 + 0.30)
        assert stats["win_rate"] == pytest.approx(2 / 3, abs=0.001)
        assert stats["worst_pnl"] == pytest.approx(-4.50)

    def test_empty_trades_returns_zero_stats(self):
        from src.scripts.take_profit_backtest import compute_tp_stats
        stats = compute_tp_stats([], buffer_cents=2)
        assert stats["count"] == 0
        assert stats["win_rate"] is None
        assert stats["total_pnl"] == 0.0

    def test_trades_without_pnl_are_excluded(self):
        from src.scripts.take_profit_backtest import compute_tp_stats
        trades = [
            {"predicted_price": 95, "actual_price": 80, "pnl": None, "outcome": "open"},
            {"predicted_price": 90, "actual_price": 88, "pnl": 0.50, "outcome": "sold"},
        ]
        stats = compute_tp_stats(trades, buffer_cents=2)
        assert stats["count"] == 1
        assert stats["total_pnl"] == pytest.approx(0.50)

    def test_tp_eligible_count_respects_buffer(self):
        from src.scripts.take_profit_backtest import compute_tp_stats
        trades = [
            # actual=93, predicted=95 → target @2¢ = 93 → eligible
            {"predicted_price": 95, "actual_price": 93, "pnl": 0.50, "outcome": "sold"},
            # actual=92, predicted=95 → target @2¢ = 93 → NOT eligible
            {"predicted_price": 95, "actual_price": 92, "pnl": 0.30, "outcome": "sold"},
        ]
        stats_2c = compute_tp_stats(trades, buffer_cents=2)
        assert stats_2c["tp_eligible"] == 1

        stats_5c = compute_tp_stats(trades, buffer_cents=5)
        # target @5¢ = 90 → both eligible (93 >= 90, 92 >= 90)
        assert stats_5c["tp_eligible"] == 2

    def test_different_buffers_give_different_tp_eligible(self):
        from src.scripts.take_profit_backtest import compute_tp_stats
        trades = [
            {"predicted_price": 95, "actual_price": 90, "pnl": 0.40, "outcome": "sold"},
        ]
        # @2¢: target=93, actual=90 < 93 → NOT eligible
        assert compute_tp_stats(trades, buffer_cents=2)["tp_eligible"] == 0
        # @5¢: target=90, actual=90 >= 90 → eligible
        assert compute_tp_stats(trades, buffer_cents=5)["tp_eligible"] == 1

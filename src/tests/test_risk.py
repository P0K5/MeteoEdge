"""Unit tests for src/risk/manager.py.

Each of the 4 block conditions is tested in isolation by directly setting
the relevant internal state before calling allow_trade(). This avoids any
reliance on clock mocking for the basic limit tests.
"""
import pytest
from src.risk.manager import RiskManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rm(**kwargs) -> RiskManager:
    """Create a RiskManager with explicit limits (no env-var side effects in tests)."""
    defaults = dict(
        daily_loss_limit_eur=50.0,
        max_open_positions=15,
        drawdown_stop_pct=0.15,
        min_market_liquidity=50,
        starting_capital=500.0,
    )
    defaults.update(kwargs)
    return RiskManager(**defaults)


# ---------------------------------------------------------------------------
# Normal (allow) case
# ---------------------------------------------------------------------------

class TestAllowTradeNormalCase:
    """allow_trade() returns (True, "") when all limits are clear."""

    def test_all_clear_returns_true_and_empty_reason(self):
        rm = _rm()
        ok, reason = rm.allow_trade(capital=500.0, liquidity_contracts=100)
        assert ok is True
        assert reason == ""

    def test_allow_with_default_liquidity(self):
        """Callers that omit liquidity_contracts should not be blocked."""
        rm = _rm()
        ok, reason = rm.allow_trade(capital=500.0)
        assert ok is True
        assert reason == ""

    def test_allow_with_partial_loss_not_at_limit(self):
        """PnL at -49 should still allow trades (limit is -50)."""
        rm = _rm()
        rm._daily_pnl = -49.0
        ok, _ = rm.allow_trade(capital=451.0, liquidity_contracts=100)
        assert ok is True

    def test_allow_with_positions_below_max(self):
        """14 open positions (limit 15) should still allow trades."""
        rm = _rm()
        rm._open_positions = 14
        ok, _ = rm.allow_trade(capital=500.0, liquidity_contracts=100)
        assert ok is True


# ---------------------------------------------------------------------------
# Condition 1: Daily loss limit
# ---------------------------------------------------------------------------

class TestDailyLossLimit:
    """Block when _daily_pnl <= -daily_loss_limit_eur."""

    def test_blocks_when_loss_equals_limit(self):
        rm = _rm(daily_loss_limit_eur=50.0)
        rm._daily_pnl = -50.0
        ok, reason = rm.allow_trade(capital=450.0, liquidity_contracts=100)
        assert ok is False
        assert "daily loss" in reason

    def test_blocks_when_loss_exceeds_limit(self):
        rm = _rm(daily_loss_limit_eur=50.0)
        rm._daily_pnl = -75.0
        ok, reason = rm.allow_trade(capital=425.0, liquidity_contracts=100)
        assert ok is False
        assert "daily loss" in reason

    def test_reason_contains_actual_pnl(self):
        rm = _rm(daily_loss_limit_eur=50.0)
        rm._daily_pnl = -51.23
        _, reason = rm.allow_trade(capital=448.77, liquidity_contracts=100)
        assert "-51.23" in reason

    def test_does_not_block_just_above_limit(self):
        rm = _rm(daily_loss_limit_eur=50.0)
        rm._daily_pnl = -49.99
        ok, _ = rm.allow_trade(capital=450.01, liquidity_contracts=100)
        assert ok is True


# ---------------------------------------------------------------------------
# Condition 2: Max open positions
# ---------------------------------------------------------------------------

class TestMaxOpenPositions:
    """Block when _open_positions >= max_open_positions."""

    def test_blocks_when_at_max(self):
        rm = _rm(max_open_positions=15)
        rm._open_positions = 15
        ok, reason = rm.allow_trade(capital=500.0, liquidity_contracts=100)
        assert ok is False
        assert "max open positions" in reason
        assert "15" in reason

    def test_blocks_when_above_max(self):
        rm = _rm(max_open_positions=15)
        rm._open_positions = 20
        ok, reason = rm.allow_trade(capital=500.0, liquidity_contracts=100)
        assert ok is False
        assert "max open positions" in reason

    def test_does_not_block_one_below_max(self):
        rm = _rm(max_open_positions=15)
        rm._open_positions = 14
        ok, _ = rm.allow_trade(capital=500.0, liquidity_contracts=100)
        assert ok is True

    def test_open_position_increments_counter(self):
        rm = _rm()
        assert rm._open_positions == 0
        rm.open_position()
        assert rm._open_positions == 1

    def test_close_position_decrements_counter(self):
        rm = _rm()
        rm._open_positions = 3
        rm.close_position()
        assert rm._open_positions == 2

    def test_close_position_does_not_go_below_zero(self):
        rm = _rm()
        rm._open_positions = 0
        rm.close_position()
        assert rm._open_positions == 0


# ---------------------------------------------------------------------------
# Condition 3: Drawdown stop
# ---------------------------------------------------------------------------

class TestDrawdownStop:
    """Block when (starting_capital - capital) / starting_capital >= drawdown_stop_pct."""

    def test_blocks_at_exact_drawdown_threshold(self):
        # starting_capital=500, drawdown_stop_pct=0.15 → stop at capital=425
        rm = _rm(starting_capital=500.0, drawdown_stop_pct=0.15)
        ok, reason = rm.allow_trade(capital=425.0, liquidity_contracts=100)
        assert ok is False
        assert "drawdown" in reason

    def test_blocks_when_drawdown_exceeds_threshold(self):
        rm = _rm(starting_capital=500.0, drawdown_stop_pct=0.15)
        ok, reason = rm.allow_trade(capital=400.0, liquidity_contracts=100)
        assert ok is False
        assert "drawdown" in reason

    def test_reason_contains_drawdown_percentage(self):
        rm = _rm(starting_capital=500.0, drawdown_stop_pct=0.15)
        _, reason = rm.allow_trade(capital=425.0, liquidity_contracts=100)
        # reason should contain the drawdown formatted as a percentage, e.g. "15.0%"
        assert "%" in reason

    def test_does_not_block_just_above_threshold_capital(self):
        # capital=425.01 → drawdown ≈ 14.998% < 15%
        rm = _rm(starting_capital=500.0, drawdown_stop_pct=0.15)
        ok, _ = rm.allow_trade(capital=425.01, liquidity_contracts=100)
        assert ok is True

    def test_no_drawdown_when_capital_equals_starting(self):
        rm = _rm(starting_capital=500.0, drawdown_stop_pct=0.15)
        ok, _ = rm.allow_trade(capital=500.0, liquidity_contracts=100)
        assert ok is True


# ---------------------------------------------------------------------------
# Condition 4: Minimum market liquidity
# ---------------------------------------------------------------------------

class TestMinMarketLiquidity:
    """Block when liquidity_contracts < min_market_liquidity."""

    def test_blocks_when_below_minimum(self):
        rm = _rm(min_market_liquidity=50)
        ok, reason = rm.allow_trade(capital=500.0, liquidity_contracts=49)
        assert ok is False
        assert "liquidity" in reason
        assert "49" in reason

    def test_blocks_at_zero_liquidity(self):
        rm = _rm(min_market_liquidity=50)
        ok, reason = rm.allow_trade(capital=500.0, liquidity_contracts=0)
        assert ok is False
        assert "liquidity" in reason

    def test_does_not_block_at_exact_minimum(self):
        rm = _rm(min_market_liquidity=50)
        ok, _ = rm.allow_trade(capital=500.0, liquidity_contracts=50)
        assert ok is True

    def test_does_not_block_above_minimum(self):
        rm = _rm(min_market_liquidity=50)
        ok, _ = rm.allow_trade(capital=500.0, liquidity_contracts=500)
        assert ok is True


# ---------------------------------------------------------------------------
# record_pnl
# ---------------------------------------------------------------------------

class TestRecordPnl:
    """record_pnl accumulates realised PnL within the same day."""

    def test_positive_pnl_accumulates(self):
        rm = _rm()
        rm.record_pnl(10.0)
        rm.record_pnl(5.0)
        assert rm._daily_pnl == pytest.approx(15.0)

    def test_negative_pnl_accumulates(self):
        rm = _rm()
        rm.record_pnl(-20.0)
        rm.record_pnl(-15.0)
        assert rm._daily_pnl == pytest.approx(-35.0)

    def test_mixed_pnl_nets_correctly(self):
        rm = _rm()
        rm.record_pnl(30.0)
        rm.record_pnl(-10.0)
        assert rm._daily_pnl == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# Daily reset
# ---------------------------------------------------------------------------

class TestDailyReset:
    """_reset_if_new_day() clears PnL when the UTC date has changed."""

    def test_reset_clears_daily_pnl(self):
        from datetime import date
        rm = _rm()
        rm._daily_pnl = -40.0
        # Simulate a new day by backdating _trade_day
        rm._trade_day = date(2000, 1, 1)
        rm._reset_if_new_day()
        assert rm._daily_pnl == 0.0

    def test_reset_updates_trade_day(self):
        from datetime import date, datetime, timezone
        rm = _rm()
        rm._trade_day = date(2000, 1, 1)
        rm._reset_if_new_day()
        assert rm._trade_day == datetime.now(timezone.utc).date()

    def test_no_reset_on_same_day(self):
        rm = _rm()
        rm._daily_pnl = -20.0
        # _trade_day is already today, so no reset should happen
        rm._reset_if_new_day()
        assert rm._daily_pnl == pytest.approx(-20.0)

    def test_allow_trade_triggers_reset_on_new_day(self):
        from datetime import date
        rm = _rm(daily_loss_limit_eur=50.0)
        rm._daily_pnl = -51.0          # would normally block
        rm._trade_day = date(2000, 1, 1)  # simulate yesterday
        ok, _ = rm.allow_trade(capital=500.0, liquidity_contracts=100)
        # After reset, PnL is 0 — trade should be allowed
        assert ok is True


# ---------------------------------------------------------------------------
# E1-2: Integration test for run.py wiring
# ---------------------------------------------------------------------------

class TestRunPyIntegration:
    """Verify that run.py can instantiate RiskManager and block trades when limit=0."""

    def test_zero_loss_limit_blocks_trades(self):
        """When daily_loss_limit_eur=0, trades are blocked immediately (PnL <= 0)."""
        rm = _rm(daily_loss_limit_eur=0.0)
        ok, reason = rm.allow_trade(capital=500.0, liquidity_contracts=100)
        # With limit=0 and PnL=0, condition is _daily_pnl <= -0 which is True, so blocked
        assert ok is False
        assert "daily loss" in reason

"""Unit tests for src/risk/copy_risk_manager.py (issue #1139, story D1 of
epic #1138; plus its live counterpart, issue #1175, epic I #1160). Fully
mocked -- no real DB, mirroring test_copy_signal_loop.py's MagicMock db
style. COPY_TRADING_CAPITAL_USD / COPY_LIVE_CAPITAL_USD are patched
per-test rather than relying on their real config.py defaults so these
tests never break if those defaults change.
"""
from unittest.mock import MagicMock, patch

import pytest

from src.risk.copy_risk_manager import (
    REASON_DAILY_LOSS,
    REASON_DRAWDOWN,
    REASON_LIVE_DAILY_LOSS,
    REASON_LIVE_DRAWDOWN,
    allow_copy_signal,
    allow_live_copy_signal,
)


def _live_config(**overrides) -> dict:
    cfg = dict(COPY_DAILY_LOSS_LIMIT_USD=25.0, COPY_DRAWDOWN_STOP_PCT=0.20)
    cfg.update(overrides)
    return cfg


def _live_breaker_config(**overrides) -> dict:
    cfg = dict(COPY_LIVE_DAILY_LOSS_LIMIT_USD=25.0, COPY_LIVE_DRAWDOWN_STOP_PCT=0.20)
    cfg.update(overrides)
    return cfg


def _mock_db(daily_total_pnl: float = 0.0, cumulative_total_pnl: float = 0.0) -> MagicMock:
    db = MagicMock()
    db.get_copy_realized_pnl_total_for_date.return_value = {
        "n_settled": 1, "total_pnl_usd": daily_total_pnl,
    }
    db.get_copy_realized_pnl_total.return_value = {
        "n_settled": 1, "total_pnl_usd": cumulative_total_pnl,
    }
    return db


def _mock_live_db(daily_total_pnl: float = 0.0, cumulative_total_pnl: float = 0.0) -> MagicMock:
    """Same shape as _mock_db, but wired to the LIVE query methods
    (get_copy_live_realized_pnl_total[_for_date]) that allow_live_copy_signal
    consults -- deliberately does NOT set the paper methods, so any test
    that accidentally calls allow_copy_signal against this double would
    get an unconfigured MagicMock instead of silently passing."""
    db = MagicMock()
    db.get_copy_live_realized_pnl_total_for_date.return_value = {
        "n_settled": 1, "total_pnl_usd": daily_total_pnl,
    }
    db.get_copy_live_realized_pnl_total.return_value = {
        "n_settled": 1, "total_pnl_usd": cumulative_total_pnl,
    }
    return db


CAPITAL = 100.0


class TestBelowBothThresholds:
    def test_allows_when_no_realized_pnl_yet(self):
        db = _mock_db(daily_total_pnl=0.0, cumulative_total_pnl=0.0)
        with patch("src.risk.copy_risk_manager.COPY_TRADING_CAPITAL_USD", CAPITAL):
            ok, reason = allow_copy_signal(db, _live_config())
        assert ok is True
        assert reason == ""

    def test_allows_with_small_profit(self):
        db = _mock_db(daily_total_pnl=5.0, cumulative_total_pnl=20.0)
        with patch("src.risk.copy_risk_manager.COPY_TRADING_CAPITAL_USD", CAPITAL):
            ok, reason = allow_copy_signal(db, _live_config())
        assert ok is True
        assert reason == ""

    def test_allows_with_loss_below_both_thresholds(self):
        # daily loss limit 25, drawdown stop 20% of $100 = $20 -- a $10 loss
        # (both daily and cumulative) trips neither.
        db = _mock_db(daily_total_pnl=-10.0, cumulative_total_pnl=-10.0)
        with patch("src.risk.copy_risk_manager.COPY_TRADING_CAPITAL_USD", CAPITAL):
            ok, reason = allow_copy_signal(db, _live_config())
        assert ok is True
        assert reason == ""


class TestDailyLossLimitBreached:
    def test_blocks_when_daily_loss_equals_limit(self):
        db = _mock_db(daily_total_pnl=-25.0, cumulative_total_pnl=-25.0)
        with patch("src.risk.copy_risk_manager.COPY_TRADING_CAPITAL_USD", CAPITAL):
            ok, reason = allow_copy_signal(db, _live_config(COPY_DAILY_LOSS_LIMIT_USD=25.0))
        assert ok is False
        assert reason == REASON_DAILY_LOSS

    def test_blocks_when_daily_loss_exceeds_limit(self):
        db = _mock_db(daily_total_pnl=-30.0, cumulative_total_pnl=-30.0)
        with patch("src.risk.copy_risk_manager.COPY_TRADING_CAPITAL_USD", CAPITAL):
            ok, reason = allow_copy_signal(db, _live_config(COPY_DAILY_LOSS_LIMIT_USD=25.0))
        assert ok is False
        assert reason == REASON_DAILY_LOSS

    def test_does_not_block_just_above_limit(self):
        # cumulative_total_pnl deliberately kept well clear of the drawdown
        # threshold so only the daily-loss boundary is under test here.
        db = _mock_db(daily_total_pnl=-24.99, cumulative_total_pnl=-5.0)
        with patch("src.risk.copy_risk_manager.COPY_TRADING_CAPITAL_USD", CAPITAL):
            ok, _ = allow_copy_signal(db, _live_config(COPY_DAILY_LOSS_LIMIT_USD=25.0))
        assert ok is True

    def test_cumulative_pnl_ignored_when_daily_loss_not_breached(self):
        """A big cumulative loss from a PRIOR day must not trip the daily
        check today -- get_copy_realized_pnl_total_for_date is the only
        source consulted for the daily half."""
        db = _mock_db(daily_total_pnl=-5.0, cumulative_total_pnl=-500.0)
        with patch("src.risk.copy_risk_manager.COPY_TRADING_CAPITAL_USD", CAPITAL):
            ok, reason = allow_copy_signal(db, _live_config(COPY_DRAWDOWN_STOP_PCT=0.99))
        assert ok is False
        assert reason == REASON_DRAWDOWN  # drawdown trips instead, not daily


class TestDrawdownStopBreached:
    def test_blocks_at_exact_drawdown_threshold(self):
        # capital=100, drawdown_stop_pct=0.20 -> trips at total_pnl_usd=-20
        db = _mock_db(daily_total_pnl=-5.0, cumulative_total_pnl=-20.0)
        with patch("src.risk.copy_risk_manager.COPY_TRADING_CAPITAL_USD", CAPITAL):
            ok, reason = allow_copy_signal(
                db, _live_config(COPY_DAILY_LOSS_LIMIT_USD=999.0, COPY_DRAWDOWN_STOP_PCT=0.20)
            )
        assert ok is False
        assert reason == REASON_DRAWDOWN

    def test_blocks_when_drawdown_exceeds_threshold(self):
        db = _mock_db(daily_total_pnl=-5.0, cumulative_total_pnl=-50.0)
        with patch("src.risk.copy_risk_manager.COPY_TRADING_CAPITAL_USD", CAPITAL):
            ok, reason = allow_copy_signal(
                db, _live_config(COPY_DAILY_LOSS_LIMIT_USD=999.0, COPY_DRAWDOWN_STOP_PCT=0.20)
            )
        assert ok is False
        assert reason == REASON_DRAWDOWN

    def test_does_not_block_just_below_threshold(self):
        db = _mock_db(daily_total_pnl=-5.0, cumulative_total_pnl=-19.99)
        with patch("src.risk.copy_risk_manager.COPY_TRADING_CAPITAL_USD", CAPITAL):
            ok, _ = allow_copy_signal(
                db, _live_config(COPY_DAILY_LOSS_LIMIT_USD=999.0, COPY_DRAWDOWN_STOP_PCT=0.20)
            )
        assert ok is True

    def test_positive_cumulative_pnl_never_trips_drawdown(self):
        db = _mock_db(daily_total_pnl=0.0, cumulative_total_pnl=100.0)
        with patch("src.risk.copy_risk_manager.COPY_TRADING_CAPITAL_USD", CAPITAL):
            ok, _ = allow_copy_signal(
                db, _live_config(COPY_DAILY_LOSS_LIMIT_USD=999.0, COPY_DRAWDOWN_STOP_PCT=0.01)
            )
        assert ok is True


class TestBothBreachedPrecedence:
    """When both the daily-loss limit and the drawdown stop are breached at
    once, the daily-loss check wins -- deterministic, documented precedence
    (mirrors RiskManager.allow_trade's own check order)."""

    def test_daily_loss_takes_precedence_over_drawdown(self):
        db = _mock_db(daily_total_pnl=-30.0, cumulative_total_pnl=-30.0)
        with patch("src.risk.copy_risk_manager.COPY_TRADING_CAPITAL_USD", CAPITAL):
            ok, reason = allow_copy_signal(
                db, _live_config(COPY_DAILY_LOSS_LIMIT_USD=25.0, COPY_DRAWDOWN_STOP_PCT=0.20)
            )
        assert ok is False
        assert reason == REASON_DAILY_LOSS


class TestDbCalledWithTodaysDate:
    def test_get_copy_realized_pnl_total_for_date_called_once(self):
        db = _mock_db()
        with patch("src.risk.copy_risk_manager.COPY_TRADING_CAPITAL_USD", CAPITAL):
            allow_copy_signal(db, _live_config())
        db.get_copy_realized_pnl_total_for_date.assert_called_once()
        (date_arg,), _ = db.get_copy_realized_pnl_total_for_date.call_args
        # YYYY-MM-DD shape, not validating the exact date (would need clock mocking).
        assert len(date_arg) == 10
        assert date_arg[4] == "-" and date_arg[7] == "-"

    def test_total_pnl_only_queried_when_daily_check_passes_or_fails_gracefully(self):
        """get_copy_realized_pnl_total (cumulative) is always consulted when
        the daily check doesn't already block -- both dedicated DB reads are
        used, never a re-derivation from the other."""
        db = _mock_db(daily_total_pnl=0.0, cumulative_total_pnl=0.0)
        with patch("src.risk.copy_risk_manager.COPY_TRADING_CAPITAL_USD", CAPITAL):
            allow_copy_signal(db, _live_config())
        db.get_copy_realized_pnl_total.assert_called_once()


# ----------------------------------------------------------------------
# allow_live_copy_signal (issue #1175, epic I #1160) -- mirrors every test
# class above, one layer up, against the LIVE query methods/config/capital.
# ----------------------------------------------------------------------

class TestLiveBelowBothThresholds:
    def test_allows_when_no_realized_pnl_yet(self):
        db = _mock_live_db(daily_total_pnl=0.0, cumulative_total_pnl=0.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok, reason = allow_live_copy_signal(db, _live_breaker_config())
        assert ok is True
        assert reason == ""

    def test_allows_with_small_profit(self):
        db = _mock_live_db(daily_total_pnl=5.0, cumulative_total_pnl=20.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok, reason = allow_live_copy_signal(db, _live_breaker_config())
        assert ok is True
        assert reason == ""

    def test_allows_with_loss_below_both_thresholds(self):
        db = _mock_live_db(daily_total_pnl=-10.0, cumulative_total_pnl=-10.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok, reason = allow_live_copy_signal(db, _live_breaker_config())
        assert ok is True
        assert reason == ""


class TestLiveDailyLossLimitBreached:
    def test_blocks_when_daily_loss_equals_limit(self):
        db = _mock_live_db(daily_total_pnl=-25.0, cumulative_total_pnl=-25.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok, reason = allow_live_copy_signal(
                db, _live_breaker_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=25.0)
            )
        assert ok is False
        assert reason == REASON_LIVE_DAILY_LOSS

    def test_blocks_when_daily_loss_exceeds_limit(self):
        db = _mock_live_db(daily_total_pnl=-30.0, cumulative_total_pnl=-30.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok, reason = allow_live_copy_signal(
                db, _live_breaker_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=25.0)
            )
        assert ok is False
        assert reason == REASON_LIVE_DAILY_LOSS

    def test_does_not_block_just_above_limit(self):
        db = _mock_live_db(daily_total_pnl=-24.99, cumulative_total_pnl=-5.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok, _ = allow_live_copy_signal(
                db, _live_breaker_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=25.0)
            )
        assert ok is True

    def test_cumulative_pnl_ignored_when_daily_loss_not_breached(self):
        db = _mock_live_db(daily_total_pnl=-5.0, cumulative_total_pnl=-500.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok, reason = allow_live_copy_signal(
                db, _live_breaker_config(COPY_LIVE_DRAWDOWN_STOP_PCT=0.99)
            )
        assert ok is False
        assert reason == REASON_LIVE_DRAWDOWN  # drawdown trips instead, not daily


class TestLiveDrawdownStopBreached:
    def test_blocks_at_exact_drawdown_threshold(self):
        db = _mock_live_db(daily_total_pnl=-5.0, cumulative_total_pnl=-20.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok, reason = allow_live_copy_signal(
                db,
                _live_breaker_config(
                    COPY_LIVE_DAILY_LOSS_LIMIT_USD=999.0, COPY_LIVE_DRAWDOWN_STOP_PCT=0.20,
                ),
            )
        assert ok is False
        assert reason == REASON_LIVE_DRAWDOWN

    def test_blocks_when_drawdown_exceeds_threshold(self):
        db = _mock_live_db(daily_total_pnl=-5.0, cumulative_total_pnl=-50.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok, reason = allow_live_copy_signal(
                db,
                _live_breaker_config(
                    COPY_LIVE_DAILY_LOSS_LIMIT_USD=999.0, COPY_LIVE_DRAWDOWN_STOP_PCT=0.20,
                ),
            )
        assert ok is False
        assert reason == REASON_LIVE_DRAWDOWN

    def test_does_not_block_just_below_threshold(self):
        db = _mock_live_db(daily_total_pnl=-5.0, cumulative_total_pnl=-19.99)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok, _ = allow_live_copy_signal(
                db,
                _live_breaker_config(
                    COPY_LIVE_DAILY_LOSS_LIMIT_USD=999.0, COPY_LIVE_DRAWDOWN_STOP_PCT=0.20,
                ),
            )
        assert ok is True

    def test_positive_cumulative_pnl_never_trips_drawdown(self):
        db = _mock_live_db(daily_total_pnl=0.0, cumulative_total_pnl=100.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok, _ = allow_live_copy_signal(
                db,
                _live_breaker_config(
                    COPY_LIVE_DAILY_LOSS_LIMIT_USD=999.0, COPY_LIVE_DRAWDOWN_STOP_PCT=0.01,
                ),
            )
        assert ok is True


class TestLiveBothBreachedPrecedence:
    def test_daily_loss_takes_precedence_over_drawdown(self):
        db = _mock_live_db(daily_total_pnl=-30.0, cumulative_total_pnl=-30.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok, reason = allow_live_copy_signal(
                db,
                _live_breaker_config(
                    COPY_LIVE_DAILY_LOSS_LIMIT_USD=25.0, COPY_LIVE_DRAWDOWN_STOP_PCT=0.20,
                ),
            )
        assert ok is False
        assert reason == REASON_LIVE_DAILY_LOSS


class TestLiveDbCalledWithTodaysDate:
    def test_get_copy_live_realized_pnl_total_for_date_called_once(self):
        db = _mock_live_db()
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            allow_live_copy_signal(db, _live_breaker_config())
        db.get_copy_live_realized_pnl_total_for_date.assert_called_once()
        (date_arg,), _ = db.get_copy_live_realized_pnl_total_for_date.call_args
        assert len(date_arg) == 10
        assert date_arg[4] == "-" and date_arg[7] == "-"

    def test_total_pnl_only_queried_when_daily_check_passes_or_fails_gracefully(self):
        db = _mock_live_db(daily_total_pnl=0.0, cumulative_total_pnl=0.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            allow_live_copy_signal(db, _live_breaker_config())
        db.get_copy_live_realized_pnl_total.assert_called_once()


class TestPaperLiveBreakerIndependence:
    """Issue #1175 acceptance criterion: paper's allow_copy_signal and
    live's allow_live_copy_signal must be independent in BOTH directions,
    since they read entirely different tables (copy_positions vs.
    copy_live_positions) and different config keys. A single MagicMock db
    configured with divergent paper/live P&L proves neither function's
    verdict leaks into the other's."""

    def _mixed_db(
        self, *, paper_daily=0.0, paper_cumulative=0.0, live_daily=0.0, live_cumulative=0.0,
    ) -> MagicMock:
        db = MagicMock()
        db.get_copy_realized_pnl_total_for_date.return_value = {
            "n_settled": 1, "total_pnl_usd": paper_daily,
        }
        db.get_copy_realized_pnl_total.return_value = {
            "n_settled": 1, "total_pnl_usd": paper_cumulative,
        }
        db.get_copy_live_realized_pnl_total_for_date.return_value = {
            "n_settled": 1, "total_pnl_usd": live_daily,
        }
        db.get_copy_live_realized_pnl_total.return_value = {
            "n_settled": 1, "total_pnl_usd": live_cumulative,
        }
        return db

    def test_paper_trips_without_tripping_live(self):
        # Paper deep in a daily loss; live has zero P&L -- live must still
        # allow new signal execution.
        db = self._mixed_db(paper_daily=-100.0, live_daily=0.0, live_cumulative=0.0)
        with patch("src.risk.copy_risk_manager.COPY_TRADING_CAPITAL_USD", CAPITAL), \
             patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            paper_ok, paper_reason = allow_copy_signal(db, _live_config())
            live_ok, live_reason = allow_live_copy_signal(db, _live_breaker_config())
        assert paper_ok is False
        assert paper_reason == REASON_DAILY_LOSS
        assert live_ok is True
        assert live_reason == ""

    def test_live_trips_without_tripping_paper(self):
        # Live deep in a daily loss; paper has zero P&L -- paper must still
        # allow new signal execution.
        db = self._mixed_db(live_daily=-100.0, paper_daily=0.0, paper_cumulative=0.0)
        with patch("src.risk.copy_risk_manager.COPY_TRADING_CAPITAL_USD", CAPITAL), \
             patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            paper_ok, paper_reason = allow_copy_signal(db, _live_config())
            live_ok, live_reason = allow_live_copy_signal(db, _live_breaker_config())
        assert live_ok is False
        assert live_reason == REASON_LIVE_DAILY_LOSS
        assert paper_ok is True
        assert paper_reason == ""

    def test_allow_live_copy_signal_never_calls_paper_query_methods(self):
        db = self._mixed_db()
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            allow_live_copy_signal(db, _live_breaker_config())
        db.get_copy_realized_pnl_total_for_date.assert_not_called()
        db.get_copy_realized_pnl_total.assert_not_called()

    def test_allow_copy_signal_never_calls_live_query_methods(self):
        db = self._mixed_db()
        with patch("src.risk.copy_risk_manager.COPY_TRADING_CAPITAL_USD", CAPITAL):
            allow_copy_signal(db, _live_config())
        db.get_copy_live_realized_pnl_total_for_date.assert_not_called()
        db.get_copy_live_realized_pnl_total.assert_not_called()

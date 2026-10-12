"""Unit tests for src/risk/copy_risk_manager.py (issue #1139, story D1 of
epic #1138; plus its live counterpart, issue #1175, epic I #1160; plus the
live breaker's persisted-trip/manual-reset mechanics, issue #1348; plus the
fail-closed-without-capital and clear-moves-a-baseline fixes, issue #1300).
Fully mocked -- no real DB, mirroring test_copy_signal_loop.py's MagicMock db
style. COPY_TRADING_CAPITAL_USD / COPY_LIVE_CAPITAL_USD are patched
per-test rather than relying on their real config.py defaults so these
tests never break if those defaults change.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.risk.copy_risk_manager import (
    REASON_DAILY_LOSS,
    REASON_DRAWDOWN,
    REASON_LIVE_DAILY_LOSS,
    REASON_LIVE_DRAWDOWN,
    REASON_LIVE_NO_CAPITAL,
    allow_copy_signal,
    allow_live_copy_signal,
    get_live_circuit_breaker_status,
    reset_live_circuit_breaker,
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


class _FakeConfigStore:
    """A tiny in-memory substitute for ``Database.get_config``/``set_config``,
    backed by a plain dict -- lets the persisted-trip tests (issue #1348)
    exercise copy_risk_manager's real read/write/clear cycle without a real
    sqlite ``Database``. Crucially, this dict lives OUTSIDE any mock db
    object: wiring a brand-new ``MagicMock`` to the SAME store (see
    ``_mock_live_db_with_config``) stands in for a ``copy_signal_loop.py``
    process restart -- a fresh object, same persisted facts, proving the
    "no in-memory module state" contract this module's docstring claims.
    """

    def __init__(self):
        self._store: dict = {}

    def get_config(self, key):
        return self._store.get(key)

    def set_config(self, key, value):
        self._store[key] = value


def _mock_live_db_with_config(
    store: "_FakeConfigStore", daily_total_pnl: float = 0.0, cumulative_total_pnl: float = 0.0,
) -> MagicMock:
    """``_mock_live_db``, plus ``get_config``/``set_config`` wired to
    *store* so the persisted live-breaker trip can actually round-trip."""
    db = _mock_live_db(daily_total_pnl=daily_total_pnl, cumulative_total_pnl=cumulative_total_pnl)
    db.get_config.side_effect = store.get_config
    db.set_config.side_effect = store.set_config
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


# ----------------------------------------------------------------------
# Persisted live-breaker trip state (issue #1348): stay-tripped-same-day,
# UTC-midnight auto-clear, manual reset, and restart-persistence.
# ----------------------------------------------------------------------

class TestLiveBreakerStaysTrippedSameDay:
    def test_daily_loss_trip_persists_across_calls_even_as_pnl_recovers(self):
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store, daily_total_pnl=-30.0)
        cfg = _live_breaker_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=25.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok1, reason1 = allow_live_copy_signal(db, cfg)
        assert ok1 is False
        assert reason1 == REASON_LIVE_DAILY_LOSS

        # PnL recovers mid-day (a later win pushes the running total back
        # above the limit) -- the trip must NOT flicker off.
        db.get_copy_live_realized_pnl_total_for_date.return_value = {
            "n_settled": 2, "total_pnl_usd": 5.0,
        }
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok2, reason2 = allow_live_copy_signal(db, cfg)
        assert ok2 is False
        assert reason2 == REASON_LIVE_DAILY_LOSS

        # The second call must be a pure short-circuit on the persisted
        # trip -- PnL is never re-read once a trip exists for today.
        db.get_copy_live_realized_pnl_total_for_date.assert_called_once()

    def test_drawdown_trip_persists_across_calls_even_as_pnl_recovers(self):
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store, daily_total_pnl=-5.0, cumulative_total_pnl=-50.0)
        cfg = _live_breaker_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=999.0, COPY_LIVE_DRAWDOWN_STOP_PCT=0.20)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok1, reason1 = allow_live_copy_signal(db, cfg)
        assert ok1 is False
        assert reason1 == REASON_LIVE_DRAWDOWN

        # Cumulative P&L "recovers" (e.g. a later settlement) -- still must
        # stay tripped for the rest of the day, one-way-door problem fixed.
        db.get_copy_live_realized_pnl_total.return_value = {"n_settled": 2, "total_pnl_usd": 0.0}
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok2, reason2 = allow_live_copy_signal(db, cfg)
        assert ok2 is False
        assert reason2 == REASON_LIVE_DRAWDOWN
        db.get_copy_live_realized_pnl_total.assert_called_once()

    def test_not_tripped_stays_not_tripped_and_still_reads_pnl_fresh_each_call(self):
        """No persisted trip ever gets written when nothing breaches --
        every call keeps re-deriving fresh, exactly like before #1348."""
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store, daily_total_pnl=0.0, cumulative_total_pnl=0.0)
        cfg = _live_breaker_config()
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok1, _ = allow_live_copy_signal(db, cfg)
            ok2, _ = allow_live_copy_signal(db, cfg)
        assert ok1 is True
        assert ok2 is True
        assert db.get_copy_live_realized_pnl_total_for_date.call_count == 2


class TestLiveBreakerAutoClearsAtUtcMidnight:
    def test_stale_trip_is_cleared_and_reevaluated_fresh_next_day(self):
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store, daily_total_pnl=-30.0)
        cfg = _live_breaker_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=25.0)

        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL), \
             patch("src.risk.copy_risk_manager._today_utc", return_value="2026-10-10"):
            ok1, reason1 = allow_live_copy_signal(db, cfg)
        assert ok1 is False
        assert reason1 == REASON_LIVE_DAILY_LOSS

        # New UTC day: PnL for the new day is healthy.
        db.get_copy_live_realized_pnl_total_for_date.return_value = {
            "n_settled": 0, "total_pnl_usd": 0.0,
        }
        db.get_copy_live_realized_pnl_total.return_value = {"n_settled": 0, "total_pnl_usd": 0.0}
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL), \
             patch("src.risk.copy_risk_manager._today_utc", return_value="2026-10-11"):
            ok2, reason2 = allow_live_copy_signal(db, cfg)
        assert ok2 is True
        assert reason2 == ""
        # Re-evaluated fresh on the new day -- PnL WAS consulted again.
        assert db.get_copy_live_realized_pnl_total_for_date.call_count == 2

    def test_stale_trip_can_immediately_retrip_on_the_new_day(self):
        """A new day re-evaluating to "still bad" is a fresh verdict, not a
        bug -- distinct from the old trip simply persisting."""
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store, daily_total_pnl=-30.0)
        cfg = _live_breaker_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=25.0)

        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL), \
             patch("src.risk.copy_risk_manager._today_utc", return_value="2026-10-10"):
            ok1, _ = allow_live_copy_signal(db, cfg)
        assert ok1 is False

        # New UTC day, but still losing just as badly.
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL), \
             patch("src.risk.copy_risk_manager._today_utc", return_value="2026-10-11"):
            ok2, reason2 = allow_live_copy_signal(db, cfg)
        assert ok2 is False
        assert reason2 == REASON_LIVE_DAILY_LOSS
        # Both days' PnL were genuinely (re-)consulted -- not a leftover flag.
        assert db.get_copy_live_realized_pnl_total_for_date.call_count == 2


class TestLiveBreakerManualReset:
    def test_reset_clears_the_trip_immediately_mid_day(self):
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store, daily_total_pnl=-30.0)
        cfg = _live_breaker_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=25.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok1, _ = allow_live_copy_signal(db, cfg)
        assert ok1 is False

        cleared = reset_live_circuit_breaker(db)
        assert cleared is True

        # P&L has since recovered (the scenario the operator reviewed) --
        # the very next call, same UTC day, must re-evaluate fresh and
        # allow trading again, independent of the day boundary.
        db.get_copy_live_realized_pnl_total_for_date.return_value = {
            "n_settled": 2, "total_pnl_usd": 10.0,
        }
        db.get_copy_live_realized_pnl_total.reset_mock()
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok2, reason2 = allow_live_copy_signal(db, cfg)
        assert ok2 is True
        assert reason2 == ""

    def test_reset_is_a_fresh_evaluation_not_a_forced_allow(self):
        """A reset clears the STATE, it does not force the next verdict to
        True -- if PnL is still bad, the very next call legitimately
        re-trips, from a genuinely fresh read (proven by the call count),
        not a leftover persisted flag."""
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store, daily_total_pnl=-30.0)
        cfg = _live_breaker_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=25.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok1, _ = allow_live_copy_signal(db, cfg)
        assert ok1 is False
        db.get_copy_live_realized_pnl_total_for_date.assert_called_once()

        assert reset_live_circuit_breaker(db) is True

        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok2, reason2 = allow_live_copy_signal(db, cfg)
        assert ok2 is False
        assert reason2 == REASON_LIVE_DAILY_LOSS
        assert db.get_copy_live_realized_pnl_total_for_date.call_count == 2

    def test_reset_is_a_noop_when_nothing_is_tripped(self):
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store, daily_total_pnl=0.0, cumulative_total_pnl=0.0)
        assert reset_live_circuit_breaker(db) is False

    def test_reset_logs_the_fact_and_timestamp(self, caplog):
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store, daily_total_pnl=-30.0)
        cfg = _live_breaker_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=25.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            allow_live_copy_signal(db, cfg)

        with caplog.at_level("WARNING", logger="src.risk.copy_risk_manager"):
            reset_live_circuit_breaker(db)
        assert any("manual reset" in rec.message for rec in caplog.records)


class TestLiveBreakerPersistsAcrossProcessRestart:
    def test_persisted_trip_survives_a_fresh_db_handle(self):
        """Same DB-backed, no-in-memory-state contract as the rest of this
        module: a brand-new MagicMock db wired to the SAME backing store
        stands in for a ``copy_signal_loop.py`` process restart -- no
        module-level state is ever touched, so the answer must be
        identical."""
        store = _FakeConfigStore()
        db1 = _mock_live_db_with_config(store, daily_total_pnl=-30.0)
        cfg = _live_breaker_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=25.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok1, reason1 = allow_live_copy_signal(db1, cfg)
        assert ok1 is False
        assert reason1 == REASON_LIVE_DAILY_LOSS

        # "Restart": a brand-new double, even wired to PnL numbers that
        # would otherwise allow trading -- proving the verdict comes
        # purely from the persisted DB row, not anything in-process.
        db2 = _mock_live_db_with_config(store, daily_total_pnl=999.0, cumulative_total_pnl=999.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok2, reason2 = allow_live_copy_signal(db2, cfg)
        assert ok2 is False
        assert reason2 == REASON_LIVE_DAILY_LOSS
        db2.get_copy_live_realized_pnl_total_for_date.assert_not_called()
        db2.get_copy_live_realized_pnl_total.assert_not_called()


class TestGetLiveCircuitBreakerStatus:
    def test_not_tripped_returns_all_none(self):
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store, daily_total_pnl=0.0, cumulative_total_pnl=0.0)
        status = get_live_circuit_breaker_status(db)
        assert status == {
            "tripped": False, "reason": None, "tripped_at": None, "trip_utc_date": None,
        }

    def test_tripped_today_returns_reason_and_timestamp(self):
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store, daily_total_pnl=-30.0)
        cfg = _live_breaker_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=25.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL), \
             patch("src.risk.copy_risk_manager._today_utc", return_value="2026-10-10"):
            allow_live_copy_signal(db, cfg)
            status = get_live_circuit_breaker_status(db)
        assert status["tripped"] is True
        assert status["reason"] == REASON_LIVE_DAILY_LOSS
        assert status["trip_utc_date"] == "2026-10-10"
        assert status["tripped_at"] is not None

    def test_stale_prior_day_trip_reports_not_tripped_without_writing(self):
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store, daily_total_pnl=-30.0)
        cfg = _live_breaker_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=25.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL), \
             patch("src.risk.copy_risk_manager._today_utc", return_value="2026-10-10"):
            allow_live_copy_signal(db, cfg)

        raw_before = dict(store._store)
        with patch("src.risk.copy_risk_manager._today_utc", return_value="2026-10-11"):
            status = get_live_circuit_breaker_status(db)
        assert status["tripped"] is False
        # A status read must stay read-only -- the stale row is left exactly
        # as-is; clearing it is allow_live_copy_signal's job, not this getter's.
        assert store._store == raw_before

    def test_corrupt_persisted_value_degrades_to_not_tripped(self):
        store = _FakeConfigStore()
        store.set_config("COPY_LIVE_BREAKER_TRIP_STATE", "not valid json")
        db = _mock_live_db_with_config(store)
        status = get_live_circuit_breaker_status(db)
        assert status["tripped"] is False


class TestGetLiveCircuitBreakerStatusNoCapital:
    """PR #1356 review feedback: the "tripped breaker visible without
    reading the database" acceptance criterion applies to the no-capital
    fail-closed case the same as it does to the pre-existing PnL-derived
    trips -- not just via the per-row rejected_reason label."""

    def test_reports_no_capital_when_trading_enabled_and_capital_missing(self):
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store)
        cfg = {"COPY_LIVE_TRADING_ENABLED": True}
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", 0.0):
            status = get_live_circuit_breaker_status(db, cfg)
        assert status == {
            "tripped": True, "reason": REASON_LIVE_NO_CAPITAL,
            "tripped_at": None, "trip_utc_date": None,
        }

    def test_reports_no_capital_with_negative_capital_too(self):
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store)
        cfg = {"COPY_LIVE_TRADING_ENABLED": True}
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", -1.0):
            status = get_live_circuit_breaker_status(db, cfg)
        assert status["tripped"] is True
        assert status["reason"] == REASON_LIVE_NO_CAPITAL

    def test_does_not_report_no_capital_when_trading_disabled(self):
        """Gated on COPY_LIVE_TRADING_ENABLED, not just capital<=0 --
        matches allow_live_copy_signal's own precondition (it is never
        even called by copy_signal_loop.py unless trading is enabled)."""
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store)
        cfg = {"COPY_LIVE_TRADING_ENABLED": False}
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", 0.0):
            status = get_live_circuit_breaker_status(db, cfg)
        assert status["tripped"] is False

    def test_no_live_config_argument_keeps_pre_1300_behavior(self):
        """Backward compatible: callers with only a Database handle (every
        existing caller before this review round) never see the no-capital
        branch fire, regardless of the real COPY_LIVE_CAPITAL_USD value --
        this is what keeps the suite's pre-existing default-capital=0.0
        tests green without themselves changing."""
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", 0.0):
            status = get_live_circuit_breaker_status(db)
        assert status["tripped"] is False

    def test_no_capital_takes_precedence_over_a_stale_persisted_trip(self):
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store, daily_total_pnl=-30.0)
        cfg = _live_breaker_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=25.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL), \
             patch("src.risk.copy_risk_manager._today_utc", return_value="2020-01-01"):
            allow_live_copy_signal(db, cfg)  # persists a stale (prior-day) trip

        enabled_cfg = {**cfg, "COPY_LIVE_TRADING_ENABLED": True}
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", 0.0):
            status = get_live_circuit_breaker_status(db, enabled_cfg)
        assert status["reason"] == REASON_LIVE_NO_CAPITAL

    def test_capital_configured_falls_through_to_normal_persisted_trip_logic(self):
        """Regression guard: passing a live_config with trading enabled AND
        real capital must not disturb the pre-existing persisted-trip
        reporting -- this test would have caught a sloppy "always skip
        persisted-trip lookup once live_config is passed" mistake."""
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store, daily_total_pnl=-30.0)
        cfg = _live_breaker_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=25.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL), \
             patch("src.risk.copy_risk_manager._today_utc", return_value="2026-10-10"):
            allow_live_copy_signal(db, cfg)
            enabled_cfg = {**cfg, "COPY_LIVE_TRADING_ENABLED": True}
            status = get_live_circuit_breaker_status(db, enabled_cfg)
        assert status["tripped"] is True
        assert status["reason"] == REASON_LIVE_DAILY_LOSS
        assert status["trip_utc_date"] == "2026-10-10"


# ----------------------------------------------------------------------
# Issue #1300: fail closed without capital, and a manual clear that moves
# a baseline rather than erasing history.
# ----------------------------------------------------------------------

class TestLiveBreakerFailsClosedWithoutCapital:
    """Acceptance criterion: COPY_LIVE_TRADING_ENABLED=true with
    COPY_LIVE_CAPITAL_USD<=0 refuses execution with a distinct reason --
    pinning the inversion of the old ``if COPY_LIVE_CAPITAL_USD > 0:``
    guard, which used to silently skip the drawdown check and fall through
    to an allow."""

    def test_refuses_when_capital_is_zero(self):
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store, daily_total_pnl=0.0, cumulative_total_pnl=0.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", 0.0):
            ok, reason = allow_live_copy_signal(db, _live_breaker_config())
        assert ok is False
        assert reason == REASON_LIVE_NO_CAPITAL

    def test_refuses_when_capital_is_negative(self):
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", -5.0):
            ok, reason = allow_live_copy_signal(db, _live_breaker_config())
        assert ok is False
        assert reason == REASON_LIVE_NO_CAPITAL

    def test_refuses_even_with_healthy_pnl(self):
        """Pins the inversion explicitly: before this fix, missing capital
        skipped the drawdown branch and fell through to an ALLOW whenever
        PnL happened to be healthy. Healthy PnL must not matter now."""
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store, daily_total_pnl=50.0, cumulative_total_pnl=200.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", 0.0):
            ok, reason = allow_live_copy_signal(db, _live_breaker_config())
        assert ok is False
        assert reason == REASON_LIVE_NO_CAPITAL

    def test_wins_over_daily_loss_too(self):
        db = _mock_live_db(daily_total_pnl=-999.0, cumulative_total_pnl=-999.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", 0.0):
            ok, reason = allow_live_copy_signal(
                db, _live_breaker_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=25.0)
            )
        assert ok is False
        assert reason == REASON_LIVE_NO_CAPITAL

    def test_does_not_consult_pnl_or_persisted_trip_when_capital_missing(self):
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", 0.0):
            allow_live_copy_signal(db, _live_breaker_config())
        db.get_config.assert_not_called()
        db.get_copy_live_realized_pnl_total_for_date.assert_not_called()
        db.get_copy_live_realized_pnl_total.assert_not_called()

    def test_never_persists_a_trip_for_the_no_capital_reason(self):
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", 0.0):
            allow_live_copy_signal(db, _live_breaker_config())
        assert store._store == {}
        # Nothing was ever written to bot_config for this reason -- the
        # status getter reports it live (see TestGetLiveCircuitBreakerStatusNoCapital)
        # only when given a live_config saying trading is enabled; called
        # with just a db handle (no live_config), it still reports
        # not-tripped, since there is no persisted row to find.
        assert get_live_circuit_breaker_status(db)["tripped"] is False

    def test_allows_normal_evaluation_once_capital_is_configured(self):
        """Regression guard: the fail-closed check must not fire once
        capital is properly configured."""
        db = _mock_live_db(daily_total_pnl=0.0, cumulative_total_pnl=0.0)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok, reason = allow_live_copy_signal(db, _live_breaker_config())
        assert ok is True
        assert reason == ""


class TestEffectiveDrawdownBaseline:
    """Unit coverage for _effective_drawdown_baseline: the LATER of the
    operator-set COPY_LIVE_DRAWDOWN_SINCE config value and the most recent
    manual-clear baseline always wins, in both directions."""

    def test_all_time_when_neither_is_set(self):
        from src.risk.copy_risk_manager import _effective_drawdown_baseline
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store)
        assert _effective_drawdown_baseline(db, _live_breaker_config()) is None

    def test_config_baseline_used_when_no_clear_exists(self):
        from src.risk.copy_risk_manager import _effective_drawdown_baseline
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store)
        cfg = _live_breaker_config(COPY_LIVE_DRAWDOWN_SINCE="2026-01-01T00:00:00+00:00")
        assert _effective_drawdown_baseline(db, cfg).startswith("2026-01-01")

    def test_clear_baseline_used_when_no_config_value(self):
        from src.risk.copy_risk_manager import (
            _effective_drawdown_baseline, _persist_live_drawdown_clear_baseline,
        )
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store)
        _persist_live_drawdown_clear_baseline(db, "2026-10-12T00:00:00+00:00")
        assert _effective_drawdown_baseline(db, _live_breaker_config()).startswith("2026-10-12")

    def test_config_baseline_wins_when_later_than_clear(self):
        from src.risk.copy_risk_manager import (
            _effective_drawdown_baseline, _persist_live_drawdown_clear_baseline,
        )
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store)
        _persist_live_drawdown_clear_baseline(db, "2020-01-01T00:00:00+00:00")
        cfg = _live_breaker_config(COPY_LIVE_DRAWDOWN_SINCE="2099-01-01T00:00:00+00:00")
        assert _effective_drawdown_baseline(db, cfg).startswith("2099-01-01")

    def test_clear_baseline_wins_when_later_than_config(self):
        from src.risk.copy_risk_manager import (
            _effective_drawdown_baseline, _persist_live_drawdown_clear_baseline,
        )
        store = _FakeConfigStore()
        db = _mock_live_db_with_config(store)
        _persist_live_drawdown_clear_baseline(db, "2026-10-12T00:00:00+00:00")
        cfg = _live_breaker_config(COPY_LIVE_DRAWDOWN_SINCE="2020-01-01T00:00:00+00:00")
        assert _effective_drawdown_baseline(db, cfg).startswith("2026-10-12")


class TestLiveBreakerManualResetMovesDrawdownBaseline:
    """Acceptance criterion: clearing moves a baseline, it does not erase
    history -- after a clear, the drawdown is measured against realized
    P&L accrued SINCE the clear, and a further loss past the threshold
    measured from the clear blocks again."""

    @staticmethod
    def _since_aware_db(store, *, all_time_pnl, since_clear_pnl):
        """A live-db double whose get_copy_live_realized_pnl_total actually
        varies with the `since` kwarg -- unlike the rest of this file's
        fixed .return_value doubles, this is needed to prove the baseline
        is genuinely threaded through to the DB call, not just computed
        and discarded."""
        db = _mock_live_db_with_config(store, daily_total_pnl=0.0)

        def _pnl(since=None):
            if since is None:
                return {"n_settled": 5, "total_pnl_usd": all_time_pnl}
            return {"n_settled": 1, "total_pnl_usd": since_clear_pnl}

        db.get_copy_live_realized_pnl_total.side_effect = _pnl
        return db

    def test_reset_permits_execution_then_a_further_loss_retrips(self):
        store = _FakeConfigStore()
        db = self._since_aware_db(store, all_time_pnl=-50.0, since_clear_pnl=0.0)
        cfg = _live_breaker_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=999.0, COPY_LIVE_DRAWDOWN_STOP_PCT=0.20)

        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok1, reason1 = allow_live_copy_signal(db, cfg)
        assert ok1 is False
        assert reason1 == REASON_LIVE_DRAWDOWN

        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            cleared = reset_live_circuit_breaker(db, cfg)
        assert cleared is True

        # The same all-time cumulative loss is still on the books, but the
        # clear moved the baseline forward -- nothing has settled SINCE
        # the clear, so execution is now permitted.
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok2, reason2 = allow_live_copy_signal(db, cfg)
        assert ok2 is True
        assert reason2 == ""

        # A further loss past the threshold, measured from the clear,
        # blocks again.
        db.get_copy_live_realized_pnl_total.side_effect = lambda since=None: (
            {"n_settled": 5, "total_pnl_usd": -50.0} if since is None
            else {"n_settled": 1, "total_pnl_usd": -25.0}
        )
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            ok3, reason3 = allow_live_copy_signal(db, cfg)
        assert ok3 is False
        assert reason3 == REASON_LIVE_DRAWDOWN

    def test_reset_passes_a_since_argument_at_or_after_the_clear(self):
        store = _FakeConfigStore()
        db = self._since_aware_db(store, all_time_pnl=-50.0, since_clear_pnl=0.0)
        cfg = _live_breaker_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=999.0, COPY_LIVE_DRAWDOWN_STOP_PCT=0.20)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            allow_live_copy_signal(db, cfg)
            before = datetime.now(timezone.utc).isoformat()
            reset_live_circuit_breaker(db, cfg)
            after = datetime.now(timezone.utc).isoformat()
            db.get_copy_live_realized_pnl_total.side_effect = None
            db.get_copy_live_realized_pnl_total.return_value = {"n_settled": 0, "total_pnl_usd": 0.0}
            allow_live_copy_signal(db, cfg)

        since_args = [
            kwargs.get("since") for _args, kwargs in db.get_copy_live_realized_pnl_total.call_args_list
        ]
        last_since = since_args[-1]
        assert last_since is not None
        assert before <= last_since <= after

    def test_reset_does_not_modify_settled_pnl_rows(self):
        """Test requirement: a clear never changes any
        copy_live_positions.settled_pnl_usd value -- asserted directly
        against the db double's call surface (no row-mutation method is
        ever invoked by a reset)."""
        store = _FakeConfigStore()
        db = self._since_aware_db(store, all_time_pnl=-50.0, since_clear_pnl=0.0)
        cfg = _live_breaker_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=999.0, COPY_LIVE_DRAWDOWN_STOP_PCT=0.20)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            allow_live_copy_signal(db, cfg)
            reset_live_circuit_breaker(db, cfg)

        allowed_methods = {
            "get_config", "set_config",
            "get_copy_live_realized_pnl_total",
            "get_copy_live_realized_pnl_total_for_date",
        }
        called_methods = {c[0] for c in db.method_calls}
        assert called_methods <= allowed_methods

    def test_reset_with_no_live_config_argument_still_works(self):
        """reset_live_circuit_breaker(db) with no live_config -- backward
        compatible with any pre-#1300 caller that only has a db handle."""
        store = _FakeConfigStore()
        db = self._since_aware_db(store, all_time_pnl=-50.0, since_clear_pnl=0.0)
        cfg = _live_breaker_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=999.0, COPY_LIVE_DRAWDOWN_STOP_PCT=0.20)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            allow_live_copy_signal(db, cfg)
            cleared = reset_live_circuit_breaker(db)
        assert cleared is True

    def test_reset_logs_the_pnl_figure_at_clear(self, caplog):
        store = _FakeConfigStore()
        db = self._since_aware_db(store, all_time_pnl=-50.0, since_clear_pnl=0.0)
        cfg = _live_breaker_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=999.0, COPY_LIVE_DRAWDOWN_STOP_PCT=0.20)
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", CAPITAL):
            allow_live_copy_signal(db, cfg)
            with caplog.at_level("WARNING", logger="src.risk.copy_risk_manager"):
                reset_live_circuit_breaker(db, cfg)
        assert any("manual reset" in rec.message and "-50.0" in rec.message for rec in caplog.records)

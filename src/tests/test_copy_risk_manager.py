"""Unit tests for src/risk/copy_risk_manager.py (issue #1139, story D1 of
epic #1138; plus its live counterpart, issue #1175, epic I #1160; plus the
live breaker's persisted-trip/manual-reset mechanics, issue #1348). Fully
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

"""Unit tests for zero-evaluation watchdog (issue #686).

Tests verify that the watchdog:
1. Tracks consecutive polls with zero evaluated brackets while markets exist
2. Fires ERROR log and guardrail_event after N consecutive ticks
3. Resets counter when evaluation resumes or when markets disappear
"""
import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest

# Import the watchdog function and the module to reset global state
import src.scripts.run as run_module
from src.scripts.run import _check_zero_eval_watchdog


@pytest.fixture(autouse=True)
def reset_watchdog_state():
    """Reset the global watchdog counter before each test."""
    run_module._zero_eval_consecutive_ticks = 0
    yield
    run_module._zero_eval_consecutive_ticks = 0


class TestZeroEvalWatchdog:
    """Test the zero-evaluation watchdog state machine."""

    def test_watchdog_increments_on_zero_eval_with_markets(self, caplog):
        """Consecutive zero-eval ticks with markets → counter increments."""
        db = MagicMock()
        db.get_config.return_value = "4"  # threshold = 4

        ts = "2026-07-11T12:00:00Z"

        # First tick: zero eval but markets available
        with caplog.at_level(logging.DEBUG):
            _check_zero_eval_watchdog(ts, num_markets=350, num_evaluated=0, db=db)

        # Counter should be 1 (no alert yet)
        assert "[watchdog] ALERT" not in caplog.text

        # Second tick: still zero
        _check_zero_eval_watchdog(ts, num_markets=350, num_evaluated=0, db=db)
        assert "[watchdog] ALERT" not in caplog.text

        # Third tick: still zero
        _check_zero_eval_watchdog(ts, num_markets=350, num_evaluated=0, db=db)
        assert "[watchdog] ALERT" not in caplog.text

        # Fourth tick: hits threshold, should fire
        with caplog.at_level(logging.ERROR):
            _check_zero_eval_watchdog(ts, num_markets=350, num_evaluated=0, db=db)

        assert "[watchdog] ALERT: 4 consecutive polls with zero evaluated brackets" in caplog.text

    def test_watchdog_resets_on_evaluation_resume(self, caplog):
        """Evaluation resumes → counter resets to 0."""
        db = MagicMock()
        db.get_config.return_value = "4"

        ts = "2026-07-11T12:00:00Z"

        # Tick 1, 2, 3: zero eval
        _check_zero_eval_watchdog(ts, num_markets=350, num_evaluated=0, db=db)
        _check_zero_eval_watchdog(ts, num_markets=350, num_evaluated=0, db=db)
        _check_zero_eval_watchdog(ts, num_markets=350, num_evaluated=0, db=db)

        # Tick 4: evaluation resumes (1 evaluated)
        with caplog.at_level(logging.INFO):
            _check_zero_eval_watchdog(ts, num_markets=350, num_evaluated=1, db=db)

        # Should see reset message
        assert "[watchdog] zero-eval counter reset" in caplog.text
        # Should not have fired alert
        assert "[watchdog] ALERT" not in caplog.text

    def test_watchdog_resets_on_no_markets(self, caplog):
        """No markets available → counter resets (not a failure condition)."""
        db = MagicMock()
        db.get_config.return_value = "4"

        ts = "2026-07-11T12:00:00Z"

        # Tick 1, 2: zero eval with markets
        _check_zero_eval_watchdog(ts, num_markets=350, num_evaluated=0, db=db)
        _check_zero_eval_watchdog(ts, num_markets=350, num_evaluated=0, db=db)

        # Tick 3: no markets available (weather outage) → reset, not alert
        with caplog.at_level(logging.INFO):
            _check_zero_eval_watchdog(ts, num_markets=0, num_evaluated=0, db=db)

        # Should see reset, not alert
        assert "[watchdog] zero-eval counter reset" in caplog.text
        assert "[watchdog] ALERT" not in caplog.text

    def test_watchdog_logs_guardrail_event_on_fire(self):
        """When threshold is hit, guardrail_event is written to DB."""
        db = MagicMock()
        db.get_config.return_value = "3"  # threshold = 3

        ts = "2026-07-11T12:00:00Z"

        # Simulate 3 consecutive zero-eval ticks
        _check_zero_eval_watchdog(ts, num_markets=350, num_evaluated=0, db=db)
        _check_zero_eval_watchdog(ts, num_markets=350, num_evaluated=0, db=db)
        _check_zero_eval_watchdog(ts, num_markets=350, num_evaluated=0, db=db)

        # Verify guardrail_event was logged
        db.log_guardrail_event.assert_called()
        call_args = db.log_guardrail_event.call_args
        assert call_args[0][0] == ts  # ts
        assert call_args[0][1] == "GLOBAL"  # station
        assert call_args[0][2] == "zero_eval_ticks"  # event_type
        assert call_args[0][3] == 350.0  # raw_value (num_markets)
        assert call_args[0][4] == 3.0  # adj_value (consecutive ticks)

    def test_watchdog_uses_default_threshold_when_db_unavailable(self, caplog):
        """If DB is None, default threshold (4) is used."""
        ts = "2026-07-11T12:00:00Z"

        # Without DB, default threshold should be 4
        for _ in range(3):
            _check_zero_eval_watchdog(ts, num_markets=350, num_evaluated=0, db=None)

        # Third tick should NOT alert (threshold=4)
        assert "[watchdog] ALERT" not in caplog.text

        # Fourth tick should alert
        with caplog.at_level(logging.ERROR):
            _check_zero_eval_watchdog(ts, num_markets=350, num_evaluated=0, db=None)

        # Fifth tick should still be alerting
        _check_zero_eval_watchdog(ts, num_markets=350, num_evaluated=0, db=None)
        assert "[watchdog] ALERT" in caplog.text

    def test_watchdog_handles_config_read_error(self, caplog):
        """If DB get_config fails, fall back to default threshold."""
        db = MagicMock()
        db.get_config.side_effect = ValueError("DB error")  # Simulate read error

        ts = "2026-07-11T12:00:00Z"

        # Should still use default threshold of 4, despite error
        for _ in range(4):
            _check_zero_eval_watchdog(ts, num_markets=350, num_evaluated=0, db=db)

        # Fourth tick should alert (using default threshold=4)
        assert "[watchdog] ALERT: 4 consecutive polls" in caplog.text

    def test_watchdog_continues_alerting_beyond_threshold(self, caplog):
        """Once threshold is reached, watchdog keeps alerting on each subsequent tick."""
        db = MagicMock()
        db.get_config.return_value = "2"  # threshold = 2

        ts = "2026-07-11T12:00:00Z"

        # Tick 1: increment
        _check_zero_eval_watchdog(ts, num_markets=100, num_evaluated=0, db=db)

        # Tick 2: hit threshold, first alert
        with caplog.at_level(logging.ERROR):
            _check_zero_eval_watchdog(ts, num_markets=100, num_evaluated=0, db=db)
        assert "[watchdog] ALERT: 2 consecutive polls" in caplog.text
        assert db.log_guardrail_event.called

        # Clear call history
        db.reset_mock()
        caplog.clear()

        # Tick 3: threshold already hit, tick counter to 3, guardrail_event again
        with caplog.at_level(logging.ERROR):
            _check_zero_eval_watchdog(ts, num_markets=100, num_evaluated=0, db=db)
        assert "[watchdog] ALERT: 3 consecutive polls" in caplog.text
        assert db.log_guardrail_event.called

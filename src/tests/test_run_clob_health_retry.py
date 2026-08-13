"""Tests for issue #1003: Retry CLOB health check with backoff instead of exiting on first failure."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch, call

import pytest

from src.scripts.run import retry_clob_health_with_backoff


class TestRetryClobHealthWithBackoff:
    """Test suite for retry_clob_health_with_backoff helper."""

    def test_successful_on_first_attempt(self):
        """Health check succeeds on first attempt — returns immediately without retry."""
        mock_check = MagicMock(return_value=True)
        mock_sleep = MagicMock()

        retry_clob_health_with_backoff(check_func=mock_check, sleep_func=mock_sleep)

        mock_check.assert_called_once()
        mock_sleep.assert_not_called()

    def test_successful_after_one_retry(self):
        """Health check fails once, succeeds on second attempt."""
        mock_check = MagicMock(side_effect=[False, True])
        mock_sleep = MagicMock()

        retry_clob_health_with_backoff(check_func=mock_check, sleep_func=mock_sleep)

        assert mock_check.call_count == 2
        mock_sleep.assert_called_once_with(2)  # First backoff is 2 seconds

    def test_successful_after_multiple_retries(self):
        """Health check fails multiple times, then succeeds."""
        mock_check = MagicMock(side_effect=[False, False, False, True])
        mock_sleep = MagicMock()

        retry_clob_health_with_backoff(check_func=mock_check, sleep_func=mock_sleep)

        assert mock_check.call_count == 4
        # Should have slept 3 times with delays: 2, 4, 8 seconds
        expected_calls = [call(2), call(4), call(8)]
        mock_sleep.assert_has_calls(expected_calls)
        assert mock_sleep.call_count == 3

    def test_all_retries_fail_raises_systemexit(self):
        """All retry attempts fail — raises SystemExit(1)."""
        mock_check = MagicMock(return_value=False)
        mock_sleep = MagicMock()

        with pytest.raises(SystemExit) as exc_info:
            retry_clob_health_with_backoff(check_func=mock_check, sleep_func=mock_sleep)

        # SystemExit should be called with 1 (int), not a string
        assert exc_info.value.code == 1

    def test_all_retries_fail_logs_error_message(self, caplog):
        """On failure, logs error with attempt count and guidance text."""
        mock_check = MagicMock(return_value=False)
        mock_sleep = MagicMock()

        with caplog.at_level(logging.ERROR):
            with pytest.raises(SystemExit):
                retry_clob_health_with_backoff(check_func=mock_check, sleep_func=mock_sleep)

        # Verify the error message contains required elements
        error_logs = [record for record in caplog.records if record.levelname == "ERROR"]
        assert len(error_logs) == 1

        log_msg = error_logs[0].getMessage()
        assert "6 attempts" in log_msg  # Attempt count
        assert "verify POLYMARKET_API_KEY and connectivity" in log_msg  # Guidance text
        assert "[run]" in log_msg  # Prefix

    def test_per_attempt_warning_logs(self, caplog):
        """Each failed attempt logs WARNING with attempt number before sleep."""
        mock_check = MagicMock(return_value=False)
        mock_sleep = MagicMock()

        with caplog.at_level(logging.WARNING):
            with pytest.raises(SystemExit):
                retry_clob_health_with_backoff(check_func=mock_check, sleep_func=mock_sleep)

        # Should log WARNING once per failed attempt (5 times: attempts 1-5; attempt 6 does not sleep)
        warning_logs = [record for record in caplog.records if record.levelname == "WARNING"]
        assert len(warning_logs) == 5

        # Verify each warning has attempt number and total attempts
        for i, record in enumerate(warning_logs, start=1):
            log_msg = record.getMessage()
            assert f"attempt {i}/6" in log_msg
            assert "retrying in" in log_msg

    def test_total_elapsed_time_budget(self):
        """Total elapsed time across all retries is at least ~60s to absorb DNS races."""
        mock_check = MagicMock(return_value=False)

        # Track cumulative sleep time across all backoff delays
        cumulative_sleep = [0.0]

        def mock_sleep_func(duration):
            cumulative_sleep[0] += duration

        with pytest.raises(SystemExit):
            retry_clob_health_with_backoff(check_func=mock_check, sleep_func=mock_sleep_func)

        # Verify cumulative sleep is at least 60 seconds
        # Backoff: 2 + 4 + 8 + 16 + 32 = 62 seconds
        assert cumulative_sleep[0] >= 60.0, f"Expected >= 60s, got {cumulative_sleep[0]}s"

    def test_exponential_backoff_delays(self):
        """Backoff delays follow exponential pattern: 2, 4, 8, 16, 32."""
        mock_check = MagicMock(return_value=False)
        mock_sleep = MagicMock()

        with pytest.raises(SystemExit):
            retry_clob_health_with_backoff(check_func=mock_check, sleep_func=mock_sleep)

        # Should have 5 sleep calls (6 attempts total, no sleep after last attempt)
        assert mock_sleep.call_count == 5

        # Verify the exact delay sequence
        expected_delays = [2, 4, 8, 16, 32]
        actual_delays = [call_obj[0][0] for call_obj in mock_sleep.call_args_list]
        assert actual_delays == expected_delays

    def test_uses_default_check_clob_health_if_not_provided(self):
        """When check_func is None, uses check_clob_health from auth module."""
        mock_sleep = MagicMock()

        with patch("src.execution.auth.check_clob_health") as mock_check:
            mock_check.return_value = True

            retry_clob_health_with_backoff(check_func=None, sleep_func=mock_sleep)

            mock_check.assert_called_once()
            mock_sleep.assert_not_called()

    def test_uses_default_time_sleep_if_not_provided(self):
        """When sleep_func is None, uses time.sleep from time module."""
        mock_check = MagicMock(side_effect=[False, True])

        with patch("src.scripts.run.time.sleep") as mock_sleep:
            retry_clob_health_with_backoff(check_func=mock_check, sleep_func=None)

            mock_check.assert_called()
            mock_sleep.assert_called_once_with(2)


class TestClobHealthRetryIntegration:
    """Integration-style tests for the retry helper with realistic scenarios."""

    def test_recovered_after_transient_network_failure(self):
        """Simulates transient network failure that resolves mid-retry."""
        # Fail 3 times, then succeed
        mock_check = MagicMock(side_effect=[False, False, False, True])
        mock_sleep = MagicMock()

        retry_clob_health_with_backoff(check_func=mock_check, sleep_func=mock_sleep)

        assert mock_check.call_count == 4
        # Sleep should occur 3 times: 2s, 4s, 8s
        assert mock_sleep.call_count == 3

    def test_persistent_failure_all_attempts_exhausted(self):
        """Simulates persistent API failure — all 6 attempts exhausted."""
        mock_check = MagicMock(return_value=False)
        mock_sleep = MagicMock()

        with pytest.raises(SystemExit) as exc_info:
            retry_clob_health_with_backoff(check_func=mock_check, sleep_func=mock_sleep)

        # Verify correct number of attempts
        assert mock_check.call_count == 6
        # Verify 5 sleep intervals (between 6 attempts)
        assert mock_sleep.call_count == 5
        # Verify SystemExit code is 1
        assert exc_info.value.code == 1


class TestPaperModeUntouched:
    """Test that paper mode does not perform health checks."""

    def test_paper_mode_no_health_check_call(self):
        """Running without --live flag must never call retry_clob_health_with_backoff.

        Guards against future refactors that might hoist the health check
        out of the `if args.live:` branch, adding unwanted startup delay to paper mode.
        """
        with patch("src.scripts.run.retry_clob_health_with_backoff") as mock_retry:
            with patch("src.scripts.run.LiveTrader"):
                with patch("src.execution.auth.get_clob_client"):
                    with patch("src.data.db.Database"):
                        with patch("src.scripts.run.setup_logging"):
                            with patch("src.scripts.run.RiskManager"):
                                with patch("src.scripts.run.AlertManager"):
                                    with patch("src.scripts.run.start_dashboard"):
                                        with patch("src.scripts.run.poll_once"):
                                            with patch("src.scripts.run._load_open_no_positions", return_value=[]):
                                                with patch("sys.argv", ["run.py", "--once"]):
                                                    # --once is paper mode (no --live flag)
                                                    from src.scripts.run import main
                                                    try:
                                                        main()
                                                    except (SystemExit, Exception):
                                                        pass  # Expected due to mocking

                                                    # Verify retry function was NOT called in paper mode
                                                    mock_retry.assert_not_called()

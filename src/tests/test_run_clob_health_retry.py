"""Tests for issue #1003: Retry CLOB health check with backoff instead of exiting on first failure.

Test additions for issue #1004: Log fatal startup errors before SystemExit so they carry a timestamp.
"""
from __future__ import annotations

import contextlib
import logging
import re
from unittest.mock import MagicMock, patch, call

import pytest

from src.scripts.run import retry_clob_health_with_backoff
from src.logging_config import setup_logging


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

        Proves main() reached poll_once (past the health check branch), then asserts
        retry_clob_health_with_backoff was never called. This ensures the test
        actually detects if the call is moved outside the `if args.live:` guard.
        """
        patches = (
            patch("src.scripts.run.retry_clob_health_with_backoff"),
            patch("src.scripts.run.LiveTrader"),
            patch("src.execution.auth.get_clob_client"),
            patch("src.data.db.Database"),
            patch("src.scripts.run.setup_logging"),
            patch("src.scripts.run.RiskManager"),
            patch("src.scripts.run.AlertManager"),
            patch("src.scripts.run.start_dashboard"),
            patch("src.scripts.run.poll_once"),
            patch("src.scripts.run._load_open_no_positions", return_value=[]),
            patch("sys.argv", ["run.py", "--once"]),
        )

        with contextlib.ExitStack() as stack:
            context_managers = [stack.enter_context(p) for p in patches]
            mock_retry, mock_live_trader, mock_get_client, mock_db, \
                mock_setup_logging, mock_risk_mgr, mock_alert_mgr, \
                mock_dashboard, mock_poll_once, mock_load_positions, \
                mock_argv = context_managers

            from src.scripts.run import main
            main()

            # Prove main() reached poll_once, which is after the health check branch
            mock_poll_once.assert_called()

            # Prove retry_clob_health_with_backoff was NOT called in paper mode
            mock_retry.assert_not_called()


class TestLoggingWithTimestamp:
    """Tests for issue #1004: Verify fatal errors are logged with timestamps for greppability.

    The issue requires proving that log.error() is called BEFORE SystemExit(1), and that
    the log output is formatted with an ISO timestamp so operators can grep logs by time.
    """

    def test_error_log_recorded_before_systemexit(self, caplog):
        """Verify that log.error() is called before SystemExit(1) is raised.

        This test captures log records and verifies that when all health check
        attempts fail, an ERROR level log is emitted with proper content before
        the SystemExit(1) is raised. This demonstrates that the log message will
        include a timestamp when formatted by setup_logging()'s configured
        formatter (which uses datefmt="%Y-%m-%dT%H:%M:%S").

        The actual formatted output (with timestamp) is verified in the
        integration test below, which uses a custom handler to apply the real
        formatter outside of pytest's caplog interception.
        """
        setup_logging()

        mock_check = MagicMock(return_value=False)
        mock_sleep = MagicMock()

        with caplog.at_level(logging.ERROR):
            with pytest.raises(SystemExit) as exc_info:
                retry_clob_health_with_backoff(check_func=mock_check, sleep_func=mock_sleep)

        # Verify SystemExit code is 1 (int, not string) — this is critical
        assert exc_info.value.code == 1, "SystemExit must use exit code 1 (int), not a string"

        # Verify ERROR log was recorded before SystemExit was raised
        error_logs = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(error_logs) == 1, f"Expected 1 ERROR log, got {len(error_logs)}"

        error_record = error_logs[0]

        # Verify error message content
        msg = error_record.getMessage()
        assert "6 attempts" in msg
        assert "verify POLYMARKET_API_KEY and connectivity" in msg

    def test_error_log_formatted_includes_iso_timestamp(self):
        """Verify that logged error includes ISO timestamp when formatted with setup_logging().

        This test verifies the specific complaint from #1004: logs must be greppable by time,
        meaning they must start with an ISO timestamp (YYYY-MM-DDTHH:MM:SS).

        Critically: this test retrieves the REAL formatter from setup_logging()'s configured
        root handler, not a hardcoded copy. This ensures we catch if #1005 (or any future change)
        modifies the format string — if the formatter changes, this test will still pass (because
        we only assert the invariant: ISO timestamp leads the line) but will validate against
        the actual configured format, not a stale copy.

        Pytest's caplog interferes with handlers, so we clean and reconfigure logging
        in isolation, then validate with a custom handler using the real formatter.
        """
        root_logger = logging.getLogger()

        # Save and clear existing handlers (pytest's caplog may have added them)
        original_handlers = root_logger.handlers[:]
        root_logger.handlers = []

        try:
            # Setup logging fresh without pytest's interference
            setup_logging()

            # Retrieve the REAL formatter from the root logger's handlers
            formatter = None
            for handler in root_logger.handlers:
                if handler.formatter:
                    formatter = handler.formatter
                    break

            assert formatter is not None, "setup_logging() must configure a formatter on root logger"

            # Create a capture handler with the REAL formatter
            class CaptureHandler(logging.Handler):
                def __init__(self):
                    super().__init__()
                    self.formatted_records = []

                def emit(self, record):
                    formatted = self.formatter.format(record)
                    self.formatted_records.append(formatted)

            capture = CaptureHandler()
            capture.setFormatter(formatter)
            root_logger.addHandler(capture)

            # Create a test log record and emit it through the logger
            logger = logging.getLogger("src.scripts.run")
            logger.error(
                "[run] CLOB health check failed after %d attempts over %.1f seconds -- "
                "verify POLYMARKET_API_KEY and connectivity",
                6,
                0.0,
            )

            # Verify we captured the formatted record
            assert len(capture.formatted_records) > 0, "Expected at least one formatted record"
            formatted = capture.formatted_records[0]

            # INVARIANT: Formatted log must start with ISO timestamp (YYYY-MM-DDTHH:MM:SS)
            # This is the core complaint from #1004 — logs must be greppable by time.
            # We assert ONLY this invariant, not the full field layout, because #1005
            # will legitimately add %(process)d and we must not fail on that.
            iso_timestamp_pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\s"
            assert re.match(iso_timestamp_pattern, formatted), (
                f"Log output must start with ISO timestamp (YYYY-MM-DDTHH:MM:SS). Got: {formatted}"
            )
        finally:
            # Restore original handlers
            root_logger.handlers = original_handlers

    def test_setup_logging_called_before_health_check_in_main_ordering(self):
        """Verify that setup_logging() is called before retry_clob_health_with_backoff() in main().

        This is a design requirement of #1004: logging must be configured before any
        fatal error path is reached, so that log.error() produces formatted output.

        NOTE: This test uses inspect.getsource() and str.find() to locate function calls
        by string position, which is fragile — it could be fooled by the string appearing
        in a comment or docstring. However, it serves as a structural guard against
        major refactoring that moves the health check outside the logging-configured
        section. A more robust test would require AST analysis, but this is acceptable
        for catching accidental regressions.
        """
        from src.scripts.run import main
        import inspect

        # Get the source code of main()
        source = inspect.getsource(main)

        # Find the positions of setup_logging and retry_clob_health_with_backoff
        setup_logging_pos = source.find("setup_logging()")
        retry_health_pos = source.find("retry_clob_health_with_backoff()")

        assert setup_logging_pos != -1, "setup_logging() not found in main()"
        assert retry_health_pos != -1, "retry_clob_health_with_backoff() not found in main()"

        # Verify setup_logging is called before retry_clob_health_with_backoff
        assert setup_logging_pos < retry_health_pos, (
            "setup_logging() must be called before retry_clob_health_with_backoff() "
            f"to ensure logs are formatted with timestamps (setup_logging at {setup_logging_pos}, "
            f"retry_health at {retry_health_pos})"
        )

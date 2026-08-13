"""Tests for logging_config.py.

Tests for issue #1005: Verify that the log formatter includes process ID (PID)
so concurrent processes are distinguishable, while maintaining timestamp-first invariant.
"""
import logging
import os
import re

from src.logging_config import setup_logging


class TestLoggingConfigFormat:
    """Test suite for log format configuration."""

    def test_log_format_includes_process_id(self):
        """Verify that the log format includes the process ID.

        Call setup_logging(), render a record through the root handler's ACTUAL
        formatter, and assert the output contains the current process ID.
        """
        root_logger = logging.getLogger()

        # Save and clear existing handlers to avoid pytest's caplog interference
        original_handlers = root_logger.handlers[:]
        root_logger.handlers = []

        try:
            setup_logging()

            # Retrieve the REAL formatter from the root logger's handlers
            formatter = None
            for handler in root_logger.handlers:
                if handler.formatter:
                    formatter = handler.formatter
                    break

            assert formatter is not None, "setup_logging() must configure a formatter"

            # Create a custom handler to capture formatted output
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
            logger = logging.getLogger("src.tests.test_logging_config")
            logger.info("Test message for PID verification")

            # Verify we captured the formatted record
            assert len(capture.formatted_records) > 0, "Expected at least one formatted record"
            formatted = capture.formatted_records[0]

            # Assert the output contains the current process ID
            current_pid = os.getpid()
            assert f"[{current_pid}]" in formatted, (
                f"Log output must contain process ID [{current_pid}]. Got: {formatted}"
            )
        finally:
            # Restore original handlers
            root_logger.handlers = original_handlers

    def test_log_timestamp_leads_line(self):
        """Verify that the ISO timestamp still leads the log line.

        This is a strict addition requirement: the PID addition must not
        re-arrange existing fields. The timestamp MUST remain first.
        """
        root_logger = logging.getLogger()

        # Save and clear existing handlers
        original_handlers = root_logger.handlers[:]
        root_logger.handlers = []

        try:
            setup_logging()

            # Retrieve the REAL formatter from the root logger's handlers
            formatter = None
            for handler in root_logger.handlers:
                if handler.formatter:
                    formatter = handler.formatter
                    break

            assert formatter is not None, "setup_logging() must configure a formatter"

            # Create a custom handler to capture formatted output
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
            logger = logging.getLogger("src.tests.test_logging_config")
            logger.info("Test message for timestamp verification")

            # Verify we captured the formatted record
            assert len(capture.formatted_records) > 0, "Expected at least one formatted record"
            formatted = capture.formatted_records[0]

            # INVARIANT: Formatted log must start with ISO timestamp (YYYY-MM-DDTHH:MM:SS)
            # This is critical for log tooling and time-based grepping
            iso_timestamp_pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\s"
            assert re.match(iso_timestamp_pattern, formatted), (
                f"Log output must start with ISO timestamp (YYYY-MM-DDTHH:MM:SS). Got: {formatted}"
            )
        finally:
            # Restore original handlers
            root_logger.handlers = original_handlers

    def test_two_processes_distinguishable(self):
        """Verify that two records with differing process IDs are distinguishable.

        Render two records with different process IDs through the same formatter
        and assert the rendered lines differ in the PID field. This validates
        the actual capability being added: distinguishing concurrent processes.
        """
        root_logger = logging.getLogger()

        # Save and clear existing handlers
        original_handlers = root_logger.handlers[:]
        root_logger.handlers = []

        try:
            setup_logging()

            # Retrieve the REAL formatter from the root logger's handlers
            formatter = None
            for handler in root_logger.handlers:
                if handler.formatter:
                    formatter = handler.formatter
                    break

            assert formatter is not None, "setup_logging() must configure a formatter"

            # Create a custom handler to capture formatted output
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

            # Create and emit two log records with different process IDs
            # We simulate this by manually creating LogRecord objects with different process values
            logger = logging.getLogger("src.tests.test_logging_config")

            # Create first record with current PID
            record1 = logging.LogRecord(
                name=logger.name,
                level=logging.INFO,
                pathname="test.py",
                lineno=1,
                msg="First process message",
                args=(),
                exc_info=None,
            )
            record1.process = 12345  # Simulate PID 12345

            # Create second record with different PID
            record2 = logging.LogRecord(
                name=logger.name,
                level=logging.INFO,
                pathname="test.py",
                lineno=2,
                msg="Second process message",
                args=(),
                exc_info=None,
            )
            record2.process = 67890  # Simulate PID 67890

            # Format both records
            formatted1 = formatter.format(record1)
            formatted2 = formatter.format(record2)

            # Extract PID fields from formatted output
            # Pattern: [NNNNNN] where N is a digit
            pid_pattern = r"\[(\d+)\]"
            pids1 = re.findall(pid_pattern, formatted1)
            pids2 = re.findall(pid_pattern, formatted2)

            # Verify both records have at least one bracketed number (the PID)
            assert len(pids1) > 0, f"Could not find PID in formatted record: {formatted1}"
            assert len(pids2) > 0, f"Could not find PID in formatted record: {formatted2}"

            # The first bracketed number should be the process ID
            assert pids1[0] == "12345", f"Expected PID [12345], got {formatted1}"
            assert pids2[0] == "67890", f"Expected PID [67890], got {formatted2}"

            # Verify the complete formatted lines differ
            assert formatted1 != formatted2, (
                "Records with different process IDs should produce different output"
            )

        finally:
            # Restore original handlers
            root_logger.handlers = original_handlers

    def test_log_level_handling_info_default(self):
        """Verify LOG_LEVEL handling: INFO is the default level."""
        root_logger = logging.getLogger()

        # Save and clear existing handlers
        original_handlers = root_logger.handlers[:]
        root_logger.handlers = []

        # Ensure LOG_LEVEL is not set
        original_log_level = os.environ.pop("LOG_LEVEL", None)

        try:
            setup_logging()

            # The root logger should be at INFO level
            assert root_logger.level == logging.INFO, (
                f"Default LOG_LEVEL should be INFO, got {logging.getLevelName(root_logger.level)}"
            )
        finally:
            # Restore original handlers and environment
            root_logger.handlers = original_handlers
            if original_log_level is not None:
                os.environ["LOG_LEVEL"] = original_log_level

    def test_log_level_handling_debug_env(self):
        """Verify LOG_LEVEL=DEBUG environment variable enables debug output."""
        root_logger = logging.getLogger()

        # Save and clear existing handlers
        original_handlers = root_logger.handlers[:]
        root_logger.handlers = []

        # Save original LOG_LEVEL
        original_log_level = os.environ.get("LOG_LEVEL")

        try:
            # Set LOG_LEVEL=DEBUG
            os.environ["LOG_LEVEL"] = "DEBUG"

            setup_logging()

            # The root logger should be at DEBUG level
            assert root_logger.level == logging.DEBUG, (
                f"LOG_LEVEL=DEBUG should set logger to DEBUG, got {logging.getLevelName(root_logger.level)}"
            )
        finally:
            # Restore original handlers and environment
            root_logger.handlers = original_handlers
            if original_log_level is not None:
                os.environ["LOG_LEVEL"] = original_log_level
            else:
                os.environ.pop("LOG_LEVEL", None)

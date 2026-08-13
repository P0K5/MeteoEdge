"""Tests for scripts/rotate_bot_log.py (generalized rotation script).

Tests verify that the script can rotate multiple logs with correct ownership handling.
"""
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest


@pytest.fixture
def fake_repo_root(tmp_path):
    """Create a fake repo root with logs directory."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    return tmp_path


def test_rotate_multiple_logs_success(fake_repo_root):
    """Test that the script successfully rotates multiple logs."""
    # Import after setting up the environment
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

    # We can't directly test the script since it's not a module,
    # but we can test the core rotation logic it uses
    from src.utils.log_rotation import rotate_plaintext_log, housekeep_plaintext

    logs_to_test = [
        "bot.log",
        "archive.log",
        "settle.log",
        "capture_forecasts.log",
    ]

    for log_name in logs_to_test:
        log_path = fake_repo_root / "logs" / log_name
        log_path.write_text(f"test content for {log_name}\n")

        # Rotate
        rotated = rotate_plaintext_log(log_path)
        assert rotated.exists(), f"{log_name}: dated file should exist"
        assert log_path.read_text() == "", f"{log_name}: original should be truncated"

        # Housekeep
        housekeep_plaintext(log_path)
        # If housekeep ran without error, it's working

        # Verify log_path still exists (important for systemd append: mode)
        assert log_path.exists(), f"{log_name}: original file should still exist after housekeep"


def test_rotate_all_required_logs(fake_repo_root):
    """Test that all required logs can be rotated."""
    from src.utils.log_rotation import rotate_plaintext_log

    # These are the exact logs from LOGS_TO_ROTATE in the script
    required_logs = [
        "bot.log",
        "archive.log",
        "capture_forecasts.log",
        "health_report.log",
        "prob_cap_report.log",
        "purge.log",
        "resolve_outcomes.log",
        "settle.log",
    ]

    # Create all logs
    for log_name in required_logs:
        log_path = fake_repo_root / "logs" / log_name
        log_path.write_text(f"{log_name} content\n")
        assert log_path.exists(), f"Created {log_name}"

    # Rotate all logs
    for log_name in required_logs:
        log_path = fake_repo_root / "logs" / log_name
        rotated = rotate_plaintext_log(log_path)
        assert rotated.exists(), f"{log_name}: rotation should create dated file"
        assert log_path.read_text() == "", f"{log_name}: original should be truncated"


def test_rotate_preserves_ownership_when_fixed(fake_repo_root):
    """Test that ownership fix is attempted for rotated files.

    Mocks os.chown (as the ownership tests in test_log_rotation.py do) so this
    exercises the call-site wiring without depending on the CI runner's actual
    privileges — a non-root CI user cannot chown to an arbitrary uid/gid.
    """
    from src.utils.log_rotation import rotate_plaintext_log

    log_path = fake_repo_root / "logs" / "test.log"
    log_path.write_text("test data\n")

    with mock.patch("src.utils.log_rotation.os.chown", create=True) as mock_chown:
        rotated = rotate_plaintext_log(log_path, owner_uid=1000, owner_gid=1000)

    assert rotated.exists(), "Rotation should succeed even with ownership fix"
    assert log_path.read_text() == "", "Log should be truncated"

    # Ownership fix should be attempted for both the dated file and the
    # truncated original (per _fix_file_ownership call sites in rotate_plaintext_log).
    assert mock_chown.call_count == 2, "chown should be called for dated file and original"
    for call in mock_chown.call_args_list:
        args = call.args
        assert args[1] == 1000, "uid should be passed through"
        assert args[2] == 1000, "gid should be passed through"


def test_rotate_handles_missing_log_gracefully(fake_repo_root):
    """Test that rotation handles missing logs without crashing."""
    from src.utils.log_rotation import rotate_plaintext_log

    log_path = fake_repo_root / "logs" / "nonexistent.log"
    assert not log_path.exists()

    # Should return the dated path without error (rotate_plaintext_log noop if file missing)
    rotated = rotate_plaintext_log(log_path)
    assert isinstance(rotated, Path), "Should return a Path object even if file missing"


def test_housekeep_all_logs(fake_repo_root):
    """Test housekeeping for multiple logs independently."""
    from datetime import date, timedelta
    from src.utils.log_rotation import housekeep_plaintext, SNAPSHOT_RETAIN_DAYS, _today_utc

    logs_to_test = ["bot.log", "settle.log", "archive.log"]

    for log_name in logs_to_test:
        log_path = fake_repo_root / "logs" / log_name
        today = _today_utc()

        # Create an ancient dated file (should be deleted)
        # Format: bot.2025-08-12.log (not bot.log.2025-08-12)
        ancient_date = today - timedelta(days=SNAPSHOT_RETAIN_DAYS + 1)
        stem = log_path.stem  # "bot" from "bot.log"
        suffix = log_path.suffix  # ".log"
        ancient_filename = f"{stem}.{ancient_date.isoformat()}{suffix}"
        ancient_file = fake_repo_root / "logs" / ancient_filename
        ancient_file.write_text("ancient data\n")
        assert ancient_file.exists()

        # Housekeep should delete it
        housekeep_plaintext(log_path)
        assert not ancient_file.exists(), f"{log_name}: ancient file should be deleted"


def test_rotation_idempotent_for_same_day(fake_repo_root):
    """Test that rotating the same log multiple times on the same day preserves all data."""
    from src.utils.log_rotation import rotate_plaintext_log

    log_path = fake_repo_root / "logs" / "test.log"

    # First rotation
    log_path.write_text("first\n")
    dated1 = rotate_plaintext_log(log_path)
    assert dated1.read_text() == "first\n"
    assert log_path.read_text() == ""

    # Second rotation (same day)
    log_path.write_text("second\n")
    dated2 = rotate_plaintext_log(log_path)

    # Should append to the same dated file
    assert dated1 == dated2, "Same date should produce same dated file"
    content = dated2.read_text()
    assert "first\n" in content and "second\n" in content, "Both rotations should be preserved"

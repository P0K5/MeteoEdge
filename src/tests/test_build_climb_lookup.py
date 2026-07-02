"""Tests for scripts/build_climb_lookup.py dirty-baseline guard."""
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest


class TestClimbLookupDirtyGuard:
    """Tests for check_climb_lookup_dirty function."""

    def test_clean_file_no_exception(self):
        """Clean file (git diff returns 0) should not raise exception."""
        from scripts.build_climb_lookup import check_climb_lookup_dirty

        # Mock subprocess.run to simulate clean file (return code 0)
        with mock.patch("scripts.build_climb_lookup.subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0)
            # Should not raise
            check_climb_lookup_dirty(force=False)
            mock_run.assert_called_once()

    def test_dirty_file_without_force_exits(self):
        """Dirty file (git diff returns 1) without force should exit."""
        from scripts.build_climb_lookup import check_climb_lookup_dirty

        # Mock subprocess.run to simulate dirty file (return code 1)
        with mock.patch("scripts.build_climb_lookup.subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=1)
            # Should raise SystemExit
            with pytest.raises(SystemExit) as exc_info:
                check_climb_lookup_dirty(force=False)
            assert exc_info.value.code == 1

    def test_dirty_file_with_force_logs_warning(self, caplog):
        """Dirty file with force=True should log warning but not exit."""
        from scripts.build_climb_lookup import check_climb_lookup_dirty

        # Mock subprocess.run to simulate dirty file (return code 1)
        with mock.patch("scripts.build_climb_lookup.subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=1)
            # Should not raise
            check_climb_lookup_dirty(force=True)
            # Should log a warning about uncommitted changes
            assert "uncommitted" in caplog.text.lower()

    def test_git_not_available_warns_and_proceeds(self, caplog):
        """If git is not available (FileNotFoundError), should warn and proceed."""
        from scripts.build_climb_lookup import check_climb_lookup_dirty

        # Mock subprocess.run to simulate git not available
        with mock.patch("scripts.build_climb_lookup.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git: command not found")
            # Should not raise
            check_climb_lookup_dirty(force=False)
            # Should log a warning
            assert "not available" in caplog.text.lower() or "WARNING" in caplog.text

    def test_dirty_file_error_message_contains_fixes(self, capsys):
        """Error message for dirty file should suggest actionable fixes."""
        from scripts.build_climb_lookup import check_climb_lookup_dirty

        # Mock subprocess.run to simulate dirty file
        with mock.patch("scripts.build_climb_lookup.subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=1)
            # Capture stderr
            with pytest.raises(SystemExit):
                check_climb_lookup_dirty(force=False)
            captured = capsys.readouterr()
            assert "git checkout" in captured.err
            assert "git commit" in captured.err
            assert "--force" in captured.err


class TestBuildClimbLookupIntegration:
    """Integration tests using real git repo (if in MeteoEdge repo)."""

    def test_git_diff_quiet_exit_codes(self):
        """Test that git diff --quiet returns expected exit codes."""
        # This test uses the real git repo
        # If the file is clean, git diff --quiet returns 0
        # If the file is dirty, git diff --quiet returns 1

        result_clean = subprocess.run(
            ["git", "diff", "--quiet", "--", "scripts/build_climb_lookup.py"],
            cwd="/home/user/MeteoEdge",
            capture_output=True,
        )
        # We expect either 0 (clean) or 1 (dirty), not an error
        assert result_clean.returncode in (0, 1)

    def test_script_parses_correctly(self):
        """Test that the script parses without syntax errors."""
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", "scripts/build_climb_lookup.py"],
            cwd="/home/user/MeteoEdge",
            capture_output=True,
        )
        assert result.returncode == 0, f"Script has syntax errors: {result.stderr}"

    def test_script_imports_correctly(self):
        """Test that the script module can be imported."""
        import sys
        sys.path.insert(0, "/home/user/MeteoEdge")
        try:
            from scripts.build_climb_lookup import check_climb_lookup_dirty
            assert callable(check_climb_lookup_dirty)
        finally:
            sys.path.pop(0)

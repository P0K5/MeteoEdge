"""Tests for scripts/build_climb_lookup.py dirty-baseline guard.

Each test builds its OWN temporary git repository (via the tmp_git_repo fixture)
and monkeypatches the working directory into it, so no test depends on the real
repo's git state (issue #592 acceptance criterion).
"""
import subprocess

import pytest

from scripts.build_climb_lookup import check_climb_lookup_dirty


def _run_git(args, cwd):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        check=True,
    )


@pytest.fixture
def tmp_git_repo(tmp_path):
    """Create a temporary git repo with src/data/climb_lookup.py committed clean.

    Returns the repo root Path. Tests monkeypatch.chdir into it so the function's
    relative path 'src/data/climb_lookup.py' resolves inside this tmp repo.
    """
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    _run_git(["init"], repo_path)
    _run_git(["config", "user.email", "test@example.com"], repo_path)
    _run_git(["config", "user.name", "Test User"], repo_path)

    src_data_dir = repo_path / "src" / "data"
    src_data_dir.mkdir(parents=True)
    climb_lookup_file = src_data_dir / "climb_lookup.py"
    climb_lookup_file.write_text("# Auto-generated\nCLIMB_LOOKUP: dict = {}\n")

    _run_git(["add", "src/data/climb_lookup.py"], repo_path)
    _run_git(["commit", "-m", "Initial commit"], repo_path)

    return repo_path


class TestClimbLookupDirtyGuard:
    """Tests for check_climb_lookup_dirty against a real tmp git repo."""

    def test_clean_file_does_not_raise(self, tmp_git_repo, monkeypatch):
        """Clean committed file must NOT raise SystemExit."""
        monkeypatch.chdir(tmp_git_repo)
        # Should return normally (no exception).
        check_climb_lookup_dirty(force=False)

    def test_dirty_file_without_force_aborts(self, tmp_git_repo, monkeypatch, capsys):
        """Dirty file with force=False must raise SystemExit(1) with actionable message."""
        climb_lookup_file = tmp_git_repo / "src" / "data" / "climb_lookup.py"
        with climb_lookup_file.open("a") as f:
            f.write("# poisoned garbage appended after commit\n")

        monkeypatch.chdir(tmp_git_repo)
        with pytest.raises(SystemExit) as exc_info:
            check_climb_lookup_dirty(force=False)
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "git checkout" in captured.err
        assert "git commit" in captured.err
        assert "--force" in captured.err

    def test_dirty_file_with_force_only_warns(self, tmp_git_repo, monkeypatch, caplog):
        """Dirty file with force=True must proceed (no raise) and log a warning."""
        climb_lookup_file = tmp_git_repo / "src" / "data" / "climb_lookup.py"
        with climb_lookup_file.open("a") as f:
            f.write("# intentional refinement appended after commit\n")

        monkeypatch.chdir(tmp_git_repo)
        # Should NOT raise.
        check_climb_lookup_dirty(force=True)
        assert "uncommitted" in caplog.text.lower()

    def test_not_a_git_repo_warns_and_proceeds(self, tmp_path, monkeypatch, caplog):
        """Outside any git repo (git diff exits 128), must warn and proceed (no raise)."""
        non_repo = tmp_path / "plain"
        non_repo.mkdir()
        monkeypatch.chdir(non_repo)
        # Should NOT raise even with force=False.
        check_climb_lookup_dirty(force=False)
        assert (
            "not available" in caplog.text.lower()
            or "not in a git repo" in caplog.text.lower()
        )

"""Tests for scripts/check_prompt_drift.sh — the single-source-of-truth guard
that fails CI if the agent prompt surface re-inlines the governance/graphify
protocols (issue #748).
"""
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_prompt_drift.sh"


def _run(cwd):
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def test_script_exists():
    assert SCRIPT.is_file()


def test_passes_on_the_committed_tree():
    # Regression guard: the real repo must stay drift-free. If a future change
    # re-inlines protocol text into an agent file, this test fails.
    result = _run(REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_detects_reinlined_graphql_mutation(tmp_path):
    # Build a fake prompt surface where CLAUDE.md re-inlines the status mutation.
    (tmp_path / "agents").mkdir()
    (tmp_path / ".claude").mkdir()
    (tmp_path / "CLAUDE.md").write_text(
        "Use updateProjectV2ItemFieldValue to set status.\n", encoding="utf-8")

    result = _run(tmp_path)
    assert result.returncode == 1
    assert "DRIFT" in result.stdout


def test_detects_reinlined_graphify_rules(tmp_path):
    (tmp_path / "agents").mkdir()
    agents_dir = tmp_path / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "mid-dev.md").write_text(
        "If graphify is missing, pip install graphifyy first.\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("clean\n", encoding="utf-8")

    result = _run(tmp_path)
    assert result.returncode == 1
    assert "DRIFT" in result.stdout

"""Issue #1184: the PR policy checks must re-read the PR body live, not from
the frozen github.event payload, and the AI review must re-run on body edits.

Offline text checks on the workflow files (no YAML dependency).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PR_BODY_CHECK = ROOT / ".github" / "workflows" / "pr-body-check.yml"
AI_REVIEW = ROOT / ".github" / "workflows" / "ai-review.yml"


def test_pr_body_check_fetches_body_live_not_from_event():
    text = PR_BODY_CHECK.read_text(encoding="utf-8")
    assert "github.event.pull_request.body" not in text
    assert re.search(r'gh api "repos/\$\{\{ github\.repository \}\}/pulls/\$\{PR_NUMBER\}"', text)
    assert "--jq '.body // \"\"'" in text


def test_pr_body_check_triggers_on_edited():
    text = PR_BODY_CHECK.read_text(encoding="utf-8")
    assert "types: [opened, edited, synchronize, reopened]" in text


def test_ai_review_triggers_on_edited():
    text = AI_REVIEW.read_text(encoding="utf-8")
    assert "types: [opened, edited, synchronize, reopened]" in text

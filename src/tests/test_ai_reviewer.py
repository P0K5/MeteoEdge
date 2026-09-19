"""Tests for the DeepSeek retry/fail-closed logic in scripts/ai_reviewer.py
(originally issue #960, provider swapped from NVIDIA NIM per #1037/#1039).

GitHub calls, graphify, and policy-summary helpers are not exercised here —
this covers only call_deepseek()'s retry behavior on client-side timeouts and
429/5xx gateway responses, plus _run()'s fail-closed handling of those errors
(#1037: a required review that could not run must not report success).
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from scripts.ai_reviewer import (
    DEEPSEEK_MAX_ATTEMPTS,
    DEEPSEEK_RETRYABLE_STATUS_CODES,
    DIFF_MAX_CHARS,
    DeepSeekDegradedError,
    DeepSeekTimeoutError,
    _run,
    build_review_packet,
    call_deepseek,
    fetch_issue,
)


def _fake_response(status_code, body_text):
    resp = MagicMock()
    resp.status_code = status_code
    resp.reason = "err"
    resp.text = body_text
    resp.ok = 200 <= status_code < 300
    resp.json.return_value = {"choices": [{"message": {"content": "VERDICT: PASS"}}]}
    return resp


@patch("scripts.ai_reviewer.time.sleep")
@patch("scripts.ai_reviewer.requests.post")
def test_retries_client_timeout_then_succeeds(mock_post, mock_sleep):
    mock_post.side_effect = [
        requests.exceptions.Timeout("simulated"),
        requests.exceptions.Timeout("simulated"),
        _fake_response(200, "ok"),
    ]
    result = call_deepseek("https://x", "key", "model", "sys", "user")
    assert result == "VERDICT: PASS"
    assert mock_post.call_count == 3
    assert mock_sleep.call_count == 2


@patch("scripts.ai_reviewer.time.sleep")
@patch("scripts.ai_reviewer.requests.post")
def test_raises_timeout_error_after_max_client_timeouts(mock_post, mock_sleep):
    mock_post.side_effect = requests.exceptions.Timeout("simulated")
    with pytest.raises(DeepSeekTimeoutError):
        call_deepseek("https://x", "key", "model", "sys", "user")
    assert mock_post.call_count == DEEPSEEK_MAX_ATTEMPTS


@pytest.mark.parametrize("status_code", DEEPSEEK_RETRYABLE_STATUS_CODES)
@patch("scripts.ai_reviewer.time.sleep")
@patch("scripts.ai_reviewer.requests.post")
def test_retries_gateway_error_then_succeeds(mock_post, mock_sleep, status_code):
    mock_post.side_effect = [
        _fake_response(status_code, "Gateway error"),
        _fake_response(200, "ok"),
    ]
    result = call_deepseek("https://x", "key", "model", "sys", "user")
    assert result == "VERDICT: PASS"
    assert mock_post.call_count == 2
    assert mock_sleep.call_count == 1


@pytest.mark.parametrize("status_code", DEEPSEEK_RETRYABLE_STATUS_CODES)
@patch("scripts.ai_reviewer.time.sleep")
@patch("scripts.ai_reviewer.requests.post")
def test_raises_timeout_error_after_max_gateway_errors(mock_post, mock_sleep, status_code):
    mock_post.return_value = _fake_response(status_code, "Gateway error")
    with pytest.raises(DeepSeekTimeoutError):
        call_deepseek("https://x", "key", "model", "sys", "user")
    assert mock_post.call_count == DEEPSEEK_MAX_ATTEMPTS


@patch("scripts.ai_reviewer.time.sleep")
@patch("scripts.ai_reviewer.requests.post")
def test_degraded_503_raises_immediately_without_retry(mock_post, mock_sleep):
    mock_post.return_value = _fake_response(503, "model function is DEGRADED right now")
    with pytest.raises(DeepSeekDegradedError):
        call_deepseek("https://x", "key", "model", "sys", "user")
    assert mock_post.call_count == 1
    mock_sleep.assert_not_called()


@patch("scripts.ai_reviewer.time.sleep")
@patch("scripts.ai_reviewer.requests.post")
def test_non_retryable_status_raises_immediately_without_retry(mock_post, mock_sleep):
    mock_post.return_value = _fake_response(400, "Bad Request: invalid payload")
    with pytest.raises(RuntimeError) as exc_info:
        call_deepseek("https://x", "key", "model", "sys", "user")
    assert not isinstance(exc_info.value, (DeepSeekDegradedError, DeepSeekTimeoutError))
    assert mock_post.call_count == 1
    mock_sleep.assert_not_called()


def _patch_run_dependencies(monkeypatch, *, call_side_effect):
    monkeypatch.setattr(
        "scripts.ai_reviewer.fetch_pr_metadata",
        lambda *a, **k: {"number": 1, "title": "t", "user": {"login": "u"},
                          "base": {"ref": "master"}, "head": {"ref": "feat"},
                          "body": "Closes #1"},
    )
    monkeypatch.setattr("scripts.ai_reviewer.fetch_pr_files", lambda *a, **k: [])
    monkeypatch.setattr("scripts.ai_reviewer.fetch_pr_diff", lambda *a, **k: "")
    monkeypatch.setattr("scripts.ai_reviewer.fetch_issue", lambda *a, **k: None)
    monkeypatch.setattr("scripts.ai_reviewer.load_graph", lambda *a, **k: {})
    monkeypatch.setattr("scripts.ai_reviewer.load_policy_summary", lambda *a, **k: "")
    monkeypatch.setattr(
        Path, "read_text", lambda self, *a, **k: "system prompt"
    )
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(
        "scripts.ai_reviewer.call_deepseek",
        MagicMock(side_effect=call_side_effect),
    )


@pytest.mark.parametrize(
    "exc,reason",
    [
        (DeepSeekTimeoutError("timed out"), "DeepSeek timeout"),
        (DeepSeekDegradedError("degraded"), "DeepSeek degraded"),
    ],
)
def test_run_fails_closed_on_unavailable_backend(monkeypatch, exc, reason):
    """A required review that could not run must fail the check, not pass it."""
    _patch_run_dependencies(monkeypatch, call_side_effect=exc)
    create_check_run_mock = MagicMock()
    monkeypatch.setattr("scripts.ai_reviewer.create_check_run", create_check_run_mock)
    post_pr_comment_mock = MagicMock()
    monkeypatch.setattr("scripts.ai_reviewer.post_pr_comment", post_pr_comment_mock)

    with pytest.raises(SystemExit) as exc_info:
        _run(
            api_key="key",
            deepseek_base_url="https://x",
            deepseek_model="model",
            github_token="tok",
            pr_number="1",
            pr_head_sha="sha",
            repo_owner="owner",
            repo_name="repo",
            repo_root=Path("."),
        )

    assert exc_info.value.code == 1, reason
    create_check_run_mock.assert_called_once()
    assert create_check_run_mock.call_args.kwargs["verdict"] == "UNAVAILABLE"
    # Fails the check, not a silent pass:
    post_pr_comment_mock.assert_not_called()


def _minimal_pr_meta(**overrides):
    base = {
        "number": 1, "title": "t", "user": {"login": "u"},
        "base": {"ref": "master"}, "head": {"ref": "feat"}, "body": "Closes #1",
    }
    base.update(overrides)
    return base


class TestDiffBudget:
    """Regression coverage for the diff-truncation budget (#1098: a 10-file,
    67,962-char PR was silently BLOCKed because path-ordered diff hunks for
    low-risk backtest_results/*.md reports exhausted a 4000-char budget
    before the reviewer ever saw the actual src/config.py and
    src/http_client.py changes it then complained it couldn't verify)."""

    def test_budget_covers_a_realistic_multi_file_pr(self):
        # Guards against silently shrinking the budget back to something
        # that would reproduce the #1098 failure.
        assert DIFF_MAX_CHARS >= 70_000

    def test_diff_under_budget_is_not_truncated(self):
        diff = "diff --git a/x.py b/x.py\n+line\n" * 10  # well under budget
        packet = build_review_packet(
            _minimal_pr_meta(), [], diff, {}, [], "policy",
        )
        assert diff in packet
        assert "diff truncated" not in packet

    def test_diff_over_budget_is_truncated_with_marker(self):
        diff = "x" * (DIFF_MAX_CHARS + 500)
        packet = build_review_packet(
            _minimal_pr_meta(), [], diff, {}, [], "policy",
        )
        assert "diff truncated" in packet
        assert len(diff) > DIFF_MAX_CHARS  # the fixture actually exercises truncation


class TestFetchIssueErrorLogging:
    """Test that fetch_issue logs errors to stderr (issue #1117: missing
    issues:read permission should be visible, not silently swallowed)."""

    @patch("scripts.ai_reviewer._github_get")
    def test_fetch_issue_logs_403_permission_error_to_stderr(
        self, mock_github_get, capsys
    ):
        """When fetch_issue gets a 403 permission error, it should log to
        stderr so the permission regression is visible in CI logs."""
        # Mock a 403 response without issues:read permission
        resp = MagicMock()
        resp.status_code = 403
        resp.reason = "Forbidden"
        resp.text = '{"message": "Resource not accessible by integration"}'
        exc = requests.exceptions.HTTPError("403 Forbidden")
        exc.response = resp
        mock_github_get.side_effect = exc

        result = fetch_issue("owner", "repo", 1117, "token")

        # Should return None (graceful degradation)
        assert result is None

        # But should log the error to stderr
        captured = capsys.readouterr()
        assert "Failed to fetch issue #1117" in captured.err
        assert "403" in captured.err

    @patch("scripts.ai_reviewer._github_get")
    def test_fetch_issue_logs_generic_exception_to_stderr(
        self, mock_github_get, capsys
    ):
        """When fetch_issue gets a generic exception (not HTTPError),
        it should still log it to stderr."""
        mock_github_get.side_effect = RuntimeError("Network timeout")

        result = fetch_issue("owner", "repo", 1117, "token")

        assert result is None
        captured = capsys.readouterr()
        assert "Failed to fetch issue #1117" in captured.err
        assert "Network timeout" in captured.err

    @patch("scripts.ai_reviewer._github_get")
    def test_fetch_issue_succeeds_silently_on_success(
        self, mock_github_get, capsys
    ):
        """When fetch_issue succeeds, it should not log anything to stderr."""
        resp = MagicMock()
        resp.json.return_value = {"number": 1117, "title": "Test issue"}
        mock_github_get.return_value = resp

        result = fetch_issue("owner", "repo", 1117, "token")

        assert result == {"number": 1117, "title": "Test issue"}
        captured = capsys.readouterr()
        assert captured.err == ""

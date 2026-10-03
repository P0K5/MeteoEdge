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
    DIFF_FALLBACK_MAX_CHARS,
    DIFF_MAX_CHARS,
    GENERATED_PATH_PREFIXES,
    DeepSeekContextLengthError,
    DeepSeekDegradedError,
    DeepSeekTimeoutError,
    _run,
    build_diff_from_files,
    build_review_packet,
    call_deepseek,
    fetch_issue,
    get_diff_max_chars,
    is_generated_path,
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


def _changed_file(filename, patch="+line", status="modified", additions=1, deletions=0):
    return {
        "filename": filename,
        "status": status,
        "additions": additions,
        "deletions": deletions,
        "patch": patch,
    }


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

    def test_default_cap_is_300k_and_fallback_is_80k(self, monkeypatch):
        monkeypatch.delenv("AI_REVIEW_DIFF_MAX_CHARS", raising=False)
        assert DIFF_MAX_CHARS == 300_000
        assert DIFF_FALLBACK_MAX_CHARS == 80_000
        assert get_diff_max_chars() == 300_000

    def test_env_override_valid(self, monkeypatch):
        monkeypatch.setenv("AI_REVIEW_DIFF_MAX_CHARS", "1234")
        assert get_diff_max_chars() == 1234

    @pytest.mark.parametrize("raw", ["abc", "0", "-5", "1.5"])
    def test_env_override_invalid_falls_back_with_log(self, monkeypatch, capsys, raw):
        monkeypatch.setenv("AI_REVIEW_DIFF_MAX_CHARS", raw)
        assert get_diff_max_chars() == DIFF_MAX_CHARS
        assert "Invalid AI_REVIEW_DIFF_MAX_CHARS" in capsys.readouterr().err

    def test_env_override_applies_to_packet(self, monkeypatch):
        monkeypatch.setenv("AI_REVIEW_DIFF_MAX_CHARS", "100")
        packet = build_review_packet(
            _minimal_pr_meta(), [_changed_file("src/x.py", patch="x" * 500)], {}, [], "p",
        )
        assert "diff truncated" in packet

    def test_diff_between_old_and_new_cap_is_not_truncated(self, monkeypatch):
        monkeypatch.delenv("AI_REVIEW_DIFF_MAX_CHARS", raising=False)
        packet = build_review_packet(
            _minimal_pr_meta(), [_changed_file("src/x.py", patch="x" * 250_000)], {}, [], "p",
        )
        assert "diff truncated" not in packet

    def test_diff_under_budget_is_not_truncated(self):
        changed_files = [_changed_file("src/x.py", patch="+line\n" * 10)]
        packet = build_review_packet(
            _minimal_pr_meta(), changed_files, {}, [], "policy",
        )
        assert "+line" in packet
        assert "diff truncated" not in packet

    def test_diff_over_budget_is_truncated_with_marker(self):
        changed_files = [_changed_file("src/x.py", patch="x" * (DIFF_MAX_CHARS + 500))]
        packet = build_review_packet(
            _minimal_pr_meta(), changed_files, {}, [], "policy",
        )
        assert "diff truncated" in packet


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


class TestGeneratedPathExclusion:
    """Regression coverage for #1118: graphify-out/** sorts ahead of src/**
    in GitHub's path-ordered diff, so generated files reliably ate the whole
    DIFF_MAX_CHARS budget before the reviewer ever saw real code (PR #1116).
    Generated paths must be excluded from the diff content entirely, not
    just budget-capped."""

    @pytest.mark.parametrize("prefix", GENERATED_PATH_PREFIXES)
    def test_is_generated_path_matches_known_prefixes(self, prefix):
        assert is_generated_path(f"{prefix}some/file.json")

    def test_is_generated_path_does_not_match_src(self):
        assert not is_generated_path("src/config.py")

    def test_build_diff_from_files_skips_generated_paths(self):
        changed_files = [
            _changed_file("graphify-out/graph.json", patch="+" + "x" * 500_000),
            _changed_file("backtest_results/report.md", patch="+huge backtest dump"),
            _changed_file("src/config.py", patch="+real code change"),
        ]
        diff = build_diff_from_files(changed_files)
        assert "graphify-out" not in diff
        assert "backtest_results" not in diff
        assert "+real code change" in diff

    def test_review_packet_includes_real_code_even_when_generated_files_precede_it(self):
        # Reproduces #1116/#1118: a huge graphify-out/graph.json diff sorted
        # (alphabetically) ahead of the actual src/ change. Before the fix,
        # this would exhaust DIFF_MAX_CHARS before the reviewer ever saw the
        # src/config.py hunk.
        changed_files = [
            _changed_file("graphify-out/graph.json", patch="+" + "x" * (DIFF_MAX_CHARS * 2)),
            _changed_file("src/config.py", patch="+real trading-safety-relevant change"),
        ]
        packet = build_review_packet(
            _minimal_pr_meta(), changed_files, {}, [], "policy",
        )
        assert "+real trading-safety-relevant change" in packet
        assert "diff truncated" not in packet

    def test_changed_files_listing_marks_generated_files_but_omits_their_diff(self):
        changed_files = [
            _changed_file("graphify-out/graph.json", patch="+lots of generated noise"),
            _changed_file("src/config.py", patch="+real code"),
        ]
        packet = build_review_packet(
            _minimal_pr_meta(), changed_files, {}, [], "policy",
        )
        assert "graphify-out/graph.json [modified]" in packet
        assert "(generated, diff omitted)" in packet
        assert "generated noise" not in packet
        # The listing line for the real file must NOT carry the suffix:
        assert "- src/config.py [modified] +1/-0\n" in packet


_CONTEXT_ERR = (
    "This model's maximum context length is 131072 tokens. However, you "
    "requested 140000 tokens"
)


@patch("scripts.ai_reviewer.time.sleep")
@patch("scripts.ai_reviewer.requests.post")
def test_context_length_400_raises_dedicated_error_without_retry(mock_post, mock_sleep):
    mock_post.return_value = _fake_response(400, _CONTEXT_ERR)
    with pytest.raises(DeepSeekContextLengthError):
        call_deepseek("https://x", "key", "model", "sys", "user")
    assert mock_post.call_count == 1
    mock_sleep.assert_not_called()


def _run_once():
    _run(
        api_key="key", deepseek_base_url="https://x", deepseek_model="model",
        github_token="tok", pr_number="1", pr_head_sha="sha",
        repo_owner="owner", repo_name="repo", repo_root=Path("."),
    )


def test_run_retries_once_with_smaller_cap_on_context_length(monkeypatch):
    monkeypatch.delenv("AI_REVIEW_DIFF_MAX_CHARS", raising=False)
    _patch_run_dependencies(
        monkeypatch,
        call_side_effect=[DeepSeekContextLengthError("too long"), "VERDICT: PASS"],
    )
    monkeypatch.setattr(
        "scripts.ai_reviewer.fetch_pr_files",
        lambda *a, **k: [_changed_file("src/x.py", patch="x" * 200_000)],
    )
    check_mock = MagicMock(return_value={"id": 1})
    monkeypatch.setattr("scripts.ai_reviewer.create_check_run", check_mock)
    monkeypatch.setattr("scripts.ai_reviewer.post_pr_comment", MagicMock(return_value={"id": 2}))

    _run_once()

    from scripts import ai_reviewer
    calls = ai_reviewer.call_deepseek.call_args_list
    assert len(calls) == 2
    first, second = (c.kwargs["user_content"] for c in calls)
    assert "diff truncated" not in first
    assert "diff truncated" in second
    assert len(second) < len(first)
    summary = check_mock.call_args.kwargs["review_text"]
    assert "truncated to 80000 characters to fit the model context" in summary


def test_run_does_not_loop_when_retry_also_fails_context_length(monkeypatch):
    _patch_run_dependencies(
        monkeypatch,
        call_side_effect=DeepSeekContextLengthError("too long"),
    )
    with pytest.raises(DeepSeekContextLengthError):
        _run_once()
    from scripts import ai_reviewer
    assert ai_reviewer.call_deepseek.call_count == 2


def test_run_does_not_retry_other_errors(monkeypatch):
    _patch_run_dependencies(monkeypatch, call_side_effect=RuntimeError("DeepSeek API 401"))
    with pytest.raises(RuntimeError):
        _run_once()
    from scripts import ai_reviewer
    assert ai_reviewer.call_deepseek.call_count == 1

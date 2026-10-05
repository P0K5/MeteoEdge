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
    AC_TEXT_MAX_CHARS,
    DEEPSEEK_MAX_ATTEMPTS,
    DEEPSEEK_RETRYABLE_STATUS_CODES,
    DIFF_FALLBACK_MAX_CHARS,
    DIFF_MAX_CHARS,
    GENERATED_PATH_PREFIXES,
    ISSUE_BODY_MAX_CHARS,
    PR_BODY_MAX_CHARS,
    DeepSeekContextLengthError,
    DeepSeekDegradedError,
    DeepSeekTimeoutError,
    _run,
    build_diff_from_files,
    build_review_packet,
    call_deepseek,
    extract_acceptance_criteria,
    fetch_issue,
    get_diff_max_chars,
    is_generated_path,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


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


def _issue(number, title="t", body=""):
    return {"number": number, "title": title, "body": body}


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


# ---------------------------------------------------------------------------
# #1243: the PR body never reached the model, and linked-issue bodies were
# truncated to 500 chars / 10 extracted AC lines, producing false BLOCKs on
# PRs whose rationale lived in the body (#1231, #1241) and whose acceptance
# criteria were cut mid-sentence (#1230) or dropped past the line cap.
# ---------------------------------------------------------------------------

class TestPRBodyInPacket:
    """The PR description is now included verbatim (within budget) under its
    own heading, instead of being discarded right after the closing-keyword
    regex ran over it."""

    def test_packet_contains_pr_body_verbatim(self):
        pr_meta = _minimal_pr_meta(body="Closes #1\n\n## Why\nBecause reasons, explained at length.")
        packet = build_review_packet(pr_meta, [], {}, [], "policy")
        assert "## PR Description" in packet
        assert "Because reasons, explained at length." in packet

    def test_empty_pr_body_is_flagged_as_policy_violation(self):
        pr_meta = _minimal_pr_meta(body="")
        packet = build_review_packet(pr_meta, [], {}, [], "policy")
        assert "PR body is empty" in packet
        assert "policy violation" in packet

    def test_whitespace_only_pr_body_is_flagged_as_policy_violation(self):
        pr_meta = _minimal_pr_meta(body="   \n\n  ")
        packet = build_review_packet(pr_meta, [], {}, [], "policy")
        assert "PR body is empty" in packet

    def test_oversized_pr_body_is_truncated_with_explicit_marker(self):
        pr_meta = _minimal_pr_meta(body="Closes #1\n" + "x" * (PR_BODY_MAX_CHARS + 500))
        packet = build_review_packet(pr_meta, [], {}, [], "policy")
        assert "PR body truncated" in packet
        assert "unverifiable" in packet

    def test_pr_body_under_budget_is_not_truncated(self):
        pr_meta = _minimal_pr_meta(body="Closes #1\n\nshort body, well under budget")
        packet = build_review_packet(pr_meta, [], {}, [], "policy")
        assert "PR body truncated" not in packet

    def test_real_world_1305_body_reaches_packet_with_closing_keyword_intact(self):
        """Live evidence (PR #1305, docs-only, 'Closes #1303' as the literal
        first line): the reviewer BLOCKed claiming no closing keyword was
        present in the body, because the body never reached it. With the
        body now in the packet, the literal keyword text is visible."""
        body = (
            "Closes #1303\n\n## Summary\n"
            "The standalone dashboard.service was retired because run.py:930 "
            "already calls start_dashboard()...\n"
        )
        pr_meta = _minimal_pr_meta(body=body)
        packet = build_review_packet(pr_meta, [], {}, [], "policy")
        assert "Closes #1303" in packet
        assert "- Closing keywords in PR body: #1303" in packet


class TestLinkedIssueBodyBudget:
    """Linked-issue bodies were previously capped at issue_body[:500]
    (~one paragraph), cutting real issues in this repo mid-sentence (#1230
    was the concrete example in #1243)."""

    def test_full_issue_body_reaches_packet_for_realistic_1230_fixture(self):
        body = (FIXTURES_DIR / "issue_1230_body.md").read_text(encoding="utf-8")
        assert len(body) < ISSUE_BODY_MAX_CHARS, "fixture should fit whole, not just survive truncation"
        issue = _issue(1230, "Fetch-RemoteData.ps1 rewrite", body)
        pr_meta = _minimal_pr_meta(body="Closes #1230")
        packet = build_review_packet(pr_meta, [], {}, [issue], "policy")
        # The sentence #1243 reports the old 500-char cap cut mid-sentence.
        assert "neither blocks nor is" in packet
        assert "blocked by the bot's writers" in packet
        assert "issue body truncated" not in packet

    def test_oversized_issue_body_is_truncated_with_explicit_marker(self):
        issue = _issue(1, "t", "x" * (ISSUE_BODY_MAX_CHARS + 500))
        pr_meta = _minimal_pr_meta(body="Closes #1")
        packet = build_review_packet(pr_meta, [], {}, [issue], "policy")
        assert "issue body truncated" in packet
        assert "unverifiable, not unmet" in packet

    def test_empty_issue_body_is_shown_explicitly(self):
        issue = _issue(1, "t", "")
        pr_meta = _minimal_pr_meta(body="Closes #1")
        packet = build_review_packet(pr_meta, [], {}, [issue], "policy")
        assert "(issue body is empty)" in packet

    def test_missing_issue_among_multiple_closing_keywords_is_named(self):
        """A PR closing two issues where only one fetch succeeds must name
        the missing one, not go silent on it (prior behavior only handled
        the all-fetches-failed case)."""
        pr_meta = _minimal_pr_meta(body="Closes #1\nCloses #2")
        issue2 = _issue(2, "t2", "body2")
        packet = build_review_packet(pr_meta, [], {}, [issue2], "policy")
        assert "#1" in packet
        assert "unavailable" in packet
        assert "### Issue #2: t2" in packet


class TestAcceptanceCriteriaHeadingStyles:
    """The extractor must recognize '## Acceptance criteria' and
    '**Acceptance criteria**' headings, keep nested/wrapped bullets, not
    false-trigger on a mid-sentence mention of the phrase, and never
    silently drop content below its stated character budget (replacing the
    old ac_lines[:10] cap, which counted each wrapped continuation line of a
    bullet as if it were its own top-level criterion)."""

    def test_atx_heading(self):
        text, truncated = extract_acceptance_criteria(
            "## Acceptance criteria\n- one\n- two\n\n## Next section\nnope"
        )
        assert "one" in text and "two" in text
        assert "nope" not in text
        assert not truncated

    def test_bold_heading(self):
        text, _ = extract_acceptance_criteria(
            "**Acceptance criteria**\n- one\n- two\n\n**Next section**\nnope"
        )
        assert "one" in text and "two" in text
        assert "nope" not in text

    def test_bold_heading_with_trailing_colon(self):
        text, _ = extract_acceptance_criteria(
            "**Acceptance Criteria:**\n- one\n\n## Next\nnope"
        )
        assert "one" in text
        assert "nope" not in text

    def test_nested_bullets_all_kept(self):
        text, _ = extract_acceptance_criteria(
            "## Acceptance criteria\n- top\n  - nested\n    - deeper\n\n## Next\nnope"
        )
        assert "top" in text
        assert "nested" in text
        assert "deeper" in text

    def test_checklist_style_bullets_kept(self):
        text, _ = extract_acceptance_criteria(
            "## Acceptance criteria\n- [ ] first\n- [ ] second\n\n## Dependencies\nnone"
        )
        assert "first" in text and "second" in text
        assert "none" not in text

    def test_mid_sentence_mention_does_not_false_trigger(self):
        body = (
            "This PR satisfies the acceptance criteria from issue #1 already.\n\n"
            "## Acceptance criteria\n- real one\n"
        )
        text, _ = extract_acceptance_criteria(body)
        assert "real one" in text
        assert "satisfies the acceptance criteria" not in text

    def test_no_ac_section_returns_none_extracted(self):
        text, truncated = extract_acceptance_criteria("just some text, no heading at all")
        assert text == "(none extracted)"
        assert not truncated

    def test_empty_body_returns_none_extracted(self):
        text, truncated = extract_acceptance_criteria("")
        assert text == "(none extracted)"
        assert not truncated

    def test_wrapped_bullets_not_dropped_by_old_line_count_cap(self):
        """Regression for #1230: the old ac_lines[:10] cap exhausted after
        roughly 2-3 wrapped bullets because each wrapped continuation line
        counted toward the cap. 15 bullets here, each wrapped across 3
        physical lines (45 lines total), must all survive."""
        bullets = "\n".join(
            f"- item {i} line one\n  continuation line two\n  continuation line three"
            for i in range(15)
        )
        body = f"## Acceptance criteria\n{bullets}\n"
        text, truncated = extract_acceptance_criteria(body)
        for i in range(15):
            assert f"item {i} line one" in text
        assert not truncated

    def test_truncated_past_char_budget_states_so(self):
        body = "## Acceptance criteria\n" + "\n".join(f"- {'x' * 100}" for _ in range(100))
        text, truncated = extract_acceptance_criteria(body)
        assert truncated
        assert len(text) == AC_TEXT_MAX_CHARS

    def test_packet_states_ac_truncation_explicitly(self):
        body = "## Acceptance criteria\n" + "\n".join(f"- {'x' * 100}" for _ in range(100))
        issue = _issue(1, "t", body)
        pr_meta = _minimal_pr_meta(body="Closes #1")
        packet = build_review_packet(pr_meta, [], {}, [issue], "policy")
        assert "acceptance criteria truncated" in packet


class TestBudgetInteraction:
    """An oversized PR body, oversized diff, and oversized issue body
    together must still yield a packet bounded by the sum of the individual
    caps, not an unbounded concatenation."""

    def test_oversized_everything_still_yields_bounded_packet(self):
        pr_meta = _minimal_pr_meta(body="Closes #1\n" + "p" * (PR_BODY_MAX_CHARS * 3))
        changed_files = [_changed_file("src/x.py", patch="d" * (DIFF_MAX_CHARS * 3))]
        issue = _issue(1, "t", "i" * (ISSUE_BODY_MAX_CHARS * 3))
        packet = build_review_packet(pr_meta, changed_files, {}, [issue], "policy")

        max_expected = (
            DIFF_MAX_CHARS + PR_BODY_MAX_CHARS + ISSUE_BODY_MAX_CHARS
            + AC_TEXT_MAX_CHARS + 5_000  # headroom: headings, labels, markers
        )
        assert len(packet) < max_expected
        assert "PR body truncated" in packet
        assert "issue body truncated" in packet
        assert "diff truncated" in packet

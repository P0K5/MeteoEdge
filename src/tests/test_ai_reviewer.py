"""Tests for the NIM retry/skip logic in scripts/ai_reviewer.py (issue #960).

GitHub calls, graphify, and policy-summary helpers are not exercised here —
this covers only call_nim()'s retry behavior on client-side timeouts and
502/503/504 gateway responses, which is what actually broke PR #958 and #955.
"""
from unittest.mock import MagicMock, patch

import pytest
import requests

from scripts.ai_reviewer import (
    NIM_MAX_ATTEMPTS,
    NIM_RETRYABLE_STATUS_CODES,
    NimDegradedError,
    NimTimeoutError,
    call_nim,
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
    result = call_nim("https://x", "key", "model", "sys", "user")
    assert result == "VERDICT: PASS"
    assert mock_post.call_count == 3
    assert mock_sleep.call_count == 2


@patch("scripts.ai_reviewer.time.sleep")
@patch("scripts.ai_reviewer.requests.post")
def test_raises_nim_timeout_error_after_max_client_timeouts(mock_post, mock_sleep):
    mock_post.side_effect = requests.exceptions.Timeout("simulated")
    with pytest.raises(NimTimeoutError):
        call_nim("https://x", "key", "model", "sys", "user")
    assert mock_post.call_count == NIM_MAX_ATTEMPTS


@pytest.mark.parametrize("status_code", NIM_RETRYABLE_STATUS_CODES)
@patch("scripts.ai_reviewer.time.sleep")
@patch("scripts.ai_reviewer.requests.post")
def test_retries_gateway_error_then_succeeds(mock_post, mock_sleep, status_code):
    mock_post.side_effect = [
        _fake_response(status_code, "Gateway error"),
        _fake_response(200, "ok"),
    ]
    result = call_nim("https://x", "key", "model", "sys", "user")
    assert result == "VERDICT: PASS"
    assert mock_post.call_count == 2
    assert mock_sleep.call_count == 1


@pytest.mark.parametrize("status_code", NIM_RETRYABLE_STATUS_CODES)
@patch("scripts.ai_reviewer.time.sleep")
@patch("scripts.ai_reviewer.requests.post")
def test_raises_nim_timeout_error_after_max_gateway_errors(mock_post, mock_sleep, status_code):
    mock_post.return_value = _fake_response(status_code, "Gateway error")
    with pytest.raises(NimTimeoutError):
        call_nim("https://x", "key", "model", "sys", "user")
    assert mock_post.call_count == NIM_MAX_ATTEMPTS


@patch("scripts.ai_reviewer.time.sleep")
@patch("scripts.ai_reviewer.requests.post")
def test_degraded_503_raises_immediately_without_retry(mock_post, mock_sleep):
    mock_post.return_value = _fake_response(503, "model function is DEGRADED right now")
    with pytest.raises(NimDegradedError):
        call_nim("https://x", "key", "model", "sys", "user")
    assert mock_post.call_count == 1
    mock_sleep.assert_not_called()


@patch("scripts.ai_reviewer.time.sleep")
@patch("scripts.ai_reviewer.requests.post")
def test_non_retryable_status_raises_immediately_without_retry(mock_post, mock_sleep):
    mock_post.return_value = _fake_response(400, "Bad Request: invalid payload")
    with pytest.raises(RuntimeError) as exc_info:
        call_nim("https://x", "key", "model", "sys", "user")
    assert not isinstance(exc_info.value, (NimDegradedError, NimTimeoutError))
    assert mock_post.call_count == 1
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Fail-closed on an unavailable reviewer (issue #1037)
# ---------------------------------------------------------------------------

class TestReviewerFailsClosedWhenItCannotRun:
    """A required merge check that could not run must NOT report success.

    Before #1037 both ``NimDegradedError`` and ``NimTimeoutError`` created a
    check run with ``verdict="PASS"`` and returned 0, so an unavailable
    reviewer produced a GREEN required check having reviewed nothing. That is
    strictly worse than a red one: a red check is loud and forces a conscious
    override, while a green one is silent and lets every PR through the gate
    unreviewed. Observed live on PR #1038, where a model that timed out on all
    three attempts still reported ``AI Review: PASS``.

    These tests pin the conclusion mapping, not the wording.
    """

    def test_unavailable_verdict_maps_to_a_failing_conclusion(self):
        """The whole fix rests on this: UNAVAILABLE must not be 'success'."""
        import scripts.ai_reviewer as ai

        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured.update(json)
            resp = MagicMock()
            resp.status_code = 201
            resp.json.return_value = {"id": 1}
            resp.raise_for_status.return_value = None
            return resp

        with patch.object(ai.requests, "post", side_effect=fake_post):
            ai.create_check_run(
                owner="o", repo="r", head_sha="sha", token="t",
                verdict="UNAVAILABLE",
                review_text="reviewer could not run",
            )

        assert captured["conclusion"] == "failure", (
            "An UNAVAILABLE reviewer must fail the check. If this maps to "
            "'success' the merge gate silently approves unreviewed code."
        )
        assert captured["output"]["title"] == "AI Review: UNAVAILABLE", (
            "The title must distinguish 'the reviewer could not run' from "
            "'the reviewer blocked this PR' -- they are different events."
        )

    def test_pass_still_maps_to_success(self):
        """Guard against over-correcting: a real PASS must stay green."""
        import scripts.ai_reviewer as ai

        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured.update(json)
            resp = MagicMock()
            resp.status_code = 201
            resp.json.return_value = {"id": 1}
            resp.raise_for_status.return_value = None
            return resp

        with patch.object(ai.requests, "post", side_effect=fake_post):
            ai.create_check_run(
                owner="o", repo="r", head_sha="sha", token="t",
                verdict="PASS", review_text="looks good",
            )

        assert captured["conclusion"] == "success"

    def test_no_handler_reports_pass_without_a_review(self):
        """Source-level: no fail-open path may hardcode verdict="PASS".

        The two handlers that regressed (#1037) both did exactly that. This
        asserts the pattern cannot come back, in the manner of the existing
        source-level assertions elsewhere in this suite.
        """
        from pathlib import Path

        src = (Path(__file__).resolve().parents[2]
               / "scripts" / "ai_reviewer.py").read_text(encoding="utf-8")
        assert 'verdict="PASS"' not in src, (
            'A handler hardcodes verdict="PASS". Only a genuine parsed '
            "verdict may pass the gate -- see #1037."
        )

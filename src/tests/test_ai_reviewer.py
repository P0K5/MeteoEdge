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

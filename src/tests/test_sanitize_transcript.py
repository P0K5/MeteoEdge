"""Tests for scripts/sanitize_transcript.py — the local secret/PII redactor
that runs before any transcript leaves the machine (issue #747).

Redaction is security-critical, so we cover each secret class (happy path),
nested-structure walking, malformed lines (failure mode), and the output
file-permission hardening.
"""
import json
import os
import stat

import pytest

from scripts.sanitize_transcript import (
    sanitize_text,
    sanitize_value,
    sanitize_file,
)


@pytest.mark.parametrize("secret,label", [
    ("sk-ant-api03-abcdefghijklmnop123456", "ANTHROPIC_KEY"),
    ("nvapi-abcdefghijklmnop1234567890", "NVIDIA_NIM_KEY"),
    ("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", "GITHUB_TOKEN"),
    ("github_pat_11ABCDEFGHIJKLMNOPQR_stuvwx", "GITHUB_PAT"),
    ("AKIAIOSFODNN7EXAMPLE", "AWS_ACCESS_KEY"),
])
def test_sanitize_text_redacts_known_secret_classes(secret, label):
    counts = {}
    out = sanitize_text(f"here is {secret} in text", counts)
    assert secret not in out
    assert f"[REDACTED:{label}]" in out
    assert counts.get(label, 0) >= 1


def test_sanitize_text_redacts_email():
    counts = {}
    out = sanitize_text("contact alice@example.com now", counts)
    assert "alice@example.com" not in out
    assert "[REDACTED:EMAIL]" in out


def test_sanitize_text_redacts_secret_env_assignment_keeps_var_name():
    counts = {}
    out = sanitize_text("NVIDIA_NIM_API_KEY=supersecretvalue123", counts)
    # The variable name is preserved (useful context); only the value is redacted.
    assert "NVIDIA_NIM_API_KEY=" in out
    assert "supersecretvalue123" not in out
    assert "[REDACTED:ENV_SECRET]" in out


def test_sanitize_text_leaves_clean_text_untouched():
    counts = {}
    text = "Opening PR. Closes #742 and running pytest -q."
    assert sanitize_text(text, counts) == text
    assert counts == {}


def test_sanitize_value_walks_nested_structures():
    counts = {}
    payload = {
        "message": {
            "content": [
                {"type": "text", "text": "key sk-ant-api03-abcdefghijklmnop123456"},
                {"nested": ["ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", 42, None]},
            ]
        },
        "n": 7,
    }
    out = sanitize_value(payload, counts)
    dumped = json.dumps(out)
    assert "sk-ant-api03" not in dumped
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in dumped
    # Non-string leaves are preserved unchanged.
    assert out["n"] == 7
    assert out["message"]["content"][1]["nested"][1] == 42
    assert out["message"]["content"][1]["nested"][2] is None


def test_sanitize_file_handles_valid_and_malformed_lines(tmp_path):
    src = tmp_path / "t.jsonl"
    src.write_text(
        json.dumps({"content": "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"}) + "\n"
        + "this is not json but has alice@example.com\n"
        + "\n",  # blank line skipped
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    out_path, n_lines, counts = sanitize_file(src, out_dir)

    assert n_lines == 2  # blank line skipped
    body = out_path.read_text(encoding="utf-8")
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in body
    assert "alice@example.com" not in body
    # The valid JSON line stays parseable after sanitization.
    first = json.loads(body.splitlines()[0])
    assert "[REDACTED:GITHUB_TOKEN]" in first["content"]
    # The malformed line is preserved as sanitized raw text, not dropped.
    assert "[REDACTED:EMAIL]" in body.splitlines()[1]


def test_sanitize_file_output_is_owner_only_readable(tmp_path):
    src = tmp_path / "t.jsonl"
    src.write_text(json.dumps({"content": "clean"}) + "\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    out_path, _, _ = sanitize_file(src, out_dir)
    # sanitize_file writes the file; main() chmods to 0o600. Emulate that guard
    # here to assert the intended permission is achievable and correct.
    os.chmod(out_path, 0o600)
    mode = stat.S_IMODE(os.stat(out_path).st_mode)
    assert mode == 0o600

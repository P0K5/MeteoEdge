"""Tests for the pure (non-network) helpers in scripts/reflect_triage.py —
the cost-guard triage pass for the reflection loop (issue #747).

The NIM call itself is not exercised here (it requires a live API key and is
covered operationally); we test transcript rendering, chunking, and the
tolerant JSON parsing of model output.
"""
import json

from scripts.reflect_triage import (
    extract_turn_text,
    chunk_transcript,
    parse_findings,
    MAX_CHUNK_CHARS,
)


def test_extract_turn_text_renders_text_and_tool_blocks():
    record = {
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "doing the thing"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            {"type": "tool_result", "content": "file1\nfile2"},
        ]},
    }
    out = extract_turn_text(record)
    assert "doing the thing" in out
    assert "[tool_use Bash]" in out
    assert "[tool_result]" in out


def test_extract_turn_text_handles_plain_string_content():
    record = {"role": "user", "message": {"content": "just a string"}}
    assert "just a string" in extract_turn_text(record)


def test_chunk_transcript_splits_on_size(tmp_path):
    f = tmp_path / "big.jsonl"
    # Each turn ~2000 chars (capped in extract); write enough to exceed one chunk.
    big_text = "x" * 5000
    n = (MAX_CHUNK_CHARS // 2000) + 5
    lines = [json.dumps({"type": "assistant",
                         "message": {"content": [{"type": "text", "text": big_text}]}})
             for _ in range(n)]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")

    chunks = list(chunk_transcript(f))
    assert len(chunks) >= 2
    # Each chunk carries a start-turn index and stays within the size bound
    # (allowing one turn of overshoot since a turn is appended before the check).
    for start, text in chunks:
        assert isinstance(start, int)
        assert len(text) <= MAX_CHUNK_CHARS + 2100


def test_chunk_transcript_skips_blank_and_malformed(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(
        json.dumps({"type": "user", "message": {"content": "hello"}}) + "\n"
        + "\n"
        + "not json\n",
        encoding="utf-8",
    )
    chunks = list(chunk_transcript(f))
    assert len(chunks) == 1
    _, text = chunks[0]
    assert "hello" in text
    assert "not json" not in text


def test_parse_findings_plain_json():
    raw = '{"findings": [{"category": "requery", "turns": [4], "confidence": "high"}]}'
    findings = parse_findings(raw)
    assert len(findings) == 1
    assert findings[0]["category"] == "requery"


def test_parse_findings_strips_markdown_fence():
    raw = '```json\n{"findings": [{"category": "verbosity"}]}\n```'
    findings = parse_findings(raw)
    assert findings == [{"category": "verbosity"}]


def test_parse_findings_returns_empty_on_garbage():
    assert parse_findings("the model refused to answer") == []

"""Tests for scripts/reflection_metrics.py — the tokens-per-completed-issue
convergence metric for the reflection loop (issue #747).
"""
import json

from scripts.reflection_metrics import (
    walk, analyze_file, classify_backend, billed_for_usage, CLOSING_RE,
)


def test_closing_re_matches_all_keywords_case_insensitive():
    found = set()
    for kw in ("Closes #1", "fixes #22", "RESOLVES #333"):
        found |= {int(m.group(1)) for m in CLOSING_RE.finditer(kw)}
    assert found == {1, 22, 333}


def test_closing_re_ignores_non_closing_mentions():
    assert CLOSING_RE.search("see #12 for context") is None


def test_walk_yields_all_nested_dicts():
    data = {"a": 1, "b": {"c": 2}, "d": [{"e": 3}, 4]}
    dicts = list(walk(data))
    # top-level dict, {"c":2}, and {"e":3}
    assert {"c": 2} in dicts
    assert {"e": 3} in dicts
    assert len(dicts) == 3


def test_classify_backend_claude_prefix_is_anthropic():
    assert classify_backend("claude-opus-5") == "anthropic"
    assert classify_backend("claude-sonnet-5") == "anthropic"


def test_classify_backend_other_model_is_other():
    assert classify_backend("deepseek-v4-pro") == "other"
    assert classify_backend("gpt-4") == "other"


def test_classify_backend_unknown_model_defaults_to_anthropic():
    # Preserves pre-#931 behavior for transcripts/fixtures with no model
    # field, and is the safe default (doesn't double-count anything).
    assert classify_backend(None) == "anthropic"
    assert classify_backend("") == "anthropic"


def test_billed_for_usage_anthropic_adds_cache_creation():
    usage = {"input_tokens": 2, "output_tokens": 224,
             "cache_creation_input_tokens": 49490, "cache_read_input_tokens": 0}
    assert billed_for_usage(usage, "anthropic") == 2 + 224 + 49490


def test_billed_for_usage_other_drops_cache_creation():
    # Real deepseek-v4-pro usage shape (issue #931): input_tokens already
    # equals cache_creation + cache_read, so it must not be added again.
    usage = {"input_tokens": 44221, "output_tokens": 540,
              "cache_creation_input_tokens": 829,
              "cache_read_input_tokens": 43392}
    assert billed_for_usage(usage, "other") == 44221 + 540


def test_analyze_file_sums_usage_and_collects_issues(tmp_path):
    f = tmp_path / "s.jsonl"
    lines = [
        {"type": "user",
         "message": {"role": "user", "content": "start"},
         "usage": {"input_tokens": 100, "output_tokens": 0}},
        {"type": "assistant",
         "message": {"role": "assistant",
                     "content": "PR body. Closes #742 and Fixes #743."},
         "usage": {"input_tokens": 2000, "output_tokens": 150,
                   "cache_read_input_tokens": 5000,
                   "cache_creation_input_tokens": 300}},
    ]
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    totals, issues, turns, billed, backends = analyze_file(f)

    assert turns == 2
    assert totals["input_tokens"] == 2100
    assert totals["output_tokens"] == 150
    assert totals["cache_read_input_tokens"] == 5000
    assert totals["cache_creation_input_tokens"] == 300
    assert issues == {742, 743}
    # No model field on either turn -> defaults to the Anthropic formula,
    # matching this script's pre-#931 behavior for both turns.
    assert billed == 100 + (2000 + 150 + 300)
    assert backends == {"anthropic": 2}


def test_analyze_file_skips_blank_and_malformed_lines(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(
        json.dumps({"usage": {"input_tokens": 5, "output_tokens": 1},
                    "message": {"content": "Closes #9"}}) + "\n"
        + "\n"
        + "not-json line that fails to parse\n",
        encoding="utf-8",
    )
    totals, issues, turns, billed, backends = analyze_file(f)
    # Blank and unparseable lines are skipped entirely (metrics needs the
    # structured usage fields), so only the one valid JSON turn counts.
    assert turns == 1
    assert totals["input_tokens"] == 5
    assert issues == {9}


def test_analyze_file_does_not_double_count_deepseek_cache_creation(tmp_path):
    """Regression test for issue #931.

    Real deepseek-v4-pro transcripts nest `model` alongside `usage` inside
    a `message` dict, and report input_tokens == cache_creation + cache_read
    (unlike Anthropic, where input_tokens excludes both). Before the fix,
    billed_tokens added cache_creation_input_tokens on top of input_tokens,
    double-counting the cached portion.
    """
    f = tmp_path / "s.jsonl"
    record = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "deepseek-v4-pro",
            "content": "Closes #931",
            "usage": {
                "input_tokens": 44221,
                "output_tokens": 540,
                "cache_creation_input_tokens": 829,
                "cache_read_input_tokens": 43392,
            },
        },
    }
    f.write_text(json.dumps(record) + "\n", encoding="utf-8")

    totals, issues, turns, billed, backends = analyze_file(f)

    assert turns == 1
    assert issues == {931}
    # Raw sums are unaffected -- only the billed formula changes.
    assert totals["input_tokens"] == 44221
    assert totals["cache_creation_input_tokens"] == 829
    # Correct: input_tokens (44221) already includes cache_creation (829)
    # and cache_read (43392), so billed is input + output only.
    assert billed == 44221 + 540
    # The old (buggy) formula would have given 44221 + 540 + 829 = 45590.
    assert billed != 44221 + 540 + 829
    assert backends == {"other": 1}


def test_analyze_file_anthropic_turn_still_adds_cache_creation(tmp_path):
    """Anthropic-backend turns are unaffected by the #931 fix."""
    f = tmp_path / "s.jsonl"
    record = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-opus-5",
            "content": "Closes #1",
            "usage": {
                "input_tokens": 2,
                "output_tokens": 224,
                "cache_creation_input_tokens": 49490,
                "cache_read_input_tokens": 0,
            },
        },
    }
    f.write_text(json.dumps(record) + "\n", encoding="utf-8")

    totals, issues, turns, billed, backends = analyze_file(f)

    assert billed == 2 + 224 + 49490
    assert backends == {"anthropic": 1}

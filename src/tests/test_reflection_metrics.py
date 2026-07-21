"""Tests for scripts/reflection_metrics.py — the tokens-per-completed-issue
convergence metric for the reflection loop (issue #747).
"""
import json

from scripts.reflection_metrics import walk, analyze_file, CLOSING_RE


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

    totals, issues, turns = analyze_file(f)

    assert turns == 2
    assert totals["input_tokens"] == 2100
    assert totals["output_tokens"] == 150
    assert totals["cache_read_input_tokens"] == 5000
    assert totals["cache_creation_input_tokens"] == 300
    assert issues == {742, 743}


def test_analyze_file_skips_blank_and_malformed_lines(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(
        json.dumps({"usage": {"input_tokens": 5, "output_tokens": 1},
                    "message": {"content": "Closes #9"}}) + "\n"
        + "\n"
        + "not-json line that fails to parse\n",
        encoding="utf-8",
    )
    totals, issues, turns = analyze_file(f)
    # Blank and unparseable lines are skipped entirely (metrics needs the
    # structured usage fields), so only the one valid JSON turn counts.
    assert turns == 1
    assert totals["input_tokens"] == 5
    assert issues == {9}

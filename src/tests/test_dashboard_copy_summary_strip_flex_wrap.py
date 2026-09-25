"""Unit tests for the dashboard's copy-summary-strip CSS (issue #1203).

Tests verify:
1. .copy-summary-strip CSS block contains flex-wrap:wrap to allow wrapping
   on mobile (primary mobile layout fix).
2. .copy-summary-strip CSS block contains row-gap:var(--space-2) to add
   vertical gap between wrapped rows (secondary spacing fix).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = REPO_ROOT / "src" / "dashboard" / "static" / "index.html"


def test_copy_summary_strip_has_flex_wrap():
    """Verify .copy-summary-strip CSS block contains flex-wrap:wrap.

    This is the mobile layout fix: when space is constrained, summary items
    wrap to the next row instead of causing the page to pan horizontally.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Find the .copy-summary-strip CSS rule and verify it contains flex-wrap:wrap
    match = re.search(r'\.copy-summary-strip\{[^}]*\}', html)
    assert match, ".copy-summary-strip CSS rule not found"

    copy_summary_strip_css = match.group(0)
    assert 'flex-wrap:wrap' in copy_summary_strip_css, (
        ".copy-summary-strip CSS must contain 'flex-wrap:wrap' to allow "
        "flex items to wrap on mobile instead of causing horizontal page pan"
    )


def test_copy_summary_strip_has_row_gap():
    """Verify .copy-summary-strip CSS block contains row-gap:var(--space-2).

    This is the spacing fix: wrapped items (now on separate rows due to
    flex-wrap:wrap) are vertically separated by the standard row gap.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Find the .copy-summary-strip CSS rule
    match = re.search(r'\.copy-summary-strip\{[^}]*\}', html)
    assert match, ".copy-summary-strip CSS rule not found"

    copy_summary_strip_css = match.group(0)
    assert 'row-gap:var(--space-2)' in copy_summary_strip_css, (
        ".copy-summary-strip CSS must contain 'row-gap:var(--space-2)' to "
        "add vertical spacing between wrapped rows"
    )

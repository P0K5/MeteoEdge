"""CSS-contract tests for issue #1286: copy tables must not widen the page.

Real-browser measurements (Chromium, 1280 px / 375 px) are recorded in the PR;
these tests pin the rules that fix the overflow so they cannot silently regress.

Root cause: ``.sr-only`` (position:absolute) header labels inside a wide,
horizontally-scrolled table had no positioned ancestor, so they were laid out
against the page and stretched ``document.documentElement.scrollWidth``. The
topbar title block also could not shrink at 375 px.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = REPO_ROOT / "src" / "dashboard" / "static" / "index.html"


def _rule(html: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\{([^}]*)\}", html)
    assert match, f"{selector} CSS rule not found"
    return match.group(1)


def test_copy_table_wrap_scrolls_and_contains_absolute_descendants():
    html = INDEX_HTML.read_text(encoding="utf-8")
    body = _rule(html, ".copy-table-wrap")
    assert "overflow-x:auto" in body
    # Containing block for .sr-only / roster <thead> so they are clipped by the
    # wrap rather than widening the page.
    assert "position:relative" in body


def test_copy_address_cell_stays_the_feedback_overlay_anchor():
    """The #1280 under-cell overlay must stay anchored to its own cell."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "position:relative" in _rule(html, ".copy-address-cell")
    assert "position:absolute" in _rule(html, ".copy-feedback-label")
    assert ".copy-table-wrap:has(.copy-copy-btn){padding-bottom:var(--space-10);}" in html


def test_topbar_title_block_can_shrink():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert re.search(r"\.logo,\.logo>div\{[^}]*min-width:0", html)
    assert "text-overflow:ellipsis" in _rule(html, ".logo-text,.logo-sub")

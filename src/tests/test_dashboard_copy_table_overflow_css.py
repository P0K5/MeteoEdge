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


def test_wallets_candidates_table_has_fixed_stable_column_layout():
    """#1287: 11 columns keep the same x/width on every page.

    Chromium measurements (1280 px and 375 px, page 1 'New' vs page 2
    'Unstable'+'Partial history' with 7-digit/negative PnL) are in the PR: the
    columns are identical, the table is exactly the 1248 px wrap at 1280 (no
    inner scroll) and 1219 px inside a 343 px wrap at 375 (scrolls in the wrap,
    page scrollWidth == clientWidth).
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    table = _rule(html, "#copy-candidates-list .copy-table")
    assert "table-layout:fixed" in table
    min_width = int(re.search(r"min-width:(\d+)px", table).group(1))
    widths = [
        int(re.search(
            r"#copy-candidates-list \.copy-th:nth-child\(%d\)\{width:(\d+)px;\}" % i, html
        ).group(1))
        for i in range(1, 12)
    ]
    assert len(widths) == 11
    # Wrap is 1248 px at a 1280 px viewport; the columns must fit without scroll
    # yet the table must stay wider than a 375 px viewport so it scrolls there.
    assert sum(widths) <= 1248
    assert min_width <= 1248
    assert min_width >= 600
    assert min_width == sum(widths)
    assert widths[8] >= 115   # Stability: 'Unstable' badge
    assert widths[9] >= 145   # History: 'Partial history' badge
    mono = _rule(html, "#copy-candidates-list .copy-address-mono")
    assert "text-overflow:ellipsis" in mono and "overflow:hidden" in mono

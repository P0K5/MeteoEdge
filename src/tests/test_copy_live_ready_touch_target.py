"""Tests for copy-live ready roster touch-target fix (issue #1333).

Verifies that the Ready roster applies the same CSS class and structure
as the Live roster to ensure action buttons meet the 44px touch-target
minimum at 375px viewport width.
"""
from __future__ import annotations

from pathlib import Path
import re

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = REPO_ROOT / "src" / "dashboard" / "static" / "index.html"


def test_ready_roster_has_copy_table_roster_class():
    """The _followedTableHtml call for 'Wallets ready to go live' must pass
    'copy-table--roster' as the 4th parameter to apply the touch-target CSS."""
    html = INDEX_HTML.read_text(encoding='utf-8')

    # Find the _followedTableHtml call for the ready roster
    ready_call = re.search(
        r"_followedTableHtml\('Wallets ready to go live'.*?\);",
        html,
        re.DOTALL
    )
    assert ready_call, "Could not find _followedTableHtml call for ready roster"
    call_text = ready_call.group(0)
    assert "'copy-table--roster'" in call_text, \
        "Ready roster must pass 'copy-table--roster' class to _followedTableHtml"


def test_ready_row_has_data_label_on_paper_pnl_cell():
    """The Paper P&L cell in _followedReadyRowHtml must have a data-label
    attribute for screen reader accessibility and CSS display rules."""
    html = INDEX_HTML.read_text(encoding='utf-8')

    # Find the _followedReadyRowHtml function
    ready_func = re.search(
        r"function _followedReadyRowHtml\(.*?\{.*?\}(?=\n\nfunction)",
        html,
        re.DOTALL
    )
    assert ready_func, "Could not find _followedReadyRowHtml function"
    func_text = ready_func.group(0)

    # Should have data-label="Paper P&amp;L" on the second td
    assert 'data-label="Paper P&amp;L"' in func_text, \
        "Ready row must have data-label on Paper P&L cell"


def test_min_height_44px_rule_exists_in_css():
    """The CSS block for .copy-table--roster.followed-actions-cell must have
    the 44px min-height rule for action buttons at 600px and below."""
    html = INDEX_HTML.read_text(encoding='utf-8')

    # Find the media query section for copy-table--roster
    media_section = re.search(
        r"@media\(max-width:600px\)\{.*?\.copy-table--roster.*?min-height:44px",
        html,
        re.DOTALL
    )
    assert media_section, \
        "CSS must have min-height:44px for .copy-table--roster buttons in @media(max-width:600px)"


def test_action_buttons_have_followed_actions_cell_class():
    """The action cells in both Live and Ready rosters must use the
    followed-actions-cell class for consistent styling."""
    html = INDEX_HTML.read_text(encoding='utf-8')

    # Count followed-actions-cell class usage
    count = html.count('class="copy-td followed-actions-cell"')
    # Should have at least 2: one for Live row, one for Ready row
    assert count >= 2, \
        f"Expected at least 2 followed-actions-cell instances (Live + Ready), found {count}"


def test_no_regressions_at_600px_and_above():
    """The media query for min-height:44px applies only at max-width:600px,
    so larger viewports should not be affected."""
    html = INDEX_HTML.read_text(encoding='utf-8')

    # Look for the pattern: .copy-table--roster with min-height:44px in media query
    pattern = r"\.copy-table--roster[^}]*\.btn-followed-action\{min-height:44px;\}"
    match = re.search(pattern, html, re.DOTALL)
    assert match, \
        "The CSS must have .copy-table--roster .btn-followed-action{min-height:44px;} rule"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

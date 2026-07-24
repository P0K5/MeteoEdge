"""Tests for the Edge tab's decision table overflow wrapper (issue #809).

Validates that:
1. The .edge-table-wrap CSS class with overflow-x:auto exists
2. The edge-decision-table is wrapped in a div with class="edge-table-wrap"
3. The wrapper structure mirrors the .promo-table-wrap pattern
"""
from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = REPO_ROOT / "src" / "dashboard" / "static" / "index.html"


def test_edge_table_wrap_css_rule_exists():
    """The .edge-table-wrap CSS rule with overflow-x:auto must exist in the style block."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert re.search(
        r"\.edge-table-wrap\s*\{\s*overflow-x\s*:\s*auto\s*;\s*\}",
        html,
    ), "CSS rule .edge-table-wrap{overflow-x:auto;} not found in index.html"


def test_edge_decision_table_wrapped_in_overflow_div():
    """The edge-decision-table must be wrapped in a div with class="edge-table-wrap"."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    # Look for the wrapper div followed by the table
    pattern = r'<div\s+class="edge-table-wrap">\s*<table\s+class="edge-decision-table"'
    assert re.search(
        pattern,
        html,
    ), "edge-decision-table is not wrapped in <div class='edge-table-wrap'>"


def test_edge_table_wrap_structure_complete():
    """Verify the complete structure: edge-table-section > edge-table-wrap > table."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    # More comprehensive pattern: verify the entire structure from section to closing div
    pattern = (
        r'<div\s+class="edge-table-section"\s+id="edge-table-section">'
        r'.*?'
        r'<div\s+class="edge-table-wrap">'
        r'\s*<table\s+class="edge-decision-table".*?</table>'
        r'\s*</div>'
        r'\s*</div>'
    )
    assert re.search(
        pattern,
        html,
        re.DOTALL,
    ), "edge-table-section structure is incomplete or malformed"


def test_edge_table_wrap_mirrors_promo_table_pattern():
    """The .edge-table-wrap pattern should mirror the existing .promo-table-wrap."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Both should have the same overflow-x:auto rule
    promo_css = re.search(
        r"\.promo-table-wrap\s*\{\s*overflow-x\s*:\s*auto\s*;\s*\}",
        html,
    )
    edge_css = re.search(
        r"\.edge-table-wrap\s*\{\s*overflow-x\s*:\s*auto\s*;\s*\}",
        html,
    )
    assert promo_css, "promo-table-wrap CSS not found"
    assert edge_css, "edge-table-wrap CSS not found"

    # Both should wrap their respective tables
    promo_pattern = r'<div\s+class="promo-table-wrap">\s*<table\s+class="promo-table"'
    edge_pattern = r'<div\s+class="edge-table-wrap">\s*<table\s+class="edge-decision-table"'
    assert re.search(promo_pattern, html), "promo-table not properly wrapped"
    assert re.search(edge_pattern, html), "edge-decision-table not properly wrapped"

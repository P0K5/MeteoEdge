"""Tests for the Copy-Trading dashboard Positions & P&L view's static
markup (epic F #1143, story F3 #1148).

The two distinct empty states called out in the acceptance criteria ("no
positions at all" vs "positions exist but none settled yet") are rendered
client-side in JS (renderCopyPositions()), so this file checks the static
scaffolding the JS depends on plus the JS logic's presence/shape directly
(mirrors test_dashboard_copy_trading_tab.py's approach of asserting on the
served HTML/JS text for this vanilla-JS, no-build-step dashboard).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


@pytest.fixture
def html_content():
    html_path = Path(__file__).resolve().parents[2] / "src" / "dashboard" / "static" / "index.html"
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()


def test_positions_section_exists_inside_copy_trading_content(html_content):
    """The new section must live inside #copy-trading-content, alongside
    (not replacing) the existing Candidates/Followed Wallets views."""
    content_start = html_content.index('id="copy-trading-content"')
    content_end = html_content.index("</section>", content_start)
    section = html_content[content_start:content_end]

    assert 'id="copy-candidates-list"' in section, "Candidates view missing from copy-trading-content"
    assert 'id="copy-followed-list"' in section, "Followed Wallets view missing from copy-trading-content"
    assert 'id="copy-positions-content"' in section, "Positions & P&L view not found inside copy-trading-content"

    # Order: Positions & P&L must come after the other two, not replace them.
    assert section.index('id="copy-candidates-list"') < section.index('id="copy-positions-content"')
    assert section.index('id="copy-followed-list"') < section.index('id="copy-positions-content"')


def test_positions_view_has_date_range_and_backtest_toggle_controls(html_content):
    assert 'id="copy-positions-range-select"' in html_content
    assert 'id="copy-backtest-toggle"' in html_content
    assert 'role="switch"' in html_content


def test_positions_fetch_wired_into_copy_trading_tab_activation(html_content):
    """fetchCopyTradingPositions must be called both on first tab
    activation and on the shared 5-minute poll interval, matching
    fetchCopyTradingCandidates/fetchFollowedWallets's existing wiring."""
    tab_block_match = re.search(
        r"if \(tab === 'copy-trading'\) \{(.*?)\n  \}\n\}",
        html_content,
        re.S,
    )
    assert tab_block_match, "copy-trading tab activation block not found"
    block = tab_block_match.group(1)
    assert "fetchCopyTradingCandidates();" in block
    assert "fetchFollowedWallets();" in block
    assert "fetchCopyTradingPositions();" in block


def test_renders_true_empty_state_distinct_from_unsettled_state(html_content):
    """Acceptance criteria: 'no positions at all' must be a different
    message than 'positions exist but none settled yet' -- assert the two
    distinct strings both exist in renderCopyPositions()'s logic and are
    not the same message."""
    assert "No copy-trading positions yet" in html_content
    assert "No settled positions yet" in html_content

    # The true-empty branch must gate on *both* open_positions and
    # per_wallet being empty (never render the chart-only message when
    # there is genuinely nothing at all).
    assert "!data.open_positions.length && !data.per_wallet.length" in html_content


def test_chart_never_renders_blank_on_no_settled_positions(html_content):
    """The chart-empty branch must explain *why* -- never an empty canvas
    with no context (acceptance criteria). The chart <canvas> markup is
    only emitted on the truthy branch of a ternary keyed on
    realized_pnl_history.length; the falsy branch renders the explanatory
    .copy-positions-chart-empty block instead of an empty canvas."""
    render_fn_start = html_content.index("function renderCopyPositions(data)")
    render_fn_end = html_content.index("\nfunction ", render_fn_start + 10)
    fn_body = html_content[render_fn_start:render_fn_end]

    chart_html_match = re.search(
        r"const chartHtml = (data\.realized_pnl_history\.length)\s*\n\s*\?\s*(`[^`]*`)\s*\n\s*:\s*(`[^`]*`)",
        fn_body,
    )
    assert chart_html_match, "chartHtml ternary not found in renderCopyPositions()"
    truthy_branch, falsy_branch = chart_html_match.group(2), chart_html_match.group(3)

    assert "<canvas" in truthy_branch
    assert "<canvas" not in falsy_branch
    assert "copy-positions-chart-empty" in falsy_branch
    assert "settle" in falsy_branch.lower()  # explanatory text, not a blank div


def test_isolation_from_weather_portfolio_tab(html_content):
    """Issue #1100 isolation requirement: the Positions & P&L view must
    never write into the weather Portfolio tab's own elements/functions."""
    positions_fn_start = html_content.index("function renderCopyPositions(data)")
    positions_fn_end = html_content.index("\nfunction _copyPositionsOnRangeChange")
    fn_body = html_content[positions_fn_start:positions_fn_end]

    for weather_id in ("val-portfolio", "val-cash", "val-invested", "open-positions"):
        assert weather_id not in fn_body, (
            f"Positions & P&L view must not touch the weather tab's #{weather_id}"
        )

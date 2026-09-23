"""Tests for the Copy-Trading dashboard Positions & P&L view's static
markup (epic F #1143, story F3 #1148; live/paper twin-panel split issue
#1186, Epic J).

The two distinct empty states called out in the acceptance criteria ("no
positions at all" vs "positions exist but none settled yet") are rendered
client-side in JS (renderCopyPositions()), so this file checks the static
scaffolding the JS depends on plus the JS logic's presence/shape directly
(mirrors test_dashboard_copy_trading_tab.py's approach of asserting on the
served HTML/JS text for this vanilla-JS, no-build-step dashboard).

The Live column's own state-transition logic (off/on-empty/populated/
error) is covered by test_copy_trading_positions_live_paper_js_logic.py
(same Node-execution technique) -- this file only checks the static twin-
panel scaffolding (headings, banners, DOM ordering) those states render
into.
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


# ---------------------------------------------------------------------------
# Live/paper twin-panel split (issue #1186, Epic J)
# ---------------------------------------------------------------------------

def test_twin_panel_structure_live_left_paper_right(html_content):
    """Live must be first in DOM/reading order, Paper second -- the fixed
    order repeated across this whole epic for a stable mental model
    (design spec's accessibility notes)."""
    twin_start = html_content.index('class="copy-positions-twin"')
    twin_end = html_content.index("<!-- ── ACTIVITY FEED VIEW")
    twin_section = html_content[twin_start:twin_end]

    assert 'id="copy-positions-live-content"' in twin_section
    assert 'id="copy-positions-content"' in twin_section
    assert twin_section.index('id="copy-positions-live-content"') < twin_section.index('id="copy-positions-content"'), (
        "Live column must come before Paper column in DOM order"
    )


def test_column_headings_use_mode_badge_styling(html_content):
    """Each column has its own <h3>LIVE</h3> / <h3>PAPER</h3> heading using
    #1185's .mode-badge styling (acceptance criteria), with a full-sentence
    aria-label (never color/text-only)."""
    twin_start = html_content.index('class="copy-positions-twin"')
    twin_end = html_content.index("<!-- ── ACTIVITY FEED VIEW")
    twin_section = html_content[twin_start:twin_end]

    assert re.search(r'<span class="mode-badge mode-badge-live"[^>]*>LIVE</span>', twin_section)
    assert re.search(r'<span class="mode-badge mode-badge-paper"[^>]*>PAPER</span>', twin_section)
    assert 'aria-label="Live positions and P&amp;L"' in twin_section
    assert 'aria-label="Paper positions and P&amp;L"' in twin_section


def test_twin_panel_responsive_stacking_keeps_live_first(html_content):
    """Narrow viewports stack the columns vertically (single grid column)
    -- DOM order alone (Live first) then determines the stacking order,
    with no viewport-specific reordering that would put Paper first."""
    assert ".copy-positions-twin{display:grid;grid-template-columns:1fr 1fr" in html_content
    assert "@media(max-width:900px){.copy-positions-twin{grid-template-columns:1fr;}}" in html_content, (
        "twin panel must collapse to a single stacked column on narrow viewports"
    )


def test_each_column_has_its_own_stale_data_banner(html_content):
    """Design spec: 'each column gets its own stale-data banner' -- never
    the single shared #copy-trading-error-banner used by Candidates/
    Followed Wallets for this view."""
    assert 'id="copy-positions-live-error-banner"' in html_content
    assert 'id="copy-positions-error-banner"' in html_content
    assert 'id="copy-positions-live-error-banner"' != 'id="copy-trading-error-banner"'


def test_summary_strip_has_two_independently_labeled_pills(html_content):
    """Never a single unqualified 'aggregate P&L' anywhere -- two
    independent, explicitly-labeled pills (acceptance criteria)."""
    assert 'id="copy-positions-live-total-pill"' in html_content
    assert 'id="copy-positions-total-pill"' in html_content
    assert "Live aggregate P&amp;L" in html_content
    assert "Paper aggregate P&amp;L" in html_content


def test_backtest_toggle_stays_paper_only(html_content):
    """The backtest-comparison toggle (Epic F phase-7 go/no-go feature)
    stays in the Paper column only -- it has no live counterpart yet."""
    paper_col_start = html_content.index('class="copy-positions-col copy-positions-col-paper"')
    live_col_start = html_content.index('class="copy-positions-col copy-positions-col-live"')
    paper_col_end = html_content.index("<!-- ── ACTIVITY FEED VIEW")
    live_col_section = html_content[live_col_start:paper_col_start]
    paper_col_section = html_content[paper_col_start:paper_col_end]

    assert 'id="copy-backtest-toggle"' not in live_col_section
    # The toggle control itself lives in the shared header (not inside
    # either column's content div), so also assert it never appears
    # inside the Live column's own render function.
    live_fn_start = html_content.index("function renderCopyLivePositions(data, liveTradingEnabled)")
    live_fn_end = html_content.index("\n// Each column gets its own stale-data banner")
    live_fn_body = html_content[live_fn_start:live_fn_end]
    assert "copy-backtest-toggle" not in live_fn_body
    assert "_copyBacktestOn" not in live_fn_body
    assert paper_col_section  # sanity: paper column section is non-empty


def test_live_off_state_has_no_numeric_figures(html_content):
    """Acceptance criteria: the off-state (live off, no historical data)
    must never show a numeric figure, not even $0.00."""
    live_fn_start = html_content.index("function renderCopyLivePositions(data, liveTradingEnabled)")
    live_fn_end = html_content.index("\n// Each column gets its own stale-data banner")
    fn_body = html_content[live_fn_start:live_fn_end]

    off_state_match = re.search(
        r"if \(!liveTradingEnabled && !hasAnyLiveData\) \{\s*wrap\.innerHTML = `([^`]*)`",
        fn_body,
    )
    assert off_state_match, "off-state branch not found in renderCopyLivePositions()"
    off_html = off_state_match.group(1)
    assert "Live trading is off" in off_html
    assert "$" not in off_html


def test_on_but_empty_state_distinct_from_off_state(html_content):
    """'No live positions yet.' (on, empty) must be a different message
    from 'Live trading is off' (off, no data) -- both literal strings must
    exist and be distinct branches."""
    assert "No live positions yet." in html_content
    assert "Live trading is off" in html_content

    live_fn_start = html_content.index("function renderCopyLivePositions(data, liveTradingEnabled)")
    live_fn_end = html_content.index("\n// Each column gets its own stale-data banner")
    fn_body = html_content[live_fn_start:live_fn_end]
    assert "!liveTradingEnabled && !hasAnyLiveData" in fn_body
    assert "liveTradingEnabled && !hasAnyLiveData" in fn_body


def test_historical_live_data_shown_even_when_currently_off(html_content):
    """Acceptance criteria: once any copy_live_positions row exists ever,
    show the real historical live aggregate even while currently off, with
    an off-state banner layered on top rather than hiding the data."""
    live_fn_start = html_content.index("function renderCopyLivePositions(data, liveTradingEnabled)")
    live_fn_end = html_content.index("\n// Each column gets its own stale-data banner")
    fn_body = html_content[live_fn_start:live_fn_end]

    assert "hasAnyLiveData" in fn_body
    assert "offBannerHtml" in fn_body
    assert "warn-banner" in fn_body


def test_live_open_positions_table_status_column_handles_null_fill_price(html_content):
    """A pending REAL order has no fill_price yet -- the table must render
    a placeholder, never a stringified 'null'."""
    row_fn_start = html_content.index("function _copyLiveOpenPositionRowHtml(p)")
    row_fn_end = html_content.index("\nfunction ", row_fn_start + 10)
    fn_body = html_content[row_fn_start:row_fn_end]
    assert "p.fill_price != null" in fn_body
    assert "p.status" in fn_body


def test_live_per_wallet_table_never_renders_backtest_columns(html_content):
    """Live per-wallet breakdown has no backtest-comparison columns at
    all, regardless of the (paper-only) backtest toggle state."""
    table_fn_start = html_content.index("function _copyLivePerWalletTableHtml(rows)")
    table_fn_end = html_content.index("\nfunction _copyLivePerWalletRowHtml")
    fn_body = html_content[table_fn_start:table_fn_end]
    assert "Projected" not in fn_body
    assert "Divergence" not in fn_body
    assert "_copyBacktestOn" not in fn_body

    row_fn_start = html_content.index("function _copyLivePerWalletRowHtml(w)")
    row_fn_end = html_content.index("\nfunction ", row_fn_start + 10)
    row_fn_body = html_content[row_fn_start:row_fn_end]
    assert "projected_flat_dollar_pnl" not in row_fn_body
    assert "divergence" not in row_fn_body.lower()


def test_live_positions_use_distinct_dom_id_prefix_from_paper(html_content):
    """Live and paper position ids come from two separate DB tables and
    can collide numerically -- the Live column's rows must be namespaced
    ('live-pos-') so they never stomp on the Paper column's own
    'pos-'-prefixed row/detail-panel elements, and vice versa."""
    assert "'live-pos-' + p.id" in html_content


def test_live_column_re_renders_off_the_fast_polled_posture_flag(html_content):
    """The Live column's off/on-empty distinction must ride the shared
    30s-polled posture flag (fetchCopyTradingModePosture(), #1185) rather
    than this endpoint's own 5-minute poll -- same precedent #1185 itself
    had to fix (a stale live-mode read persisting for up to 5 minutes)."""
    posture_fn_start = html_content.index("function renderCopyTradingModePosture(liveEnabled)")
    posture_fn_end = html_content.index("\nasync function fetchCopyTradingModePosture")
    fn_body = html_content[posture_fn_start:posture_fn_end]
    assert "renderCopyLivePositions" in fn_body, (
        "posture updates must re-render the already-loaded Live column immediately"
    )


def test_live_error_isolated_from_paper_error_banner(html_content):
    """fetchCopyTradingPositions must use two independent per-column
    banners for the shared HTTP-failure path, and the Live column's own
    renderCopyLivePositions must react to live_error without touching the
    paper banner/content at all."""
    fetch_fn_start = html_content.index("async function fetchCopyTradingPositions()")
    fetch_fn_end = html_content.index("\n/* ─────────────────────────────────────────\n   COPY-TRADING — ACTIVITY FEED VIEW")
    fn_body = html_content[fetch_fn_start:fetch_fn_end]
    assert "copy-positions-live-error-banner" in fn_body
    assert "copy-positions-error-banner" in fn_body

    live_fn_start = html_content.index("function renderCopyLivePositions(data, liveTradingEnabled)")
    live_fn_end = html_content.index("\n// Each column gets its own stale-data banner")
    live_fn_body = html_content[live_fn_start:live_fn_end]
    # "copy-positions-content" (paper's exact id) is not a substring of
    # "copy-positions-live-content" (live's id), so this correctly detects
    # any stray reference to the paper column's own content div.
    assert "copy-positions-content" not in live_fn_body, (
        "renderCopyLivePositions must never write into the paper column's #copy-positions-content"
    )

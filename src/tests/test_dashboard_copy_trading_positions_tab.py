"""Tests for the Copy-Trading dashboard Positions & P&L view's static
markup (epic F #1143, story F3 #1148; live/paper twin-panel split issue
#1186, Epic J; each mode now owns its own tab, issue #1275).

The two distinct empty states called out in the acceptance criteria ("no
positions at all" vs "positions exist but none settled yet") are rendered
client-side in JS (renderCopyPositions()), so this file checks the static
scaffolding the JS depends on plus the JS logic's presence/shape directly
(mirrors test_dashboard_copy_trading_tab.py's approach of asserting on the
served HTML/JS text for this vanilla-JS, no-build-step dashboard).

The Live column's own state-transition logic (off/on-empty/populated/
error) is covered by test_copy_trading_positions_live_paper_js_logic.py
(same Node-execution technique) -- this file only checks the static
scaffolding (headings, banners, tab placement) those states render into.
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


def _tab_section(html: str, tab: str) -> str:
    start = html.index(f'<section id="tab-{tab}"')
    end = html.index("</section>", start)
    return html[start:end]


def _live_open_renderer(html_content: str) -> str:
    """Body of _copyLiveRenderOpen() (the Live Open Positions section's
    off/empty/populated state logic, issue #1278)."""
    start = html_content.index("function _copyLiveRenderOpen(data, liveTradingEnabled, hasHistory)")
    end = html_content.index("\nfunction _copyLiveRenderPnl", start)
    return html_content[start:end]


def test_positions_sections_live_in_the_paper_and_live_tabs(html_content):
    """Paper positions live ONLY in the Paper tab, live positions ONLY in
    the Live tab (issue #1275: the tab is the hard separator)."""
    paper = _tab_section(html_content, "copy-paper")
    live = _tab_section(html_content, "copy-live")
    wallets = _tab_section(html_content, "copy-wallets")

    assert 'id="copy-positions-content"' in paper
    assert 'id="copy-positions-live-content"' not in paper
    assert 'id="copy-positions-live-content"' in live
    assert 'id="copy-positions-content"' not in live
    assert 'id="copy-positions-content"' not in wallets
    assert 'id="copy-positions-live-content"' not in wallets
    assert 'id="copy-candidates-list"' in wallets
    # Paper tab: roster comes before the positions view.
    assert paper.index('id="copy-followed-list"') < paper.index('id="copy-positions-content"')


def test_positions_fetch_wired_per_tab(html_content):
    """Each tab polls only its own mode's positions through its job table
    (the shared cache makes the single /positions request cheap)."""
    jobs = re.search(r"const COPY_TAB_JOBS = \{(.*?)\n\};", html_content, re.S).group(1)
    wallets_jobs, rest = jobs.split("'copy-paper': [")
    paper_jobs, live_jobs = rest.split("'copy-live': [")
    assert "fetchCopyTradingCandidates()" in wallets_jobs
    assert "fetchCopyTradingPositions" not in wallets_jobs
    assert "fetchFollowedWallets('paper')" in paper_jobs
    assert "fetchCopyTradingPositions('paper')" in paper_jobs
    assert "fetchCopyTradingPositions('live')" not in paper_jobs
    assert "fetchFollowedWallets('live')" in live_jobs
    assert "fetchCopyTradingPositions('live')" in live_jobs
    assert "fetchCopyTradingPositions('paper')" not in live_jobs


def test_positions_view_has_date_range_and_backtest_toggle_controls(html_content):
    assert 'id="copy-positions-range-select"' in html_content
    assert 'id="copy-backtest-toggle"' in html_content
    assert 'role="switch"' in html_content


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

    # Issue #1277: the chart slot is written through stable slots; the canvas
    # markup is only emitted when settled history exists, the explanatory
    # block otherwise (behaviour is covered in
    # test_dashboard_copy_paper_tab_behavior.py).
    branch = re.search(
        r"if \(data\.realized_pnl_history\.length\) \{(.*?)\n  \} else \{(.*?)\n  \}\n",
        fn_body,
        re.S,
    )
    assert branch, "chart slot if/else on realized_pnl_history.length not found in renderCopyPositions()"
    truthy_branch, falsy_branch = branch.group(1), branch.group(2)

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

def test_each_mode_tab_has_its_mode_badge_heading(html_content):
    """Each tab's Positions & P&L heading carries a mode badge using
    #1185's .mode-badge styling, with a full-sentence aria-label (never
    color/text-only)."""
    paper = _tab_section(html_content, "copy-paper")
    live = _tab_section(html_content, "copy-live")

    assert re.search(r'<span class="mode-badge mode-badge-paper"[^>]*>PAPER</span>', paper)
    assert 'aria-label="Paper positions and P&amp;L"' in paper
    assert re.search(r'<span class="mode-badge mode-badge-live"[^>]*>LIVE</span>', live)
    # (issue #1278: the live block is split into Open Positions / Recent
    # Closed / Live P&L sections, each with its own LIVE badge)
    for label in ("Live open positions", "Live closed positions", "Live realized P&amp;L"):
        assert f'aria-label="{label}"' in live
    # Never the other mode's badge as a heading on this tab.
    assert 'aria-label="Live positions and P&amp;L"' not in paper
    assert 'aria-label="Paper positions and P&amp;L"' not in live


def test_twin_panel_layout_is_retired(html_content):
    """The tab itself is the separator now: no side-by-side twin markup/CSS."""
    assert "copy-positions-twin" not in html_content
    assert "copy-positions-col" not in html_content


def test_each_mode_has_its_own_stale_data_banner(html_content):
    """Each mode gets its own stale-data banner, inside its own tab --
    never a banner shared across modes."""
    assert 'id="copy-positions-live-error-banner"' in _tab_section(html_content, "copy-live")
    assert 'id="copy-positions-error-banner"' in _tab_section(html_content, "copy-paper")
    assert 'id="copy-positions-live-error-banner"' not in _tab_section(html_content, "copy-paper")
    assert 'id="copy-positions-error-banner"' not in _tab_section(html_content, "copy-live")


def test_each_tab_has_only_its_own_labeled_aggregate_pill(html_content):
    """Never a single unqualified 'aggregate P&L', and (issue #1275) never
    the other mode's aggregate on a tab."""
    paper = _tab_section(html_content, "copy-paper")
    live = _tab_section(html_content, "copy-live")
    assert 'id="copy-positions-total-pill"' in paper
    assert "Paper aggregate P&amp;L" in paper
    assert "Live aggregate P&amp;L" not in paper
    # Live: the figure lives in the KPI card (issue #1278), qualified "Live".
    assert 'id="copy-live-kpis"' in live
    assert '<div class="wc-label">Live P&amp;L</div>' in live
    assert "aggregate P&amp;L" not in live   # never an unqualified/paper aggregate
    assert "Paper aggregate P&amp;L" not in live


def test_backtest_toggle_stays_paper_only(html_content):
    """The backtest-comparison toggle (Epic F phase-7 go/no-go feature)
    stays on the Paper tab only -- it has no live counterpart yet."""
    assert 'id="copy-backtest-toggle"' in _tab_section(html_content, "copy-paper")
    assert 'id="copy-backtest-toggle"' not in _tab_section(html_content, "copy-live")
    live_fn_start = html_content.index("function renderCopyLivePositions(data, liveTradingEnabled)")
    live_fn_end = html_content.index("\n// scope: 'paper' | 'live'. Both tabs read the SAME")
    live_fn_body = html_content[live_fn_start:live_fn_end]
    assert "copy-backtest-toggle" not in live_fn_body
    assert "_copyBacktestOn" not in live_fn_body


def test_live_off_state_has_no_numeric_figures(html_content):
    """Acceptance criteria: the off-state (live off, no historical data)
    must never show a numeric figure, not even $0.00."""
    fn_body = _live_open_renderer(html_content)

    off_state_match = re.search(
        r"if \(offNoHistory\) \{\s*_copySetHtml\(wrap, `([^`]*)`",
        fn_body,
    )
    assert off_state_match, "off-state branch not found in _copyLiveRenderOpen()"
    off_html = off_state_match.group(1)
    assert "Live trading is off" in off_html
    assert "$" not in off_html


def test_on_but_empty_state_distinct_from_off_state(html_content):
    """'No live positions yet.' (on, empty) must be a different message
    from 'Live trading is off' (off, no data) -- both literal strings must
    exist and be distinct branches."""
    assert "No live positions yet." in html_content
    assert "Live trading is off" in html_content

    fn_body = _live_open_renderer(html_content)
    assert "const offNoHistory = !liveTradingEnabled && !hasHistory" in fn_body
    assert "liveTradingEnabled && !hasHistory" in fn_body


def test_historical_live_data_shown_even_when_currently_off(html_content):
    """Acceptance criteria: once any copy_live_positions row exists ever,
    show the real historical live aggregate even while currently off, with
    an off-state banner layered on top rather than hiding the data."""
    fn_body = _live_open_renderer(html_content)

    assert "hasHistory" in fn_body
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
    fetch_fn_start = html_content.index("async function fetchCopyTradingPositions(scope = 'paper')")
    fetch_fn_end = html_content.index("\n/* ─────────────────────────────────────────\n   COPY-TRADING — ACTIVITY FEED VIEW")
    fn_body = html_content[fetch_fn_start:fetch_fn_end]
    assert "copy-positions-live-error-banner" in fn_body
    assert "copy-positions-error-banner" in fn_body

    live_fn_start = html_content.index("function renderCopyLivePositions(data, liveTradingEnabled)")
    live_fn_end = html_content.index("\n// scope: 'paper' | 'live'. Both tabs read the SAME")
    live_fn_body = html_content[live_fn_start:live_fn_end]
    # "copy-positions-content" (paper's exact id) is not a substring of
    # "copy-positions-live-content" (live's id), so this correctly detects
    # any stray reference to the paper column's own content div.
    assert "copy-positions-content" not in live_fn_body, (
        "renderCopyLivePositions must never write into the paper column's #copy-positions-content"
    )

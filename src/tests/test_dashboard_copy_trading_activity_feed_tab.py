"""Tests for the Copy-Trading dashboard Activity Feed view's static
markup (epic F #1143, story F4 #1149; split per mode into the Paper and
Live tabs, issue #1275).

Mirrors test_dashboard_copy_trading_positions_tab.py's approach of
asserting on the served HTML/JS text for this vanilla-JS, no-build-step
dashboard -- the JS-logic behaviors themselves (stale-feed indicator,
filters, click-through) are covered by
test_copy_trading_activity_feed_js_logic.py, which executes the real
shipped script under Node.
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


def _select_options(section: str, select_id: str) -> str:
    start = section.index(f'id="{select_id}"')
    return section[start:section.index("</select>", start)]


def test_activity_feed_sections_live_in_the_paper_and_live_tabs(html_content):
    """One feed per mode, each inside its own tab, after Positions & P&L."""
    paper = _tab_section(html_content, "copy-paper")
    live = _tab_section(html_content, "copy-live")
    wallets = _tab_section(html_content, "copy-wallets")

    assert 'id="copy-activity-list"' in paper
    assert paper.index('id="copy-positions-content"') < paper.index('id="copy-activity-list"')
    assert 'id="copy-live-activity-list"' in live
    assert live.index('id="copy-positions-live-content"') < live.index('id="copy-live-activity-list"')
    assert "activity-list" not in wallets
    assert 'id="copy-live-activity-list"' not in paper
    assert 'id="copy-activity-list"' not in live


def test_activity_feeds_have_their_own_stale_banners(html_content):
    """Design decision (no toast/modal component): the stale-feed indicator
    reuses the existing .error-banner/.visible inline pattern, with a
    dedicated element per feed -- never a banner shared with an unrelated
    fetch (which could race-clear it)."""
    paper = _tab_section(html_content, "copy-paper")
    live = _tab_section(html_content, "copy-live")
    assert 'class="error-banner" id="copy-activity-stale-banner"' in paper
    assert 'id="copy-activity-stale-text"' in paper
    assert 'class="error-banner" id="copy-live-activity-stale-banner"' in live
    assert 'id="copy-live-activity-stale-text"' in live


def test_activity_feeds_have_wallet_and_event_type_filters_but_no_mode_select(html_content):
    """The Mode select is removed: the tab is the mode (issue #1275)."""
    assert "copy-activity-mode-select" not in html_content
    paper = _tab_section(html_content, "copy-paper")
    live = _tab_section(html_content, "copy-live")
    assert 'id="copy-activity-wallet-select"' in paper
    assert 'id="copy-live-activity-wallet-select"' in live

    paper_types = _select_options(paper, "copy-activity-type-select")
    for event_type in ("order_placed", "order_skipped", "wallet_paused"):
        assert f'value="{event_type}"' in paper_types
    assert "live_" not in paper_types, "Paper tab must not offer live event types"

    live_types = _select_options(live, "copy-live-activity-type-select")
    for event_type in (
        "live_order_pending", "live_order_filled", "live_order_partial",
        "live_order_rejected", "live_order_skipped",
        "live_circuit_breaker_tripped", "live_position_settled",
        "live_balance_mismatch",
    ):
        assert f'value="{event_type}"' in live_types, f"missing event-type filter option: {event_type}"
    for event_type in ("order_placed", "order_skipped", "wallet_paused"):
        assert f'value="{event_type}"' not in live_types, "Live tab must not offer paper event types"


def test_activity_feed_fetch_wired_per_tab_with_mode_cadences(html_content):
    """Paper polls its feed every 5 min (matching the Promotion tab's own
    cadence, issue #1149's explicit decision), Live every 30 s (real money)."""
    jobs = re.search(r"const COPY_TAB_JOBS = \{(.*?)\n\};", html_content, re.S).group(1)
    _, rest = jobs.split("'copy-paper': [")
    paper_jobs, live_jobs = rest.split("'copy-live': [")
    assert re.search(r"fetchCopyTradingActivityFeed\('paper'\), everyMs: 300_000", paper_jobs)
    assert re.search(r"fetchCopyTradingActivityFeed\('live'\), everyMs: 30_000", live_jobs)
    assert "fetchCopyTradingActivityFeed('live')" not in paper_jobs
    assert "fetchCopyTradingActivityFeed('paper')" not in live_jobs


def test_activity_feed_requests_are_mode_scoped(html_content):
    assert "/api/copy-trading/activity-feed?mode=${sc.mode}" in html_content


def test_promotion_tab_uses_the_same_300s_cadence(html_content):
    """Cross-check against the Promotion tab's own setInterval call, per
    the issue's explicit instruction to match it exactly."""
    promotion_match = re.search(
        r"promotionIntervalId = setInterval\(fetchPromotionBar, ([\d_]+)\);",
        html_content,
    )
    assert promotion_match, "Promotion tab's setInterval call not found"
    assert promotion_match.group(1).replace("_", "") == "300000"


def test_activity_feed_endpoint_url_used_by_frontend(html_content):
    assert "/api/copy-trading/activity-feed" in html_content


def test_empty_state_text_present(html_content):
    assert "No activity yet" in html_content


def test_mode_live_empty_states_present(html_content):
    """issue #1188 States section: distinguish 'live has never been
    switched on' from 'live is on, but nothing happened in this window'."""
    assert "hasn't been turned on yet" in html_content
    assert "No live activity in this range" in html_content


def test_activity_feed_uses_shared_mode_badge_classes(html_content):
    """Must reuse #1185's existing .mode-badge/.mode-badge-live/
    .mode-badge-paper classes verbatim, not redefine a parallel set."""
    render_fn_start = html_content.index("function _copyActivityModeBadgeHtml(e)")
    render_fn_end = html_content.index("\nfunction ", render_fn_start + 10)
    fn_body = html_content[render_fn_start:render_fn_end]
    assert "mode-badge-live" in fn_body
    assert "mode-badge-paper" in fn_body
    assert "aria-label" in fn_body, "the mode badge must carry an explicit aria-label, never color-only"


def test_activity_item_has_left_border_accent_classes(html_content):
    """Design spec: colored left-border row accent (green/live, amber/paper)
    in addition to badge + text -- three redundant mode signals."""
    assert ".copy-activity-item-live{" in html_content
    assert ".copy-activity-item-paper{" in html_content
    assert "copy-activity-item-live" in html_content.split("function _copyActivityItemHtml")[1][:1000]
    assert "copy-activity-item-paper" in html_content.split("function _copyActivityItemHtml")[1][:1000]


def test_activity_feed_click_targets_are_keyboard_accessible(html_content):
    """Design spec accessibility notes: interactive elements need visible
    focus states / keyboard reachability, not mouse-only handlers."""
    render_fn_start = html_content.index("function _copyActivityItemHtml(e)")
    render_fn_end = html_content.index("\nfunction ", render_fn_start + 10)
    fn_body = html_content[render_fn_start:render_fn_end]
    assert 'role="button"' in fn_body
    assert 'tabindex="0"' in fn_body

    assert "_copyActivityOnListKeydown" in html_content
    assert "event.key !== 'Enter' && event.key !== ' '" in html_content.split(
        "function _copyActivityOnListKeydown"
    )[1][:300]


def test_candidate_row_has_a_stable_id_for_click_through_lookup(html_content):
    """The Activity Feed's signal-event click-through looks up the
    Candidates row by id (copy-row-<safeId>) rather than a raw-address
    attribute selector, matching this file's existing _copySafeId()-
    derived-id convention (see _copySafeId's own docstring) instead of
    introducing a new CSS.escape() dependency."""
    assert 'id="copy-row-${safeId}"' in html_content


def test_isolation_from_weather_portfolio_tab(html_content):
    """Issue #1100 isolation requirement: the Activity Feed view must
    never write into the weather Portfolio tab's own elements/functions."""
    render_fn_start = html_content.index("function renderCopyActivityFeed(data, scope = 'paper')")
    render_fn_end = html_content.index("\nasync function fetchCopyTradingActivityFeed")
    fn_body = html_content[render_fn_start:render_fn_end]

    for weather_id in ("val-portfolio", "val-cash", "val-invested", "open-positions"):
        assert weather_id not in fn_body, (
            f"Activity Feed view must not touch the weather tab's #{weather_id}"
        )

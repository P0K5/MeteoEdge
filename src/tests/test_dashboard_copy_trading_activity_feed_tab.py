"""Tests for the Copy-Trading dashboard Activity Feed view's static
markup (epic F #1143, story F4 #1149).

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


def test_activity_feed_section_exists_inside_copy_trading_content(html_content):
    """The new section must live inside #copy-trading-content, alongside
    (not replacing) the existing Candidates/Followed Wallets/Positions
    views, and must come last (story F4 is the last story in epic F)."""
    content_start = html_content.index('id="copy-trading-content"')
    content_end = html_content.index("</section>", content_start)
    section = html_content[content_start:content_end]

    assert 'id="copy-candidates-list"' in section
    assert 'id="copy-followed-list"' in section
    assert 'id="copy-positions-content"' in section
    assert 'id="copy-activity-list"' in section, "Activity Feed view not found inside copy-trading-content"

    assert section.index('id="copy-positions-content"') < section.index('id="copy-activity-list"'), (
        "Activity Feed must come after Positions & P&L, not replace or precede it"
    )


def test_activity_feed_has_its_own_stale_banner(html_content):
    """Design decision (no toast/modal component): the stale-feed
    indicator reuses the existing .error-banner/.visible inline pattern,
    but with its own dedicated element -- not the shared
    #copy-trading-error-banner used by the other three views (which would
    let an unrelated Candidates/Followed/Positions fetch race-clear it)."""
    assert 'id="copy-activity-stale-banner"' in html_content
    assert 'class="error-banner" id="copy-activity-stale-banner"' in html_content
    assert 'id="copy-activity-stale-text"' in html_content


def test_activity_feed_has_wallet_and_event_type_filter_dropdowns(html_content):
    assert 'id="copy-activity-wallet-select"' in html_content
    assert 'id="copy-activity-type-select"' in html_content
    assert 'value="order_placed"' in html_content
    assert 'value="order_skipped"' in html_content
    assert 'value="wallet_paused"' in html_content


def test_activity_feed_has_mode_filter_dropdown(html_content):
    """issue #1188 acceptance criteria: new Mode (Live/Paper/All) filter
    alongside the existing wallet and event-type filters."""
    assert 'id="copy-activity-mode-select"' in html_content
    content_start = html_content.index('id="copy-activity-mode-select"')
    content_end = html_content.index('</select>', content_start)
    section = html_content[content_start:content_end]
    assert 'value="live"' in section
    assert 'value="paper"' in section


def test_activity_feed_event_type_dropdown_has_live_options(html_content):
    """issue #1188: every new live event_type must be selectable, matching
    the existing per-event_type filter pattern."""
    content_start = html_content.index('id="copy-activity-type-select"')
    content_end = html_content.index('</select>', content_start)
    section = html_content[content_start:content_end]
    for event_type in (
        "live_order_pending", "live_order_filled", "live_order_partial",
        "live_order_rejected", "live_order_skipped",
        "live_circuit_breaker_tripped", "live_position_settled",
    ):
        assert f'value="{event_type}"' in section, f"missing event-type filter option: {event_type}"


def test_activity_feed_fetch_wired_into_copy_trading_tab_activation(html_content):
    """fetchCopyTradingActivityFeed must be called both on first tab
    activation and on the shared 5-minute poll interval, matching the
    other three views' existing wiring -- and that interval must be
    300_000ms (300s / 5min), matching the Promotion tab's own cadence
    exactly (issue #1149's explicit design decision, not a new value)."""
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
    assert "fetchCopyTradingActivityFeed();" in block
    assert "}, 300_000);" in block, "Copy-Trading poll interval must stay 300s (5 min)"


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
    render_fn_start = html_content.index("function renderCopyActivityFeed(data)")
    render_fn_end = html_content.index("\nasync function fetchCopyTradingActivityFeed")
    fn_body = html_content[render_fn_start:render_fn_end]

    for weather_id in ("val-portfolio", "val-cash", "val-invested", "open-positions"):
        assert weather_id not in fn_body, (
            f"Activity Feed view must not touch the weather tab's #{weather_id}"
        )

"""Tests for the Copy-Trading live/paper badge CSS + global posture banner
(epic J #1161, issue #1185 -- foundation issue for #1186/#1187/#1188).

Structural/markup checks only; the banner's config-driven fetch/render
logic is covered separately in
test_copy_trading_mode_banner_js_logic.py (same Node-execution technique
as the other Copy-Trading JS-logic tests).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = REPO_ROOT / "src" / "dashboard" / "static" / "index.html"


@pytest.fixture(scope="module")
def html_content() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# CSS classes
# ---------------------------------------------------------------------------

def _css_rule(html: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\{([^}]*)\}", html)
    assert match, f"CSS rule {selector} not found"
    return match.group(1)


def test_mode_badge_base_class_matches_side_mode_badge_shape(html_content):
    """`.mode-badge` must be structurally identical to `.side-mode-badge`
    (the design spec's explicit reuse-the-pattern requirement) but is its
    own class name -- never aliased/shared, so a future change to one
    never silently affects the other."""
    side = _css_rule(html_content, ".side-mode-badge")
    mode = _css_rule(html_content, ".mode-badge")

    def norm(s: str) -> set:
        # Compare the declaration set, ignoring whitespace differences.
        return {d.strip() for d in s.split(";") if d.strip()}

    assert norm(side) == norm(mode), (
        f".mode-badge must have the same declarations as .side-mode-badge; "
        f"got {norm(mode)} vs {norm(side)}"
    )


def test_mode_badge_live_and_paper_variants(html_content):
    live = _css_rule(html_content, ".mode-badge-live")
    paper = _css_rule(html_content, ".mode-badge-paper")
    assert "var(--yes-bg)" in live and "var(--yes)" in live
    assert "var(--warn-bg)" in paper and "var(--warn)" in paper


def test_mode_badge_classes_distinct_from_side_mode_classes(html_content):
    """Never alias `.mode-badge*` to `.side-mode-badge*` -- different
    semantic axis (live/paper execution regime vs. per-side YES/NO)."""
    assert ".mode-badge{" in html_content or ".mode-badge {" in html_content
    assert ".mode-badge-live{" in html_content
    assert ".mode-badge-paper{" in html_content
    # The two families must be genuinely separate rules, not one aliasing
    # the other via a shared selector list.
    assert ".mode-badge,.side-mode-badge" not in html_content.replace(" ", "")
    assert ".side-mode-badge,.mode-badge" not in html_content.replace(" ", "")


# ---------------------------------------------------------------------------
# Global posture banner
# ---------------------------------------------------------------------------

def test_posture_banner_element_exists(html_content):
    assert 'id="copy-trading-mode-banner"' in html_content, (
        "Global live/paper posture banner element not found"
    )


def test_posture_banner_is_distinct_from_live_pill(html_content):
    """The existing `.live-pill` denotes data-connection freshness, not
    trading mode, and must not be repurposed (explicit acceptance
    criterion)."""
    banner_match = re.search(
        r'<span id="copy-trading-mode-banner"[^>]*>.*?</span>', html_content, re.S
    )
    assert banner_match, "Could not locate the posture banner element"
    banner_tag = banner_match.group(0)
    assert "live-pill" not in banner_tag

    # And the pre-existing .live-pill element must still exist, unmodified
    # in purpose (connection freshness), elsewhere in the page.
    assert '<div class="live-pill">' in html_content


def test_posture_banner_is_landmark_region(html_content):
    banner_match = re.search(
        r'<span id="copy-trading-mode-banner"([^>]*)>', html_content
    )
    assert banner_match, "Could not locate the posture banner opening tag"
    attrs = banner_match.group(1)
    assert 'role="status"' in attrs
    assert 'aria-live="polite"' in attrs


def test_posture_banner_default_state_is_conservative_paper_off(html_content):
    """Before the config fetch resolves, the banner must default to the
    OFF/paper state -- never claim LIVE without a confirmed signal (same
    conservative-default rule as the execution_mode precedent)."""
    banner_match = re.search(
        r'<span id="copy-trading-mode-banner"([^>]*)>([^<]*)</span>', html_content
    )
    assert banner_match, "Could not locate the posture banner element"
    attrs, text = banner_match.groups()
    assert "mode-badge-paper" in attrs
    assert "mode-badge-live" not in attrs
    assert text.strip() == "LIVE TRADING OFF — paper only"
    assert 'aria-label="Live trading is off — paper only"' in attrs


def test_posture_banner_lives_in_copy_trading_shared_header(html_content):
    """Wired into the tab's shared header (not one specific view) so all
    four Copy-Trading views can rely on it, per the issue's explicit
    scope ("no view-specific content in this issue")."""
    tab_start = html_content.find('id="tab-copy-trading"')
    banner_idx = html_content.find('id="copy-trading-mode-banner"')
    candidates_idx = html_content.find('id="copy-candidates-list"')
    assert tab_start != -1 and banner_idx != -1 and candidates_idx != -1
    assert tab_start < banner_idx < candidates_idx, (
        "Posture banner must sit in the Copy-Trading tab's shared header, "
        "before any individual view's content"
    )


# ---------------------------------------------------------------------------
# Posture banner refresh cadence (PR #1192 review finding)
#
# The banner must refetch on every tab re-entry, on its own interval
# decoupled from the four-view 5-minute poll group -- not just once on the
# tab's very first activation, which could leave a stale live/paper read on
# screen for up to 5 minutes on every later revisit. Runtime behavior is
# covered by test_copy_trading_mode_banner_js_logic.py; these are static
# source-shape checks (regex over the inline <script>) that fail fast and
# cheaply on an obvious wiring regression.
# ---------------------------------------------------------------------------

def _copy_trading_switch_tab_block(html: str) -> str:
    match = re.search(
        r"if \(tab === 'copy-trading'\) \{.*?\n  \}\n\}", html, re.S
    )
    assert match, "Could not locate the Copy-Trading switchTab() entry block"
    return match.group(0)


def test_posture_banner_has_its_own_dedicated_interval(html_content):
    block = _copy_trading_switch_tab_block(html_content)
    assert "copyTradingModeIntervalId" in block, (
        "Posture banner must use its own interval variable, decoupled from "
        "copyTradingIntervalId (the four-view 5-minute group)"
    )
    assert re.search(
        r"copyTradingModeIntervalId\s*=\s*setInterval\(\s*fetchCopyTradingModePosture\s*,\s*30_000\s*\)",
        block,
    ), "Posture banner interval must be 30s (30_000), not the 5-minute (300_000) view-data cadence"


def test_posture_banner_refetches_unconditionally_on_every_tab_entry(html_content):
    """fetchCopyTradingModePosture() must be called outside/after the
    `if (!copyTradingLoaded)` first-activation guard too, so a tab
    re-entry (copyTradingLoaded already true) still fetches immediately
    rather than waiting for the next interval tick."""
    block = _copy_trading_switch_tab_block(html_content)
    loaded_guard = re.search(r"if \(!copyTradingLoaded\) \{(.*?)\n    \}", block, re.S)
    assert loaded_guard, "Could not locate the copyTradingLoaded first-activation guard"
    after_guard = block[loaded_guard.end():]
    assert "fetchCopyTradingModePosture()" in after_guard, (
        "fetchCopyTradingModePosture() must also be called unconditionally "
        "on every tab entry, not only inside the copyTradingLoaded guard"
    )


def test_posture_banner_interval_cleared_when_leaving_tab(html_content):
    teardown_match = re.search(
        r"Stop Copy-Trading polling when leaving the Copy-Trading tab.*?\n  \}",
        html_content,
        re.S,
    )
    assert teardown_match, "Could not locate the Copy-Trading tab teardown block"
    teardown = teardown_match.group(0)
    assert "clearInterval(copyTradingModeIntervalId)" in teardown
    assert "copyTradingModeIntervalId = null" in teardown

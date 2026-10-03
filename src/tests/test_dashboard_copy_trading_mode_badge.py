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

BANNER_IDS = [
    "copy-wallets-mode-banner",
    "copy-paper-mode-banner",
    "copy-live-mode-banner",
]


def _banner_tag(html: str, banner_id: str) -> str:
    match = re.search(rf'<span id="{banner_id}"[^>]*>[^<]*</span>', html)
    assert match, f"Could not locate the posture banner element #{banner_id}"
    return match.group(0)


def test_posture_banner_element_exists_on_every_copy_tab(html_content):
    """One shared component rendered on all three copy tabs (issue #1275):
    class-based, per-tab-suffixed ids so ids stay unique."""
    for banner_id in BANNER_IDS:
        assert f'id="{banner_id}"' in html_content, (
            f"Global live/paper posture banner #{banner_id} not found"
        )
        assert "copy-trading-mode-banner" in _banner_tag(html_content, banner_id)
    assert 'id="copy-trading-mode-banner"' not in html_content


def test_posture_banner_is_distinct_from_live_pill(html_content):
    """The existing `.live-pill` denotes data-connection freshness, not
    trading mode, and must not be repurposed (explicit acceptance
    criterion)."""
    for banner_id in BANNER_IDS:
        assert "live-pill" not in _banner_tag(html_content, banner_id)

    # And the pre-existing .live-pill element must still exist, unmodified
    # in purpose (connection freshness), elsewhere in the page.
    assert '<div class="live-pill">' in html_content


def test_posture_banner_is_landmark_region(html_content):
    for banner_id in BANNER_IDS:
        tag = _banner_tag(html_content, banner_id)
        assert 'role="status"' in tag
        assert 'aria-live="polite"' in tag


def test_posture_banner_default_state_is_conservative_paper_off(html_content):
    """Before the config fetch resolves, every banner instance must default
    to the OFF/paper state -- never claim LIVE without a confirmed signal
    (same conservative-default rule as the execution_mode precedent)."""
    for banner_id in BANNER_IDS:
        match = re.search(
            rf'<span id="{banner_id}"([^>]*)>([^<]*)</span>', html_content
        )
        assert match, f"Could not locate #{banner_id}"
        attrs, text = match.groups()
        assert "mode-badge-paper" in attrs
        assert "mode-badge-live" not in attrs
        assert text.strip() == "LIVE TRADING OFF — paper only"
        assert 'aria-label="Live trading is off — paper only"' in attrs


def test_posture_banner_lives_in_each_tabs_shared_header(html_content):
    """Wired into each tab's header (not one specific view), before any
    individual view's content."""
    for tab, banner_id, first_view in [
        ("copy-wallets", "copy-wallets-mode-banner", "copy-candidates-list"),
        ("copy-paper", "copy-paper-mode-banner", "copy-followed-list"),
        ("copy-live", "copy-live-mode-banner", "copy-live-followed-list"),
    ]:
        tab_start = html_content.find(f'id="tab-{tab}"')
        banner_idx = html_content.find(f'id="{banner_id}"')
        view_idx = html_content.find(f'id="{first_view}"')
        assert tab_start != -1 and banner_idx != -1 and view_idx != -1
        assert tab_start < banner_idx < view_idx, (
            f"Posture banner must sit in the {tab} tab's header, before any "
            "individual view's content"
        )


# ---------------------------------------------------------------------------
# Posture banner refresh cadence (PR #1192 review finding)
#
# The banner must refetch on every tab (re-)entry and on its own 30 s
# interval -- not just once on a tab's very first activation, which could
# leave a stale live/paper read on screen for minutes. Runtime behavior is
# covered by test_dashboard_copy_tab_controller_behavior.py; this is a static
# source-shape check that fails fast on an obvious wiring regression.
# ---------------------------------------------------------------------------

def test_posture_job_is_30s_and_always_on_every_copy_tab(html_content):
    jobs_match = re.search(r"const COPY_TAB_JOBS = \{(.*?)\n\};", html_content, re.S)
    assert jobs_match, "Could not locate COPY_TAB_JOBS"
    jobs = jobs_match.group(1)
    posture_jobs = re.findall(
        r"\{ key: 'posture', run: \(\) => fetchCopyTradingModePosture\(\), "
        r"everyMs: 30_000, always: true \}",
        jobs,
    )
    assert len(posture_jobs) == 3, (
        "Each copy tab must poll the posture banner every 30 s and refetch it "
        "on every entry (always: true)"
    )

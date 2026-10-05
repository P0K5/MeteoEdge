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


# ---------------------------------------------------------------------------
# WCAG contrast ratio validation (issue #602)
#
# Light-theme badge text colors must achieve 4.5:1 contrast ratio (WCAG AA)
# against their backgrounds. Test extracts token values from the HTML and
# verifies all badge pairs meet the minimum requirement.
# ---------------------------------------------------------------------------

def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert hex color to RGB tuple."""
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i : i + 2], 16) for i in (0, 2, 4))


def _rgb_to_luminance(r: int, g: int, b: int) -> float:
    """
    Calculate relative luminance per WCAG 2.1 algorithm.
    Applies gamma correction and returns the linear luminance value.
    """

    def linearize(c: int) -> float:
        c = c / 255.0
        if c <= 0.03928:
            return c / 12.92
        else:
            return ((c + 0.055) / 1.055) ** 2.4

    R = linearize(r)
    G = linearize(g)
    B = linearize(b)
    return 0.2126 * R + 0.7152 * G + 0.0722 * B


def _contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    """Calculate WCAG contrast ratio between foreground and background."""
    fg_rgb = _hex_to_rgb(fg_hex)
    bg_rgb = _hex_to_rgb(bg_hex)

    fg_lum = _rgb_to_luminance(*fg_rgb)
    bg_lum = _rgb_to_luminance(*bg_rgb)

    lighter = max(fg_lum, bg_lum)
    darker = min(fg_lum, bg_lum)

    return (lighter + 0.05) / (darker + 0.05)


def test_light_theme_badge_contrast_wcag_aa(html_content):
    """Light-theme badge text (--yes, --warn) must achieve 4.5:1 contrast
    ratio against their badge backgrounds (issue #602 fix).
    Dark theme is unaffected; dark badge pairs already pass.
    """
    # Extract light theme token values from the CSS
    light_theme_match = re.search(r'\[data-theme="light"\]\{([^}]*)\}', html_content)
    assert light_theme_match, "Could not find [data-theme='light'] CSS rule"

    tokens_str = light_theme_match.group(1)
    tokens = {}

    for token, var_match in [
        ("--yes", r"--yes:([#\da-f]+)"),
        ("--warn", r"--warn:([#\da-f]+)"),
        ("--yes-bg", r"--yes-bg:([#\da-f]+)"),
        ("--warn-bg", r"--warn-bg:([#\da-f]+)"),
    ]:
        match = re.search(var_match, tokens_str)
        assert match, f"Could not extract {token} from light theme CSS"
        tokens[token] = match.group(1)

    # Test primary badge pairs (the critical paths per issue #602)
    pairs_to_test = [
        ("--yes on --yes-bg", tokens["--yes"], tokens["--yes-bg"]),
        ("--warn on --warn-bg", tokens["--warn"], tokens["--warn-bg"]),
    ]

    min_ratio = 4.5  # WCAG AA standard
    for pair_name, fg_hex, bg_hex in pairs_to_test:
        ratio = _contrast_ratio(fg_hex, bg_hex)
        assert ratio >= min_ratio, (
            f"Badge pair '{pair_name}' fails WCAG AA: ratio={ratio:.2f}:1 "
            f"(need {min_ratio}:1) — fg={fg_hex}, bg={bg_hex}"
        )

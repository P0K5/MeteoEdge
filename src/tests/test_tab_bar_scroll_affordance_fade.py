"""Unit tests for the tab bar scroll affordance edge fade (issue #1445).

Tests verify:
1. Both dark and light theme .tab-bar CSS rules contain linear-gradients (4-layer technique)
2. Correct background-attachment values (local, local, scroll, scroll for 4-layer trick)
3. Theme-aware colors for visibility in both themes
4. No false affordance on desktop (scroll-attached layers ensure fade disappears at end)
5. No JavaScript changes; pure CSS implementation
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = REPO_ROOT / "src" / "dashboard" / "static" / "index.html"


def test_dark_theme_tab_bar_has_scroll_affordance_gradient():
    """Verify dark theme .tab-bar CSS contains linear-gradients with 4-layer technique.

    Dark theme should use semi-transparent white overlay for visibility against dark surface.
    Implements the two-background-attachment trick with 4 layers to prevent false
    affordance on desktop where tabs don't overflow.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Find the dark theme tab-bar CSS rule
    match = re.search(r'\[data-theme="dark"\]\s+\.tab-bar\{([^}]*?)\}', html, re.DOTALL)
    assert match, "[data-theme=\"dark\"] .tab-bar CSS rule not found"

    dark_tab_bar_css = match.group(1)

    # Verify 4-layer technique with two linear-gradients (plus solid fills)
    gradient_count = dark_tab_bar_css.count('linear-gradient')
    assert gradient_count >= 2, (
        "Dark theme .tab-bar must have at least 2 linear-gradients for 4-layer technique"
    )

    # Verify theme-aware color: semi-transparent white for dark theme
    assert 'rgba(255,255,255' in dark_tab_bar_css, (
        "Dark theme gradient must use semi-transparent white for visibility against dark surface"
    )


def test_light_theme_tab_bar_has_scroll_affordance_gradient():
    """Verify light theme .tab-bar CSS contains linear-gradients with 4-layer technique.

    Light theme should use semi-transparent black overlay for visibility against light surface.
    Implements the two-background-attachment trick with 4 layers to prevent false
    affordance on desktop where tabs don't overflow.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Find the light theme tab-bar CSS rule
    match = re.search(r'\[data-theme="light"\]\s+\.tab-bar\{([^}]*?)\}', html, re.DOTALL)
    assert match, "[data-theme=\"light\"] .tab-bar CSS rule not found"

    light_tab_bar_css = match.group(1)

    # Verify 4-layer technique with two linear-gradients (plus solid fills)
    gradient_count = light_tab_bar_css.count('linear-gradient')
    assert gradient_count >= 2, (
        "Light theme .tab-bar must have at least 2 linear-gradients for 4-layer technique"
    )

    # Verify theme-aware color: semi-transparent black for light theme
    assert 'rgba(0,0,0' in light_tab_bar_css, (
        "Light theme gradient must use semi-transparent black for visibility against light surface"
    )


def test_tab_bar_uses_scroll_affordance_with_correct_attachments():
    """Verify both theme .tab-bar rules use Lea Verou's scroll affordance pattern.

    The pattern uses: local-attachment cover (solid fading to transparent, moves with
    content, masks shadow at scroll end) + scroll-attachment shadow (fade pinned to
    viewport right edge, always visible until masked).
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Check both theme rules
    for theme in ["dark", "light"]:
        match = re.search(
            rf'\[data-theme="{theme}"\]\s+\.tab-bar\{{([^}}]*?)\}}',
            html,
            re.DOTALL
        )
        assert match, f"[data-theme=\"{theme}\"] .tab-bar CSS rule not found"

        theme_css = match.group(1)

        # Verify both local and scroll attachment values are present
        assert 'local' in theme_css, (
            f"{theme} theme must include 'local' attachment for cover gradient"
        )
        assert 'scroll' in theme_css, (
            f"{theme} theme must include 'scroll' attachment for shadow gradient"
        )
        # Verify two gradients present (cover + shadow)
        gradient_count = theme_css.count('linear-gradient')
        assert gradient_count >= 2, (
            f"{theme} theme must have at least 2 gradients (cover + shadow)"
        )


def test_tab_bar_gradient_positioned_right():
    """Verify gradients are positioned at the right edge.

    Both the cover (local) and shadow (scroll) gradients are positioned at the
    right edge of the tab bar to create the scroll affordance fade effect.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Check both theme rules contain "right" positioning
    for theme in ["dark", "light"]:
        match = re.search(
            rf'\[data-theme="{theme}"\]\s+\.tab-bar\{{([^}}]*?)\}}',
            html,
            re.DOTALL
        )
        assert match, f"[data-theme=\"{theme}\"] .tab-bar CSS rule not found"

        theme_css = match.group(1)

        # Both should have right positioning for right-edge fade
        assert 'right' in theme_css, (
            f"{theme} theme gradient must use 'right' positioning for right-edge fade"
        )


def test_tab_bar_gradient_sized_appropriately():
    """Verify fade gradients have appropriate width (~40px).

    The gradient should be wide enough to be visible but not obscure the last tab(s).
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Check both theme rules
    for theme in ["dark", "light"]:
        match = re.search(
            rf'\[data-theme="{theme}"\]\s+\.tab-bar\{{([^}}]*?)\}}',
            html,
            re.DOTALL
        )
        assert match, f"[data-theme=\"{theme}\"] .tab-bar CSS rule not found"

        theme_css = match.group(1)

        # Should contain 40px sizing for the fade width
        assert '40px' in theme_css, (
            f"{theme} theme .tab-bar must have '40px' width for the fade gradient"
        )


def test_tab_bar_still_has_required_overflow_properties():
    """Verify the scroll affordance doesn't break overflow containment.

    The tab bar must still have overflow-x:auto and flex-wrap:nowrap
    for the existing scroll behavior to work. These are in the base .tab-bar
    rule (after theme-specific background rules).
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Find the base .tab-bar CSS rule by searching for the pattern after light theme rule
    # Look for the standalone .tab-bar{ rule that comes after the theme-specific ones
    match = re.search(r'\.tab-bar\{\s*border-bottom[^}]*\}', html, re.DOTALL)
    assert match, ".tab-bar CSS rule with border-bottom property not found"

    tab_bar_css = match.group(0)

    assert 'overflow-x:auto' in tab_bar_css, (
        ".tab-bar must retain 'overflow-x:auto' for horizontal scroll"
    )
    assert 'flex-wrap:nowrap' in tab_bar_css, (
        ".tab-bar must retain 'flex-wrap:nowrap' to keep tabs in one row"
    )


def test_tab_bar_gradient_no_js_changes():
    """Verify the scroll affordance is CSS-only (no JavaScript changes).

    The fade should not require any JavaScript modifications to the
    existing switchTab() or scrollIntoView() behavior.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Verify switchTab still calls scrollIntoView (regression check)
    assert 'switchTab' in html, "switchTab function must exist"
    assert 'scrollIntoView' in html, "scrollIntoView must still be called"

    # Find the switchTab function to verify no unnecessary JS changes
    switch_tab_match = re.search(
        r'function switchTab\(tab\)\s*\{(.*?)(?=\n\s*(?:function|const|let|var|\}(?!\s*\))|$))',
        html,
        re.DOTALL
    )
    assert switch_tab_match, "switchTab function not found"
    assert 'scrollIntoView' in switch_tab_match.group(1), (
        "switchTab must still call scrollIntoView"
    )

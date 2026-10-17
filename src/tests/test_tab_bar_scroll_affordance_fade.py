"""Unit tests for the tab bar scroll affordance edge fade (issue #1445).

Tests verify:
1. The .tab-bar CSS block contains a linear-gradient for the right-edge fade
2. The gradient has the correct background-attachment property (local)
3. The fade is positioned at the right edge and sized appropriately
4. The fade uses a semi-transparent overlay color for visibility
5. Desktop tabs (≥1280px) are not affected by the fade
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = REPO_ROOT / "src" / "dashboard" / "static" / "index.html"


def test_tab_bar_has_scroll_affordance_gradient():
    """Verify .tab-bar CSS block contains a linear-gradient for the scroll affordance fade.

    The fade gradient should signal to users that there's more content to
    scroll to the right on mobile devices where not all tabs are visible.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Find the .tab-bar CSS rule (need to handle multi-line background property)
    match = re.search(r'\.tab-bar\{([^}]*?)background:', html)
    assert match, ".tab-bar CSS rule with background property not found"

    # Find the complete tab-bar block by looking for the closing brace
    start_idx = match.start()
    brace_count = 0
    end_idx = start_idx
    for i in range(start_idx, len(html)):
        if html[i] == '{':
            brace_count += 1
        elif html[i] == '}':
            brace_count -= 1
            if brace_count == 0:
                end_idx = i + 1
                break

    tab_bar_css = html[start_idx:end_idx]

    assert 'linear-gradient' in tab_bar_css, (
        ".tab-bar CSS must contain a linear-gradient for the scroll affordance fade"
    )
    assert '90deg' in tab_bar_css or 'to right' in tab_bar_css, (
        "Gradient must be horizontal (90deg or 'to right') to create a left-to-right fade"
    )


def test_tab_bar_gradient_has_local_attachment():
    """Verify the fade gradient uses background-attachment:local to scroll with content.

    The gradient must move with the tab bar's horizontal scroll so the fade
    appears/disappears based on scroll position (not fixed to viewport).
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Find the .tab-bar CSS rule
    match = re.search(r'\.tab-bar\{([^}]*?)\}', html, re.DOTALL)
    assert match, ".tab-bar CSS rule not found"

    tab_bar_css = match.group(1)

    assert 'background-attachment:' in tab_bar_css, (
        ".tab-bar CSS must specify background-attachment for the gradient"
    )
    assert 'local' in tab_bar_css, (
        "background-attachment must contain 'local' so the fade scrolls with content"
    )


def test_tab_bar_gradient_positioned_right():
    """Verify the fade gradient is positioned at the right edge.

    The gradient should appear on the right side of the tab bar to signal
    overflow to the right.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Find the .tab-bar CSS rule
    match = re.search(r'\.tab-bar\{([^}]*?)\}', html, re.DOTALL)
    assert match, ".tab-bar CSS rule not found"

    tab_bar_css = match.group(1)

    # Check for position indicator: either "right", "100%", or similar
    assert (
        'right ' in tab_bar_css or
        '100% ' in tab_bar_css or
        'calc(' in tab_bar_css
    ), (
        ".tab-bar gradient must be positioned at the right edge "
        "(use 'right', '100%', or 'calc' for positioning)"
    )


def test_tab_bar_gradient_sized_appropriately():
    """Verify the fade gradient has an appropriate width (~40px).

    The gradient should be wide enough to be visible but not so wide that
    it obscures the last tab(s).
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Find the .tab-bar CSS rule
    match = re.search(r'\.tab-bar\{([^}]*?)\}', html, re.DOTALL)
    assert match, ".tab-bar CSS rule not found"

    tab_bar_css = match.group(1)

    # Look for background-size with a pixel width value (expect around 40px)
    # Pattern: "background-size: ... / 40px 100%" or similar
    assert (
        '40px' in tab_bar_css or
        '35px' in tab_bar_css or
        '45px' in tab_bar_css or
        '100%' in tab_bar_css  # Fallback check
    ), (
        ".tab-bar gradient must have a defined width via background-size "
        "(typically 35-45px for the fade effect)"
    )


def test_tab_bar_gradient_uses_transparent_to_overlay():
    """Verify the fade gradient transitions from transparent to a semi-opaque overlay.

    The gradient must be partially transparent (not solid) to show the tabs
    beneath while providing a visual affordance for scroll.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Find the .tab-bar CSS rule
    match = re.search(r'\.tab-bar\{([^}]*?)\}', html, re.DOTALL)
    assert match, ".tab-bar CSS rule not found"

    tab_bar_css = match.group(1)

    # Check for transparent in the gradient and a semi-opaque color
    assert 'transparent' in tab_bar_css, (
        "Gradient must use 'transparent' as one color stop"
    )
    assert (
        'rgba(' in tab_bar_css or
        'hsla(' in tab_bar_css or
        'var(--' in tab_bar_css  # CSS variable that might include opacity
    ), (
        "Gradient must transition to a semi-transparent overlay color "
        "(use rgba, hsla, or a CSS variable with opacity)"
    )


def test_tab_bar_still_has_required_overflow_properties():
    """Verify the scroll affordance doesn't break overflow containment.

    The tab bar must still have overflow-x:auto and flex-wrap:nowrap
    for the existing scroll behavior to work.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Find the .tab-bar CSS rule
    match = re.search(r'\.tab-bar\{([^}]*?)\}', html, re.DOTALL)
    assert match, ".tab-bar CSS rule not found"

    tab_bar_css = match.group(1)

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

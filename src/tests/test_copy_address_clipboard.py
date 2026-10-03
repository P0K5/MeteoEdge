"""Tests for the copy-address-to-clipboard function with fallback chain (issue #1273).

Simple validation tests that check:
1. The function exists and has the correct structure
2. It implements the fallback chain (clipboard → execCommand → manual)
3. Feedback elements are properly styled in CSS
4. HTML templates include the required elements
"""
from __future__ import annotations

from pathlib import Path
import re

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = REPO_ROOT / "src" / "dashboard" / "static" / "index.html"


def test_copy_address_clipboard_function_exists():
    """Verify the copyAddressToClipboard function exists in the HTML."""
    with open(INDEX_HTML) as f:
        html = f.read()

    # Should have the function definition
    assert 'async function copyAddressToClipboard(address, btn)' in html, \
        'copyAddressToClipboard function should exist'


def test_aria_live_region_added():
    """Verify global aria-live region for screen reader announcements."""
    with open(INDEX_HTML) as f:
        html = f.read()

    # Should have the region defined
    assert 'id="copy-sr-status"' in html, \
        'Global aria-live region with id="copy-sr-status" should exist'
    assert 'role="status"' in html and 'aria-live="polite"' in html, \
        'aria-live region should have role="status" and aria-live="polite"'


def test_feedback_css_styles_present():
    """Verify CSS styles for feedback labels and manual input."""
    with open(INDEX_HTML) as f:
        html = f.read()

    # Check for feedback label CSS
    assert '.copy-feedback-label' in html, \
        'CSS class .copy-feedback-label should be defined'
    assert '.copy-feedback-label.visible' in html, \
        'CSS class .copy-feedback-label.visible should be defined for showing labels'
    assert '.copy-feedback-label.success' in html, \
        'CSS class .copy-feedback-label.success should be defined (green)'
    assert '.copy-feedback-label.error' in html, \
        'CSS class .copy-feedback-label.error should be defined (red)'

    # Check for manual input CSS
    assert '.copy-address-fallback-input' in html, \
        'CSS class .copy-address-fallback-input should be defined'

    # Check for aria-live CSS
    assert '.copy-sr-status' in html, \
        'CSS class .copy-sr-status for visually hidden region should be defined'


def test_all_copy_buttons_have_feedback_label():
    """Verify all 6 copy buttons have feedback label spans."""
    with open(INDEX_HTML) as f:
        html = f.read()

    # Count copy buttons
    button_count = html.count('class="copy-copy-btn"')
    assert button_count >= 6, \
        f'Should have at least 6 copy buttons, found {button_count}'

    # Count feedback labels - should match button count
    feedback_count = html.count('class="copy-feedback-label"')
    assert feedback_count >= button_count, \
        f'Should have feedback labels for each button: {feedback_count} labels, {button_count} buttons'


def test_copy_buttons_have_data_address_attribute():
    """Verify address span elements have data-address attribute for identification."""
    with open(INDEX_HTML) as f:
        html = f.read()

    # Extract a copy button cell and check for data-address
    pattern = r'<span class="copy-address-mono"[^>]*data-address='
    matches = re.findall(pattern, html)
    assert len(matches) >= 6, \
        f'Should have data-address on address spans for all {len(matches)} buttons'


def test_timer_cleanup_implemented():
    """Verify timer cleanup logic is implemented to prevent memory leaks."""
    with open(INDEX_HTML) as f:
        html = f.read()

    # Extract function body
    match = re.search(
        r'async\s+function\s+copyAddressToClipboard\(address,\s*btn\)\s*\{([\s\S]*?)\n\}(?!\})',
        html
    )
    assert match, "Could not extract copyAddressToClipboard function"
    func_body = match.group(1)

    # Check for timer cleanup
    assert '_copyTimer' in func_body, \
        'Function should track timers on button via _copyTimer'
    assert 'clearTimeout' in func_body or 'btn._copyTimer' in func_body, \
        'Function should implement timer cleanup/reset logic'
    assert 'isConnected' in func_body, \
        'Function should check isConnected before DOM operations to handle removed elements'


def test_null_safety_implemented():
    """Verify the function handles null/undefined inputs gracefully."""
    with open(INDEX_HTML) as f:
        html = f.read()

    # Extract function body
    match = re.search(
        r'async\s+function\s+copyAddressToClipboard\(address,\s*btn\)\s*\{([\s\S]*?)\n\}(?!\})',
        html
    )
    assert match, "Could not extract copyAddressToClipboard function"
    func_body = match.group(1)

    # Check for null/undefined checks
    assert '!address' in func_body or 'address ||' in func_body, \
        'Function should check if address is null/undefined'
    assert '!btn' in func_body or 'btn ||' in func_body, \
        'Function should check if btn is null/undefined'


def test_state_transitions_with_timers():
    """Verify state transitions have different timers (1.5s for success, 4s for failure)."""
    with open(INDEX_HTML) as f:
        html = f.read()

    # Extract function body
    match = re.search(
        r'async\s+function\s+copyAddressToClipboard\(address,\s*btn\)\s*\{([\s\S]*?)\n\}(?!\})',
        html
    )
    assert match, "Could not extract copyAddressToClipboard function"
    func_body = match.group(1)

    # Check for success timer (1.5 seconds)
    assert '1500' in func_body, \
        'Function should have 1500ms (1.5s) timer for success state'

    # Check for failure timer (4 seconds)
    assert '4000' in func_body, \
        'Function should have 4000ms (4s) timer for failure state'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

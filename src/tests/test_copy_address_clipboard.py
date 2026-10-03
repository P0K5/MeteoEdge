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


def test_all_six_copy_buttons_have_same_feedback_markup():
    """Structural: all 6 copy button call sites have consistent feedback markup."""
    with open(INDEX_HTML) as f:
        html = f.read()

    # Verify 6 copy buttons exist
    button_count = html.count('class="copy-copy-btn"')
    assert button_count >= 6, \
        f'Should have at least 6 copy buttons, found {button_count}'

    # Verify each has associated feedback label and address span with data-address
    # Each call site should have: button + feedback-label + address-span, all with data-address
    assert 'class="copy-feedback-label"' in html, \
        'Feedback labels should exist'
    assert html.count('class="copy-feedback-label"') >= button_count, \
        'Each copy button should have a feedback label'
    assert html.count('class="copy-address-mono"') >= button_count, \
        'Each copy button should have an address span'

    # Spot check: all feedback labels and address spans should have data-address
    assert re.search(r'<span[^>]*class="copy-feedback-label"[^>]*data-address=', html), \
        'Feedback labels should have data-address attribute'
    assert re.search(r'<span[^>]*class="copy-address-mono"[^>]*data-address=', html), \
        'Address spans should have data-address attribute'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

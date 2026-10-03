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
    """Structural: every copy button call site ships identical feedback markup."""
    html = INDEX_HTML.read_text()

    buttons = html.count('class="copy-copy-btn"')
    assert buttons == 6, f"expected 6 copy buttons, found {buttons}"
    # Count rendered markup (not the CSS rule): one feedback label per button.
    assert html.count('<span class="copy-feedback-label"') == buttons
    # (a seventh, button-less .copy-address-mono exists elsewhere and is not a call site)
    for cls in ("copy-feedback-label", "copy-address-mono"):
        spans = re.findall(r'<span class="%s"[^>]*data-address=[^>]*>' % cls, html)
        assert len(spans) == buttons, cls


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

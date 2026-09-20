"""Tests for Copy-Trading dashboard tab shell (issue #1144)."""
import pytest
from pathlib import Path
from html.parser import HTMLParser


class TabButtonParser(HTMLParser):
    """Parser to extract tab buttons and panels from HTML."""

    def __init__(self):
        super().__init__()
        self.tab_buttons = {}
        self.tab_panels = {}
        self.current_tag = None
        self.current_attrs = {}

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "button" and "tab-btn" in attrs_dict.get("class", ""):
            # Extract tab name from onclick="switchTab('tab-name')"
            onclick = attrs_dict.get("onclick", "")
            if "switchTab(" in onclick:
                import re

                match = re.search(r"switchTab\('([^']+)'\)", onclick)
                if match:
                    tab_name = match.group(1)
                    self.tab_buttons[tab_name] = True
        elif tag == "section" and "tab-panel" in attrs_dict.get("class", ""):
            section_id = attrs_dict.get("id", "")
            if section_id.startswith("tab-"):
                tab_name = section_id[4:]  # Remove "tab-" prefix
                self.tab_panels[tab_name] = True


@pytest.fixture
def html_content():
    """Load the dashboard HTML file."""
    html_path = Path(__file__).resolve().parents[2] / "src" / "dashboard" / "static" / "index.html"
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()


def test_copy_trading_tab_button_exists(html_content):
    """Verify that the copy-trading tab button exists in the HTML."""
    parser = TabButtonParser()
    parser.feed(html_content)

    assert "copy-trading" in parser.tab_buttons, (
        "Copy-Trading tab button not found in tab bar"
    )


def test_copy_trading_tab_panel_exists(html_content):
    """Verify that the copy-trading tab panel section exists in the HTML."""
    parser = TabButtonParser()
    parser.feed(html_content)

    assert "copy-trading" in parser.tab_panels, (
        "Copy-Trading tab panel not found in HTML sections"
    )


def test_copy_trading_tab_has_error_banner(html_content):
    """Verify the tab has its own error-banner div, matching every other
    tab's pattern (e.g. #promotion-error-banner) -- required so a future
    story's fetch-failure handling has somewhere to render into without
    adding new markup."""
    assert 'id="copy-trading-error-banner"' in html_content, (
        "Copy-Trading error-banner div not found"
    )
    assert 'class="error-banner" id="copy-trading-error-banner"' in html_content, (
        "Copy-Trading error-banner div missing the error-banner class"
    )


def test_copy_trading_tab_panel_has_empty_content(html_content):
    """Verify that the copy-trading tab panel contains an empty state."""
    # Look for the empty state div within the copy-trading tab
    assert 'id="tab-copy-trading"' in html_content, (
        "Copy-Trading tab panel section ID not found"
    )
    assert 'id="copy-trading-content"' in html_content, (
        "Copy-Trading content container not found"
    )
    # The empty state should be visible initially
    assert '<div class="empty">' in html_content, (
        "Empty state placeholder not found"
    )


def test_copy_trading_tab_matches_existing_tab_structure(html_content):
    """Verify that copy-trading tab follows same structure as existing tabs."""
    # Check for standard tab structure elements
    assert 'class="tab-panel"' in html_content, "Tab-panel class not found"
    assert 'class="section-hdr"' in html_content, "Section header class not found"
    assert 'class="section-title"' in html_content, "Section title class not found"
    # Copy-trading specific markers
    assert 'id="tab-copy-trading"' in html_content, (
        "Copy-Trading tab section not found"
    )
    assert 'class="tab-panel"' in html_content and 'id="tab-copy-trading"' in html_content, (
        "Copy-Trading tab is not marked as a tab-panel"
    )


def test_copy_trading_tab_order(html_content):
    """Verify that copy-trading tab appears after config tab (last position)."""
    config_idx = html_content.find('onclick="switchTab(\'config\')"')
    copy_trading_idx = html_content.find('onclick="switchTab(\'copy-trading\')"')

    assert config_idx != -1, "Config tab button not found"
    assert copy_trading_idx != -1, "Copy-Trading tab button not found"
    assert copy_trading_idx > config_idx, (
        "Copy-Trading tab should appear after Config tab"
    )

"""Tests for the Copy-Trading dashboard tab shell (issue #1144; split into
Wallets / Paper / Live tabs, issue #1275)."""
import re

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


COPY_TABS = ["copy-wallets", "copy-paper", "copy-live"]


def test_old_copy_trading_tab_id_is_gone(html_content):
    """The single 'copy-trading' tab was replaced by three tabs -- no
    leftover button, panel, or switchTab('copy-trading') route."""
    parser = TabButtonParser()
    parser.feed(html_content)
    assert "copy-trading" not in parser.tab_buttons
    assert "copy-trading" not in parser.tab_panels
    assert "switchTab('copy-trading')" not in html_content
    assert 'id="tab-copy-trading"' not in html_content
    assert "tab-btn-copy-trading" not in html_content


def test_three_copy_tab_buttons_and_panels_exist(html_content):
    parser = TabButtonParser()
    parser.feed(html_content)
    for tab in COPY_TABS:
        assert tab in parser.tab_buttons, f"{tab} tab button not found in tab bar"
        assert tab in parser.tab_panels, f"{tab} tab panel not found in HTML sections"


def test_copy_tab_labels(html_content):
    """PM decision (Open Q1): labels are 'Copy · Wallets/Paper/Live'."""
    for tab, label in [
        ("copy-wallets", "Copy · Wallets"),
        ("copy-paper", "Copy · Paper"),
        ("copy-live", "Copy · Live"),
    ]:
        assert re.search(
            rf'<button class="tab-btn" id="tab-btn-{tab}"[^>]*>{label}</button>', html_content
        ), f"{label} tab button missing or mislabelled"


def test_each_copy_tab_has_its_own_error_banner(html_content):
    """Matches every other tab's pattern (e.g. #promotion-error-banner)."""
    for tab in COPY_TABS:
        scope = tab.split("-", 1)[1]
        assert f'class="error-banner" id="copy-{scope}-error-banner"' in html_content, (
            f"{tab} error-banner div missing"
        )
        assert f'id="copy-{scope}-error-text"' in html_content


def test_each_copy_panel_has_content_container(html_content):
    for scope in ("wallets", "paper", "live"):
        assert f'id="copy-{scope}-content"' in html_content
    assert '<div class="empty">' in html_content


def test_copy_panels_are_tab_panels(html_content):
    for tab in COPY_TABS:
        assert re.search(rf'<section id="tab-{tab}" class="tab-panel">', html_content), (
            f"{tab} is not marked as a tab-panel"
        )


def test_copy_tab_order(html_content):
    """Portfolio | Stations | Edge | EMOS | Promotion | Copy · Wallets |
    Copy · Paper | Copy · Live | Config."""
    order = [
        "portfolio", "stations", "edge", "emos", "promotion",
        "copy-wallets", "copy-paper", "copy-live", "config",
    ]
    idx = [html_content.find(f"onclick=\"switchTab('{t}')\"") for t in order]
    assert all(i != -1 for i in idx), f"missing tab button(s): {list(zip(order, idx))}"
    assert idx == sorted(idx), "tab buttons are not in the required order"


def test_dom_ids_are_unique(html_content):
    """The shared posture banner is rendered on three tabs; every id must
    stay unique (class-based component, per-tab-suffixed ids)."""
    static_markup = html_content.split("<script>", 1)[0]
    ids = re.findall(r'\sid="([^"]+)"', static_markup)
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"duplicate DOM ids: {dupes}"

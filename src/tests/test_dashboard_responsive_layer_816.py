"""CSS/markup-contract tests for issue #816 (responsive layer: mobile
breakpoints, collapsing grids, wide-table treatment).

Design spec: docs/design/816-responsive-layer.md. These are static
HTML/CSS-contract tests (repo convention for this class of change, see
test_edge_table_overflow.py / test_dashboard_copy_table_overflow_css.py) --
real-browser viewport measurements are recorded in the PR description.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = REPO_ROOT / "src" / "dashboard" / "static" / "index.html"


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _rule(html: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\{([^}]*)\}", html)
    assert match, f"{selector} CSS rule not found"
    return match.group(1)


# ── Shared .table-wrap class (new, for Stations' tables only) ──────────────

def test_table_wrap_class_scrolls_and_is_positioned():
    html = _html()
    body = _rule(html, ".table-wrap")
    assert "overflow-x:auto" in body
    assert "position:relative" in body


def test_stations_perf_table_uses_table_wrap():
    html = _html()
    assert re.search(
        r'<div class="station-perf table-wrap">\s*<table class="perf-table"',
        html,
    ), "perf-table is not wrapped in .table-wrap"


def test_stations_bias_table_uses_table_wrap():
    html = _html()
    assert re.search(
        r'<div class="table-wrap">\s*<table class="bias-table" role="table" '
        r'aria-label="Bias correction',
        html,
    ), "Stations' bias-table is not wrapped in .table-wrap"


def test_stations_deb_table_uses_table_wrap():
    html = _html()
    assert re.search(
        r'<div class="table-wrap">\s*<table class="deb-table"',
        html,
    ), "deb-table is not wrapped in .table-wrap"


def test_table_wrap_does_not_rename_shipped_classes():
    """Per Tech Lead PM decision on #816: don't rename .edge-table-wrap /
    .promo-table-wrap -- .table-wrap is additive, for new call sites only."""
    html = _html()
    assert ".edge-table-wrap{" in html
    assert ".promo-table-wrap{" in html


# ── #1286-class fix: existing wraps gain position:relative ─────────────────

def test_edge_and_promo_table_wraps_are_positioned():
    html = _html()
    for selector in (".edge-table-wrap", ".promo-table-wrap"):
        body = _rule(html, selector)
        assert "overflow-x:auto" in body
        assert "position:relative" in body, (
            f"{selector} is missing position:relative "
            "(the exact #1286 bug shape: an unpositioned scroll container "
            "lets absolute-positioned descendants widen the page)"
        )


# ── Topbar: icon-only Refresh below 480px ───────────────────────────────────

def test_refresh_button_has_label_span_and_aria_label():
    html = _html()
    assert re.search(
        r'<button class="btn-outline" onclick="refresh\(\)" aria-label="Refresh">'
        r'<i data-lucide="refresh-cw"></i><span class="btn-outline-label">Refresh</span>'
        r'</button>',
        html,
    ), "Refresh button must keep aria-label and wrap its text in .btn-outline-label"


def test_refresh_label_hidden_below_480px():
    html = _html()
    assert re.search(
        r"@media\(max-width:480px\)\{\.btn-outline \.btn-outline-label\{display:none;\}\}",
        html,
    )


# ── Tab bar: AC2 regression guard (issue #816, already-shipped behavior this
#    PR intentionally does not touch -- pinned here so a future change can't
#    silently remove the "no forced scroll of critical nav" guarantee) ──────

def test_tab_bar_scrolls_horizontally_and_keeps_active_tab_visible():
    html = _html()
    body = _rule(html, ".tab-bar")
    assert "overflow-x:auto" in body
    assert re.search(
        r"event\.target\.scrollIntoView\(_copyScrollOpts\(\{behavior:'smooth', "
        r"inline:'nearest', block:'nearest'\}\)\)",
        html,
    ), "switchTab() must keep scrolling the clicked tab into view"


# ── Grids: wallet-row / prob-row ─────────────────────────────────────────────

def test_wallet_row_orphan_card_spans_full_width_at_600px():
    html = _html()
    assert ".wallet-row>.wallet-card:last-child:nth-child(3n){grid-column:1/-1;}" in html


def test_prob_row_has_768px_tablet_step_before_600px_phone_step():
    html = _html()
    assert "@media(max-width:768px){.prob-row{grid-template-columns:1fr 1fr 1fr;}}" in html
    tablet_idx = html.index("@media(max-width:768px){.prob-row")
    phone_idx = html.index("@media(max-width:600px){\n  .prob-row{grid-template-columns:1fr 1fr;}")
    assert tablet_idx < phone_idx


def test_prob_row_orphan_card_spans_full_width_at_600px():
    html = _html()
    assert ".prob-row>.prob-item:last-child:nth-child(5n){grid-column:1/-1;}" in html


# ── Station / EMOS card headers wrap at 600px ───────────────────────────────

def test_station_and_emos_headers_wrap_at_600px():
    html = _html()
    assert re.search(
        r"@media\(max-width:600px\)\{\s*"
        r"\.station-header,\.emos-header\{flex-wrap:wrap;row-gap:var\(--space-2\);\}",
        html,
    )


def test_station_header_right_realigns_when_wrapped():
    html = _html()
    assert ".station-header-right{align-items:flex-start;}" in html


# ── Config rows: defensive wrap at 480px ────────────────────────────────────

def test_config_param_wraps_at_480px():
    html = _html()
    assert "@media(max-width:480px){.config-param{flex-wrap:wrap;row-gap:var(--space-2);}}" in html


# ── Desktop-unchanged guard: no existing unqualified rule touched ──────────

def test_no_new_bare_breakpoint_values_introduced():
    """New rules must use 768px/600px/480px only (design spec's breakpoint
    convention) -- not an arbitrary fourth value."""
    html = _html()
    for marker in (
        ".wallet-row>.wallet-card:last-child:nth-child(3n)",
        ".prob-row>.prob-item:last-child:nth-child(5n)",
        ".btn-outline-label{display:none;}",
        ".config-param{flex-wrap:wrap;row-gap:var(--space-2);}",
    ):
        assert marker in html, f"expected marker not found in index.html: {marker}"
        idx = html.index(marker)
        # walk backwards to the nearest preceding @media(max-width:...) to
        # confirm the rule is scoped to one of the three sanctioned values.
        preceding = html[:idx]
        last_media = re.findall(r"@media\(max-width:(\d+)px\)", preceding)
        assert last_media, f"{marker} has no enclosing @media(max-width:...) guard"
        assert last_media[-1] in {"768", "600", "480"}, (
            f"{marker} is scoped to an unsanctioned breakpoint: {last_media[-1]}px"
        )

"""Tests for copy-live ready roster touch-target fix (issue #1333).

Verifies that the Ready roster applies the same CSS class and structure
as the Live roster to ensure action buttons meet the 44px touch-target
minimum at 375px viewport width.

AC4 test (_followedGoLiveButtonHtml disabled states) uses the established
pattern from test_copy_trading_followed_wallets_js_logic.py: extracts and
executes the actual inline <script> block under Node, calling the real
function with test wallet objects and asserting on the actual returned HTML.
"""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import textwrap

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = REPO_ROOT / "src" / "dashboard" / "static" / "index.html"
NODE = shutil.which("node")

# Minimal DOM/window stubs (same pattern as test_copy_trading_followed_wallets_js_logic.py)
_PRELUDE = textwrap.dedent("""
    function makeStubElement() {
      const el = {
        style: {}, classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
        dataset: {}, children: [], childElementCount: 0,
        addEventListener(){}, removeEventListener(){},
        querySelectorAll(){ return []; }, querySelector(){ return null; },
        appendChild(){}, remove(){}, disabled: false, title: '',
        focus(){}, setAttribute(){}, removeAttribute(){}, closest(){ return null; },
      };
      Object.defineProperty(el, 'innerHTML', { get(){ return this._innerHTML || ''; }, set(v){ this._innerHTML = v; } });
      Object.defineProperty(el, 'textContent', { get(){ return this._textContent || ''; }, set(v){ this._textContent = v; } });
      return el;
    }
    const _elementsById = {};
    global.document = {
      getElementById(id) {
        if (!_elementsById[id]) _elementsById[id] = makeStubElement();
        return _elementsById[id];
      },
      querySelectorAll() { return []; },
      querySelector() { return null; },
      createElement() { return makeStubElement(); },
      documentElement: { getAttribute(){ return 'dark'; }, setAttribute(){} },
      addEventListener() {},
    };
    global.window = { addEventListener() {}, prompt: () => '', confirm: () => true };
    global.localStorage = { getItem() { return null; }, setItem() {} };
    global.fetch = async () => ({ ok: true, json: async () => ({}) });
    global.lucide = { createIcons() {} };
""")


def test_ready_roster_has_copy_table_roster_class():
    """The _followedTableHtml call for 'Wallets ready to go live' must pass
    'copy-table--roster' as the 4th parameter to apply the touch-target CSS."""
    html = INDEX_HTML.read_text(encoding='utf-8')

    # Find the _followedTableHtml call for the ready roster
    ready_call = re.search(
        r"_followedTableHtml\('Wallets ready to go live'.*?\);",
        html,
        re.DOTALL
    )
    assert ready_call, "Could not find _followedTableHtml call for ready roster"
    call_text = ready_call.group(0)
    assert "'copy-table--roster'" in call_text, \
        "Ready roster must pass 'copy-table--roster' class to _followedTableHtml"


def test_ready_row_has_data_label_on_paper_pnl_cell():
    """The Paper P&L cell in _followedReadyRowHtml must have a data-label
    attribute for screen reader accessibility and CSS display rules."""
    html = INDEX_HTML.read_text(encoding='utf-8')

    # Find the _followedReadyRowHtml function
    ready_func = re.search(
        r"function _followedReadyRowHtml\(.*?\{.*?\}(?=\n\nfunction)",
        html,
        re.DOTALL
    )
    assert ready_func, "Could not find _followedReadyRowHtml function"
    func_text = ready_func.group(0)

    # Should have data-label="Paper P&amp;L" on the second td
    assert 'data-label="Paper P&amp;L"' in func_text, \
        "Ready row must have data-label on Paper P&L cell"


def test_min_height_44px_rule_exists_in_css():
    """The CSS block for .copy-table--roster.followed-actions-cell must have
    the 44px min-height rule for action buttons at 600px and below."""
    html = INDEX_HTML.read_text(encoding='utf-8')

    # Find the media query section for copy-table--roster
    media_section = re.search(
        r"@media\(max-width:600px\)\{.*?\.copy-table--roster.*?min-height:44px",
        html,
        re.DOTALL
    )
    assert media_section, \
        "CSS must have min-height:44px for .copy-table--roster buttons in @media(max-width:600px)"


def test_action_buttons_have_followed_actions_cell_class():
    """The action cells in both Live and Ready rosters must use the
    followed-actions-cell class for consistent styling."""
    html = INDEX_HTML.read_text(encoding='utf-8')

    # Count followed-actions-cell class usage
    count = html.count('class="copy-td followed-actions-cell"')
    # Should have at least 2: one for Live row, one for Ready row
    assert count >= 2, \
        f"Expected at least 2 followed-actions-cell instances (Live + Ready), found {count}"


def test_no_regressions_at_600px_and_above():
    """The media query for min-height:44px applies only at max-width:600px,
    so larger viewports should not be affected. Verifies the rule is scoped
    INSIDE the media query, not at root level."""
    html = INDEX_HTML.read_text(encoding='utf-8')

    # Verify the rule exists AND is inside a @media query (search forward from the rule to find @media before it)
    # Find the min-height:44px rule for copy-table--roster
    rule_pattern = r"\.copy-table--roster\s+\.followed-actions-cell\s+\.btn-followed-action\s*\{\s*min-height\s*:\s*44px\s*;\s*\}"
    rule_match = re.search(rule_pattern, html)
    assert rule_match, "The min-height:44px rule for .copy-table--roster .btn-followed-action must exist"

    # Now verify this rule is inside @media(max-width:600px), not at root level
    # Search backward from the rule to find the nearest @media query
    rule_start = rule_match.start()
    before_rule = html[:rule_start]

    # Find the last @media(max-width: before the rule
    last_media = before_rule.rfind("@media(max-width:600px)")
    assert last_media >= 0, "The min-height:44px rule should be inside an @media(max-width:600px) query"

    # Find the closing brace of that media query (count braces after @media)
    media_start = last_media + len("@media(max-width:600px){")
    brace_count = 1
    pos = media_start
    while brace_count > 0 and pos < len(html):
        if html[pos] == "{":
            brace_count += 1
        elif html[pos] == "}":
            brace_count -= 1
        pos += 1
    media_end = pos

    # Verify the rule is between media_start and media_end
    assert last_media < rule_start < media_end, \
        "The min-height:44px rule must be inside the @media(max-width:600px) block, not at root level"


def _extract_inline_script() -> str:
    """Extract the inline <script> block from index.html."""
    html = INDEX_HTML.read_text(encoding='utf-8')
    match = re.search(r"<script>([\s\S]*?)</script>", html)
    assert match, "Could not find the dashboard's inline <script> block"
    return match.group(1)


@pytest.mark.skipif(NODE is None, reason="node is not on PATH in this environment")
def test_disabled_go_live_button_keeps_css_classes(tmp_path):
    """AC4: The disabled 'Go live' button (paused wallet or posture unavailable)
    must still carry btn-followed-action class and disabled attribute to maintain
    the 44px min-height in the copy-table--roster context.

    This test executes the actual _followedGoLiveButtonHtml function under Node
    with real wallet objects, not just scanning source text."""

    # Build test script: prelude + real script + assertions
    test_script = _PRELUDE + "\n" + _extract_inline_script() + "\n" + textwrap.dedent("""
        const assert = require('assert');

        // Test 1: paused wallet renders disabled button with btn-followed-action
        const pausedWallet = {
          address: '0xPausedWallet123456789abcdef0123456789ab',
          status: 'paused',
          live_enabled: false
        };
        let pausedHtml = _followedGoLiveButtonHtml(pausedWallet);
        assert.ok(pausedHtml.includes('disabled'), 'paused wallet should render disabled button');
        assert.ok(pausedHtml.includes('btn-followed-action'), 'paused wallet button must have btn-followed-action class');
        assert.ok(pausedHtml.includes('<button'), 'paused wallet must return a button element');

        // Test 2: posture-unavailable wallet renders disabled button with btn-followed-action
        _copyPostureUnavailable = true;
        const postureWallet = {
          address: '0xPostureUnavail123456789abcdef0123456789ab',
          status: 'active',
          live_enabled: false
        };
        let postureHtml = _followedGoLiveButtonHtml(postureWallet);
        assert.ok(postureHtml.includes('disabled'), 'posture-unavailable wallet should render disabled button');
        assert.ok(postureHtml.includes('btn-followed-action'), 'posture-unavailable button must have btn-followed-action class');
        assert.ok(postureHtml.includes('<button'), 'posture-unavailable must return a button element');

        // Test 3: enabled wallet renders enabled button with btn-followed-action
        _copyPostureUnavailable = false;
        const enabledWallet = {
          address: '0xEnabledWallet12345678 9abcdef0123456789ab',
          status: 'active',
          live_enabled: false
        };
        let enabledHtml = _followedGoLiveButtonHtml(enabledWallet);
        assert.ok(!enabledHtml.includes('disabled'), 'enabled wallet should render enabled button (no disabled attribute)');
        assert.ok(enabledHtml.includes('btn-followed-action'), 'enabled button must also have btn-followed-action class');
        assert.ok(enabledHtml.includes('<button'), 'enabled must return a button element');

        console.log('DISABLED_BUTTON_TEST_PASSED');
    """)

    script_path = tmp_path / "disabled_button_test.js"
    script_path.write_text(test_script, encoding='utf-8')

    result = subprocess.run(
        [NODE, str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, \
        f"_followedGoLiveButtonHtml test failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "DISABLED_BUTTON_TEST_PASSED" in result.stdout, \
        f"Test assertions did not pass:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

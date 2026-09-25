"""Unit tests for the dashboard's tab bar scroll containment (issue #1201).

Tests verify:
1. Copy-Trading tab's <main> element has both 'main' and 'main--wide' classes
   (desktop table widening fix).
2. .tab-bar CSS block contains overflow-x:auto and flex-wrap:nowrap
   (mobile scroll containment fix).
3. switchTab() function calls scrollIntoView() on activated buttons
   (auto-scroll-to-view behavior).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = REPO_ROOT / "src" / "dashboard" / "static" / "index.html"

NODE = shutil.which("node")


def test_copy_trading_tab_main_has_wide_modifier():
    """Verify Copy-Trading tab's <main> element has both 'main' and 'main--wide' classes.

    This is the desktop table-widening fix: the Copy-Trading tab can use
    max-width: 1280px instead of 980px to fit wider tables without clipping.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Find the Copy-Trading tab section and its main element
    # Should match: <section id="tab-copy-trading" ...>...<main class="main main--wide">
    match = re.search(
        r'<section id="tab-copy-trading"[^>]*>.*?<main class="main main--wide">',
        html,
        re.DOTALL
    )
    assert match, (
        "Copy-Trading tab's <main> element must have both 'main' and 'main--wide' classes. "
        "Expected: <main class=\"main main--wide\"> within <section id=\"tab-copy-trading\">"
    )


def test_tab_bar_has_overflow_x_auto():
    """Verify .tab-bar CSS block contains overflow-x:auto for scroll containment.

    This is the mobile fix: the tab bar becomes its own scrollable container
    instead of allowing the whole page to pan.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Find the .tab-bar CSS rule and verify it contains overflow-x:auto
    match = re.search(r'\.tab-bar\{[^}]*\}', html)
    assert match, ".tab-bar CSS rule not found"

    tab_bar_css = match.group(0)
    assert 'overflow-x:auto' in tab_bar_css, (
        ".tab-bar CSS must contain 'overflow-x:auto' for horizontal scroll containment"
    )


def test_tab_bar_has_flex_wrap_nowrap():
    """Verify .tab-bar CSS contains flex-wrap:nowrap to prevent tab wrapping."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Find the .tab-bar CSS rule
    match = re.search(r'\.tab-bar\{[^}]*\}', html)
    assert match, ".tab-bar CSS rule not found"

    tab_bar_css = match.group(0)
    assert 'flex-wrap:nowrap' in tab_bar_css, (
        ".tab-bar CSS must contain 'flex-wrap:nowrap' to keep all tabs in a single row"
    )


def test_switchTab_calls_scrollIntoView():
    """Verify switchTab() function calls scrollIntoView() on the activated button.

    This is the mobile auto-scroll fix: when a tab is activated, it scrolls
    into view within the tab bar strip (not the viewport).
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Find the switchTab function definition
    match = re.search(
        r'function switchTab\(tab\)\s*\{(.*?)(?=\n\s*(?:function|const|let|var|\}(?!\s*\))|$))',
        html,
        re.DOTALL
    )
    assert match, "switchTab function not found"

    switch_tab_body = match.group(1)

    # Verify it calls scrollIntoView somewhere
    assert 'scrollIntoView' in switch_tab_body, (
        "switchTab() must call scrollIntoView() to auto-scroll tabs into view"
    )

    # Verify it's guarded (defensive against test stubs that don't have scrollIntoView)
    assert re.search(
        r'if\s*\(\s*event\.target\.scrollIntoView\s*\)',
        switch_tab_body
    ), (
        "scrollIntoView() call must be guarded with 'if (event.target.scrollIntoView)' "
        "to handle test environments where stubs may not provide the method"
    )


@pytest.mark.skipif(NODE is None, reason="node is not on PATH in this environment")
def test_switchTab_scrollIntoView_behavior(tmp_path):
    """Executes the real dashboard script under Node and verifies scrollIntoView behavior.

    Tests that when switchTab() activates a button, it calls scrollIntoView with
    the correct options (behavior:'smooth', inline:'nearest', block:'nearest').
    """
    _PRELUDE = textwrap.dedent("""
        function makeStubElement() {
          const el = {
            style: {}, classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
            dataset: {}, children: [], childElementCount: 0,
            addEventListener(){}, removeEventListener(){},
            querySelectorAll(){ return []; }, querySelector(){ return null; },
            appendChild(){}, remove(){}, disabled: false, value: '',
            focus(){}, removeAttribute(){}, closest(){ return null; },
            scrollIntoView(){}, click(){},
            _attrs: {}, _computedStyle: {},
            setAttribute(name, value) { this._attrs[name] = String(value); },
            getAttribute(name) { return this._attrs[name] !== undefined ? this._attrs[name] : null; },
          };
          Object.defineProperty(el, 'innerHTML', { get(){ return this._innerHTML || ''; }, set(v){ this._innerHTML = v; } });
          Object.defineProperty(el, 'textContent', { get(){ return this._textContent || ''; }, set(v){ this._textContent = v; } });
          Object.defineProperty(el, 'className', { get(){ return this._className || ''; }, set(v){ this._className = v; } });
          return el;
        }
        const _elementsById = {};
        const _elementsByQuery = {};
        global.document = {
          getElementById(id) {
            if (!_elementsById[id]) _elementsById[id] = makeStubElement();
            return _elementsById[id];
          },
          querySelectorAll(query) {
            if (query === '.tab-bar') {
              if (!_elementsByQuery['.tab-bar']) {
                _elementsByQuery['.tab-bar'] = [makeStubElement()];
              }
              return _elementsByQuery['.tab-bar'];
            }
            if (query === '.tab-panel' || query === '.tab-btn') {
              if (!_elementsByQuery[query]) {
                _elementsByQuery[query] = [makeStubElement(), makeStubElement()];
              }
              return _elementsByQuery[query];
            }
            return [];
          },
          querySelector() { return null; },
          createElement() { return makeStubElement(); },
          documentElement: { getAttribute(){ return 'dark'; }, setAttribute(){} },
          addEventListener() {},
        };
        global.window = { addEventListener() {}, prompt: () => '', confirm: () => true };
        global.localStorage = { getItem() { return null; }, setItem() {} };
        global.fetch = async () => ({ ok: true, json: async () => ({}) });
        global.lucide = { createIcons() {} };
        global.Chart = function () {};
        global.getComputedStyle = () => ({ getPropertyValue: () => '' });
        global.event = { target: makeStubElement() };
        global.navigator = { clipboard: { writeText: async () => {} } };
    """)

    _ASSERTIONS = textwrap.dedent("""
        const assert = require('assert');

        (async () => {
          const tabBtn = document.querySelectorAll('.tab-btn')[0];

          let scrollIntoViewCalled = false;
          let scrollIntoViewOptions = null;
          tabBtn.scrollIntoView = function(options) {
            scrollIntoViewCalled = true;
            scrollIntoViewOptions = options;
          };

          // ------------------------------------------------------------------
          // Test: switchTab() calls scrollIntoView on the activated button
          // with the correct options
          // ------------------------------------------------------------------
          currentTab = 'portfolio';
          event.target = tabBtn;
          document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
          document.querySelectorAll('.tab-panel').forEach(panel => panel.classList.remove('active'));

          switchTab('copy-trading');
          assert.ok(scrollIntoViewCalled, 'switchTab() must call scrollIntoView on the activated button');
          assert.deepStrictEqual(scrollIntoViewOptions,
            {behavior:'smooth', inline:'nearest', block:'nearest'},
            'scrollIntoView() must be called with behavior:smooth, inline:nearest, block:nearest'
          );

          console.log('ALL_SWITCHTAB_SCROLLINTOVIEW_ASSERTIONS_PASSED');
          process.exit(0);
        })().catch((err) => {
          console.error(err);
          process.exit(1);
        });
    """)

    html = INDEX_HTML.read_text(encoding="utf-8")
    match = re.search(r"<script>([\s\S]*?)</script>", html)
    assert match, "Could not find the dashboard's inline <script> block"
    inline_script = match.group(1)

    combined = _PRELUDE + "\n" + inline_script + "\n" + _ASSERTIONS
    script_path = tmp_path / "switchtab_scrollintoview_check.js"
    script_path.write_text(combined, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "ALL_SWITCHTAB_SCROLLINTOVIEW_ASSERTIONS_PASSED" in result.stdout

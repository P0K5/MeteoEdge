"""Unit tests for the Copy-Trading dashboard's global live/paper posture
banner client-side logic (epic J #1161, issue #1185; shared across the three
copy tabs, issue #1275).

Same technique as test_copy_trading_activity_feed_js_logic.py /
test_edge_tab_js_logic.py (issue #758): this repo has no JS test framework,
so this extracts the *actual* inline <script> block shipped in
src/dashboard/static/index.html and executes it under plain Node, with
minimal DOM/fetch/window stubs.

Covers the issue's explicit test requirements:
- Banner reflects ON/OFF state correctly on load.
- Banner updates on config change (via its own dedicated 30s poll).
- Both badge CSS variants render correctly and both carry an aria-label.
- Conservative default: a failed config fetch must never claim LIVE.
- (#1275) ONE fetch updates EVERY banner instance (one per copy tab).

The per-tab refetch-on-entry / 30 s interval wiring (PR #1192 review
finding) is covered against a faithful DOM in
test_dashboard_copy_tab_controller_behavior.py.
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

# Minimal browser-global stubs, mirroring
# test_copy_trading_activity_feed_js_logic.py's _PRELUDE. setAttribute/
# getAttribute/className/textContent are all backed by real state (not
# no-ops) since this test asserts on the banner's rendered class, text,
# and aria-label.
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
        _attrs: {},
        setAttribute(name, value) { this._attrs[name] = String(value); },
        getAttribute(name) { return this._attrs[name] !== undefined ? this._attrs[name] : null; },
      };
      Object.defineProperty(el, 'innerHTML', { get(){ return this._innerHTML || ''; }, set(v){ this._innerHTML = v; } });
      Object.defineProperty(el, 'textContent', { get(){ return this._textContent || ''; }, set(v){ this._textContent = v; } });
      Object.defineProperty(el, 'className', { get(){ return this._className || ''; }, set(v){ this._className = v; } });
      return el;
    }
    const _elementsById = {};
    // One shared posture banner per copy tab (class-based component).
    const _banners = ['copy-wallets', 'copy-paper', 'copy-live'].map(t => {
      const el = makeStubElement();
      el.className = 'mode-badge mode-badge-paper copy-trading-mode-banner';
      _elementsById[t + '-mode-banner'] = el;
      return el;
    });
    global.document = {
      getElementById(id) {
        if (!_elementsById[id]) _elementsById[id] = makeStubElement();
        return _elementsById[id];
      },
      querySelectorAll(sel) { return sel === '.copy-trading-mode-banner' ? _banners : []; },
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

    function configResp(liveEnabled) {
      return {
        ok: true,
        json: async () => ({
          copy_trading: {
            COPY_LIVE_TRADING_ENABLED: { value: liveEnabled, type: 'bool', description: 'x' },
          },
        }),
      };
    }

    // Every instance must always read the same state.
    function assertAllBanners(cls, text, aria, msg = '') {
      const banners = ['copy-wallets', 'copy-paper', 'copy-live']
        .map(t => document.getElementById(t + '-mode-banner'));
      assert.strictEqual(banners.length, 3);
      for (const b of banners) {
        assert.strictEqual(b.className, cls + ' copy-trading-mode-banner', msg);
        assert.strictEqual(b.textContent, text, msg);
        assert.strictEqual(b.getAttribute('aria-label'), aria, msg);
      }
    }
    const LIVE = ['mode-badge mode-badge-live', 'LIVE TRADING ON', 'Live trading is on'];
    const OFF = ['mode-badge mode-badge-paper', 'LIVE TRADING OFF — paper only', 'Live trading is off — paper only'];

    (async () => {
      // ------------------------------------------------------------------
      // 1. Banner reflects the ON state correctly on load -- on all three
      //    instances, from ONE fetch.
      // ------------------------------------------------------------------
      let configFetches = 0;
      global.fetch = async (url) => {
        assert.strictEqual(url, '/api/config');
        configFetches++;
        return configResp(true);
      };
      await fetchCopyTradingModePosture();
      assert.strictEqual(configFetches, 1, 'one fetch must feed every banner instance');
      assertAllBanners(...LIVE);

      // ------------------------------------------------------------------
      // 2. Banner reflects the OFF state correctly on load, including the
      //    literal "paper only" words (a statement about current
      //    behavior, not just an inert switch position).
      // ------------------------------------------------------------------
      _copyInvalidateShared();  // a later poll, past the short cache TTL
      global.fetch = async () => configResp(false);
      await fetchCopyTradingModePosture();
      assertAllBanners(...OFF);

      // ------------------------------------------------------------------
      // 3. Banner updates on config change: a later poll picking up a
      //    flipped switch must re-render every instance.
      // ------------------------------------------------------------------
      _copyInvalidateShared();
      global.fetch = async () => configResp(true);
      await fetchCopyTradingModePosture();
      assertAllBanners(...LIVE);

      _copyInvalidateShared();
      global.fetch = async () => configResp(false);
      await fetchCopyTradingModePosture();
      assertAllBanners(...OFF);

      // ------------------------------------------------------------------
      // 4. Conservative default: a failed fetch must never claim LIVE,
      //    even if the banners were previously showing ON (mirrors the
      //    execution_mode "assume paper" fallback rule verbatim).
      // ------------------------------------------------------------------
      _copyInvalidateShared();
      global.fetch = async () => configResp(true);
      await fetchCopyTradingModePosture();
      assertAllBanners(...LIVE);

      _copyInvalidateShared();
      global.fetch = async () => { throw new Error('network down'); };
      await fetchCopyTradingModePosture();
      // Issue #1290: unknown is its own neutral state -- never LIVE, never a
      // guessed "off"/paper.
      for (const t of ['copy-wallets', 'copy-paper', 'copy-live']) {
        const b = document.getElementById(t + '-mode-banner');
        assert.strictEqual(b.className, 'mode-badge mode-badge-unknown copy-trading-mode-banner', 'a failed fetch must never leave/claim the LIVE state');
        assert.ok(b.innerHTML.includes('Live status unavailable'));
        assert.ok(!/off|paper/i.test(b.innerHTML.replace(/<[^>]*>/g, '')));
        assert.ok(b.getAttribute('aria-label').startsWith('Live status unavailable'));
      }

      // ------------------------------------------------------------------
      // 5. A missing/malformed config payload (e.g. key absent) also
      //    degrades to the conservative paper/off default, never throws.
      // ------------------------------------------------------------------
      _copyInvalidateShared();
      global.fetch = async () => ({ ok: true, json: async () => ({}) });
      await fetchCopyTradingModePosture();
      assertAllBanners(...OFF);

      // ------------------------------------------------------------------
      // 6. Both variants, exercised directly via the render helper, always
      //    carry a full-sentence aria-label (never color/class only).
      // ------------------------------------------------------------------
      renderCopyTradingModePosture(true);
      assertAllBanners(...LIVE);
      renderCopyTradingModePosture(false);
      assertAllBanners(...OFF);

      // ------------------------------------------------------------------
      // 7. A config change made in the Config tab invalidates the cached
      //    posture read: the next fetch is fresh, not served from the TTL.
      // ------------------------------------------------------------------
      configFetches = 0;
      _copyInvalidateShared();
      global.fetch = async () => { configFetches++; return configResp(true); };
      await fetchCopyTradingModePosture();
      await fetchCopyTradingModePosture();   // within TTL -> cached
      assert.strictEqual(configFetches, 1);
      const realFetch = global.fetch;
      global.fetch = async (url, opts) => {
        if (opts && opts.method === 'PATCH') return { ok: true, json: async () => ({ value: false }) };
        return realFetch(url, opts);
      };
      await _patchConfig('COPY_LIVE_TRADING_ENABLED', false);   // the Config tab saves
      await fetchCopyTradingModePosture();   // -> fresh, not the cached read
      assert.strictEqual(configFetches, 2);

      console.log('ALL_MODE_BANNER_JS_ASSERTIONS_PASSED');
    })().catch((err) => {
      console.error(err);
      process.exit(1);
    });
""")


def _extract_inline_script() -> str:
    html = INDEX_HTML.read_text(encoding="utf-8")
    match = re.search(r"<script>([\s\S]*?)</script>", html)
    assert match, "Could not find the dashboard's inline <script> block"
    return match.group(1)


@pytest.mark.skipif(NODE is None, reason="node is not on PATH in this environment")
def test_mode_banner_reflects_config_state_and_degrades_conservatively(tmp_path):
    """Executes the real shipped dashboard script under Node and exercises
    the global live/paper posture banner's fetch/render logic."""
    combined = _PRELUDE + "\n" + _extract_inline_script() + "\n" + _ASSERTIONS
    script_path = tmp_path / "mode_banner_logic_check.js"
    script_path.write_text(combined, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "ALL_MODE_BANNER_JS_ASSERTIONS_PASSED" in result.stdout

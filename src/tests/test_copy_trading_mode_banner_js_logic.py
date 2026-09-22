"""Unit tests for the Copy-Trading dashboard's global live/paper posture
banner client-side logic (epic J #1161, issue #1185).

Same technique as test_copy_trading_activity_feed_js_logic.py /
test_edge_tab_js_logic.py (issue #758): this repo has no JS test framework,
so this extracts the *actual* inline <script> block shipped in
src/dashboard/static/index.html and executes it under plain Node, with
minimal DOM/fetch/window stubs.

Covers the issue's explicit test requirements:
- Banner reflects ON/OFF state correctly on load.
- Banner updates on config change (via its own dedicated 30s poll --
  see PR #1192 review below).
- Both badge CSS variants render correctly and both carry an aria-label.
- Conservative default: a failed config fetch must never claim LIVE.

Also covers a PR #1192 review finding (Designer change request): the
banner must refetch on every tab re-entry, not just the tab's very first
activation. The original wiring only called
fetchCopyTradingModePosture() inside switchTab()'s `if (!copyTradingLoaded)`
first-activation guard, sharing the four-view group's 5-minute interval --
so a revisit to the Copy-Trading tab after the first one relied on that
interval's next tick, meaning a stale live/paper read could sit on screen
for up to 5 minutes on every single tab revisit (e.g. right after
halt_live_copy_trading.py runs mid-incident and the operator tabs away
and back), not just during one long viewing session. Fixed by giving the
banner its own dedicated `copyTradingModeIntervalId` (30s, decoupled from
the 300s view-data group) and fetching it unconditionally on every
`tab === 'copy-trading'` entry, mirroring the existing
fetchPortfolio()/fetchBotLog() pattern.
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

    (async () => {
      const banner = document.getElementById('copy-trading-mode-banner');

      // ------------------------------------------------------------------
      // 1. Banner reflects the ON state correctly on load.
      // ------------------------------------------------------------------
      global.fetch = async (url) => {
        assert.strictEqual(url, '/api/config');
        return configResp(true);
      };
      await fetchCopyTradingModePosture();
      assert.strictEqual(banner.className, 'mode-badge mode-badge-live');
      assert.strictEqual(banner.textContent, 'LIVE TRADING ON');
      assert.strictEqual(banner.getAttribute('aria-label'), 'Live trading is on');

      // ------------------------------------------------------------------
      // 2. Banner reflects the OFF state correctly on load, including the
      //    literal "paper only" words (a statement about current
      //    behavior, not just an inert switch position).
      // ------------------------------------------------------------------
      global.fetch = async () => configResp(false);
      await fetchCopyTradingModePosture();
      assert.strictEqual(banner.className, 'mode-badge mode-badge-paper');
      assert.strictEqual(banner.textContent, 'LIVE TRADING OFF — paper only');
      assert.strictEqual(banner.getAttribute('aria-label'), 'Live trading is off — paper only');

      // ------------------------------------------------------------------
      // 3. Banner updates on config change: a later poll picking up a
      //    flipped switch must re-render, matching how the rest of the
      //    Copy-Trading tab refreshes every 5 minutes.
      // ------------------------------------------------------------------
      global.fetch = async () => configResp(true);
      await fetchCopyTradingModePosture();
      assert.strictEqual(banner.className, 'mode-badge mode-badge-live');
      assert.strictEqual(banner.textContent, 'LIVE TRADING ON');

      global.fetch = async () => configResp(false);
      await fetchCopyTradingModePosture();
      assert.strictEqual(banner.className, 'mode-badge mode-badge-paper');
      assert.strictEqual(banner.textContent, 'LIVE TRADING OFF — paper only');

      // ------------------------------------------------------------------
      // 4. Conservative default: a failed fetch must never claim LIVE,
      //    even if the banner was previously showing ON (mirrors the
      //    execution_mode "assume paper" fallback rule verbatim).
      // ------------------------------------------------------------------
      global.fetch = async () => configResp(true);
      await fetchCopyTradingModePosture();
      assert.strictEqual(banner.className, 'mode-badge mode-badge-live');

      global.fetch = async () => { throw new Error('network down'); };
      await fetchCopyTradingModePosture();
      assert.strictEqual(banner.className, 'mode-badge mode-badge-paper', 'a failed fetch must never leave/claim the LIVE state');
      assert.strictEqual(banner.textContent, 'LIVE TRADING OFF — paper only');
      assert.strictEqual(banner.getAttribute('aria-label'), 'Live trading is off — paper only');

      // ------------------------------------------------------------------
      // 5. A missing/malformed config payload (e.g. key absent) also
      //    degrades to the conservative paper/off default, never throws.
      // ------------------------------------------------------------------
      global.fetch = async () => ({ ok: true, json: async () => ({}) });
      await fetchCopyTradingModePosture();
      assert.strictEqual(banner.className, 'mode-badge mode-badge-paper');

      // ------------------------------------------------------------------
      // 6. Both variants, exercised directly via the render helper, always
      //    carry a full-sentence aria-label (never color/class only).
      // ------------------------------------------------------------------
      renderCopyTradingModePosture(true);
      assert.ok(banner.getAttribute('aria-label').length > 5);
      renderCopyTradingModePosture(false);
      assert.ok(banner.getAttribute('aria-label').length > 5);

      // ------------------------------------------------------------------
      // 7. The posture banner must refetch on EVERY tab re-entry (not
      //    just the very first activation), on its own dedicated 30s
      //    interval decoupled from the four-view 5-minute poll group.
      //    PR #1192 review finding: the original wiring only called
      //    fetchCopyTradingModePosture() inside the `if (!copyTradingLoaded)`
      //    first-activation guard, so a revisit to the tab relied on the
      //    5-minute interval's first tick -- meaning a stale live/paper
      //    read (e.g. right after halt_live_copy_trading.py runs
      //    mid-incident) could persist on screen for up to 5 minutes
      //    every time an operator tabs away and back, not just once.
      // ------------------------------------------------------------------
      let postureFetchCount = 0;
      fetchCopyTradingModePosture = async () => { postureFetchCount++; };
      fetchCopyTradingCandidates = async () => {};
      fetchFollowedWallets = async () => {};
      fetchCopyTradingPositions = async () => {};
      fetchCopyTradingActivityFeed = async () => {};

      const setIntervalCalls = [];
      const clearIntervalCalls = [];
      let fakeIntervalId = 0;
      global.setInterval = (fn, delay) => { setIntervalCalls.push({ fn, delay }); return ++fakeIntervalId; };
      global.clearInterval = (id) => { clearIntervalCalls.push(id); };

      currentTab = 'portfolio';
      copyTradingLoaded = false;
      copyTradingIntervalId = null;
      copyTradingModeIntervalId = null;

      // First entry: immediate fetch (called once from inside the
      // first-activation guard, and once more from the unconditional
      // call right after -- both are intentional, see index.html), plus
      // both a 30s posture interval and the separate shared 5-minute
      // view-data interval.
      switchTab('copy-trading');
      assert.ok(postureFetchCount >= 1, 'first tab entry must fetch the posture banner immediately');
      assert.ok(setIntervalCalls.some(c => c.delay === 30_000), 'posture banner must get its own 30s interval');
      const fiveMinCalls = setIntervalCalls.filter(c => c.delay === 300_000);
      assert.strictEqual(fiveMinCalls.length, 1, 'the four-view group must stay a single shared 5-minute interval, not per-fetch');

      // Leave the tab (an unrelated tab, to avoid exercising unrelated
      // lazy-load branches) -- both Copy-Trading intervals must be torn
      // down, same as every other tab's teardown block.
      const clearedBeforeLeaving = clearIntervalCalls.length;
      switchTab('other-unrelated-tab');
      assert.ok(clearIntervalCalls.length >= clearedBeforeLeaving + 2, 'leaving the tab must clear both the posture and the view-data intervals');

      // Re-enter: THE bug this fixes. copyTradingLoaded is already true
      // at this point, so the old code silently skipped
      // fetchCopyTradingModePosture() on this second entry entirely,
      // leaving the banner stale until the next 5-minute poll tick.
      postureFetchCount = 0;
      setIntervalCalls.length = 0;
      switchTab('copy-trading');
      assert.strictEqual(postureFetchCount, 1, 'the posture banner must refetch immediately on every tab re-entry, not just the first');
      assert.ok(setIntervalCalls.some(c => c.delay === 30_000), 'a fresh 30s posture interval must be created on re-entry too');

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

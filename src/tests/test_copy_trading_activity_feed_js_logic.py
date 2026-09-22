"""Unit tests for the Copy-Trading dashboard Activity Feed view's
client-side logic (epic F #1143, story F4 #1149; live events + mode
filter, epic J #1161, issue #1188).

Same technique as test_copy_trading_followed_wallets_js_logic.py /
test_edge_tab_js_logic.py (issue #758): this repo has no JS test framework,
so this extracts the *actual* inline <script> block shipped in
src/dashboard/static/index.html and executes it under plain Node, with
minimal DOM/fetch/window stubs.

Covers the acceptance criteria's explicitly-called-out, separately-tested
requirement: "stale-feed indicator appears on a simulated poll failure" --
plus the merged-feed rendering, wallet/event-type/mode filters (client-side,
against the cached payload), click-through cross-navigation, the per-row
LIVE/PAPER mode badge + left-border accent, and the Mode=Live empty states.
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
# test_copy_trading_followed_wallets_js_logic.py's _PRELUDE.
_PRELUDE = textwrap.dedent("""
    function makeStubElement() {
      const el = {
        style: {}, classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
        dataset: {}, children: [], childElementCount: 0,
        addEventListener(){}, removeEventListener(){},
        querySelectorAll(){ return []; }, querySelector(){ return null; },
        appendChild(){}, remove(){}, disabled: false, title: '', value: '',
        focus(){}, setAttribute(){}, removeAttribute(){}, closest(){ return null; },
        scrollIntoView(){ this._scrolledIntoView = true; }, click(){},
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
    global.Chart = function () {};
    global.getComputedStyle = () => ({ getPropertyValue: () => '' });
    global.event = { target: makeStubElement() };
    global.navigator = { clipboard: { writeText: async () => {} } };
""")

_ASSERTIONS = textwrap.dedent("""
    const assert = require('assert');

    function jsonResp(body, ok = true) { return { ok, json: async () => body }; }

    const eventsFixture = [
      { event_type: 'wallet_paused', ts: '2026-09-03T00:00:00Z', address: '0xW2',
        paused_reason: 'unstable', market: null, signal_id: null },
      { event_type: 'order_skipped', ts: '2026-09-02T00:00:00Z', address: '0xW1',
        market: 'M1', skip_reason: 'market_resolved', signal_id: 5 },
      { event_type: 'order_placed', ts: '2026-09-01T00:00:00Z', address: '0xW1',
        market: 'M1', fill_price: 0.42, size_usd: 5.0, signal_id: 4 },
    ];

    (async () => {
      // ------------------------------------------------------------------
      // 1. A successful fetch renders every event and clears the stale
      //    banner.
      // ------------------------------------------------------------------
      global.fetch = async () => jsonResp({ events: eventsFixture });
      const banner = document.getElementById('copy-activity-stale-banner');
      let bannerRemoveCalled = false;
      banner.classList.remove = (cls) => { if (cls === 'visible') bannerRemoveCalled = true; };
      let bannerAddCalled = false;
      banner.classList.add = (cls) => { if (cls === 'visible') bannerAddCalled = true; };

      await fetchCopyTradingActivityFeed();
      assert.strictEqual(bannerRemoveCalled, true, 'a successful fetch must clear the stale banner');
      assert.strictEqual(bannerAddCalled, false, 'a successful fetch must never show the stale banner');
      assert.deepStrictEqual(_copyActivityData.events, eventsFixture);

      const listWrap = document.getElementById('copy-activity-list');
      assert.ok(listWrap.innerHTML.includes('Order placed'), 'order_placed event must render');
      assert.ok(listWrap.innerHTML.includes('Order skipped'), 'order_skipped event must render');
      assert.ok(listWrap.innerHTML.includes('Wallet paused'), 'wallet_paused event must render');
      assert.ok(listWrap.innerHTML.includes('market_resolved'), 'skip_reason must render');
      assert.ok(listWrap.innerHTML.includes('unstable'), 'paused_reason must render');

      // ------------------------------------------------------------------
      // 2. Stale-feed indicator: a poll failure AFTER data already loaded
      //    must show the banner (with last-known data still on screen),
      //    never silently go stale with no indication (acceptance
      //    criteria, issue #1149).
      // ------------------------------------------------------------------
      bannerAddCalled = false;
      bannerRemoveCalled = false;
      const textEl = document.getElementById('copy-activity-stale-text');
      const priorListHtml = listWrap.innerHTML;

      global.fetch = async () => { throw new Error('network down'); };
      await fetchCopyTradingActivityFeed();

      assert.strictEqual(bannerAddCalled, true, 'a poll failure with existing data must show the stale banner');
      assert.strictEqual(bannerRemoveCalled, false, 'a poll failure must not clear the stale banner');
      assert.ok(textEl.textContent.includes('network down'), 'stale banner text must include the failure reason');
      assert.ok(textEl.textContent.includes('retrying in 5 min'), 'stale banner text must say it will retry');
      assert.strictEqual(listWrap.innerHTML, priorListHtml, 'last-known-good events must stay on screen during a stale poll');

      // ------------------------------------------------------------------
      // 3. A subsequent successful fetch clears the stale banner again.
      // ------------------------------------------------------------------
      bannerRemoveCalled = false;
      global.fetch = async () => jsonResp({ events: eventsFixture });
      await fetchCopyTradingActivityFeed();
      assert.strictEqual(bannerRemoveCalled, true, 'recovering must clear the stale banner');

      // ------------------------------------------------------------------
      // 4. First-ever fetch failure (no prior data): the list itself shows
      //    an error state, not a blank screen.
      // ------------------------------------------------------------------
      _copyActivityData = null;
      global.fetch = async () => { throw new Error('boom'); };
      await fetchCopyTradingActivityFeed();
      assert.ok(listWrap.innerHTML.includes('Could not load activity feed'), 'first-load failure must show an explicit error state');
      assert.ok(listWrap.innerHTML.includes('boom'), 'first-load failure must include the underlying error message');

      // ------------------------------------------------------------------
      // 5. Empty feed renders the "No activity yet" empty state.
      // ------------------------------------------------------------------
      global.fetch = async () => jsonResp({ events: [] });
      await fetchCopyTradingActivityFeed();
      assert.ok(listWrap.innerHTML.includes('No activity yet'), 'an empty feed must show the empty state');

      // ------------------------------------------------------------------
      // 6. Wallet and event-type filters apply client-side against the
      //    cached payload (no re-fetch), mirroring the Positions view's
      //    date-range filter.
      // ------------------------------------------------------------------
      let refetchCount = 0;
      global.fetch = async () => { refetchCount += 1; return jsonResp({ events: eventsFixture }); };
      await fetchCopyTradingActivityFeed();
      assert.strictEqual(refetchCount, 1);

      _copyActivityOnWalletFilterChange({ target: { value: '0xW1' } });
      assert.strictEqual(refetchCount, 1, 'changing the wallet filter must not trigger a network re-fetch');
      assert.ok(!listWrap.innerHTML.includes('Wallet paused'), 'wallet filter must exclude events for other wallets');
      assert.ok(listWrap.innerHTML.includes('Order placed') && listWrap.innerHTML.includes('Order skipped'));

      _copyActivityOnWalletFilterChange({ target: { value: '' } });
      _copyActivityOnEventTypeFilterChange({ target: { value: 'wallet_paused' } });
      assert.strictEqual(refetchCount, 1, 'changing the event-type filter must not trigger a network re-fetch');
      assert.ok(listWrap.innerHTML.includes('Wallet paused'));
      assert.ok(!listWrap.innerHTML.includes('Order placed') && !listWrap.innerHTML.includes('Order skipped'));

      // A filter combination matching nothing shows the "no matching
      // activity" state, not a blank screen or the true-empty message.
      _copyActivityOnWalletFilterChange({ target: { value: '0xNoSuchWallet' } });
      assert.ok(listWrap.innerHTML.includes('No matching activity'));
      assert.ok(!listWrap.innerHTML.includes('No activity yet'));

      // Reset filters for the click-through checks below.
      _copyActivityOnWalletFilterChange({ target: { value: '' } });
      _copyActivityOnEventTypeFilterChange({ target: { value: '' } });

      // ------------------------------------------------------------------
      // 7. Click-through cross-navigation: a signal event scrolls to +
      //    expands the Candidates row; a pause event scrolls to the
      //    Followed Wallets row. Both route through the tab button's
      //    switchTab() handler when not already on the copy-trading tab.
      // ------------------------------------------------------------------
      currentTab = 'portfolio';
      const tabBtn = document.getElementById('tab-btn-copy-trading');
      let tabBtnClicked = false;
      tabBtn.click = () => { tabBtnClicked = true; };

      const candidateRow = document.getElementById('copy-row-' + _copySafeId('0xW1'));
      let candidateScrolled = false;
      candidateRow.scrollIntoView = () => { candidateScrolled = true; };
      // toggleCandidateDetail() itself is exercised by its own call path
      // (it in turn calls _renderCandidateDetail(), a no-op-safe fetch
      // against the stubbed fetch above) -- this checks the jump
      // function's own observable DOM effect (the scroll), not
      // toggleCandidateDetail()'s internals.
      _copyExpanded = new Set();
      _copyActivityJumpToWallet('signal', '0xW1');
      assert.strictEqual(tabBtnClicked, true, 'jumping while on another tab must click the copy-trading tab button');
      assert.strictEqual(candidateScrolled, true, 'a signal event must scroll to its Candidates row');

      currentTab = 'copy-trading';
      tabBtnClicked = false;
      const followedRow = document.getElementById('followed-row-' + _copySafeId('0xW2'));
      let followedScrolled = false;
      followedRow.scrollIntoView = () => { followedScrolled = true; };
      _copyActivityJumpToWallet('pause', '0xW2');
      assert.strictEqual(tabBtnClicked, false, 'already being on the copy-trading tab must not re-click the tab button');
      assert.strictEqual(followedScrolled, true, 'a pause event must scroll to its Followed Wallets row');

      // ------------------------------------------------------------------
      // 8. issue #1188: events with no 'mode' field (older/unmapped data)
      //    default to the PAPER badge + border, never LIVE -- "default to
      //    PAPER when a mode can't be determined" acceptance criterion.
      //    The eventsFixture above has no 'mode' key on any event.
      // ------------------------------------------------------------------
      global.fetch = async () => jsonResp({ events: eventsFixture });
      await fetchCopyTradingActivityFeed();
      assert.ok(listWrap.innerHTML.includes('mode-badge-paper'), 'a missing mode must default to the paper badge');
      assert.ok(listWrap.innerHTML.includes('copy-activity-item-paper'), 'a missing mode must default to the paper left-border accent');
      assert.ok(!listWrap.innerHTML.includes('mode-badge-live'), 'a missing mode must never render as LIVE');
      assert.ok(listWrap.innerHTML.includes('PAPER'), 'the PAPER badge text must be visible');
      assert.ok(listWrap.innerHTML.includes('Signal detected'), '"Signal detected" badge replaces the old per-outcome badge text');
      assert.ok(listWrap.innerHTML.includes('Order placed (paper)'), 'the description text must independently state the mode, not just the badge');
      assert.ok(listWrap.innerHTML.includes('Order skipped (paper)'));
      assert.ok(listWrap.innerHTML.includes('Wallet auto-paused'));

      // ------------------------------------------------------------------
      // 9. Live events: mode badge, left-border accent, Mode filter, and
      //    live-specific badge/description text (issue #1188).
      // ------------------------------------------------------------------
      const liveAndPaperFixture = [
        { event_type: 'live_order_rejected', ts: '2026-09-06T00:00:00Z', address: '0xW1', mode: 'live',
          market: 'M2', rejected_reason: 'no fill before the timeout window closed', signal_id: 9 },
        { event_type: 'live_circuit_breaker_tripped', ts: '2026-09-05T12:00:00Z', address: '0xW1', mode: 'live',
          market: 'M2', skip_reason: 'the live daily loss limit was reached', signal_id: 8 },
        { event_type: 'live_position_settled', ts: '2026-09-05T00:00:00Z', address: '0xW1', mode: 'live',
          market: 'M2', settled_pnl_usd: -1.5, signal_id: 7 },
        { event_type: 'order_placed', ts: '2026-09-01T00:00:00Z', address: '0xW1', mode: 'paper',
          market: 'M1', fill_price: 0.42, size_usd: 5.0, signal_id: 4 },
      ];
      global.fetch = async () => jsonResp({ events: liveAndPaperFixture });
      await fetchCopyTradingActivityFeed();

      assert.ok(listWrap.innerHTML.includes('mode-badge-live'), 'a live event must render the LIVE badge');
      assert.ok(listWrap.innerHTML.includes('copy-activity-item-live'), 'a live event must render the live left-border accent');
      assert.ok(listWrap.innerHTML.includes('Live order rejected'));
      assert.ok(listWrap.innerHTML.includes('no fill before the timeout window closed'), 'plain-language rejected_reason must render, not a raw constant');
      assert.ok(listWrap.innerHTML.includes('Live circuit breaker tripped'));
      assert.ok(listWrap.innerHTML.includes('the live daily loss limit was reached'));
      assert.ok(listWrap.innerHTML.includes('Live position settled'));
      assert.ok(listWrap.innerHTML.includes('-$1.50') || listWrap.innerHTML.includes('copy-pnl-neg'), 'a negative settled P&L must render with the negative styling/sign');

      // Mode filter: client-side, no re-fetch.
      let liveRefetchCount = 0;
      global.fetch = async () => { liveRefetchCount += 1; return jsonResp({ events: liveAndPaperFixture }); };
      await fetchCopyTradingActivityFeed();
      assert.strictEqual(liveRefetchCount, 1);

      _copyActivityOnModeFilterChange({ target: { value: 'live' } });
      assert.strictEqual(liveRefetchCount, 1, 'changing the mode filter must not trigger a network re-fetch');
      assert.ok(!listWrap.innerHTML.includes('mode-badge-paper'), 'Mode=Live must exclude paper events');
      assert.ok(listWrap.innerHTML.includes('mode-badge-live'));

      _copyActivityOnModeFilterChange({ target: { value: 'paper' } });
      assert.ok(!listWrap.innerHTML.includes('mode-badge-live'), 'Mode=Paper must exclude live events');
      assert.ok(listWrap.innerHTML.includes('mode-badge-paper'));

      _copyActivityOnModeFilterChange({ target: { value: '' } });
      assert.ok(listWrap.innerHTML.includes('mode-badge-live') && listWrap.innerHTML.includes('mode-badge-paper'), 'Mode=All must include both');

      // ------------------------------------------------------------------
      // 10. Mode=Live empty states (issue #1188): distinguish "live has
      //     never been turned on" from "live is on, nothing happened yet",
      //     read from the shared #copy-trading-mode-banner (issue #1185).
      // ------------------------------------------------------------------
      _copyActivityOnModeFilterChange({ target: { value: 'live' } });
      const modeBanner = document.getElementById('copy-trading-mode-banner');

      // Live currently OFF (banner not carrying mode-badge-live).
      modeBanner.classList.contains = (cls) => false;
      global.fetch = async () => jsonResp({ events: [
        { event_type: 'order_placed', ts: '2026-09-01T00:00:00Z', address: '0xW1', mode: 'paper', market: 'M1', signal_id: 1 },
      ] });
      await fetchCopyTradingActivityFeed();
      assert.ok(listWrap.innerHTML.includes("hasn't been turned on yet"), 'live-off + Mode=Live-empty must say live was never turned on');
      assert.ok(!listWrap.innerHTML.includes('No live activity in this range'));

      // Live currently ON (banner carrying mode-badge-live) but still no
      // live events matching the current filters.
      modeBanner.classList.contains = (cls) => cls === 'mode-badge-live';
      await fetchCopyTradingActivityFeed();
      assert.ok(listWrap.innerHTML.includes('No live activity in this range'), 'live-on + Mode=Live-empty must say nothing has happened, not that live is off');
      assert.ok(!listWrap.innerHTML.includes("hasn't been turned on yet"));

      // Reset filters/state for cleanliness.
      _copyActivityOnModeFilterChange({ target: { value: '' } });
      _copyActivityOnWalletFilterChange({ target: { value: '' } });
      _copyActivityOnEventTypeFilterChange({ target: { value: '' } });

      // ------------------------------------------------------------------
      // 11. A live event's click-through jumps to the Followed Wallets row
      //     (no Candidates-equivalent row exists for a live position).
      // ------------------------------------------------------------------
      currentTab = 'copy-trading';
      const liveFollowedRow = document.getElementById('followed-row-' + _copySafeId('0xW1'));
      let liveFollowedScrolled = false;
      liveFollowedRow.scrollIntoView = () => { liveFollowedScrolled = true; };
      _copyActivityJumpToWallet('live', '0xW1');
      assert.strictEqual(liveFollowedScrolled, true, 'a live event must scroll to its Followed Wallets row');

      console.log('ALL_ACTIVITY_FEED_JS_ASSERTIONS_PASSED');
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
def test_activity_feed_stale_indicator_filters_and_click_through(tmp_path):
    """Executes the real shipped dashboard script under Node and exercises
    the Activity Feed view's stale-feed indicator (issue #1149's explicit,
    separately-tested acceptance criterion), client-side filters, and
    click-through cross-navigation.
    """
    combined = _PRELUDE + "\n" + _extract_inline_script() + "\n" + _ASSERTIONS
    script_path = tmp_path / "activity_feed_logic_check.js"
    script_path.write_text(combined, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "ALL_ACTIVITY_FEED_JS_ASSERTIONS_PASSED" in result.stdout

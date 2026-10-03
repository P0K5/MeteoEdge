"""Unit tests for the Copy-Trading dashboard Activity Feed view's
client-side logic (epic F #1143, story F4 #1149; live events + mode
filter, epic J #1161, issue #1188; one feed per mode on the Paper / Live
tabs, issue #1275).

Same technique as test_copy_trading_followed_wallets_js_logic.py /
test_edge_tab_js_logic.py (issue #758): this repo has no JS test framework,
so this extracts the *actual* inline <script> block shipped in
src/dashboard/static/index.html and executes it under plain Node, with
minimal DOM/fetch/window stubs.

Covers the acceptance criteria's explicitly-called-out, separately-tested
requirement: "stale-feed indicator appears on a simulated poll failure" --
plus the per-mode feed rendering, wallet/event-type filters (client-side,
against the cached payload), click-through cross-navigation, the per-row
LIVE/PAPER mode badge + left-border accent, the guarantee that the Paper tab
never renders a live event nor the Live tab a paper one, and the Live feed's
empty states.

Also covers the wallet-balance-drift verdict's synthetic
'live_balance_mismatch' event (issue #1189): address-less rendering (fixed
label, not a blank address), the un-softened "manual reconciliation
required" text, its no-op click-through, exclusion from the Wallet filter
dropdown's option list, and event-type filtering.
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

    // The static controls bind their change handlers via addEventListener --
    // capture them so the filters can be driven exactly as a browser would.
    const handlers = {};
    for (const id of ['copy-activity-wallet-select', 'copy-activity-type-select',
                      'copy-live-activity-wallet-select', 'copy-live-activity-type-select']) {
      document.getElementById(id).addEventListener = (ev, fn) => { handlers[id] = fn; };
    }
    _copyActivityInitStaticControls();
    const paperWallet = (v) => handlers['copy-activity-wallet-select']({ target: { value: v } });
    const paperType = (v) => handlers['copy-activity-type-select']({ target: { value: v } });
    const liveWallet = (v) => handlers['copy-live-activity-wallet-select']({ target: { value: v } });
    const liveType = (v) => handlers['copy-live-activity-type-select']({ target: { value: v } });

    (async () => {
      // ------------------------------------------------------------------
      // 1. A successful fetch renders every event, clears the stale banner,
      //    and asks the server for PAPER events only.
      // ------------------------------------------------------------------
      const requested = [];
      global.fetch = async (url) => { requested.push(url); return jsonResp({ events: eventsFixture }); };
      const banner = document.getElementById('copy-activity-stale-banner');
      let bannerRemoveCalled = false;
      banner.classList.remove = (cls) => { if (cls === 'visible') bannerRemoveCalled = true; };
      let bannerAddCalled = false;
      banner.classList.add = (cls) => { if (cls === 'visible') bannerAddCalled = true; };

      await fetchCopyTradingActivityFeed();
      assert.deepStrictEqual(requested, ['/api/copy-trading/activity-feed?mode=paper']);
      assert.strictEqual(bannerRemoveCalled, true, 'a successful fetch must clear the stale banner');
      assert.strictEqual(bannerAddCalled, false, 'a successful fetch must never show the stale banner');
      assert.deepStrictEqual(_copyActivityScopes.paper.data.events, eventsFixture);

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
      assert.ok(textEl.textContent.includes('retrying'), 'stale banner text must say it will retry');
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
      _copyActivityScopes.paper.data = null;
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
      //    cached payload (no re-fetch).
      // ------------------------------------------------------------------
      let refetchCount = 0;
      global.fetch = async () => { refetchCount += 1; return jsonResp({ events: eventsFixture }); };
      await fetchCopyTradingActivityFeed();
      assert.strictEqual(refetchCount, 1);

      paperWallet('0xW1');
      assert.strictEqual(refetchCount, 1, 'changing the wallet filter must not trigger a network re-fetch');
      assert.ok(!listWrap.innerHTML.includes('Wallet paused'), 'wallet filter must exclude events for other wallets');
      assert.ok(listWrap.innerHTML.includes('Order placed') && listWrap.innerHTML.includes('Order skipped'));

      paperWallet('');
      paperType('wallet_paused');
      assert.strictEqual(refetchCount, 1, 'changing the event-type filter must not trigger a network re-fetch');
      assert.ok(listWrap.innerHTML.includes('Wallet paused'));
      assert.ok(!listWrap.innerHTML.includes('Order placed') && !listWrap.innerHTML.includes('Order skipped'));

      // A filter combination matching nothing shows the "no matching
      // activity" state, not a blank screen or the true-empty message.
      paperWallet('0xNoSuchWallet');
      assert.ok(listWrap.innerHTML.includes('No matching activity'));
      assert.ok(!listWrap.innerHTML.includes('No activity yet'));

      paperWallet('');
      paperType('');

      // ------------------------------------------------------------------
      // 7. Click-through cross-navigation. A paper signal event lives on
      //    the Copy · Wallets tab (Candidates): routed via the tab button's
      //    own switchTab() handler, then scrolls to + expands the row. A
      //    pause event scrolls to the PAPER roster row on this same tab.
      // ------------------------------------------------------------------
      currentTab = 'copy-paper';
      const walletsBtn = document.getElementById('tab-btn-copy-wallets');
      let walletsBtnClicked = false;
      walletsBtn.click = () => { walletsBtnClicked = true; };

      const candidateRow = document.getElementById('copy-row-' + _copySafeId('0xW1'));
      let candidateScrolled = false;
      candidateRow.scrollIntoView = () => { candidateScrolled = true; };
      _copyExpanded = new Set();
      _copyCandidatesData = { candidates: [] };   // candidates table already rendered
      _copyActivityJumpToWallet('signal', '0xW1');
      assert.strictEqual(walletsBtnClicked, true, 'a signal event must route through the Wallets tab button');
      assert.strictEqual(candidateScrolled, true, 'a signal event must scroll to its Candidates row');

      // Candidates not rendered yet: the jump is parked and consumed by
      // the next render -- never lost, never replayed later.
      candidateScrolled = false;
      walletsBtnClicked = false;
      _copyCandidatesData = null;
      _copyActivityJumpToWallet('signal', '0xW1');
      assert.strictEqual(walletsBtnClicked, true);
      assert.strictEqual(candidateScrolled, false, 'cannot scroll before the candidates table exists');
      assert.strictEqual(_copyPendingCandidateJump, '0xW1');
      _copyConsumePendingCandidateJump();
      assert.strictEqual(candidateScrolled, true, 'the parked jump runs once candidates render');
      assert.strictEqual(_copyPendingCandidateJump, null);

      // Already on the Wallets tab: no tab switch.
      currentTab = 'copy-wallets';
      walletsBtnClicked = false;
      _copyActivityJumpToWallet('signal', '0xW1');
      assert.strictEqual(walletsBtnClicked, false, 'already being on the Wallets tab must not re-click the tab button');

      currentTab = 'copy-paper';
      const followedRow = document.getElementById('followed-row-' + _copySafeId('0xW2'));
      let followedScrolled = false;
      followedRow.scrollIntoView = () => { followedScrolled = true; };
      walletsBtnClicked = false;
      _copyActivityJumpToWallet('pause', '0xW2');
      assert.strictEqual(walletsBtnClicked, false, 'a pause event stays on the Paper tab');
      assert.strictEqual(followedScrolled, true, 'a pause event must scroll to its Followed Wallets row');

      // ------------------------------------------------------------------
      // 8. issue #1188: events with no 'mode' field (older/unmapped data)
      //    default to the PAPER badge + border, never LIVE -- and are
      //    therefore Paper-tab events, never Live-tab ones.
      // ------------------------------------------------------------------
      global.fetch = async () => jsonResp({ events: eventsFixture });
      await fetchCopyTradingActivityFeed();
      assert.ok(listWrap.innerHTML.includes('mode-badge-paper'), 'a missing mode must default to the paper badge');
      assert.ok(listWrap.innerHTML.includes('copy-activity-item-paper'), 'a missing mode must default to the paper left-border accent');
      assert.ok(!listWrap.innerHTML.includes('mode-badge-live'), 'a missing mode must never render as LIVE');
      assert.ok(listWrap.innerHTML.includes('PAPER'), 'the PAPER badge text must be visible');
      // Designer review (PR #1195): row1 keeps Epic F's own distinct,
      // colored order_placed/order_skipped badges.
      assert.ok(listWrap.innerHTML.includes('copy-activity-badge-placed'), 'order_placed must keep its own distinct row1 badge');
      assert.ok(listWrap.innerHTML.includes('copy-activity-badge-skipped'), 'order_skipped must keep its own distinct row1 badge');
      assert.ok(!listWrap.innerHTML.includes('Signal detected'), 'the unified "Signal detected" badge must not be used');
      assert.ok(listWrap.innerHTML.includes('Order placed (paper)'), 'the description text must independently state the mode, not just the badge');
      assert.ok(listWrap.innerHTML.includes('Order skipped (paper)'));
      assert.ok(listWrap.innerHTML.includes('Wallet auto-paused'));

      // ------------------------------------------------------------------
      // 9. Paper and Live feeds never show each other's events. Even if the
      //    server (or a stale/older backend) returned a mixed payload, the
      //    Paper tab renders only paper rows and the Live tab only live
      //    rows -- the tab, not a Mode filter, is the separator.
      // ------------------------------------------------------------------
      const mixedFixture = [
        { event_type: 'live_order_rejected', ts: '2026-09-06T00:00:00Z', address: '0xW1', mode: 'live',
          market: 'M2', rejected_reason: 'no fill before the timeout window closed', signal_id: 9 },
        { event_type: 'live_circuit_breaker_tripped', ts: '2026-09-05T12:00:00Z', address: '0xW1', mode: 'live',
          market: 'M2', skip_reason: 'the live daily loss limit was reached', signal_id: 8 },
        { event_type: 'live_position_settled', ts: '2026-09-05T00:00:00Z', address: '0xW1', mode: 'live',
          market: 'M2', settled_pnl_usd: -1.5, signal_id: 7 },
        { event_type: 'order_placed', ts: '2026-09-01T00:00:00Z', address: '0xW3', mode: 'paper',
          market: 'M1', fill_price: 0.42, size_usd: 5.0, signal_id: 4 },
      ];
      const liveRequested = [];
      global.fetch = async (url) => { liveRequested.push(url); return jsonResp({ events: mixedFixture }); };
      await fetchCopyTradingActivityFeed('live');
      await fetchCopyTradingActivityFeed('paper');
      assert.deepStrictEqual(liveRequested, [
        '/api/copy-trading/activity-feed?mode=live',
        '/api/copy-trading/activity-feed?mode=paper',
      ]);

      const liveWrap = document.getElementById('copy-live-activity-list');
      assert.ok(liveWrap.innerHTML.includes('mode-badge-live'), 'a live event must render the LIVE badge');
      assert.ok(liveWrap.innerHTML.includes('copy-activity-item-live'), 'a live event must render the live left-border accent');
      assert.ok(liveWrap.innerHTML.includes('Live order rejected'));
      assert.ok(liveWrap.innerHTML.includes('no fill before the timeout window closed'), 'plain-language rejected_reason must render, not a raw constant');
      assert.ok(liveWrap.innerHTML.includes('Live circuit breaker tripped'));
      assert.ok(liveWrap.innerHTML.includes('the live daily loss limit was reached'));
      assert.ok(liveWrap.innerHTML.includes('Live position settled'));
      assert.ok(liveWrap.innerHTML.includes('copy-pnl-neg'), 'a negative settled P&L must render with the negative styling');
      assert.ok(!liveWrap.innerHTML.includes('mode-badge-paper'), 'the Live feed must never render a paper event');
      assert.ok(!liveWrap.innerHTML.includes('Order placed'), 'the Live feed must never render a paper event');

      assert.ok(listWrap.innerHTML.includes('mode-badge-paper'));
      assert.ok(listWrap.innerHTML.includes('Order placed (paper)'));
      assert.ok(!listWrap.innerHTML.includes('mode-badge-live'), 'the Paper feed must never render a live event');
      assert.ok(!listWrap.innerHTML.includes('Live order rejected'), 'the Paper feed must never render a live event');
      // The wallet filter of each feed only lists that feed's own wallets.
      assert.ok(document.getElementById('copy-activity-wallet-select').innerHTML.includes('0xW3'));
      assert.ok(!document.getElementById('copy-activity-wallet-select').innerHTML.includes('0xW1'));
      assert.ok(document.getElementById('copy-live-activity-wallet-select').innerHTML.includes('0xW1'));

      // Live filters are client-side too (no re-fetch) and independent of
      // the Paper feed's filter state.
      let liveRefetchCount = 0;
      global.fetch = async () => { liveRefetchCount += 1; return jsonResp({ events: mixedFixture }); };
      await fetchCopyTradingActivityFeed('live');
      liveRefetchCount = 0;
      liveType('live_position_settled');
      assert.strictEqual(liveRefetchCount, 0, 'changing a filter must not trigger a network re-fetch');
      assert.ok(liveWrap.innerHTML.includes('Live position settled'));
      assert.ok(!liveWrap.innerHTML.includes('Live order rejected'));
      assert.strictEqual(_copyActivityScopes.paper.filters.eventType, '', 'a Live filter must not leak into the Paper feed');
      liveType('');

      // Each feed has its own stale banner.
      const liveBanner = document.getElementById('copy-live-activity-stale-banner');
      let liveBannerShown = false;
      liveBanner.classList.add = (cls) => { if (cls === 'visible') liveBannerShown = true; };
      let paperBannerShown = false;
      banner.classList.add = (cls) => { if (cls === 'visible') paperBannerShown = true; };
      global.fetch = async () => { throw new Error('live feed down'); };
      await fetchCopyTradingActivityFeed('live');
      assert.strictEqual(liveBannerShown, true, 'a failed live poll shows the LIVE stale banner');
      assert.strictEqual(paperBannerShown, false, 'a failed live poll must not touch the Paper stale banner');
      assert.ok(document.getElementById('copy-live-activity-stale-text').textContent.includes('live feed down'));

      // ------------------------------------------------------------------
      // 10. Live-feed empty states (issue #1188): "live has never been
      //     turned on" vs "live is on, nothing happened yet", read from the
      //     shared, fast-polled posture flag. A posture flip re-renders the
      //     loaded Live feed's wording immediately.
      // ------------------------------------------------------------------
      global.fetch = async () => jsonResp({ events: [
        { event_type: 'order_placed', ts: '2026-09-01T00:00:00Z', address: '0xW1', mode: 'paper', market: 'M1', signal_id: 1 },
      ] });
      renderCopyTradingModePosture(false);
      await fetchCopyTradingActivityFeed('live');
      assert.ok(liveWrap.innerHTML.includes("hasn't been turned on yet"), 'live off + no live events must say live was never turned on');
      assert.ok(!liveWrap.innerHTML.includes('No live activity in this range'));

      renderCopyTradingModePosture(true);   // posture poll flips the switch
      assert.ok(liveWrap.innerHTML.includes('No live activity in this range'), 'live on + no live events must say nothing has happened, not that live is off');
      assert.ok(!liveWrap.innerHTML.includes("hasn't been turned on yet"));
      renderCopyTradingModePosture(false);

      // ------------------------------------------------------------------
      // 11. A live event's click-through jumps to the LIVE roster row on
      //     this same tab (Live wallets, else "Ready to go live").
      // ------------------------------------------------------------------
      currentTab = 'copy-live';
      const liveFollowedRow = document.getElementById('live-followed-row-' + _copySafeId('0xW1'));
      let liveFollowedScrolled = false;
      liveFollowedRow.scrollIntoView = () => { liveFollowedScrolled = true; };
      walletsBtnClicked = false;
      _copyActivityJumpToWallet('live', '0xW1');
      assert.strictEqual(liveFollowedScrolled, true, 'a live event must scroll to its Live roster row');
      assert.strictEqual(walletsBtnClicked, false, 'a live event must not leave the Live tab');

      // ------------------------------------------------------------------
      // 12. issue #1189: the wallet-balance-drift verdict's synthetic
      //     'live_balance_mismatch' event -- address-less rendering, the
      //     un-softened "manual reconciliation required" text, no-op
      //     click-through, and exclusion from the Wallet filter dropdown
      //     (its empty address must never produce a stray blank option).
      // ------------------------------------------------------------------
      const mismatchFixture = [
        { event_type: 'live_balance_mismatch', ts: '2026-09-10T00:00:00Z', address: '', mode: 'live',
          drift_usd: 12.34, expected_balance_usd: 100.0, actual_balance_usd: 112.34 },
      ];
      global.fetch = async () => jsonResp({ events: mismatchFixture });
      await fetchCopyTradingActivityFeed('live');

      assert.ok(liveWrap.innerHTML.includes('Live balance mismatch'), 'the mismatch event must render its badge/label');
      assert.ok(liveWrap.innerHTML.includes('manual reconciliation required'), 'the mismatch event text must never soften this language');
      assert.ok(liveWrap.innerHTML.includes('12.34'), 'the drift amount must render');
      assert.ok(liveWrap.innerHTML.includes('Live CLOB wallet'), 'the address-less event must show a fixed label, not a blank address');
      assert.ok(liveWrap.innerHTML.includes('mode-badge-live'), 'the mismatch event is always mode=live');

      const liveWalletSelect = document.getElementById('copy-live-activity-wallet-select');
      assert.ok(!liveWalletSelect.innerHTML.includes('<option value="">All wallets</option><option value="">'),
        'the empty address must not produce a second, indistinguishable blank wallet-filter option');

      // Clicking/activating the mismatch row is a documented no-op -- no
      // tab switch, no scroll (it has no wallet row to jump to).
      currentTab = 'portfolio';
      let mismatchTabClicked = false;
      walletsBtn.click = () => { mismatchTabClicked = true; };
      document.getElementById('tab-btn-copy-live').click = () => { mismatchTabClicked = true; };
      _copyActivityJumpToWallet('none', '');
      assert.strictEqual(mismatchTabClicked, false, 'the mismatch event must never trigger a tab switch');

      // event_type filter isolates it correctly.
      liveType('live_balance_mismatch');
      assert.ok(liveWrap.innerHTML.includes('Live balance mismatch'));
      liveType('');

      // ------------------------------------------------------------------
      // 13. An unchanged poll does not rebuild the feed DOM.
      // ------------------------------------------------------------------
      _copyLastPayloadSig['activity:paper'] = undefined;
      global.fetch = async () => jsonResp({ events: eventsFixture });
      await fetchCopyTradingActivityFeed('paper');
      listWrap.innerHTML = 'SENTINEL';   // any rebuild would overwrite this
      await fetchCopyTradingActivityFeed('paper');
      assert.strictEqual(listWrap.innerHTML, 'SENTINEL', 'identical payload must not rebuild the list');
      global.fetch = async () => jsonResp({ events: eventsFixture.slice(1) });
      await fetchCopyTradingActivityFeed('paper');
      assert.notStrictEqual(listWrap.innerHTML, 'SENTINEL', 'a changed payload rebuilds the list');

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

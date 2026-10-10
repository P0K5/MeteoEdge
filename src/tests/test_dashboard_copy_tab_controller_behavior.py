"""Behavior tests for the Copy-Trading tab split (epic #1272, issue #1275).

Runs the REAL inline <script> of src/dashboard/static/index.html under Node
against a small but honest DOM stub, and drives it the way a browser would:

* the tab bar / panels / posture banners are built from the REAL markup, so a
  click on a tab button runs the real `switchTab('<id>')` route and the
  assertions see the real tab ids, order and banner instances;
* timers are a virtual clock (`setInterval`/`clearInterval`/`Date.now` are
  faked and advanced explicitly), so "this interval is gone" / "nothing
  polls while hidden" are checked by letting minutes of virtual time pass;
* `fetch` is a recording stub keyed by URL, with deferred responses for the
  in-flight de-duplication cases;
* `document.hidden` + `visibilitychange` are real, dispatchable state.

Covers: tab routing, lazy per-tab loading, interval start/stop per tab,
hidden-tab pause/resume, shared-cache de-duplication, the shared posture
banner updating every instance, and "unchanged payload -> no DOM rebuild".
(Static string-grep coverage lives in the other test_dashboard_copy_* files
and is deliberately not the main coverage here.)
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

pytestmark = pytest.mark.skipif(not NODE, reason="requires node.js")


def _html_parts() -> tuple[str, str]:
    html = INDEX_HTML.read_text(encoding="utf-8")
    script = re.search(r"<script>([\s\S]*?)</script>", html)
    assert script, "Could not find the dashboard's inline <script> block"
    return html.split("<script>", 1)[0], script.group(1)


_PRELUDE = textwrap.dedent(r"""
    const assert = require('assert');

    // ---- DOM stub -------------------------------------------------------
    class Element {
      constructor(id) {
        this.id = id || '';
        this._classes = new Set();
        this._attrs = {};
        this._listeners = {};
        this._innerHTML = '';
        this._textContent = '';
        this.style = {};
        this.dataset = {};
        this.children = [];
        this.disabled = false;
        this.value = '';
        const self = this;
        this.classList = {
          add: (...c) => c.forEach(x => self._classes.add(x)),
          remove: (...c) => c.forEach(x => self._classes.delete(x)),
          contains: (c) => self._classes.has(c),
          toggle: (c, force) => {
            const on = force === undefined ? !self._classes.has(c) : !!force;
            if (on) self._classes.add(c); else self._classes.delete(c);
            return on;
          },
        };
      }
      get className() { return [...this._classes].join(' '); }
      set className(v) { this._classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
      get innerHTML() { return this._innerHTML; }
      set innerHTML(v) { this._innerHTML = v; this._writes = (this._writes || 0) + 1; }
      get textContent() { return this._textContent; }
      set textContent(v) { this._textContent = v; }
      setAttribute(k, v) { this._attrs[k] = String(v); }
      getAttribute(k) { return k in this._attrs ? this._attrs[k] : null; }
      removeAttribute(k) { delete this._attrs[k]; }
      addEventListener(t, h) { (this._listeners[t] = this._listeners[t] || []).push(h); }
      removeEventListener() {}
      appendChild() {}
      remove() {}
      focus() {}
      closest() { return null; }
      querySelector() { return null; }
      querySelectorAll() { return []; }
      scrollIntoView(opts) { this._scrolled = opts; }
    }

    const _byId = {};
    function el(id) { return _byId[id] || (_byId[id] = new Element(id)); }

    // Tab bar, panels and posture banners come from the REAL markup.
    const _tabButtons = [];
    const _tabPanels = [];
    const _banners = [];
    (function buildFromHtml(html) {
      const btnRe = /<button class="tab-btn[^"]*"(?: id="([^"]+)")? onclick="switchTab\('([^']+)'\)"/g;
      let m;
      while ((m = btnRe.exec(html))) {
        const b = el(m[1] || ('tab-btn-' + m[2]));
        b._tab = m[2];
        b.classList.add('tab-btn');
        // What the browser does for onclick="switchTab('x')".
        b.click = () => { global.event = { target: b }; switchTab(b._tab); };
        _tabButtons.push(b);
      }
      const panelRe = /<section id="(tab-[^"]+)" class="tab-panel/g;
      while ((m = panelRe.exec(html))) {
        const p = el(m[1]);
        p.classList.add('tab-panel');
        _tabPanels.push(p);
      }
      const banRe = /<span id="([^"]+)" class="[^"]*copy-trading-mode-banner[^"]*"/g;
      while ((m = banRe.exec(html))) {
        const b = el(m[1]);
        b.className = 'mode-badge mode-badge-paper copy-trading-mode-banner';
        _banners.push(b);
      }
    })(__HTML__);
    _tabPanels[0].classList.add('active');
    _tabButtons[0].classList.add('active');

    // ---- virtual clock + timers -------------------------------------------
    let _now = 1_000_000;
    Date.now = () => _now;
    let _tid = 0;
    const _intervals = new Map();   // id -> { fn, every, next }
    global.setInterval = (fn, every) => { const id = ++_tid; _intervals.set(id, { fn, every, next: _now + every }); return id; };
    global.clearInterval = (id) => { _intervals.delete(id); };
    const activeDelays = () => [..._intervals.values()].map(i => i.every).sort((a, b) => a - b);
    const settle = async () => { for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r)); };
    async function advance(ms) {
      const end = _now + ms;
      for (;;) {
        const due = [..._intervals.entries()].filter(([, t]) => t.next <= end).sort((a, b) => a[1].next - b[1].next)[0];
        if (!due) break;
        const [id, t] = due;
        _now = Math.max(_now, t.next);
        t.next += t.every;
        t.fn();
        await settle();
        if (!_intervals.has(id)) continue;
      }
      _now = end;
      await settle();
    }

    // ---- document / window ----------------------------------------------
    const _docListeners = {};
    global.document = {
      hidden: false,
      getElementById: (id) => el(id),
      querySelectorAll(sel) {
        if (sel === '.tab-panel') return _tabPanels;
        if (sel === '.tab-btn') return _tabButtons;
        if (sel === '.copy-trading-mode-banner') return _banners;
        return [];
      },
      querySelector() { return null; },
      createElement: () => new Element(),
      documentElement: { getAttribute() { return 'dark'; }, setAttribute() {} },
      addEventListener(t, h) { (_docListeners[t] = _docListeners[t] || []).push(h); },
    };
    function setHidden(hidden) {
      document.hidden = hidden;
      (_docListeners.visibilitychange || []).forEach(h => h());
    }
    global.window = { addEventListener() {}, prompt: () => '', confirm: () => true };
    global.localStorage = { getItem() { return null; }, setItem() {} };
    global.lucide = { createIcons() {} };
    global.Chart = function () { this.destroy = () => {}; };
    global.getComputedStyle = () => ({ getPropertyValue: () => '' });
    global.event = { target: new Element() };
    Object.defineProperty(global, 'navigator', { value: { clipboard: { writeText: async () => {} } }, writable: true, configurable: true });
    console.error = () => {};   // fetch-failure logging in the app is expected noise

    // ---- fetch stub ----------------------------------------------------------
    const requests = [];        // every URL requested, in order
    const count = (needle) => requests.filter(u => u.includes(needle)).length;
    const resetRequests = () => { requests.length = 0; };
    const FOLLOWED = {
      wallets: [
        { address: '0xLiveOne', stake_per_trade: 5, status: 'active', paused_reason: null,
          added_at: '2026-09-01T00:00:00Z', n_settled: 3, realized_pnl_usd: 12.5,
          live_enabled: true, live_eligible: true, live_status_reason: 'eligible for live execution',
          live_stake_per_trade: 2, live_stake_is_override: true },
        { address: '0xPaperOne', stake_per_trade: 7, status: 'active', paused_reason: null,
          added_at: '2026-09-02T00:00:00Z', n_settled: 1, realized_pnl_usd: -3.25,
          live_enabled: false, live_eligible: false, live_status_reason: 'live is not enabled for this wallet',
          live_stake_per_trade: 7, live_stake_is_override: false },
      ],
      active_count: 2, paused_count: 0, aggregate_pnl_usd: 9.25, n_settled_total: 4,
      live_eligible_count: 1, paper_only_count: 1, live_aggregate_pnl_usd: 40, live_n_settled_total: 6,
      live_trading_enabled: true, live_cap_usd: 10, live_opted_in_count: 1,
    };
    const POSITIONS = {
      total: { realized_pnl_usd: 1, n_settled: 1 }, open_positions: [], per_wallet: [],
      realized_pnl_history: [],
      live_total: { realized_pnl_usd: 2, n_settled: 1 }, live_open_positions: [], live_per_wallet: [],
      live_realized_pnl_history: [], live_error: null,
    };
    const state = {
      config: { copy_trading: { COPY_LIVE_TRADING_ENABLED: { value: false } } },
      followed: FOLLOWED, positions: POSITIONS,
    };
    function bodyFor(url) {
      if (/\/wallets\/[^/]+\/(pause|resume|unfollow|live)$/.test(url)) return { success: true, message: 'ok' };
      if (url.startsWith('/api/config')) return state.config;
      if (url.startsWith('/api/copy-trading/candidates')) return { candidates: [], slots_remaining: 5, max_followed: 5 };
      if (url.startsWith('/api/copy-trading/followed-wallets')) return state.followed;
      if (url.startsWith('/api/copy-trading/positions')) return state.positions;
      if (url.startsWith('/api/copy-trading/activity-feed')) return { events: [] };
      if (url.startsWith('/api/copy-trading/balance-drift')) return { within_tolerance: true };
      return {};
    }
    let _hold = null;   // when set: fetches return a promise resolved by release()
    global.fetch = (url) => {
      requests.push(String(url));
      const body = bodyFor(String(url));
      if (_hold) return new Promise(res => _hold.push(() => res({ ok: true, json: async () => body })));
      return Promise.resolve({ ok: true, json: async () => body });
    };
    const click = (tab) => el('tab-btn-' + tab).click();
    const PAPER_ONLY = ['/api/copy-trading/followed-wallets', '/api/copy-trading/positions'];
""")


def run_js(body: str, tmp_path: Path) -> None:
    html_markup, script = _html_parts()
    prelude = _PRELUDE.replace("__HTML__", repr(html_markup))
    code = (
        prelude
        + "\n"
        + script
        + "\n(async () => {\n"
        + textwrap.dedent(body)
        + "\n})().then(() => console.log('OK'), e => { console.log('FAIL'); console.log(e && e.stack || e); process.exit(1); });"
    )
    path = tmp_path / "copy_tab_controller_check.js"
    path.write_text(code, encoding="utf-8")
    r = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "OK" in r.stdout, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"


# ---------------------------------------------------------------------------
# Tab routing
# ---------------------------------------------------------------------------

def test_tab_bar_routes_to_the_three_copy_panels(tmp_path):
    run_js("""
        const order = _tabButtons.map(b => b._tab);
        assert.deepStrictEqual(order, ['portfolio', 'stations', 'edge', 'emos', 'promotion',
          'copy-wallets', 'copy-paper', 'copy-live', 'config']);
        assert.deepStrictEqual(_tabPanels.map(p => p.id).filter(i => i.startsWith('tab-copy')),
          ['tab-copy-wallets', 'tab-copy-paper', 'tab-copy-live']);
        assert.ok(!_tabButtons.some(b => b._tab === 'copy-trading'), 'the old tab id must be gone');

        for (const tab of ['copy-wallets', 'copy-paper', 'copy-live', 'copy-paper', 'config']) {
          click(tab);
          const active = _tabPanels.filter(p => p.classList.contains('active')).map(p => p.id);
          assert.deepStrictEqual(active, ['tab-' + tab], 'exactly one panel active after clicking ' + tab);
          const activeBtns = _tabButtons.filter(b => b.classList.contains('active')).map(b => b._tab);
          assert.deepStrictEqual(activeBtns, [tab], 'exactly one tab button active after clicking ' + tab);
          assert.strictEqual(currentTab, tab);
        }
        // The active button is scrolled into view (mobile tab bar).
        assert.ok(el('tab-btn-config')._scrolled);
    """, tmp_path)


def test_clicking_a_copy_tab_never_activates_a_sibling_copy_panel(tmp_path):
    run_js("""
        click('copy-live');
        assert.ok(el('tab-copy-live').classList.contains('active'));
        assert.ok(!el('tab-copy-paper').classList.contains('active'));
        assert.ok(!el('tab-copy-wallets').classList.contains('active'));
    """, tmp_path)


# ---------------------------------------------------------------------------
# Lazy per-tab loading
# ---------------------------------------------------------------------------

def test_each_tab_lazy_loads_only_its_own_data_on_first_activation(tmp_path):
    run_js("""
        await settle();
        assert.strictEqual(requests.length, 0, 'nothing copy-related is fetched before a copy tab opens');

        click('copy-wallets');
        await settle();
        assert.ok(count('/api/config') >= 1 && count('/api/copy-trading/candidates') === 1);
        for (const u of ['followed-wallets', 'positions', 'activity-feed', 'balance-drift']) {
          assert.strictEqual(count(u), 0, 'Wallets must not fetch ' + u);
        }

        resetRequests();
        click('copy-paper');
        await settle();
        assert.strictEqual(count('/api/copy-trading/followed-wallets'), 1);
        assert.strictEqual(count('/api/copy-trading/positions'), 1);
        assert.deepStrictEqual(requests.filter(u => u.includes('activity-feed')),
          ['/api/copy-trading/activity-feed?mode=paper']);
        assert.strictEqual(count('candidates'), 0, 'Paper must not fetch candidates');
        assert.strictEqual(count('balance-drift'), 0, 'balance-drift is Live-only');

        resetRequests();
        click('copy-live');
        await settle();
        assert.deepStrictEqual(requests.filter(u => u.includes('activity-feed')),
          ['/api/copy-trading/activity-feed?mode=live']);
        assert.strictEqual(count('balance-drift'), 1);
        assert.strictEqual(count('candidates'), 0, 'Live must not fetch candidates');
    """, tmp_path)


def test_a_tab_renders_nothing_for_the_other_modes_panels(tmp_path):
    """Lazy + separated: opening Paper must not render Live's DOM (and vice
    versa) -- the other mode's sections stay untouched until their tab opens."""
    run_js("""
        click('copy-paper');
        await settle();
        assert.ok(el('copy-followed-list').innerHTML.includes('0xPaperOne'), 'Paper roster rendered');
        assert.strictEqual(el('copy-live-followed-list').innerHTML, '', 'Live roster not rendered by Paper');
        assert.strictEqual(el('copy-positions-live-content').innerHTML, '', 'Live positions not rendered by Paper');
        assert.strictEqual(el('copy-live-activity-list').innerHTML, '', 'Live activity not rendered by Paper');

        click('copy-live');
        await settle();
        assert.ok(el('copy-live-followed-list').innerHTML.includes('0xLiveOne'), 'Live roster rendered');
        assert.ok(!el('copy-live-followed-list').innerHTML.includes('0xPaperOne'));
        assert.ok(el('copy-positions-live-content').innerHTML !== '');
    """, tmp_path)


# ---------------------------------------------------------------------------
# Interval start / stop per tab
# ---------------------------------------------------------------------------

def test_intervals_start_on_entry_and_stop_when_leaving_each_copy_tab(tmp_path):
    run_js("""
        const S = 30_000, M = 300_000;
        assert.deepStrictEqual(activeDelays(), []);

        click('copy-wallets');
        assert.deepStrictEqual(activeDelays(), [S, M], 'Wallets: posture 30s + candidates 5min');

        // Moving to ANOTHER COPY TAB must stop the left tab's intervals
        // (the old code never cleared them) and start only the new tab's.
        click('copy-paper');
        assert.deepStrictEqual(activeDelays(), [S, S, M, M], 'Paper: posture, followed (30s); positions, activity (5min)');

        click('copy-live');
        assert.deepStrictEqual(activeDelays(), [S, S, S, S, S, M], 'Live: posture, followed, positions, activity, breaker (30s); drift (5min)');

        click('copy-paper');
        assert.deepStrictEqual(activeDelays(), [S, S, M, M], 'back on Paper: Live intervals are gone');

        click('config');
        assert.deepStrictEqual(activeDelays(), [], 'leaving to a non-copy tab stops everything');

        // Re-entry restarts, and re-clicking the same tab never stacks intervals.
        click('copy-live'); click('copy-live'); click('copy-live');
        assert.deepStrictEqual(activeDelays(), [S, S, S, S, S, M]);
    """, tmp_path)


def test_a_left_tabs_jobs_stop_firing(tmp_path):
    """Behavioural proof (not just interval bookkeeping): after leaving Live
    for Paper, minutes of virtual time produce no Live-only requests, and
    after leaving for Config nothing is requested at all."""
    run_js("""
        click('copy-live');
        await settle();
        click('copy-paper');
        await settle();
        resetRequests();
        await advance(10 * 60_000);
        assert.strictEqual(count('balance-drift'), 0, 'Live-only drift poll must be stopped');
        assert.strictEqual(count('live-breaker'), 0, 'Live-only breaker-status poll must be stopped');
        assert.ok(!requests.includes('/api/copy-trading/activity-feed?mode=live'), 'Live activity poll must be stopped');
        assert.strictEqual(count('candidates'), 0, 'Wallets poll never ran on Paper');
        assert.ok(count('/api/copy-trading/followed-wallets') >= 1, 'Paper still polls its own roster');

        click('config');
        await settle();
        resetRequests();
        await advance(10 * 60_000);
        assert.strictEqual(requests.length, 0, 'no copy polling at all once off the copy tabs');
    """, tmp_path)


def test_poll_cadences_per_tab(tmp_path):
    run_js("""
        click('copy-paper');
        await settle();
        resetRequests();
        await advance(5 * 60_000);
        // Roster + posture every 30s (cache TTL 10s < 30s, so each is a real request);
        // positions + activity only every 5 minutes on Paper.
        assert.strictEqual(count('/api/copy-trading/followed-wallets'), 10);
        assert.strictEqual(count('/api/config'), 10);
        assert.strictEqual(count('/api/copy-trading/positions'), 1);
        assert.strictEqual(count('activity-feed'), 1);

        click('copy-live');
        await settle();
        resetRequests();
        await advance(5 * 60_000);
        assert.strictEqual(count('/api/copy-trading/positions'), 10, 'Live polls positions every 30s (real money)');
        assert.strictEqual(count('activity-feed?mode=live'), 10);
        assert.strictEqual(count('balance-drift'), 1);
        assert.strictEqual(count('live-breaker'), 10, 'Breaker status polls every 30s (real money)');
    """, tmp_path)


# ---------------------------------------------------------------------------
# Hidden-tab pause / resume
# ---------------------------------------------------------------------------

def test_polling_pauses_while_document_hidden_and_resumes_with_immediate_refresh(tmp_path):
    run_js("""
        click('copy-live');
        await settle();
        assert.strictEqual(activeDelays().length, 6);

        setHidden(true);
        assert.deepStrictEqual(activeDelays(), [], 'hiding the page stops every copy interval');
        resetRequests();
        await advance(30 * 60_000);
        assert.strictEqual(requests.length, 0, 'no requests at all while the document is hidden');

        setHidden(false);
        await settle();
        assert.strictEqual(activeDelays().length, 6, 'intervals restarted when visible again');
        assert.strictEqual(count('/api/config'), 1, 'immediate posture refresh on return');
        assert.strictEqual(count('/api/copy-trading/followed-wallets'), 1, 'immediate roster refresh on return');
        assert.strictEqual(count('/api/copy-trading/positions'), 1, 'immediate positions refresh on return');
        assert.strictEqual(count('activity-feed?mode=live'), 1, 'stale (30 min old) activity refreshed on return');
        assert.strictEqual(count('balance-drift'), 1, 'stale (30 min old) drift refreshed on return');
        assert.strictEqual(count('live-breaker'), 1, 'immediate breaker-status refresh on return');

        // And the restarted intervals keep working.
        resetRequests();
        await advance(31_000);
        assert.ok(count('/api/copy-trading/followed-wallets') >= 1);
    """, tmp_path)


def test_no_request_fires_from_a_stray_timer_while_hidden(tmp_path):
    """Belt and braces: even if a timer callback somehow fires while the
    document is hidden, the job refuses to fetch."""
    run_js("""
        click('copy-paper');
        await settle();
        const timers = [..._intervals.values()].map(t => t.fn);   // grab before hiding
        setHidden(true);
        resetRequests();
        timers.forEach(fn => fn());
        await settle();
        assert.strictEqual(requests.length, 0);
    """, tmp_path)


def test_visibility_change_off_the_copy_tabs_does_nothing(tmp_path):
    run_js("""
        click('config');
        await settle();
        resetRequests();
        setHidden(true);
        setHidden(false);
        await settle();
        assert.strictEqual(requests.length, 0);
        assert.deepStrictEqual(activeDelays(), []);
    """, tmp_path)


def test_entering_a_copy_tab_while_hidden_defers_until_visible(tmp_path):
    run_js("""
        document.hidden = true;
        _copyTabStart('copy-paper');
        await settle();
        assert.strictEqual(requests.length, 0);
        assert.deepStrictEqual(activeDelays(), []);
    """, tmp_path)


# ---------------------------------------------------------------------------
# Shared fetch cache
# ---------------------------------------------------------------------------

def test_paper_live_switch_reuses_followed_and_positions_within_ttl(tmp_path):
    run_js("""
        click('copy-paper');
        await settle();
        assert.strictEqual(count('/api/copy-trading/followed-wallets'), 1);
        assert.strictEqual(count('/api/copy-trading/positions'), 1);

        // Paper <-> Live within the 10s TTL: ZERO new requests for either payload.
        await advance(3_000);
        click('copy-live');
        await settle();
        click('copy-paper');
        await settle();
        click('copy-live');
        await settle();
        assert.strictEqual(count('/api/copy-trading/followed-wallets'), 1, 'one followed-wallets request total');
        assert.strictEqual(count('/api/copy-trading/positions'), 1, 'one positions request total');
        // ...yet each tab still rendered its own slice from that one payload.
        assert.ok(el('copy-live-followed-list').innerHTML.includes('0xLiveOne'));
        assert.ok(el('copy-followed-list').innerHTML.includes('0xPaperOne'));

        // After the TTL a switch refreshes each payload exactly once.
        await advance(12_000);
        resetRequests();
        click('copy-paper');
        await settle();
        assert.strictEqual(count('/api/copy-trading/followed-wallets'), 1);
        assert.strictEqual(count('/api/copy-trading/positions'), 1);
    """, tmp_path)


def test_concurrent_fetches_share_one_in_flight_request(tmp_path):
    run_js("""
        _hold = [];
        const a = fetchFollowedWallets('paper');
        const b = fetchFollowedWallets('live');
        const c = fetchCopyTradingPositions('paper');
        const d = fetchCopyTradingPositions('live');
        await settle();
        assert.strictEqual(count('/api/copy-trading/followed-wallets'), 1, 'Paper+Live callers share one in-flight roster request');
        assert.strictEqual(count('/api/copy-trading/positions'), 1, 'Paper+Live callers share one in-flight positions request');
        _hold.splice(0).forEach(release => release());
        await Promise.all([a, b, c, d]);
        assert.ok(el('copy-followed-list').innerHTML.includes('0xPaperOne'));
        assert.ok(el('copy-live-followed-list').innerHTML.includes('0xLiveOne'));
    """, tmp_path)


def test_invalidation_after_a_mutation_forces_a_fresh_read_and_drops_stale_inflight(tmp_path):
    run_js("""
        await fetchFollowedWallets('paper');
        assert.strictEqual(count('/api/copy-trading/followed-wallets'), 1);
        await fetchFollowedWallets('live');                       // cached
        assert.strictEqual(count('/api/copy-trading/followed-wallets'), 1);

        _copyInvalidateShared();                                  // e.g. after Pause / Go live / Follow
        await fetchFollowedWallets('paper');
        assert.strictEqual(count('/api/copy-trading/followed-wallets'), 2, 'a mutation forces a fresh read');

        // A request already in flight when the mutation lands must not
        // repopulate the cache with its pre-mutation answer.
        _copyInvalidateShared();
        _hold = [];
        const stale = _copySharedFetch('/api/copy-trading/followed-wallets');   // pre-mutation, in flight
        await settle();
        _copyInvalidateShared();                                                // mutation lands
        state.followed = { ...FOLLOWED, active_count: 99 };
        const fresh = _copySharedFetch('/api/copy-trading/followed-wallets');   // post-mutation
        await settle();
        assert.strictEqual(count('/api/copy-trading/followed-wallets'), 4, 'the post-mutation read is its own request');
        _hold.splice(0).forEach(release => release());
        await Promise.all([stale, fresh]);
        _hold = null;
        const cached = await _copySharedFetch('/api/copy-trading/followed-wallets');
        assert.strictEqual(cached.active_count, 99, 'the cache holds the post-mutation payload');
    """, tmp_path)


def test_a_successful_row_action_invalidates_the_shared_cache(tmp_path):
    run_js("""
        click('copy-paper');
        await settle();
        resetRequests();
        window.prompt = () => 'flagged';
        const btn = new Element();
        await followedPauseWallet('0xPaperOne', btn);
        // POST /pause is not a GET read; the refetch after it is a REAL request (cache dropped).
        assert.strictEqual(count('/api/copy-trading/followed-wallets'), 1, 'post-mutation refetch bypasses the TTL');
    """, tmp_path)


def test_failed_shared_fetch_is_not_cached(tmp_path):
    run_js("""
        let fail = true;
        global.fetch = (url) => {
          requests.push(String(url));
          return Promise.resolve(fail ? { ok: false, status: 500, json: async () => ({}) }
                                      : { ok: true, json: async () => FOLLOWED });
        };
        await fetchFollowedWallets('paper');
        assert.strictEqual(count('followed-wallets'), 1);
        fail = false;
        await fetchFollowedWallets('paper');      // immediately retried, not served from a poisoned cache
        assert.strictEqual(count('followed-wallets'), 2);
        assert.ok(el('copy-followed-list').innerHTML.includes('0xPaperOne'));
    """, tmp_path)


# ---------------------------------------------------------------------------
# Shared posture banner
# ---------------------------------------------------------------------------

def test_one_posture_fetch_updates_every_banner_instance(tmp_path):
    run_js("""
        assert.deepStrictEqual(_banners.map(b => b.id),
          ['copy-wallets-mode-banner', 'copy-paper-mode-banner', 'copy-live-mode-banner']);
        state.config = { copy_trading: { COPY_LIVE_TRADING_ENABLED: { value: true } } };

        click('copy-wallets');
        await settle();
        assert.strictEqual(count('/api/config'), 1, 'ONE fetch');
        for (const b of _banners) {
          assert.strictEqual(b.textContent, 'LIVE TRADING ON', b.id);
          assert.ok(b.classList.contains('mode-badge-live') && b.classList.contains('copy-trading-mode-banner'), b.id);
          assert.ok(!b.classList.contains('mode-badge-paper'), b.id);
          assert.strictEqual(b.getAttribute('aria-label'), 'Live trading is on');
        }

        // Switching tabs shortly after does not re-request the posture...
        click('copy-paper'); click('copy-live');
        await settle();
        assert.strictEqual(count('/api/config'), 1, 'tab switches within the TTL reuse the posture read');

        // ...and the next poll flipping the switch updates ALL instances,
        // including the ones on tabs that are not showing.
        await advance(31_000);
        state.config = { copy_trading: { COPY_LIVE_TRADING_ENABLED: { value: false } } };
        await advance(31_000);
        for (const b of _banners) {
          assert.strictEqual(b.textContent, 'LIVE TRADING OFF — paper only', b.id);
          assert.ok(b.classList.contains('mode-badge-paper'), b.id);
        }
    """, tmp_path)


# ---------------------------------------------------------------------------
# Unchanged payload -> no DOM rebuild
# ---------------------------------------------------------------------------

def test_unchanged_payload_does_not_rebuild_the_list_dom(tmp_path):
    run_js("""
        click('copy-paper');
        await settle();
        const roster = el('copy-followed-list');
        const positions = el('copy-positions-content');
        const writesBefore = [roster._writes, positions._writes];
        assert.ok(writesBefore[0] >= 1 && writesBefore[1] >= 1);

        await advance(31_000);                       // a real, identical re-poll
        assert.ok(count('/api/copy-trading/followed-wallets') >= 2, 'the poll did run');
        assert.strictEqual(roster._writes, writesBefore[0], 'identical roster payload must not rebuild the DOM');

        state.followed = { ...FOLLOWED, active_count: 1 };
        await advance(31_000);
        assert.strictEqual(roster._writes, writesBefore[0] + 1, 'a changed payload rebuilds exactly once');
    """, tmp_path)


def test_live_positions_rerender_on_posture_flip_but_not_on_identical_polls(tmp_path):
    run_js("""
        click('copy-live');
        await settle();
        const live = el('copy-positions-live-content');
        const base = live._writes;
        await advance(31_000);
        assert.strictEqual(live._writes, base, 'identical live positions payload + posture: no rebuild');

        state.config = { copy_trading: { COPY_LIVE_TRADING_ENABLED: { value: true } } };
        await advance(31_000);
        assert.ok(live._writes > base, 'a posture flip is reflected in the Live positions view');
    """, tmp_path)


# ---------------------------------------------------------------------------
# Live range filter is independent of the Paper one
# ---------------------------------------------------------------------------

def test_live_and_paper_chart_ranges_are_independent(tmp_path):
    run_js("""
        const paperBefore = _copyPositionsRangeDays;
        _copyLivePositionsOnRangeChange({ target: { value: '7' } });
        assert.strictEqual(_copyLiveRangeDays, 7);
        assert.strictEqual(_copyPositionsRangeDays, paperBefore, 'the Live range must not move the Paper range');
        _copyPositionsOnRangeChange({ target: { value: '90' } });
        assert.strictEqual(_copyPositionsRangeDays, 90);
        assert.strictEqual(_copyLiveRangeDays, 7);
    """, tmp_path)

"""Behavior tests for the Copy · Wallets candidates list (epic #1272, issue #1276).

Runs the REAL inline <script> of src/dashboard/static/index.html under Node
against a DOM stub and a FAKE candidates API that implements the real
contract of GET /api/copy-trading/candidates (issue #1274: page, page_size,
sort, dir, q; total / unfiltered_total / page / page_size / total_pages), so
the assertions are about what the dashboard REQUESTS and what it RENDERS:

* tab bar / toolbar / pagination footer ids come from the real markup, and a
  click on the tab button runs the real switchTab() -> controller route;
* timers are a virtual clock (setTimeout AND setInterval), so the 200 ms
  search debounce and the controller's 5 min poll are advanced explicitly;
* fetch honours AbortSignal like a browser, and the fake server can hold
  responses (deferred) or ignore aborts (to prove the stale-response guard
  does not rely on the transport);
* setting innerHTML on an element destroys the elements that its previous
  markup contained and creates the ones in the new markup, so "a rebuild lost
  the armed Follow input / expanded row" is observable, not masked by a
  stub that keeps stale children alive.

(Static string-grep coverage is deliberately not the main coverage here.)
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

    // ---- DOM stub ---------------------------------------------------------
    const _byId = {};
    function _forget(id) {
      const e = _byId[id];
      if (!e) return;
      delete _byId[id];
      (e._childIds || []).forEach(_forget);
    }
    class Element {
      constructor(id) {
        this.id = id || '';
        this._classes = new Set();
        this._attrs = {};
        this._listeners = {};
        this._innerHTML = '';
        this._textContent = '';
        this._childIds = [];
        this.style = {};
        this.dataset = {};
        this.disabled = false;
        this.hidden = false;
        this.value = '';
        this.writes = 0;
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
      set innerHTML(v) {
        // The old children are destroyed; the ids in the new markup are born.
        this._childIds.forEach(_forget);
        this._innerHTML = v;
        this.writes += 1;
        this._childIds = [];
        const re = /<[a-z]+\b[^>]*?\bid="([^"]+)"[^>]*>/g;
        let m;
        while ((m = re.exec(v))) {
          const child = new Element(m[1]);
          if (/style="display:none"/.test(m[0])) child.style.display = 'none';
          _byId[m[1]] = child;
          this._childIds.push(m[1]);
        }
      }
      get textContent() { return this._textContent; }
      set textContent(v) { this._textContent = v; }
      setAttribute(k, v) { this._attrs[k] = String(v); }
      getAttribute(k) { return k in this._attrs ? this._attrs[k] : null; }
      removeAttribute(k) { delete this._attrs[k]; }
      addEventListener(t, h) { (this._listeners[t] = this._listeners[t] || []).push(h); }
      removeEventListener() {}
      appendChild() {}
      remove() {}
      focus() { document.activeElement = this; }
      closest() { return null; }
      querySelector() { return null; }
      querySelectorAll() { return []; }
      scrollIntoView(opts) { this._scrolled = opts; }
      fire(type, ev) { (this._listeners[type] || []).forEach(h => h(ev || {})); }
    }
    function el(id) { return _byId[id] || (_byId[id] = new Element(id)); }

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
        b.click = () => { global.event = { target: b }; switchTab(b._tab); };
        _tabButtons.push(b);
      }
      const panelRe = /<section id="(tab-[^"]+)" class="tab-panel/g;
      while ((m = panelRe.exec(html))) { const p = el(m[1]); p.classList.add('tab-panel'); _tabPanels.push(p); }
      const banRe = /<span id="([^"]+)" class="[^"]*copy-trading-mode-banner[^"]*"/g;
      while ((m = banRe.exec(html))) { const b = el(m[1]); b.className = 'copy-trading-mode-banner'; _banners.push(b); }
      // The real toolbar / footer ids must exist in the markup.
      for (const id of ['copy-search-input', 'copy-search-clear', 'copy-page-size', 'copy-refresh-btn',
                        'copy-showing', 'copy-candidates-list', 'copy-pagination', 'copy-pg-prev',
                        'copy-pg-info', 'copy-pg-next']) {
        assert.ok(html.includes('id="' + id + '"'), 'markup is missing #' + id);
        el(id);
      }
    })(__HTML__);
    _tabPanels[0].classList.add('active');
    _tabButtons[0].classList.add('active');
    el('copy-pagination').hidden = true;

    // ---- virtual clock (timeouts + intervals) ----------------------------------
    let _now = 1_000_000;
    Date.now = () => _now;
    let _tid = 0;
    const _timers = new Map();   // id -> { fn, every, next, once }
    global.setInterval = (fn, every) => { const id = ++_tid; _timers.set(id, { fn, every, next: _now + every }); return id; };
    global.setTimeout = (fn, ms) => { const id = ++_tid; _timers.set(id, { fn, every: ms, next: _now + ms, once: true }); return id; };
    global.clearInterval = global.clearTimeout = (id) => { _timers.delete(id); };
    const settle = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setImmediate(r)); };
    async function advance(ms) {
      const end = _now + ms;
      for (;;) {
        const due = [..._timers.entries()].filter(([, t]) => t.next <= end).sort((a, b) => a[1].next - b[1].next)[0];
        if (!due) break;
        const [id, t] = due;
        _now = Math.max(_now, t.next);
        if (t.once) _timers.delete(id); else t.next += t.every;
        t.fn();
        await settle();
      }
      _now = end;
      await settle();
    }

    // ---- document / window / storage ---------------------------------------------
    const _docListeners = {};
    global.document = {
      hidden: false,
      activeElement: null,
      // Row-level ids only exist while their markup is on screen (a rebuilt or
      // paged-away row is really gone); everything else is created lazily.
      getElementById: (id) => (/^copy-(row|detail|detail-row|chev|follow-section|stake|follow-msg|canvas|sort)-/.test(id)
        ? (_byId[id] || null) : el(id)),
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
    const storage = { data: __STORAGE__, sets: [], throws: __THROWS__ };
    global.localStorage = {
      getItem(k) { if (storage.throws) throw new Error('blocked'); return k in storage.data ? storage.data[k] : null; },
      setItem(k, v) { if (storage.throws) throw new Error('blocked'); storage.data[k] = String(v); storage.sets.push([k, String(v)]); },
    };
    global.lucide = { createIcons() {} };
    global.Chart = function () { this.destroy = () => {}; };
    global.getComputedStyle = () => ({ getPropertyValue: () => '' });
    global.event = { target: new Element() };
    Object.defineProperty(global, 'navigator', { value: { clipboard: { writeText: async () => {} } }, writable: true, configurable: true });
    console.error = () => {};

    // ---- fake candidates API (the #1274 contract) -----------------------------------
    const ALL = Array.from({ length: 120 }, (_, i) => ({
      address: '0x' + i.toString(16).padStart(4, '0') + (i % 7 === 0 ? 'abc' : 'def') + '0000000000',
      window: '90d', screened_at: '2026-09-01T00:00:00Z', n_buy_trades: 50 + i, n_resolved: i,
      win_rate: ((i * 37) % 100) / 100, mean_roi: ((i * 13) % 50) / 100, median_roi: (120 - i) / 100,
      mirrored_dollar_pnl: i * 1.5, flat_dollar_pnl: i * 0.5, flat_stake: 5, eligible_to_follow: true,
      unstable: false, has_prior_run: true, truncated: false, followed: false, follow_status: null,
    }));
    const srv = { rows: ALL, fail: false, hold: false, ignoreAbort: false, held: [] };
    function candidatesBody(url) {
      const u = new URL('http://x' + url);
      const size = parseInt(u.searchParams.get('page_size') || '25', 10);
      const page = parseInt(u.searchParams.get('page') || '1', 10);
      const sort = u.searchParams.get('sort') || 'median_roi';
      const desc = (u.searchParams.get('dir') || 'desc') === 'desc';
      const q = (u.searchParams.get('q') || '').toLowerCase();
      let rows = srv.rows.filter(r => !q || r.address.toLowerCase().includes(q));
      const filtered = rows.length;
      rows = [...rows].sort((a, b) => (desc ? -1 : 1) * (a[sort] > b[sort] ? 1 : a[sort] < b[sort] ? -1 : 0));
      return {
        candidates: rows.slice((page - 1) * size, page * size),
        slots_remaining: 3, max_followed: 5, active_follow_count: 2,
        total: filtered, unfiltered_total: srv.rows.length, page, page_size: size,
        total_pages: Math.ceil(filtered / size),
      };
    }
    const requests = [];          // [{url, signal}]
    const urlsOf = (needle) => requests.filter(r => r.url.includes(needle)).map(r => r.url);
    const candReqs = () => urlsOf('/api/copy-trading/candidates');
    const lastCand = () => candReqs().slice(-1)[0];
    const params = (url) => Object.fromEntries(new URL('http://x' + url).searchParams);
    const abortErr = () => Object.assign(new Error('aborted'), { name: 'AbortError' });
    const posts = [];
    global.fetch = (url, opts = {}) => {
      url = String(url);
      requests.push({ url, signal: opts.signal });
      if (/\/history/.test(url)) {
        return Promise.resolve({ ok: true, json: async () => ({ runs: [
          { screened_at: '2026-08-01T00:00:00Z', median_roi: 0.2, eligible_to_follow: true },
          { screened_at: '2026-09-01T00:00:00Z', median_roi: 0.3, eligible_to_follow: true } ] }) });
      }
      if (/\/follow$/.test(url)) { posts.push(url); return Promise.resolve({ ok: true, json: async () => ({ success: true, message: 'ok' }) }); }
      if (url.startsWith('/api/copy-trading/candidates')) {
        const body = candidatesBody(url);
        return new Promise((resolve, reject) => {
          const respond = () => {
            if (srv.fail) return resolve({ ok: false, status: 500, json: async () => ({}) });
            resolve({ ok: true, json: async () => body });
          };
          if (opts.signal && !srv.ignoreAbort) {
            if (opts.signal.aborted) return reject(abortErr());
            opts.signal.addEventListener('abort', () => reject(abortErr()));
          }
          if (srv.hold) srv.held.push(respond); else respond();
        });
      }
      if (url.startsWith('/api/config')) return Promise.resolve({ ok: true, json: async () => ({ copy_trading: { COPY_LIVE_TRADING_ENABLED: { value: false } } }) });
      return Promise.resolve({ ok: true, json: async () => ({}) });
    };
    const releaseAll = async () => { const h = srv.held.splice(0); h.forEach(f => f()); await settle(); };

    // ---- helpers to drive the UI -------------------------------------------------------
    const list = el('copy-candidates-list');
    const click = (tab) => el('tab-btn-' + tab).click();
    const rowsOnScreen = () => (list.innerHTML.match(/class="copy-row copy-row-expandable"/g) || []).length;
    const addrsOnScreen = () => [...list.innerHTML.matchAll(/class="copy-row copy-row-expandable" id="copy-row-[^"]+" tabindex="0" role="button"\s+data-address="([^"]+)"/g)].map(m => m[1]);
    const showing = () => el('copy-showing').textContent;
    const pgInfo = () => el('copy-pg-info').textContent;
    const type = async (text) => { el('copy-search-input').value = text; el('copy-search-input').fire('input'); };
    const target = (map) => ({ closest: (sel) => (sel in map ? map[sel] : null) });
    const rowTarget = (address) => target({
      '.copy-row-expandable': { dataset: { address } },
      '[data-address]': { dataset: { address } },
    });
    const listClick = (t) => list.fire('click', { target: t, stopPropagation() {} });
    const sortBy = async (key) => { listClick(target({ '.copy-th-btn': { dataset: { sortKey: key } } })); await settle(); };
    const goNext = async () => { el('copy-pg-next').fire('click'); await settle(); };
    const goPrev = async () => { el('copy-pg-prev').fire('click'); await settle(); };
    const openWallets = async () => { click('copy-wallets'); await settle(); };
    const expand = async (address) => { listClick(rowTarget(address)); await settle(); };
    const armFollow = async (address) => {
      const armBtn = { closest: (sel) => (sel === '[data-address]' ? { dataset: { address } } : null) };
      listClick(target({ '.copy-follow-arm-btn': armBtn }));
      await settle();
    };
""")


def run_js(body: str, tmp_path: Path, storage: str = "{}", throws: bool = False) -> None:
    html_markup, script = _html_parts()
    prelude = _PRELUDE.replace("__HTML__", repr(html_markup)).replace("__STORAGE__", storage)
    prelude = prelude.replace("__THROWS__", "true" if throws else "false")
    code = (
        prelude
        + "\n"
        + script
        + "\n(async () => {\n"
        + textwrap.dedent(body)
        + "\n})().then(() => console.log('OK'), e => { console.log('FAIL'); console.log(e && e.stack || e); process.exit(1); });"
    )
    path = tmp_path / "copy_wallets_check.js"
    path.write_text(code, encoding="utf-8")
    r = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "OK" in r.stdout, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"


# ---------------------------------------------------------------------------
# First load + paging math
# ---------------------------------------------------------------------------

def test_first_load_requests_page_one_with_the_default_sort_and_renders_only_that_page(tmp_path):
    run_js("""
        await openWallets();
        assert.strictEqual(candReqs().length, 1);
        const p = params(lastCand());
        assert.deepStrictEqual(p, { page: '1', page_size: '25', sort: 'median_roi', dir: 'desc' });
        assert.ok(!('q' in p), 'no q without a search');
        assert.notStrictEqual(p.sort, 'followed'); assert.notStrictEqual(p.sort, 'unstable');
        assert.strictEqual(rowsOnScreen(), 25, 'DOM holds the page, not all 120 rows');
        assert.strictEqual(showing(), 'Showing 1–25 of 120');
        assert.strictEqual(pgInfo(), 'Page 1 of 5');
        assert.strictEqual(el('copy-pagination').hidden, false);
        assert.strictEqual(el('copy-pg-prev').disabled, true);
        assert.strictEqual(el('copy-pg-next').disabled, false);
        assert.ok(list.innerHTML.includes('rows 1 to 25 of 120'), 'table aria-label carries the range');
        assert.strictEqual(list._attrs['aria-busy'], 'false');
    """, tmp_path)


def test_prev_next_walk_the_pages_and_disable_at_the_ends(tmp_path):
    run_js("""
        await openWallets();
        await goNext();
        assert.strictEqual(params(lastCand()).page, '2');
        assert.strictEqual(pgInfo(), 'Page 2 of 5');
        assert.strictEqual(showing(), 'Showing 26–50 of 120');
        assert.strictEqual(el('copy-pg-prev').disabled, false);
        for (let i = 0; i < 3; i++) await goNext();
        assert.strictEqual(pgInfo(), 'Page 5 of 5');
        assert.strictEqual(showing(), 'Showing 101–120 of 120', 'last page is partial');
        assert.strictEqual(rowsOnScreen(), 20);
        assert.strictEqual(el('copy-pg-next').disabled, true);
        const n = candReqs().length;
        await goNext();     // past the end: no request
        assert.strictEqual(candReqs().length, n);
        await goPrev();
        assert.strictEqual(params(lastCand()).page, '4');
    """, tmp_path)


def test_pagination_footer_is_hidden_when_everything_fits_one_page(tmp_path):
    run_js("""
        srv.rows = ALL.slice(0, 10);
        await openWallets();
        assert.strictEqual(el('copy-pagination').hidden, true);
        assert.strictEqual(showing(), 'Showing 1–10 of 10');
        assert.ok(!pgInfo().includes('Page'));
    """, tmp_path)


# ---------------------------------------------------------------------------
# Server-side sort
# ---------------------------------------------------------------------------

def test_sort_is_requested_from_the_server_with_response_field_names_and_persists_across_pages(tmp_path):
    run_js("""
        await openWallets();
        await goNext(); await goNext();
        assert.strictEqual(params(lastCand()).page, '3');
        const before = candReqs().length;

        await sortBy('win_rate');
        assert.strictEqual(candReqs().length, before + 1, 'sorting is a server round-trip, not a client re-sort');
        let p = params(lastCand());
        assert.strictEqual(p.sort, 'win_rate'); assert.strictEqual(p.dir, 'desc');
        assert.strictEqual(p.page, '1', 'sort change resets to page 1');
        assert.strictEqual(pgInfo(), 'Page 1 of 5');

        // The rows really are the server's global order, not a re-sort of the old page.
        const expected = [...ALL].sort((a, b) => b.win_rate - a.win_rate).slice(0, 25).map(r => r.address);
        assert.deepStrictEqual(addrsOnScreen().map(a => a.toLowerCase()).sort(), [...expected].sort());

        await goNext();
        p = params(lastCand());
        assert.strictEqual(p.sort, 'win_rate'); assert.strictEqual(p.dir, 'desc'); assert.strictEqual(p.page, '2');

        await sortBy('win_rate');     // same key flips direction
        p = params(lastCand());
        assert.strictEqual(p.dir, 'asc'); assert.strictEqual(p.page, '1');
        assert.ok(list.innerHTML.includes('aria-sort="ascending"'));

        await sortBy('mean_roi');     // new key -> desc
        p = params(lastCand());
        assert.strictEqual(p.sort, 'mean_roi'); assert.strictEqual(p.dir, 'desc');
    """, tmp_path)


def test_every_sortable_header_uses_a_field_name_the_api_returns_and_none_is_a_slow_sort(tmp_path):
    run_js("""
        await openWallets();
        const keys = [...list.innerHTML.matchAll(/data-sort-key="([^"]+)"/g)].map(m => m[1]);
        assert.ok(keys.length >= 7);
        for (const k of keys) {
          assert.ok(k in ALL[0], k + ' is not a response field of the candidates payload');
          assert.ok(!['followed', 'unstable'].includes(k), k + ' is a slow server sort');
        }
    """, tmp_path)


# ---------------------------------------------------------------------------
# Page size
# ---------------------------------------------------------------------------

def test_page_size_change_resets_to_page_one_requests_that_size_and_is_persisted(tmp_path):
    run_js("""
        await openWallets();
        await goNext();
        el('copy-page-size').value = '50';
        el('copy-page-size').fire('change');
        await settle();
        const p = params(lastCand());
        assert.strictEqual(p.page_size, '50'); assert.strictEqual(p.page, '1');
        assert.strictEqual(rowsOnScreen(), 50);
        assert.strictEqual(pgInfo(), 'Page 1 of 3');
        assert.deepStrictEqual(storage.sets, [['copyWalletsPageSize', '50']]);

        el('copy-page-size').value = '7';      // not an allowed size: ignored
        el('copy-page-size').fire('change');
        await settle();
        assert.strictEqual(params(lastCand()).page_size, '50');
    """, tmp_path)


def test_a_persisted_page_size_is_used_on_load_and_the_select_shows_it(tmp_path):
    run_js("""
        await openWallets();
        assert.strictEqual(params(lastCand()).page_size, '100');
        assert.strictEqual(el('copy-page-size').value, '100');
        assert.strictEqual(rowsOnScreen(), 100);
    """, tmp_path, storage='{"copyWalletsPageSize": "100"}')


def test_a_garbage_persisted_page_size_falls_back_to_25(tmp_path):
    run_js("""
        await openWallets();
        assert.strictEqual(params(lastCand()).page_size, '25');
    """, tmp_path, storage='{"copyWalletsPageSize": "9999"}')


def test_blocked_localstorage_never_breaks_the_view(tmp_path):
    # Storage throws from the very start (getItem at load, setItem on change).
    run_js("""
        await openWallets();
        assert.strictEqual(params(lastCand()).page_size, '25');
        el('copy-page-size').value = '50';
        el('copy-page-size').fire('change');
        await settle();
        assert.strictEqual(params(lastCand()).page_size, '50');
        assert.strictEqual(rowsOnScreen(), 50);
    """, tmp_path, throws=True)


# ---------------------------------------------------------------------------
# Search: min chars, debounce, abort, stale-response guard
# ---------------------------------------------------------------------------

def test_search_sends_q_only_from_three_chars_after_a_200ms_debounce(tmp_path):
    run_js("""
        await openWallets();
        await goNext();
        const n = candReqs().length;

        await type('0'); await advance(50); await type('0x'); await advance(500);
        assert.strictEqual(candReqs().length, n, 'under 3 chars: no request at all');

        await type('0x0'); await advance(100); await type('0x00'); await advance(100);
        assert.strictEqual(candReqs().length, n, 'debounce: nothing before 200 ms of quiet');
        await advance(150);
        assert.strictEqual(candReqs().length, n + 1, 'exactly one request for the burst');
        const p = params(lastCand());
        assert.strictEqual(p.q, '0x00'); assert.strictEqual(p.page, '1', 'search resets to page 1');
        assert.strictEqual(p.sort, 'median_roi');
        assert.strictEqual(el('copy-search-clear').hidden, false);

        await type('0x'); await advance(250);        // back under 3 chars -> unfiltered again
        assert.ok(!('q' in params(lastCand())));
        assert.strictEqual(candReqs().length, n + 2);
    """, tmp_path)


def test_search_matches_a_substring_and_shows_filtered_counts(tmp_path):
    run_js("""
        await openWallets();
        await type('abc'); await advance(250);
        const matches = ALL.filter(r => r.address.includes('abc')).length;
        assert.ok(matches > 0 && matches <= 25);
        assert.strictEqual(rowsOnScreen(), matches);
        assert.strictEqual(showing(), `Showing 1–${matches} of ${matches} (filtered from 120)`);
        assert.strictEqual(el('copy-pagination').hidden, true);
    """, tmp_path)


def test_a_new_search_aborts_the_inflight_request_and_a_stale_response_never_wins(tmp_path):
    run_js("""
        await openWallets();
        srv.hold = true;
        await type('0x00'); await advance(250);
        const first = requests.filter(r => r.url.includes('q=0x00'))[0];
        assert.ok(first && first.signal && !first.signal.aborted);
        await type('abc'); await advance(250);
        assert.strictEqual(first.signal.aborted, true, 'the older request is aborted by the newer one');
        srv.hold = false;
        await releaseAll();
        assert.ok(params(lastCand()).q === 'abc');
        assert.ok(list.innerHTML.includes('abc'), 'the newer query is what is on screen');
    """, tmp_path)


def test_stale_response_is_dropped_even_if_the_transport_ignores_the_abort(tmp_path):
    run_js("""
        await openWallets();
        srv.hold = true; srv.ignoreAbort = true;
        await type('0x00'); await advance(250);       // request A (will resolve LAST)
        await type('abc'); await advance(250);        // request B
        const [respondA, respondB] = srv.held.splice(0);
        respondB(); await settle();
        const afterB = list.innerHTML;
        assert.ok(afterB.includes('abc'));
        respondA(); await settle();
        assert.strictEqual(list.innerHTML, afterB, 'the late answer to the OLD query must not overwrite the newer one');
        assert.strictEqual(showing().includes('filtered from'), true);
    """, tmp_path)


def test_clear_button_and_escape_reset_the_search_to_page_one(tmp_path):
    run_js("""
        await openWallets();
        await type('abc'); await advance(250);
        assert.strictEqual(params(lastCand()).q, 'abc');
        el('copy-search-clear').fire('click'); await settle();
        assert.ok(!('q' in params(lastCand())));
        assert.strictEqual(el('copy-search-input').value, '');
        assert.strictEqual(el('copy-search-clear').hidden, true);
        assert.strictEqual(rowsOnScreen(), 25);

        await type('abc'); await advance(250);
        el('copy-search-input').fire('keydown', { key: 'Escape' }); await settle();
        assert.ok(!('q' in params(lastCand())));
    """, tmp_path)


def test_no_match_shows_an_explicit_empty_state_never_page_1_of_0(tmp_path):
    run_js("""
        await openWallets();
        await type('zzzz'); await advance(250);
        assert.strictEqual(candReqs().length, 2);
        assert.ok(list.innerHTML.includes('No wallets match'));
        assert.ok(list.innerHTML.includes('zzzz'));
        assert.ok(list.innerHTML.includes('data-clear-search'));
        assert.strictEqual(el('copy-pagination').hidden, true);
        assert.ok(!/Page \\d+ of 0/.test(pgInfo()), pgInfo());
        assert.strictEqual(showing(), '');
    """, tmp_path)


def test_clear_search_link_in_the_empty_row_restores_the_list(tmp_path):
    run_js("""
        await openWallets();
        await type('zzzz'); await advance(250);
        listClick(target({ '[data-clear-search]': {} }));
        await settle();
        assert.ok(!('q' in params(lastCand())), 'clearing re-requests the unfiltered list');
        assert.strictEqual(rowsOnScreen(), 25);
    """, tmp_path)


def test_never_screened_shows_the_original_empty_state(tmp_path):
    run_js("""
        srv.rows = [];
        await openWallets();
        assert.ok(list.innerHTML.includes('No wallets screened yet'));
        assert.strictEqual(el('copy-pagination').hidden, true);
        assert.ok(!list.innerHTML.includes('No wallets match'));
    """, tmp_path)


# ---------------------------------------------------------------------------
# Refresh keeps state; expanded row; armed Follow
# ---------------------------------------------------------------------------

def test_background_refresh_keeps_page_sort_search_size_expanded_row_and_armed_follow(tmp_path):
    run_js("""
        await openWallets();
        await sortBy('n_resolved');
        await goNext();
        const addr = addrsOnScreen()[2];
        await expand(addr);
        await armFollow(addr);
        const stake = el('copy-stake-' + addr.replace(/[^a-zA-Z0-9]/g, '_'));
        stake.value = '12.5';
        list.fire('input', { target: Object.assign(stake, { classList: { contains: c => c === 'copy-stake-input' }, closest: () => ({ dataset: { address: addr } }) }) });
        stake.focus();
        assert.strictEqual(document.activeElement, stake);

        srv.rows = ALL.map(r => ({ ...r, mean_roi: r.mean_roi + 0.001 }));   // data changed on the server
        const before = candReqs().length;
        await advance(5 * 60_000);          // controller poll
        assert.strictEqual(candReqs().length, before + 1, 'one poll per 5 min, no more');
        const p = params(lastCand());
        assert.deepStrictEqual([p.page, p.page_size, p.sort, p.dir], ['2', '25', 'n_resolved', 'desc']);
        assert.strictEqual(pgInfo(), 'Page 2 of 5');

        assert.ok(list.innerHTML.includes('mean_roi') || rowsOnScreen() === 25);
        assert.strictEqual(el('copy-detail-row-' + addr.replace(/[^a-zA-Z0-9]/g, '_')).style.display, 'table-row', 'row still expanded');
        const section = el('copy-follow-section-' + addr.replace(/[^a-zA-Z0-9]/g, '_'));
        assert.ok(section.innerHTML.includes('copy-stake-'), 'Follow input is still armed');
        assert.ok(section.innerHTML.includes('value="12.5"'), 'typed stake preserved');
        const stake2 = el('copy-stake-' + addr.replace(/[^a-zA-Z0-9]/g, '_'));
        assert.notStrictEqual(stake2, stake, 'the DOM really was rebuilt');
        assert.strictEqual(document.activeElement, stake2, 'focus restored into the armed input');
    """, tmp_path)


def test_history_is_fetched_once_per_address_not_on_every_rebuild(tmp_path):
    run_js("""
        await openWallets();
        const addr = addrsOnScreen()[0];
        await expand(addr);
        assert.strictEqual(urlsOf('/history').length, 1);
        const detail = el('copy-detail-' + addr.replace(/[^a-zA-Z0-9]/g, '_'));
        assert.ok(detail.innerHTML.includes('Median ROI'));
        srv.rows = ALL.map(r => ({ ...r, win_rate: r.win_rate + 0.01 }));
        await advance(5 * 60_000);
        assert.strictEqual(urlsOf('/history').length, 1, 'refresh re-renders the detail from cache');
        assert.ok(el('copy-detail-' + addr.replace(/[^a-zA-Z0-9]/g, '_')).innerHTML.includes('Median ROI'));
    """, tmp_path)


def test_unchanged_response_skips_the_dom_rebuild_but_changed_data_rebuilds(tmp_path):
    run_js("""
        await openWallets();
        const writes = list.writes;
        await advance(5 * 60_000);
        assert.strictEqual(candReqs().length, 2, 'the poll did request');
        assert.strictEqual(list.writes, writes, 'identical payload: no rebuild');
        srv.rows = ALL.map(r => ({ ...r, n_resolved: r.n_resolved + 1 }));
        await advance(5 * 60_000);
        assert.ok(list.writes > writes, 'changed payload: rebuilt');
    """, tmp_path)


def test_paging_away_collapses_the_expanded_row_and_it_does_not_come_back(tmp_path):
    run_js("""
        await openWallets();
        const addr = addrsOnScreen()[0];
        await expand(addr);
        assert.ok(_copyExpanded.has(addr));
        await goNext();
        assert.strictEqual(_copyExpanded.size, 0, 'paging away collapses');
        await goPrev();
        const id = 'copy-detail-row-' + addr.replace(/[^a-zA-Z0-9]/g, '_');
        assert.strictEqual(el(id).style.display, 'none', 'not re-expanded when paging back');
    """, tmp_path)


def test_sort_search_and_size_changes_collapse_the_expanded_row_too(tmp_path):
    run_js("""
        await openWallets();
        let addr = addrsOnScreen()[0];
        await expand(addr); await sortBy('win_rate');
        assert.strictEqual(_copyExpanded.size, 0);
        addr = addrsOnScreen()[0];
        await expand(addr); await type('abc'); await advance(250);
        assert.strictEqual(_copyExpanded.size, 0);
    """, tmp_path)


def test_following_refreshes_the_same_page_and_sort(tmp_path):
    run_js("""
        await openWallets();
        await sortBy('win_rate'); await goNext();
        const addr = addrsOnScreen()[0];
        await expand(addr); await armFollow(addr);
        el('copy-stake-' + addr.replace(/[^a-zA-Z0-9]/g, '_')).value = '7';
        srv.rows = ALL.map(r => r.address === addr ? { ...r, followed: true, follow_status: 'active' } : r);
        const btn = { disabled: false, classList: { add() {}, remove() {} } };
        await submitFollow(addr, btn);
        await settle();
        assert.strictEqual(posts.length, 1);
        const p = params(lastCand());
        assert.deepStrictEqual([p.page, p.sort, p.dir], ['2', 'win_rate', 'desc']);
        assert.ok(list.innerHTML.includes('copy-followed-tag'), 'row now shows Followed');
        assert.ok(el('copy-detail-' + addr.replace(/[^a-zA-Z0-9]/g, '_')).innerHTML.includes('Already followed'));
        assert.strictEqual(_copyArmed.size, 0);
    """, tmp_path)


# ---------------------------------------------------------------------------
# Loading / failure / races
# ---------------------------------------------------------------------------

def test_page_change_keeps_prior_rows_dimmed_until_the_new_page_lands(tmp_path):
    run_js("""
        await openWallets();
        const page1 = list.innerHTML;
        srv.hold = true;
        await goNext();
        assert.strictEqual(list.innerHTML, page1, 'prior rows stay on screen (no skeleton swap, no layout jump)');
        assert.ok(list.classList.contains('copy-list-loading'));
        assert.strictEqual(list._attrs['aria-busy'], 'true');
        srv.hold = false; await releaseAll();
        assert.ok(!list.classList.contains('copy-list-loading'));
        assert.strictEqual(list._attrs['aria-busy'], 'false');
        assert.notStrictEqual(list.innerHTML, page1);
    """, tmp_path)


def test_failed_page_change_keeps_last_known_good_rows_shows_the_banner_and_rolls_back_state(tmp_path):
    run_js("""
        await openWallets();
        const good = list.innerHTML;
        srv.fail = true;
        await goNext();
        assert.ok(el('copy-wallets-error-banner').classList.contains('visible'));
        assert.ok(el('copy-wallets-error-text').textContent.includes('last-known'));
        assert.ok(list.innerHTML.includes(addrsOnScreen()[0]));
        assert.strictEqual(pgInfo(), 'Page 1 of 5', 'footer still describes the rows on screen');
        assert.strictEqual(showing(), 'Showing 1–25 of 120');
        assert.ok(!list.classList.contains('copy-list-loading'));

        await sortBy('win_rate');            // failing sort rolls back too
        assert.ok(list.innerHTML.includes('Sort by Median ROI, currently descending'));

        srv.fail = false;
        el('copy-refresh-btn').fire('click'); await settle();
        assert.ok(!el('copy-wallets-error-banner').classList.contains('visible'), 'banner clears on success');
        assert.deepStrictEqual(Object.values(params(lastCand())).slice(0, 4), ['1', '25', 'median_roi', 'desc']);
    """, tmp_path)


def test_first_load_failure_shows_the_error_state_and_manual_refresh_recovers(tmp_path):
    run_js("""
        srv.fail = true;
        await openWallets();
        assert.ok(list.innerHTML.includes('Could not load candidates'));
        srv.fail = false;
        el('copy-refresh-btn').fire('click'); await settle();
        assert.strictEqual(rowsOnScreen(), 25);
    """, tmp_path)


def test_a_page_past_the_end_after_the_list_shrinks_falls_back_to_the_last_page(tmp_path):
    run_js("""
        await openWallets();
        for (let i = 0; i < 4; i++) await goNext();
        assert.strictEqual(pgInfo(), 'Page 5 of 5');
        srv.rows = ALL.slice(0, 60);                 // shrank to 3 pages
        await advance(5 * 60_000);
        assert.strictEqual(pgInfo(), 'Page 3 of 3');
        assert.strictEqual(rowsOnScreen(), 10);
        assert.deepStrictEqual(candReqs().slice(-2).map(u => params(u).page), ['5', '3']);
    """, tmp_path)


def test_a_poll_never_aborts_or_duplicates_a_user_request_in_flight(tmp_path):
    run_js("""
        await openWallets();
        srv.hold = true;
        await goNext();                              // user request in flight
        const user = requests[requests.length - 1];
        const n = candReqs().length;
        await advance(5 * 60_000);                   // poll fires meanwhile
        assert.strictEqual(candReqs().length, n, 'poll is skipped while a request is out');
        assert.strictEqual(user.signal.aborted, false);
        srv.hold = false; await releaseAll();
        assert.strictEqual(pgInfo(), 'Page 2 of 5');
    """, tmp_path)


# ---------------------------------------------------------------------------
# Polling rules
# ---------------------------------------------------------------------------

def test_candidates_poll_is_5_minutes_only_while_active_and_visible(tmp_path):
    run_js("""
        await openWallets();
        assert.strictEqual(candReqs().length, 1);
        await advance(4 * 60_000);
        assert.strictEqual(candReqs().length, 1, 'no auto-poll before 5 min');
        await advance(60_000);
        assert.strictEqual(candReqs().length, 2);

        setHidden(true);
        await advance(20 * 60_000);
        assert.strictEqual(candReqs().length, 2, 'hidden document: no polling');
        setHidden(false); await settle();
        assert.strictEqual(candReqs().length, 3, 'one catch-up fetch on return');

        click('copy-paper'); await settle();
        await advance(20 * 60_000);
        assert.strictEqual(candReqs().length, 3, 'no polling once the tab is left');
        click('copy-wallets'); await settle();
        assert.strictEqual(candReqs().length, 4, 'stale (> 5 min) data is refreshed on return');
        click('copy-paper'); await settle();
        click('copy-wallets'); await settle();
        assert.strictEqual(candReqs().length, 4, 'fresh data is not re-fetched on a quick return');
        assert.strictEqual(rowsOnScreen(), 25);
    """, tmp_path)


def test_page_and_sort_survive_leaving_and_returning_to_the_tab(tmp_path):
    run_js("""
        await openWallets();
        await sortBy('win_rate'); await goNext();
        click('copy-paper'); await settle();
        await advance(6 * 60_000);
        click('copy-wallets'); await settle();          // stale -> refreshed with the SAME state
        const p = params(lastCand());
        assert.deepStrictEqual([p.page, p.sort, p.dir], ['2', 'win_rate', 'desc']);
    """, tmp_path)


# ---------------------------------------------------------------------------
# Activity-feed jump on a paged list
# ---------------------------------------------------------------------------

def test_jump_to_a_wallet_not_on_the_page_finds_it_by_address_search_and_expands_it(tmp_path):
    run_js("""
        await openWallets();
        const far = ALL[110].address;                 // page 5 under the default sort
        assert.ok(!addrsOnScreen().includes(far));
        _copyActivityJumpToWallet('signal', far);
        await settle();
        assert.strictEqual(params(lastCand()).q, far);
        assert.strictEqual(el('copy-search-input').value, far);
        assert.deepStrictEqual(addrsOnScreen(), [far]);
        assert.ok(_copyExpanded.has(far));
    """, tmp_path)


def test_jump_to_an_unknown_wallet_does_not_loop(tmp_path):
    run_js("""
        await openWallets();
        _copyActivityJumpToWallet('signal', '0xnotscreened000000');
        await settle();
        assert.strictEqual(candReqs().length, 2, 'one search, no retry loop');
        assert.ok(list.innerHTML.includes('No wallets match'));
    """, tmp_path)

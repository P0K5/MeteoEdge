"""Behavior tests for the Copy · Paper tab (epic #1272, issue #1277).

Runs the REAL inline <script> of src/dashboard/static/index.html under Node
against a DOM stub, like test_dashboard_copy_tab_controller_behavior.py, with
two additions that make "is this element on the page?" checks honest:

* `document.getElementById` returns null for an id that is not currently in
  the page -- ids come from the real static markup, plus the ids inside any
  `innerHTML` a script writes, and they disappear again when that container's
  `innerHTML` is overwritten (so a rebuilt row is a NEW element, exactly as in
  a browser; `_writes` counts how often a container was rebuilt);
* `Chart` is a recording fake (instances / update() / destroy()).

Timers and Date.now are a virtual clock; `fetch` is a URL-keyed stub.

Covers: paper KPI cards, the HARD RULE that no live figure is ever rendered on
Paper (even though the shared payloads carry live fields), the paper roster
actions, "Go live ->" jump + highlight, Recent Closed paging (newest first,
15/page), the paper activity feed (mode locked, 25/page, filter reset), the
Chart.js lifecycle (reuse / update / destroy on tab leave) and "unchanged ->
no DOM rebuild", and per-section error states.
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

    // ---- DOM stub with a live id registry --------------------------------
    const _reg = new Map();          // id -> Element currently "in the page"
    class Element {
      constructor(id) {
        this.id = id || '';
        this._classes = new Set();
        this._attrs = {};
        this._listeners = {};
        this._innerHTML = '';
        this._textContent = '';
        this._kids = [];
        this._writes = 0;
        this.style = {};
        this.dataset = {};
        this.disabled = false;
        this.value = '';
        this.offsetWidth = 0;
        this.scrollTop = 0;
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
        this._clearKids();
        this._innerHTML = String(v);
        this._writes += 1;
        const ids = [...this._innerHTML.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]);
        for (const id of ids) { const k = new Element(id); _reg.set(id, k); this._kids.push(k); }
      }
      _clearKids() {
        for (const k of this._kids) { k._clearKids(); if (_reg.get(k.id) === k) _reg.delete(k.id); }
        this._kids = [];
      }
      get textContent() { return this._textContent; }
      set textContent(v) { this._textContent = String(v); }
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
      scrollIntoView(opts) { this._scrolled = (this._scrolled || 0) + 1; this._scrollOpts = opts; }
      fire(type, ev) { (this._listeners[type] || []).forEach(h => h(ev)); }
      get all() { return this._innerHTML + '|' + this._textContent + '|' + JSON.stringify(this._attrs); }
    }
    function el(id) {
      const e = _reg.get(id);
      if (!e) throw new Error('element not in page: ' + id);
      return e;
    }
    const has = (id) => _reg.has(id);

    const _tabButtons = [];
    const _tabPanels = [];
    const _banners = [];
    (function buildFromHtml(html) {
      for (const m of html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)) {
        const e = _reg.get(m[1]) || new Element(m[1]);
        _reg.set(m[1], e);
        // Static attributes the tests care about: aria-busy and inline style.
        if (/aria-busy="true"/.test(m[0])) e.setAttribute('aria-busy', 'true');
        const st = m[0].match(/\bstyle="([^"]*)"/);
        if (st) st[1].split(';').filter(Boolean).forEach(kv => { const [k, v] = kv.split(':'); e.style[k.trim()] = v.trim(); });
      }
      const btnRe = /<button class="tab-btn[^"]*"(?: id="([^"]+)")? onclick="switchTab\('([^']+)'\)"/g;
      let m;
      while ((m = btnRe.exec(html))) {
        const bid = m[1] || ('tab-btn-' + m[2]);
        if (!_reg.has(bid)) _reg.set(bid, new Element(bid));
        const b = el(bid);
        b._tab = m[2];
        b.classList.add('tab-btn');
        b.click = () => { global.event = { target: b }; switchTab(b._tab); };
        _tabButtons.push(b);
      }
      const panelRe = /<section id="(tab-[^"]+)" class="tab-panel/g;
      while ((m = panelRe.exec(html))) { const p = el(m[1]); p.classList.add('tab-panel'); _tabPanels.push(p); }
      const banRe = /<span id="([^"]+)" class="[^"]*copy-trading-mode-banner[^"]*"/g;
      while ((m = banRe.exec(html))) {
        const b = el(m[1]);
        b.className = 'mode-badge mode-badge-paper copy-trading-mode-banner';
        _banners.push(b);
      }
      // Static ids inside the Paper panel's markup (for "is this a paper element").
      const sec = html.match(/<section id="tab-copy-paper"[\s\S]*?<\/section>/)[0];
      global.PAPER_STATIC_IDS = [...sec.matchAll(/\bid="([^"]+)"/g)].map(x => x[1]);
    })(__HTML__);
    _tabPanels[0].classList.add('active');
    _tabButtons[0].classList.add('active');

    // ---- virtual clock + timers -------------------------------------------
    let _now = Date.parse('2026-09-15T12:00:00Z');
    Date.now = () => _now;
    let _tid = 0;
    const _intervals = new Map();
    global.setInterval = (fn, every) => { const id = ++_tid; _intervals.set(id, { fn, every, next: _now + every }); return id; };
    global.clearInterval = (id) => { _intervals.delete(id); };
    const settle = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setImmediate(r)); };

    // ---- document / window ----------------------------------------------
    const _docListeners = {};
    global.document = {
      hidden: false,
      getElementById: (id) => _reg.get(id) || null,
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
    global.window = { addEventListener() {}, prompt: () => 'because', confirm: () => true };
    global.localStorage = { getItem() { return null; }, setItem() {} };
    global.lucide = { createIcons() {} };
    global.getComputedStyle = () => ({ getPropertyValue: () => '#123456' });
    global.event = { target: new Element() };
    Object.defineProperty(global, 'navigator', { value: { clipboard: { writeText: async () => {} } }, writable: true, configurable: true });
    console.error = () => {};

    // ---- recording Chart.js fake -----------------------------------------
    const charts = [];
    global.Chart = function (canvas, cfg) {
      this.canvas = canvas; this.data = cfg.data; this.options = cfg.options;
      this.updates = 0; this.destroyed = false;
      this.update = () => { this.updates += 1; };
      this.destroy = () => { this.destroyed = true; };
      charts.push(this);
    };
    const liveCharts = () => charts.filter(c => !c.destroyed && c.canvas.id === 'copy-positions-canvas');   // the PAPER chart only
    const paperChartsCreated = () => charts.filter(c => c.canvas.id === 'copy-positions-canvas');

    // ---- payload builders -----------------------------------------------------
    // Sentinel values: a number that appears ONLY in a live field, so finding it
    // anywhere on the Paper tab proves a live figure leaked.
    const LIVE = { aggPnl: '7777.77', total: '8888.88', stake: '3131.31', cap: '4242.42',
                   open: '9999.99', hist: '6666.66', evt: '2323.23', settled: '5151', mkt: 'LIVE-MKT-SENTINEL' };
    const iso = (daysAgo, extraMin = 0) => new Date(_now - daysAgo * 86_400_000 + extraMin * 60_000).toISOString();
    function wallet(addr, over) {
      return Object.assign({ address: addr, stake_per_trade: 7, status: 'active', paused_reason: null,
        added_at: '2026-09-02T00:00:00Z', n_settled: 1, realized_pnl_usd: -3.25,
        live_enabled: false, live_eligible: false, live_status_reason: 'live is not enabled for this wallet',
        live_stake_per_trade: Number(LIVE.stake), live_stake_is_override: true }, over || {});
    }
    function followed(over) {
      return Object.assign({
        wallets: [wallet('0xPaperOne'), wallet('0xPausedOne', { status: 'paused', paused_reason: 'manual' }),
                  wallet('0xLiveOne', { live_enabled: true, live_eligible: true,
                    live_status_reason: 'eligible for live execution' })],
        active_count: 2, paused_count: 1, aggregate_pnl_usd: 9.25, n_settled_total: 4,
        live_eligible_count: 1, paper_only_count: 2,
        live_aggregate_pnl_usd: Number(LIVE.aggPnl), live_n_settled_total: Number(LIVE.settled),
        live_trading_enabled: true, live_cap_usd: Number(LIVE.cap), live_opted_in_count: 1,
      }, over || {});
    }
    function positions(over) {
      return Object.assign({
        total: { realized_pnl_usd: 4.5, n_settled: 3 },
        open_positions: [
          { id: 1, address: '0xPaperOne', market: 'Will it rain in Paris?', outcome_index: 0, entry_price: 0.4, stake_usd: 5, entry_ts: '2026-09-14T12:00:00.000Z', signal_id: 11 },
          { id: 2, address: '0xPaperOne', market: 'Will it snow in Oslo?', outcome_index: 1, entry_price: 0.3, stake_usd: 7.25, entry_ts: '2026-09-14T12:00:00.000Z', signal_id: 12 },
        ],
        per_wallet: [{ address: '0xPaperOne', n_settled: 3, realized_pnl_usd: 4.5,
                       projected_flat_dollar_pnl: 6, divergence_usd: -1.5, divergence_pct: -25 }],
        realized_pnl_history: [
          { address: '0xPaperOne', market: 'M-a', settled_at: iso(40), settled_pnl_usd: 1, stake_usd: 5 },
          { address: '0xPaperOne', market: 'M-b', settled_at: iso(5), settled_pnl_usd: 2, stake_usd: 5 },
          { address: '0xPaperOne', market: 'M-c', settled_at: iso(2), settled_pnl_usd: 1.5, stake_usd: 5 },
        ],
        backtest_total: { n_wallets: 1, n_settled: 3, realized_pnl_usd: 4.5, projected_flat_dollar_pnl: 6, divergence_usd: -1.5 },
        live_total: { realized_pnl_usd: Number(LIVE.total), n_settled: Number(LIVE.settled) },
        live_open_positions: [{ id: 91, address: '0xLiveOne', market: LIVE.mkt, outcome_index: 0, status: 'filled',
                                fill_price: 0.5, stake_usd: Number(LIVE.open), entry_ts: iso(1), signal_id: 21 }],
        live_per_wallet: [{ address: '0xLiveOne', n_settled: 2, realized_pnl_usd: Number(LIVE.hist) }],
        live_realized_pnl_history: [{ address: '0xLiveOne', market: LIVE.mkt, settled_at: iso(1), settled_pnl_usd: Number(LIVE.hist), stake_usd: 3 }],
        live_error: null,
      }, over || {});
    }
    const ev = (i, over) => Object.assign({ ts: iso(0, -i), mode: 'paper', event_type: 'order_placed',
      address: '0xPaperOne', market: 'E' + String(i).padStart(2, '0'), fill_price: 0.5, size_usd: 5 }, over || {});

    const state = {
      config: { copy_trading: { COPY_LIVE_TRADING_ENABLED: { value: false } } },
      followed: followed(), positions: positions(),
      activity: { events: [ev(0), ev(1, { mode: 'live', event_type: 'live_order_filled', market: LIVE.mkt, size_usd: Number(LIVE.evt) })] },
      fail: new Set(),
    };
    const requests = [];
    const count = (needle) => requests.filter(u => u.includes(needle)).length;
    function bodyFor(url) {
      if (/\/wallets\/[^/]+\/(pause|resume|unfollow|live)$/.test(url)) return { success: true, message: 'ok' };
      if (url.startsWith('/api/config')) return state.config;
      if (url.startsWith('/api/copy-trading/followed-wallets')) return state.followed;
      if (url.startsWith('/api/copy-trading/positions')) return state.positions;
      if (url.startsWith('/api/copy-trading/activity-feed')) return state.activity;
      if (url.startsWith('/api/copy-trading/balance-drift')) return { within_tolerance: true };
      if (url.startsWith('/api/copy-trading/candidates')) return { candidates: [], slots_remaining: 5, max_followed: 5 };
      return {};
    }
    global.fetch = (url, opts) => {
      url = String(url);
      requests.push((opts && opts.method ? opts.method + ' ' : '') + url);
      if ([...state.fail].some(f => url.includes(f))) return Promise.resolve({ ok: false, status: 503, json: async () => ({}) });
      const body = bodyFor(url);
      return Promise.resolve({ ok: true, json: async () => body });
    };
    const click = (tab) => el('tab-btn-' + tab).click();
    // Fresh poll of the Paper tab's sources (the shared 10 s cache is dropped
    // by moving the clock past its TTL).
    async function repoll() {
      _now += 60_000;
      await fetchFollowedWallets('paper');
      await fetchCopyTradingPositions('paper');
      await fetchCopyTradingActivityFeed('paper');
      await settle();
    }
    // Everything currently "in the page" that is NOT part of the Live/Wallets panels.
    const paperText = () => [..._reg.values()].map(e => e.all).join('\n');
    const rowCount = (html) => (html.match(/<tr class="copy-row"/g) || []).length;
    const marketsIn = (html) => [...html.matchAll(/<td class="copy-td" title="([^"]*)">/g)].map(m => m[1]);
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
    path = tmp_path / "copy_paper_tab_check.js"
    path.write_text(code, encoding="utf-8")
    r = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "OK" in r.stdout, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"


# ---------------------------------------------------------------------------
# KPI cards
# ---------------------------------------------------------------------------

def test_kpi_cards_are_skeletons_until_data_then_paper_figures(tmp_path):
    run_js("""
        // Before any payload: skeleton blocks, busy.
        for (const id of ['pnl', 'open', 'followed']) {
          assert.strictEqual(el('paper-kpi-' + id).getAttribute('aria-busy'), 'true', id + ' starts busy');
        }
        click('copy-paper');
        await settle();
        assert.strictEqual(el('paper-kpi-pnl').textContent, '+$4.50');
        assert.ok(el('paper-kpi-pnl').classList.contains('pos'));
        assert.strictEqual(el('paper-kpi-pnl-sub').textContent, 'Paper · realized · 3 settled');
        assert.strictEqual(el('paper-kpi-open').textContent, '2');
        assert.strictEqual(el('paper-kpi-open-sub').textContent, 'Paper cost basis $12.25');
        assert.strictEqual(el('paper-kpi-followed').textContent, '3');
        assert.strictEqual(el('paper-kpi-followed-sub').textContent, '2 active · 1 paused (paper)');
        for (const id of ['pnl', 'open', 'followed']) assert.strictEqual(el('paper-kpi-' + id).getAttribute('aria-busy'), null);

        // A losing book flips to the negative class.
        state.positions = positions({ total: { realized_pnl_usd: -2, n_settled: 1 } });
        await repoll();
        assert.strictEqual(el('paper-kpi-pnl').textContent, '-$2.00');
        assert.ok(el('paper-kpi-pnl').classList.contains('neg') && !el('paper-kpi-pnl').classList.contains('pos'));
    """, tmp_path)


def test_every_kpi_card_label_says_paper_and_there_is_no_combined_card():
    html, _ = _html_parts()
    kpis = re.search(r'<div class="wallet-row copy-paper-kpis".*?\n      </div>\n', html, re.S).group(0)
    labels = re.findall(r'<div class="wc-label">(.*?)</div>', kpis)
    assert len(labels) == 3
    assert all("paper" in lbl.lower() for lbl in labels), labels
    assert not re.search(r"\blive\b|all modes|total", kpis, re.I)


# ---------------------------------------------------------------------------
# HARD RULE: no live figure on the Paper tab
# ---------------------------------------------------------------------------

def test_no_live_figure_is_rendered_on_paper_even_though_payloads_carry_live_fields(tmp_path):
    run_js("""
        // Sanity: the shared payloads really DO carry the live sentinels.
        const wire = JSON.stringify([state.followed, state.positions, state.activity]);
        for (const v of Object.values(LIVE)) assert.ok(wire.includes(v), 'sentinel ' + v + ' is in the payloads');

        click('copy-paper');
        await settle();
        // Exercise every paper control that re-renders, too.
        _copyToggleBacktestComparison();
        await repoll();

        const text = paperText();
        for (const [k, v] of Object.entries(LIVE)) {
          assert.ok(!text.includes(v), 'live sentinel ' + k + '=' + v + ' leaked onto the Paper tab');
        }
        assert.ok(!text.includes('LIVE-MKT'), 'a live activity row must not render on Paper');
        // The paper roster still shows only the paper stake.
        assert.ok(el('copy-followed-list').innerHTML.includes('Paper: $7.00'));
        assert.ok(!/Live: \\$/.test(el('copy-followed-list').innerHTML));

        // Control: the SAME payloads do show those live figures on the Live tab,
        // so the assertions above can fail if Paper ever started rendering them.
        click('copy-live');
        await settle();
        const live = paperText();
        for (const v of [LIVE.total, LIVE.mkt]) assert.ok(live.includes(v), 'control: Live tab shows ' + v);
    """, tmp_path)


def test_paper_activity_feed_is_mode_locked_and_drops_off_mode_rows(tmp_path):
    run_js("""
        click('copy-paper');
        await settle();
        assert.deepStrictEqual(requests.filter(u => u.includes('activity-feed')), ['/api/copy-trading/activity-feed?mode=paper']);
        assert.ok(!has('copy-activity-mode-select'), 'no Mode select on Paper');
        const list = el('copy-activity-list').innerHTML;
        assert.ok(list.includes('E00') && !list.includes('LIVE-MKT-SENTINEL'));
        assert.ok(!list.includes('mode-badge-live'));
    """, tmp_path)


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------

def test_roster_rows_keep_paper_actions_and_stack_labels_for_narrow_screens(tmp_path):
    run_js("""
        click('copy-paper');
        await settle();
        const html = el('copy-followed-list').innerHTML;
        assert.ok(html.includes('copy-table copy-table--roster'), 'roster table carries the stacking class');
        // Active wallet: Pause, Edit stake, Go live link, Unfollow.
        const row = html.split('<tr class="copy-row"').filter(r => r.includes('0xPaperOne') && r.includes('followed-actions-cell'))[0];
        for (const cls of ['btn-followed-pause', 'btn-followed-edit-stake', 'btn-followed-goto-live', 'btn-followed-unfollow']) {
          assert.ok(row.includes(cls), cls);
        }
        // Paused wallet: Resume, and no Go live link.
        const paused = html.split('<tr class="copy-row"').filter(r => r.includes('0xPausedOne'))[0];
        assert.ok(paused.includes('btn-followed-resume') && !paused.includes('btn-followed-goto-live'));
        // Labels used by the <=600px stacked layout.
        for (const l of ['Paper stake/trade', 'Status', 'Date added', 'Paper running P&amp;L']) assert.ok(row.includes('data-label="' + l + '"'), l);
        // Live roster shares the stacked-block rule (issue #1290).
        click('copy-live');
        await settle();
        assert.ok(el('copy-live-followed-list').innerHTML.includes('copy-table--roster'));
    """, tmp_path)


def test_paper_roster_row_actions_are_wired_and_failures_surface_on_the_paper_banner(tmp_path):
    """The delegated roster handler still drives Pause / Edit stake / Unfollow
    (unchanged by #1277) and a failed action reports on the PAPER tab's banner."""
    run_js("""
        click('copy-paper');
        await settle();
        const fire = (cls, address) => {
          const btn = { dataset: { address }, disabled: false, classList: new Element().classList,
                        setAttribute() {}, removeAttribute() {} };
          el('copy-followed-list').fire('click', { target: { closest: (sel) => sel === '.' + cls ? btn : null } });
          return btn;
        };
        let before = requests.length;
        fire('btn-followed-pause', '0xPaperOne');
        await settle();
        assert.ok(requests.slice(before).includes('POST /api/copy-trading/wallets/0xPaperOne/pause'), 'pause POSTs');
        assert.ok(requests.slice(before).includes('/api/copy-trading/followed-wallets'), 'roster refetched afterwards');

        // A refused action: banner on the Paper tab, not another tab's.
        const realFetch = global.fetch;
        global.fetch = (url, opts) => (opts && opts.method)
          ? Promise.resolve({ ok: false, status: 409, json: async () => ({ success: false, message: 'refused' }) })
          : realFetch(url, opts);
        fire('btn-followed-unfollow', '0xPaperOne');
        await settle();
        global.fetch = realFetch;
        assert.ok(el('copy-paper-error-banner').classList.contains('visible'));
        assert.ok(el('copy-paper-error-text').textContent.includes('refused'));
        assert.ok(!el('copy-live-error-banner').classList.contains('visible'));
    """, tmp_path)


def test_static_markup_ships_skeletons_busy_states_and_the_paper_badge_headings():
    html, _ = _html_parts()
    paper = re.search(r'<section id="tab-copy-paper".*?</section>', html, re.S).group(0)
    # KPI values start as skeleton blocks flagged busy; Recent Closed starts as skeleton rows.
    for kid in ("pnl", "open", "followed"):
        assert re.search(r'id="paper-kpi-%s" aria-busy="true"><span class="skeleton wc-skeleton"' % kid, paper), kid
    closed = re.search(r'<div id="copy-paper-closed-list" aria-busy="true">(.*?)</table>', paper, re.S).group(1)
    assert "copy-table-skeleton" in closed
    # Section headings carry a PAPER badge; pagers start hidden.
    assert 'aria-label="Paper closed trades">PAPER' in paper
    assert re.search(r'id="copy-paper-closed-pagination" style="display:none"', paper)
    assert re.search(r'id="copy-activity-pagination" style="display:none"', paper)
    # Paper activity has no Mode select (locked to mode=paper).
    assert "copy-activity-mode-select" not in paper


# ---------------------------------------------------------------------------
# Go live ->
# ---------------------------------------------------------------------------

def _click_goto_live(addr: str) -> str:
    return f"""
        const html = el('copy-followed-list').innerHTML;
        const m = html.match(/class="btn-followed-action btn-followed-goto-live" data-address="({addr})"/);
        assert.ok(m, 'Go live link carries the wallet address');
        const btn = {{ dataset: {{ address: m[1] }} }};
        el('copy-followed-list').fire('click', {{ target: {{ closest: (sel) => sel === '.btn-followed-goto-live' ? btn : null }} }});
        await settle();
    """


def test_go_live_link_switches_to_live_and_highlights_the_wallet_row(tmp_path):
    run_js(f"""
        click('copy-paper');
        await settle();
        const resetCalls = requests.length;
        {_click_goto_live('0xPaperOne')}
        assert.strictEqual(currentTab, 'copy-live');
        const row = el('live-ready-row-0xPaperOne');
        assert.strictEqual(row._scrolled, 1, 'scrolled into view once');
        assert.deepStrictEqual(row._scrollOpts, {{ behavior: 'smooth', block: 'center' }});
        assert.ok(row.classList.contains('copy-row-highlight'));
        assert.ok(!el('live-followed-row-0xLiveOne').classList.contains('copy-row-highlight'), 'only the chosen row');
        // It is a link, never an action: no POST of any kind.
        assert.ok(!requests.slice(resetCalls).some(r => r.startsWith('POST') || r.startsWith('PATCH')), 'no mutation fired');
        // The request was consumed: a later Live re-render does not scroll again.
        state.followed = followed({{ active_count: 3 }});
        await repoll();
        await fetchFollowedWallets('live');
        assert.strictEqual(_copyPendingLiveHighlight, null);
        assert.ok(![..._reg.values()].some(e => e.classList.contains('copy-row-highlight')), 'no second highlight');
    """, tmp_path)


def test_go_live_link_highlights_immediately_when_live_was_already_rendered(tmp_path):
    run_js(f"""
        click('copy-live');
        await settle();
        click('copy-paper');
        await settle();
        {_click_goto_live('0xPaperOne')}
        assert.strictEqual(currentTab, 'copy-live');
        assert.ok(el('live-ready-row-0xPaperOne').classList.contains('copy-row-highlight'));
        assert.strictEqual(el('live-ready-row-0xPaperOne')._scrolled, 1);
    """, tmp_path)


def test_go_live_opens_a_collapsed_ready_list_before_scrolling(tmp_path):
    run_js("""
        click('copy-live');
        await settle();
        const row = el('live-ready-row-0xPaperOne');
        const details = { open: false };
        row.closest = (sel) => sel === 'details' ? details : null;
        assert.strictEqual(_copyHighlightLiveRow('0xPaperOne'), true);
        assert.strictEqual(details.open, true, 'the collapsed <details> is opened');
        assert.strictEqual(row._scrolled, 1);
    """, tmp_path)


def test_go_live_for_a_wallet_not_in_the_ready_list_only_switches_tab(tmp_path):
    run_js("""
        click('copy-paper');
        await settle();
        _copyGoLiveForWallet('0xGhost');
        await settle();
        assert.strictEqual(currentTab, 'copy-live');
        assert.strictEqual(_copyPendingLiveHighlight, null, 'the request is dropped, never parked');
        assert.ok(![..._reg.values()].some(e => e.classList.contains('copy-row-highlight')));
    """, tmp_path)


def test_a_stale_go_live_request_never_fires_later(tmp_path):
    run_js("""
        click('copy-paper');
        await settle();
        _copyPendingLiveHighlight = { address: '0xPaperOne', ts: Date.now() - 11_000 };
        click('copy-live');
        await settle();
        assert.ok(!el('live-ready-row-0xPaperOne').classList.contains('copy-row-highlight'));
        assert.strictEqual(_copyPendingLiveHighlight, null);
    """, tmp_path)


# ---------------------------------------------------------------------------
# Recent Closed
# ---------------------------------------------------------------------------

_CLOSED_SETUP = """
    const hist = (n) => Array.from({ length: n }, (_, i) => ({            // OLDEST first, like the API
      address: '0xPaperOne', market: 'M' + String(i + 1).padStart(2, '0'),
      settled_at: iso(100 - i), settled_pnl_usd: i % 2 ? -1.25 : 2.5, stake_usd: 5 }));
    const closedMarkets = () => marketsIn(el('copy-paper-closed-list').innerHTML);
    const info = () => el('copy-paper-closed-info').textContent;
"""


def test_recent_closed_is_newest_first_15_per_page_with_prev_next(tmp_path):
    run_js(_CLOSED_SETUP + """
        state.positions = positions({ realized_pnl_history: hist(40) });
        click('copy-paper');
        await settle();

        let m = closedMarkets();
        assert.strictEqual(m.length, 15);
        assert.strictEqual(m[0], 'M40', 'newest first');
        assert.strictEqual(m[14], 'M26');
        assert.strictEqual(info(), 'Page 1 of 3');
        assert.strictEqual(el('copy-paper-closed-pagination').style.display, 'flex');
        assert.strictEqual(el('copy-paper-closed-prev').disabled, true, 'Prev disabled on page 1');
        assert.strictEqual(el('copy-paper-closed-next').disabled, false);
        assert.strictEqual(el('copy-paper-closed-count').textContent, '40 closed (paper)');

        copyPaperClosedChangePage(1);
        m = closedMarkets();
        assert.deepStrictEqual([m[0], m[m.length - 1], m.length], ['M25', 'M11', 15]);
        assert.strictEqual(info(), 'Page 2 of 3');
        assert.strictEqual(el('copy-paper-closed-prev').disabled, false);

        copyPaperClosedChangePage(1);
        m = closedMarkets();
        assert.deepStrictEqual([m[0], m[m.length - 1], m.length], ['M10', 'M01', 10], 'last page is the remainder');
        assert.strictEqual(el('copy-paper-closed-next').disabled, true, 'Next disabled on the last page');

        copyPaperClosedChangePage(1);                       // past the end: clamped
        assert.strictEqual(info(), 'Page 3 of 3');
        copyPaperClosedChangePage(-1); copyPaperClosedChangePage(-1); copyPaperClosedChangePage(-1);
        assert.strictEqual(info(), 'Page 1 of 3', 'before the start: clamped');

        // P&L colouring from the signed value.
        const html = el('copy-paper-closed-list').innerHTML;
        assert.ok(html.includes('copy-pnl-pos">+$2.50') && html.includes('copy-pnl-neg">-$1.25'));
    """, tmp_path)


def test_recent_closed_keeps_its_page_on_background_refresh_and_clamps_when_shrinking(tmp_path):
    run_js(_CLOSED_SETUP + """
        state.positions = positions({ realized_pnl_history: hist(40) });
        click('copy-paper');
        await settle();
        copyPaperClosedChangePage(1);
        assert.strictEqual(info(), 'Page 2 of 3');

        state.positions = positions({ realized_pnl_history: hist(41) });   // one more settle
        await repoll();
        assert.strictEqual(info(), 'Page 2 of 3', 'refresh must not jump back to page 1');
        assert.strictEqual(closedMarkets()[0], 'M26', 'page 2 content shifted by the new newest row');

        state.positions = positions({ realized_pnl_history: hist(12) });   // shrinks to 1 page
        await repoll();
        assert.strictEqual(el('copy-paper-closed-pagination').style.display, 'none', 'hidden when <= 1 page');
        assert.strictEqual(closedMarkets().length, 12);
        assert.strictEqual(closedMarkets()[0], 'M12');
    """, tmp_path)


def test_recent_closed_empty_and_error_states(tmp_path):
    run_js(_CLOSED_SETUP + """
        state.positions = positions({ realized_pnl_history: [] });
        click('copy-paper');
        await settle();
        assert.ok(el('copy-paper-closed-list').innerHTML.includes('No closed paper trades yet'));
        assert.strictEqual(el('copy-paper-closed-pagination').style.display, 'none');
        assert.strictEqual(el('copy-paper-closed-list').getAttribute('aria-busy'), null);
    """, tmp_path)


# ---------------------------------------------------------------------------
# Paper activity feed paging
# ---------------------------------------------------------------------------

_ACT_SETUP = """
    const events = (n) => Array.from({ length: n }, (_, i) => ev(i));      // newest first, like the API
    const acts = () => [...el('copy-activity-list').innerHTML.matchAll(/Order placed \\(paper\\) — (E\\d+)/g)].map(m => m[1]);
    const ainfo = () => el('copy-activity-info').textContent;
"""


def test_paper_activity_is_paged_25_per_page_newest_first(tmp_path):
    run_js(_ACT_SETUP + """
        state.activity = { events: events(60) };
        click('copy-paper');
        await settle();
        _copyActivityInitStaticControls();

        let a = acts();
        assert.deepStrictEqual([a.length, a[0], a[24]], [25, 'E00', 'E24']);
        assert.strictEqual(ainfo(), 'Page 1 of 3');
        assert.strictEqual(el('copy-activity-prev').disabled, true);
        assert.strictEqual(el('copy-activity-pagination').style.display, 'flex');

        copyPaperActivityChangePage(1);
        a = acts();
        assert.deepStrictEqual([a.length, a[0], a[24]], [25, 'E25', 'E49']);
        copyPaperActivityChangePage(1);
        a = acts();
        assert.deepStrictEqual([a.length, a[0], a[9]], [10, 'E50', 'E59']);
        assert.strictEqual(el('copy-activity-next').disabled, true);
        copyPaperActivityChangePage(1);
        assert.strictEqual(ainfo(), 'Page 3 of 3', 'clamped at the end');
        assert.strictEqual(el('copy-activity-list').scrollTop, 0, 'a new page starts at the top');

        // Background refresh keeps the page.
        state.activity = { events: events(61) };
        await repoll();
        assert.strictEqual(ainfo(), 'Page 3 of 3');
        assert.strictEqual(acts()[0], 'E50');
    """, tmp_path)


def test_changing_an_activity_filter_resets_to_page_one(tmp_path):
    run_js(_ACT_SETUP + """
        const mixed = Array.from({ length: 60 }, (_, i) => ev(i, { address: i % 2 ? '0xOdd' : '0xEven',
          event_type: i % 3 === 0 ? 'order_skipped' : 'order_placed', skip_reason: 'x' }));
        state.activity = { events: mixed };
        click('copy-paper');
        await settle();
        _copyActivityInitStaticControls();
        copyPaperActivityChangePage(1);
        assert.strictEqual(ainfo(), 'Page 2 of 3');

        el('copy-activity-wallet-select').fire('change', { target: { value: '0xEven' } });
        assert.strictEqual(ainfo(), 'Page 1 of 2', '30 events -> 2 pages, back on page 1');

        copyPaperActivityChangePage(1);
        el('copy-activity-type-select').fire('change', { target: { value: 'order_skipped' } });
        // 0xEven AND skipped -> 10 events -> a single page: pager hidden.
        assert.strictEqual(el('copy-activity-pagination').style.display, 'none');
        assert.strictEqual(el('copy-activity-list').innerHTML.match(/class="copy-activity-item /g).length, 10);

        el('copy-activity-type-select').fire('change', { target: { value: 'wallet_paused' } });
        assert.ok(el('copy-activity-list').innerHTML.includes('No matching activity'));
        assert.strictEqual(el('copy-activity-pagination').style.display, 'none');
    """, tmp_path)


def test_paper_and_live_activity_pagers_are_independent(tmp_path):
    """Both feeds page at 25, each with its own page state and its own buttons."""
    run_js("""
        state.activity = { events: Array.from({ length: 40 }, (_, i) => ev(i, { mode: 'live', event_type: 'live_order_filled' })) };
        click('copy-live');
        await settle();
        assert.strictEqual(el('copy-live-activity-list').innerHTML.match(/class="copy-activity-item /g).length, 25);
        copyLiveActivityChangePage(1);
        assert.strictEqual(_copyActivityScopes.live.page, 1);
        assert.strictEqual(_copyActivityScopes.paper.page, 0, 'Paper page untouched by Live paging');
        assert.strictEqual(_copyActivityScopes.paper.pager.id, 'copy-activity-pagination');
        assert.strictEqual(_copyActivityScopes.live.pager.id, 'copy-live-activity-pagination');
    """, tmp_path)


# ---------------------------------------------------------------------------
# Chart.js lifecycle + "unchanged -> no rebuild"
# ---------------------------------------------------------------------------

def test_chart_instance_is_reused_and_updated_in_place(tmp_path):
    run_js("""
        click('copy-paper');
        await settle();
        assert.strictEqual(charts.length, 1, 'one Chart created on first render');
        const chart = charts[0];
        assert.strictEqual(chart.canvas, el('copy-positions-canvas'));
        const pts = () => chart.data.datasets[0].data.length;
        assert.strictEqual(pts(), 2, 'last 30 days: the 40-day-old settle is outside the range');

        // New settled row arrives on a poll -> same instance, updated.
        const h = state.positions.realized_pnl_history.concat([{ address: '0xPaperOne', market: 'M-d', settled_at: iso(0, -5), settled_pnl_usd: 3, stake_usd: 5 }]);
        state.positions = positions({ realized_pnl_history: h, total: { realized_pnl_usd: 7.5, n_settled: 4 } });
        await repoll();
        assert.strictEqual(charts.length, 1, 'no new instance');
        assert.strictEqual(chart.destroyed, false);
        assert.strictEqual(chart.updates, 1);
        assert.strictEqual(pts(), 3);

        // Range select: still the same instance, now with the old row too.
        _copyPositionsInitStaticControls();
        el('copy-positions-range-select').fire('change', { target: { value: 'all' } });
        assert.strictEqual(charts.length, 1);
        assert.strictEqual(pts(), 4);
        assert.strictEqual(chart.updates, 2);
        el('copy-positions-range-select').fire('change', { target: { value: 'all' } });
        assert.strictEqual(chart.updates, 2, 'identical points: no redundant update');

        // Backtest toggle: chart untouched.
        el('copy-backtest-toggle').fire('click');
        assert.strictEqual(charts.length, 1);
        assert.strictEqual(chart.updates, 2);
    """, tmp_path)


def test_chart_is_destroyed_on_tab_leave_and_redrawn_on_return_even_if_data_unchanged(tmp_path):
    run_js("""
        click('copy-paper');
        await settle();
        assert.strictEqual(liveCharts().length, 1);

        click('copy-live');                       // leaving Paper for a sibling copy tab
        await settle();
        assert.strictEqual(liveCharts().length, 0, 'Paper chart destroyed on leave');
        assert.strictEqual(paperChartsCreated()[0].destroyed, true);

        click('copy-paper');                      // payload identical -> fetch gate skips render
        await settle();
        assert.strictEqual(liveCharts().length, 1, 'redrawn from the cached payload');
        assert.strictEqual(paperChartsCreated().length, 2);

        click('config');
        await settle();
        assert.strictEqual(liveCharts().length, 0, 'destroyed when leaving to a non-copy tab');
        // Leaving a DIFFERENT copy tab must not touch Paper's (nonexistent) chart.
        click('copy-wallets');
        await settle();
        assert.strictEqual(paperChartsCreated().length, 2);
    """, tmp_path)


def test_unchanged_poll_rebuilds_nothing_and_partial_change_rebuilds_only_that_section(tmp_path):
    run_js("""
        click('copy-paper');
        await settle();
        const W = () => ['copy-followed-list', 'copy-positions-content', 'copy-positions-chart-slot',
          'copy-positions-open-slot', 'copy-positions-wallet-slot', 'copy-paper-closed-list', 'copy-activity-list']
          .map(id => el(id)._writes);
        const base = W();

        await repoll();                                           // byte-identical payloads
        assert.deepStrictEqual(W(), base, 'identical polls must not rebuild any section');
        assert.strictEqual(charts.length, 1);
        assert.strictEqual(charts[0].updates, 0);

        // Only a new settle (history/total/per-wallet) changes: the OPEN-positions
        // table and the chart canvas must not be rebuilt (the expanded signal rows live there).
        const h = state.positions.realized_pnl_history.concat([{ address: '0xPaperOne', market: 'M-d', settled_at: iso(0, -5), settled_pnl_usd: 3, stake_usd: 5 }]);
        state.positions = positions({ realized_pnl_history: h, total: { realized_pnl_usd: 7.5, n_settled: 4 },
          per_wallet: [{ address: '0xPaperOne', n_settled: 4, realized_pnl_usd: 7.5 }] });
        await repoll();
        const after = W();
        const [fl, content, chartSlot, openSlot, walletSlot, closed] = after;
        assert.strictEqual(openSlot, base[3], 'open positions table untouched');
        assert.strictEqual(chartSlot, base[2], 'chart canvas untouched');
        assert.strictEqual(content, base[1], 'scaffold untouched');
        assert.strictEqual(fl, base[0], 'roster untouched');
        assert.strictEqual(walletSlot, base[4] + 1, 'per-wallet table rebuilt');
        assert.strictEqual(closed, base[5] + 1, 'closed list rebuilt');
    """, tmp_path)


def test_backtest_toggle_only_rebuilds_the_per_wallet_table(tmp_path):
    run_js("""
        click('copy-paper');
        await settle();
        _copyPositionsInitStaticControls();
        const open0 = el('copy-positions-open-slot')._writes, wallet0 = el('copy-positions-wallet-slot')._writes;
        el('copy-backtest-toggle').fire('click');
        assert.ok(el('copy-positions-wallet-slot').innerHTML.includes('Projected (backtest)'));
        assert.strictEqual(el('copy-positions-wallet-slot')._writes, wallet0 + 1);
        assert.strictEqual(el('copy-positions-open-slot')._writes, open0, 'open positions untouched');
        assert.strictEqual(el('copy-backtest-toggle').getAttribute('aria-checked'), 'true');
    """, tmp_path)


def test_expanded_signal_row_survives_polls(tmp_path):
    run_js("""
        click('copy-paper');
        await settle();
        _copyExpandedPositionIds.add(1);
        await repoll();                                            // unchanged -> same DOM
        assert.ok(has('copy-signal-row-pos-1'));
        assert.ok(_copyExpandedPositionIds.has(1));
        // A NEW open position arrives: the table is rebuilt and the expanded
        // row is re-opened from the remembered set.
        state.positions = positions({ open_positions: state.positions.open_positions.concat([
          { id: 3, address: '0xPaperOne', market: 'Third', outcome_index: 0, entry_price: 0.5, stake_usd: 1, entry_ts: iso(0), signal_id: 13 }]) });
        await repoll();
        assert.ok(has('copy-signal-row-pos-3'));
        assert.strictEqual(el('copy-signal-row-pos-1').style.display, 'table-row', 'still expanded after the rebuild');
    """, tmp_path)


# ---------------------------------------------------------------------------
# Empty / error states
# ---------------------------------------------------------------------------

def test_empty_states_per_section(tmp_path):
    run_js("""
        state.followed = followed({ wallets: [], active_count: 0, paused_count: 0 });
        state.positions = positions({ open_positions: [], per_wallet: [], realized_pnl_history: [],
          total: { realized_pnl_usd: 0, n_settled: 0 } });
        state.activity = { events: [] };
        click('copy-paper');
        await settle();
        assert.ok(el('copy-followed-list').innerHTML.includes('Nothing followed yet'));
        assert.ok(el('copy-positions-content').innerHTML.includes('No copy-trading positions yet'));
        assert.ok(el('copy-paper-closed-list').innerHTML.includes('No closed paper trades yet'));
        assert.ok(el('copy-activity-list').innerHTML.includes('No activity yet'));
        assert.strictEqual(el('paper-kpi-pnl').textContent, '+$0.00');
        assert.strictEqual(el('paper-kpi-open').textContent, '0');
        assert.strictEqual(el('paper-kpi-followed').textContent, '0');
        assert.strictEqual(liveCharts().length, 0);
    """, tmp_path)


def test_positions_with_open_but_nothing_settled_shows_the_chart_explanation(tmp_path):
    run_js("""
        state.positions = positions({ realized_pnl_history: [] });
        click('copy-paper');
        await settle();
        assert.ok(el('copy-positions-chart-slot').innerHTML.includes('No settled positions yet'));
        assert.ok(!el('copy-positions-chart-slot').innerHTML.includes('<canvas'));
        assert.strictEqual(charts.length, 0);
        assert.ok(el('copy-positions-open-slot').innerHTML.includes('Will it rain in Paris?'));
    """, tmp_path)


def test_first_load_failures_give_each_section_its_own_error_without_blanking_the_others(tmp_path):
    run_js("""
        state.fail.add('/api/copy-trading/positions');
        click('copy-paper');
        await settle();
        // positions-fed sections: explicit errors, not endless skeletons
        assert.strictEqual(el('paper-kpi-pnl').textContent, '—');
        assert.strictEqual(el('paper-kpi-pnl-sub').textContent, 'Could not load — retrying');
        assert.strictEqual(el('paper-kpi-open').textContent, '—');
        assert.ok(el('copy-positions-content').innerHTML.includes('Could not load positions'));
        assert.ok(el('copy-paper-closed-list').innerHTML.includes('Could not load closed trades'));
        // ... while the roster, followed KPI and activity are unaffected
        assert.ok(el('copy-followed-list').innerHTML.includes('0xPaperOne'));
        assert.strictEqual(el('paper-kpi-followed').textContent, '3');
        assert.ok(el('copy-activity-list').innerHTML.includes('E00'));

        // Recovery: the next successful poll replaces the errors.
        state.fail.clear();
        await repoll();
        assert.strictEqual(el('paper-kpi-pnl').textContent, '+$4.50');
        assert.ok(!el('copy-paper-closed-list').innerHTML.includes('Could not load'));
        assert.ok(el('copy-positions-open-slot').innerHTML.includes('Will it rain in Paris?'));
    """, tmp_path)


def test_followed_failure_marks_only_the_followed_kpi_and_activity_failure_keeps_data(tmp_path):
    run_js("""
        state.fail.add('/api/copy-trading/followed-wallets');
        click('copy-paper');
        await settle();
        assert.strictEqual(el('paper-kpi-followed').textContent, '—');
        assert.strictEqual(el('paper-kpi-pnl').textContent, '+$4.50');

        state.fail.clear();
        await repoll();
        assert.strictEqual(el('paper-kpi-followed').textContent, '3');

        // A later failure keeps last-known-good numbers and shows the banner.
        state.fail.add('/api/copy-trading/positions');
        state.fail.add('/api/copy-trading/activity-feed');
        await repoll();
        assert.strictEqual(el('paper-kpi-pnl').textContent, '+$4.50');
        assert.ok(el('copy-positions-error-banner').classList.contains('visible'));
        assert.ok(el('copy-activity-stale-banner').classList.contains('visible'));
        assert.ok(el('copy-activity-list').innerHTML.includes('E00'), 'last-known activity kept');
    """, tmp_path)


# ---------------------------------------------------------------------------
# Optional "Live positions are on the Live tab" line
# ---------------------------------------------------------------------------

def test_live_tab_cross_link_note_follows_the_posture_without_any_number(tmp_path):
    run_js("""
        const note = el('copy-paper-live-note');
        assert.strictEqual(note.style.visibility, 'hidden');
        renderCopyTradingModePosture(true);
        assert.strictEqual(note.style.visibility, 'visible');
        assert.ok(!/\\d/.test(note.textContent || 'Live positions are on the Live tab.'));
        renderCopyTradingModePosture(false);
        assert.strictEqual(note.style.visibility, 'hidden');
    """, tmp_path)


# ---------------------------------------------------------------------------
# Controller rules still hold with the new Paper content
# ---------------------------------------------------------------------------

def test_paper_polling_cadence_unchanged(tmp_path):
    run_js("""
        const delays = () => [..._intervals.values()].map(i => i.every).sort((a, b) => a - b);
        click('copy-paper');
        assert.deepStrictEqual(delays(), [30_000, 30_000, 300_000, 300_000]);
        click('copy-live');
        assert.strictEqual(delays().length, 6);
        click('copy-paper');
        assert.deepStrictEqual(delays(), [30_000, 30_000, 300_000, 300_000]);
        click('config');
        assert.deepStrictEqual(delays(), []);
    """, tmp_path)


# ---------------------------------------------------------------------------
# Polish (#1291): neutral highlight, reduced motion, Recent Closed address cell,
# column order, KPI third card, pager scroll
# ---------------------------------------------------------------------------

def test_go_live_highlight_is_neutral_primary_not_amber():
    html = INDEX_HTML.read_text(encoding="utf-8")
    kf = re.search(r"@keyframes copy-row-flash\{([^@]*?)\}\s*\.copy-row-highlight", html)
    assert kf, "copy-row-flash keyframes not found"
    assert "var(--primary-bg)" in kf.group(1) and "var(--primary)" in kf.group(1)
    assert "--warn" not in kf.group(1), "amber means paper; the jump highlight must be neutral"


def test_reduced_motion_css_and_kpi_third_card_span():
    html = INDEX_HTML.read_text(encoding="utf-8")
    rm = re.search(r"@media \(prefers-reduced-motion: reduce\)\{(.*?)\n\}", html, re.S)
    assert rm, "reduced-motion block missing"
    assert "scroll-behavior:auto" in rm.group(1)
    assert ".copy-row-highlight{animation:none" in rm.group(1)
    assert ".live-dot,.skeleton,.spinner{animation:none;}" in rm.group(1)
    assert re.search(
        r"@media\(max-width:600px\)\{\.copy-paper-kpis \.wallet-card:last-child:nth-child\(odd\)\{grid-column:1/-1;\}\}", html
    ), "third Paper KPI card must span the full row at <=600px"
    assert "min-width:44px;min-height:44px" in html


def test_recent_closed_column_order_is_identical_on_paper_and_live(tmp_path):
    run_js(_CLOSED_SETUP + """
        click('copy-paper');
        await settle();
        const heads = (html) => [...html.matchAll(/<th class="copy-th" scope="col">([^<]*)<\\/th>/g)].map(m => m[1]);
        assert.deepStrictEqual(heads(el('copy-paper-closed-list').innerHTML),
          ['Market', 'Paper settled P&amp;L', 'Paper stake', 'Settled', 'Wallet']);
        assert.deepStrictEqual(heads(_copyLiveClosedTableHtml([])),
          ['Market', 'Settled P&amp;L', 'Stake', 'Closed', 'Wallet']);
        // Body cells follow the header: P&L is the 2nd cell, the wallet cell the last.
        const row = el('copy-paper-closed-list').innerHTML.match(/<tr class="copy-row"[\\s\\S]*?<\\/tr>/)[0];
        const cells = [...row.matchAll(/<td class="copy-td([^"]*)"/g)].map(m => m[1].trim());
        assert.ok(/copy-pnl-(pos|neg)/.test(cells[1]), 'P&L second');
        assert.ok(cells[4].includes('copy-address-cell'), 'wallet last');
    """, tmp_path)


def test_paper_recent_closed_has_a_labelled_copy_button_wired_to_the_address(tmp_path):
    run_js(_CLOSED_SETUP + """
        click('copy-paper');
        await settle();
        const html = el('copy-paper-closed-list').innerHTML;
        assert.ok(/<button type="button" class="copy-copy-btn" aria-label="Copy wallet address 0xPape/.test(html),
          'native button with a labelled address');
        assert.ok(html.includes('class="copy-feedback-label"'));
        // The delegated click on the Paper closed list copies the row's full address.
        let copied = null;
        document.body = { appendChild() {} };
        window.isSecureContext = true;
        navigator.clipboard.writeText = async (t) => { copied = t; };
        const btn = new Element();
        btn.isConnected = true;
        btn.querySelector = () => null;
        btn.parentElement = { querySelector: () => null };
        btn.closest = (sel) => sel === '[data-address]' ? { dataset: { address: '0xPaperOne' } } : null;
        el('copy-paper-closed-list').fire('click', { target: { closest: (s) => s === '.copy-copy-btn' ? btn : null }, stopPropagation() {} });
        await settle();
        assert.strictEqual(copied, '0xPaperOne');
    """, tmp_path)


def test_reduced_motion_scrolls_without_smooth_and_drops_the_highlight(tmp_path):
    run_js(f"""
        window.matchMedia = (q) => ({{ matches: /reduce/.test(q) }});
        const timers = [];
        global.setTimeout = (fn, ms) => {{ timers.push({{ fn, ms }}); return timers.length; }};
        click('copy-paper');
        await settle();
        {_click_goto_live('0xPaperOne')}
        const row = el('live-ready-row-0xPaperOne');
        assert.deepStrictEqual(row._scrollOpts, {{ block: 'center' }}, 'no smooth behaviour');
        assert.ok(row.classList.contains('copy-row-highlight'));
        const t = timers.find(x => x.ms === 2400);
        assert.ok(t, 'highlight removal scheduled at 2400 ms');
        t.fn();
        assert.ok(!row.classList.contains('copy-row-highlight'), 'static highlight does not persist');
    """, tmp_path)


def test_recent_closed_pager_scrolls_the_list_back_into_view(tmp_path):
    run_js(_CLOSED_SETUP + """
        state.positions = positions({ realized_pnl_history: hist(40) });
        click('copy-paper');
        await settle();
        const wrap = el('copy-paper-closed-list');
        wrap.scrollTop = 99;
        wrap.getBoundingClientRect = () => ({ top: -300 });   // list top is above the viewport
        copyPaperClosedChangePage(1);
        assert.strictEqual(wrap.scrollTop, 0);
        assert.deepStrictEqual(wrap._scrollOpts, { behavior: 'smooth', block: 'start' });
        assert.strictEqual(info(), 'Page 2 of 3');
    """, tmp_path)


def test_reduced_motion_skeleton_is_static_and_busy_buttons_keep_their_text_label():
    html = INDEX_HTML.read_text(encoding="utf-8")
    rm = re.search(r"@media \(prefers-reduced-motion: reduce\)\{(.*?)\n\}", html, re.S).group(1)
    assert ".skeleton{background:var(--surface-off);}" in rm, "static --surface-off block"
    # Busy state is never rotation-only: every roster button that hosts a spinner
    # also carries a visible text label (the JS only toggles the `loading` class
    # and never replaces the button text), so state survives animation:none.
    for m in re.finditer(r'<button[^>]*btn-followed-(?:pause|resume)[^>]*>(.*?)</button>', html):
        label = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        assert "spinner" in m.group(1) and label in ("Pause", "Resume")

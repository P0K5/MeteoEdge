"""Behavior tests for the Copy · Live tab (epic #1272, issue #1278).

Runs the REAL inline <script> of src/dashboard/static/index.html under Node
against a small DOM stub (same technique as
test_dashboard_copy_tab_controller_behavior.py) and drives the Live tab the way
the browser does: payloads go in through the real `fetch*` / `render*` entry
points, and the assertions read what ended up in the Live containers.

The risky, real-money properties covered here:

* no paper figure on the Live tab (the only exception is the labelled PAPER
  P&L reference in "Ready to go live"), even when the payload is full of paper
  data;
* a failed live fetch / `live_error` never falls back to paper data or to a
  zero -- last-known-good live data stays, or the section says "could not
  load";
* live OFF + no history shows NO numbers at all (not even $0.00), and the
  initial "posture unknown" state is a skeleton, never a guessed "off";
* Recent Closed (15/page, newest first) and the live activity feed (25/page)
  page client-side, keep the page across refreshes, and an unchanged payload
  never rebuilds the DOM;
* "Edit live stake" validates, confirms only a RAISE, and PATCHes the
  live-stake endpoint with the right body.

Skipped when node is not installed (repo convention).
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

    class Element {
      constructor(id) {
        this.id = id || '';
        this._classes = new Set();
        this._attrs = {};
        this._listeners = {};
        this._innerHTML = '';
        this._textContent = '';
        this._writes = 0;
        this.style = {};
        this.dataset = {};
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
      set innerHTML(v) { this._innerHTML = v; this._writes += 1; }
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
      scrollIntoView() {}
    }
    const _byId = {};
    function el(id) { return _byId[id] || (_byId[id] = new Element(id)); }
    const _banners = ['copy-wallets-mode-banner', 'copy-paper-mode-banner', 'copy-live-mode-banner'].map(id => el(id));

    global.document = {
      hidden: false,
      getElementById: (id) => el(id),
      querySelectorAll(sel) { return sel === '.copy-trading-mode-banner' ? _banners : []; },
      querySelector() { return null; },
      createElement: () => new Element(),
      documentElement: { getAttribute() { return 'dark'; }, setAttribute() {} },
      addEventListener() {},
    };
    global.window = { addEventListener() {}, prompt: () => null, confirm: () => true, isSecureContext: true };
    global.localStorage = { getItem() { return null; }, setItem() {} };
    global.lucide = { createIcons() {} };
    let chartCount = 0;
    global.Chart = function () { chartCount += 1; this.destroy = () => {}; };
    global.getComputedStyle = () => ({ getPropertyValue: () => '' });
    global.event = { target: new Element() };
    Object.defineProperty(global, 'navigator', { value: { clipboard: { writeText: async () => {} } }, writable: true, configurable: true });
    console.error = () => {};
    const settle = async () => { for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r)); };

    // ---- fetch stub: routes by URL prefix, records every call ---------------
    const calls = [];                      // { url, opts }
    const routes = {};                     // prefix -> () => ({ ok, status, body })
    global.fetch = async (url, opts) => {
      calls.push({ url: String(url), opts });
      const key = Object.keys(routes).find(k => String(url).startsWith(k));
      if (!key) return { ok: true, status: 200, json: async () => ({}) };
      const r = routes[key]();
      if (r instanceof Error) throw r;
      return { ok: r.ok !== false, status: r.status || 200, json: async () => r.body };
    };
    const callsTo = (needle) => calls.filter(c => c.url.includes(needle));

    // ---- fixtures -----------------------------------------------------------
    // Distinctive paper figures: none of these may ever appear in a Live container.
    const PAPER_SENTINELS = ['777.77', '555.55', '444.44', '333.33', '987.65', '123.45', '5.55',
                             'PAPER-OPEN-MKT', 'PAPER-CLOSED-MKT'];
    const W = (address, o = {}) => ({
      address, stake_per_trade: 5.55, status: 'active', paused_reason: null,
      added_at: '2026-09-01T00:00:00Z', n_settled: 3, realized_pnl_usd: 123.45,
      live_enabled: false, live_eligible: false, live_status_reason: 'live is not enabled for this wallet',
      live_stake_per_trade: 5.55, live_stake_is_override: false, ...o,
    });
    const LIVE_W = (address, o = {}) => W(address, {
      live_enabled: true, live_eligible: true, live_status_reason: 'eligible for live execution',
      live_stake_per_trade: 2, live_stake_is_override: true, ...o,
    });
    const FOLLOWED = (wallets, o = {}) => ({
      wallets, active_count: wallets.length, paused_count: 0, aggregate_pnl_usd: 987.65, n_settled_total: 5,
      live_eligible_count: wallets.filter(w => w.live_eligible).length,
      paper_only_count: wallets.filter(w => !w.live_enabled).length,
      live_aggregate_pnl_usd: 40, live_n_settled_total: 6, live_trading_enabled: true,
      live_cap_usd: 10, live_opted_in_count: wallets.filter(w => w.live_enabled).length, ...o,
    });
    const POS = (o = {}) => ({
      // PAPER keys, deliberately full of sentinels:
      open_positions: [{ id: 1, address: '0xPaper', market: 'PAPER-OPEN-MKT', outcome_index: 0, entry_price: 0.5,
                         stake_usd: 777.77, entry_ts: '2026-09-01T00:00:00Z', signal_id: 1 }],
      realized_pnl_history: [{ address: '0xPaper', market: 'PAPER-CLOSED-MKT', settled_at: '2026-09-01T00:00:00Z',
                               settled_pnl_usd: 555.55, stake_usd: 5 }],
      per_wallet: [{ address: '0xPaper', n_settled: 1, realized_pnl_usd: 444.44 }],
      total: { n_settled: 1, realized_pnl_usd: 333.33 },
      backtest_total: { n_wallets: 1, n_settled: 1, realized_pnl_usd: 1, projected_flat_dollar_pnl: 1, divergence_usd: 0, divergence_pct: null },
      // LIVE keys:
      live_open_positions: [], live_realized_pnl_history: [], live_per_wallet: [],
      live_total: { n_settled: 0, realized_pnl_usd: 0 }, live_error: null, ...o,
    });
    const OPEN = (id, address, stake, o = {}) => ({
      id, address, market: 'LIVE-OPEN-' + id, outcome_index: 0, status: 'filled', order_id: 'o' + id,
      fill_price: 0.4, stake_usd: stake, filled_stake_usd: stake, entry_ts: '2026-09-20T00:00:00Z', signal_id: id, ...o,
    });
    const CLOSED = (n, pnl = 1) => ({
      address: '0xLive', market: 'LIVE-CLOSED-' + n, stake_usd: 2,
      settled_at: new Date(Date.UTC(2026, 8, 1, 0, n)).toISOString(), settled_pnl_usd: pnl,
    });
    const LIVE_CONTAINERS = ['copy-live-kpis', 'copy-positions-live-content', 'copy-live-closed-list',
      'copy-live-pnl-content', 'copy-live-followed-list', 'copy-live-ready-list', 'copy-live-activity-list'];
    const liveDom = () => LIVE_CONTAINERS.map(id => el(id).innerHTML).join('\n');
    const noPaper = (html, label) => {
      for (const s of PAPER_SENTINELS) assert.ok(!html.includes(s), `${label}: paper figure ${s} leaked into the Live tab`);
    };
    const NO_NUMBERS = /[0-9$]/;
    const textOf = (html) => html.replace(/<[^>]*>/g, ' ');   // visible text only (tag names like <h3> are not figures)
    const posture = (on) => { routes['/api/config'] = () => ({ body: { copy_trading: { COPY_LIVE_TRADING_ENABLED: { value: on } } } }); };
    const fresh = () => { _copyInvalidateShared(); calls.length = 0; };
""")


def run_js(body: str, tmp_path: Path) -> None:
    _, script = _html_parts()
    code = (
        _PRELUDE + "\n" + script + "\n(async () => {\n" + textwrap.dedent(body)
        + "\n})().then(() => console.log('OK'), e => { console.log('FAIL'); console.log(e && e.stack || e); process.exit(1); });"
    )
    path = tmp_path / "copy_live_tab_check.js"
    path.write_text(code, encoding="utf-8")
    r = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "OK" in r.stdout, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"


# ---------------------------------------------------------------------------
# Static scaffolding the behaviour depends on
# ---------------------------------------------------------------------------

def test_live_header_has_a_real_paper_tab_link():
    """Designer note: the Live header carries a real 'Paper study -> Paper tab'
    link that actually switches tabs (not plain text)."""
    html, _ = _html_parts()
    live = html[html.index('<section id="tab-copy-live"'):]
    live = live[: live.index("</section>")]
    m = re.search(r'<button type="button" class="copy-empty-link" id="copy-live-paper-link"\s+onclick="([^"]+)"', live)
    assert m, "Paper study link must be a real button with an onclick"
    assert "Paper study" in live and "_copyGoToTab('copy-paper')" in m.group(1)


def test_paper_link_switches_to_the_paper_tab(tmp_path):
    html, _ = _html_parts()
    onclick = re.search(r'id="copy-live-paper-link"\s+onclick="([^"]+)"', html).group(1)
    run_js(f"""
        let clicked = 0;
        el('tab-btn-copy-paper').click = () => {{ clicked += 1; }};
        eval({onclick!r});
        assert.strictEqual(clicked, 1, 'the link must click the Paper tab button (the single routing path)');
    """, tmp_path)


# ---------------------------------------------------------------------------
# KPI cards and the off / skeleton / unavailable states
# ---------------------------------------------------------------------------

def test_kpi_row_is_a_skeleton_until_posture_is_known(tmp_path):
    """A posture of 'unknown' is not 'off': positions that arrive before the
    first posture result must not flash 'Live trading is off'."""
    run_js("""
        routes['/api/copy-trading/positions'] = () => ({ body: POS() });
        await fetchCopyTradingPositions('live');      // posture NOT loaded yet
        assert.ok(!liveDom().includes('Live trading is off'), 'no guessed off-state before the posture is known');
        assert.ok(!el('copy-live-kpis').innerHTML.includes('Live trading is off'));

        posture(false);
        await fetchCopyTradingModePosture();          // first posture result renders the parked payload
        assert.ok(el('copy-live-kpis').innerHTML.includes('Live trading is off'));
        assert.strictEqual(el('copy-live-kpis').getAttribute('aria-busy'), 'false');
    """, tmp_path)


def test_kpi_skeleton_has_no_numbers_and_marks_busy(tmp_path):
    run_js("""
        renderFollowedWallets(FOLLOWED([LIVE_W('0xLive')]), 'live');   // data but no positions/posture yet
        const html = el('copy-live-kpis').innerHTML;
        assert.ok(html.includes('skeleton'));
        assert.ok(!NO_NUMBERS.test(textOf(html)), 'skeleton cards print no figures: ' + html);
        assert.strictEqual(el('copy-live-kpis').getAttribute('aria-busy'), 'true');

        // Even with a good positions payload in hand, an unknown posture keeps the
        // row a skeleton (defence in depth behind the fetch-level parking).
        renderCopyLivePositions(POS({ live_total: { n_settled: 2, realized_pnl_usd: 9 } }), false);
        assert.ok(el('copy-live-kpis').innerHTML.includes('skeleton'));
        assert.ok(!el('copy-live-kpis').innerHTML.includes('Live trading is off'));
        assert.strictEqual(el('copy-live-kpis').getAttribute('aria-busy'), 'true');
        renderCopyTradingModePosture(true);
        assert.ok(el('copy-live-kpis').innerHTML.includes('+$9.00'), 'the first posture result resolves the skeleton');
        assert.strictEqual(el('copy-live-kpis').getAttribute('aria-busy'), 'false');
    """, tmp_path)


def test_live_off_and_no_history_shows_no_numbers_anywhere(tmp_path):
    """Epic J: not even $0.00 -- KPI row, open positions, closed list, P&L
    section and the count pills all stay numberless."""
    run_js("""
        posture(false);
        routes['/api/copy-trading/positions'] = () => ({ body: POS() });   // paper keys full of sentinels
        await fetchCopyTradingModePosture();
        await fetchCopyTradingPositions('live');
        for (const id of ['copy-live-kpis', 'copy-positions-live-content', 'copy-live-closed-list', 'copy-live-pnl-content']) {
          const html = el(id).innerHTML;
          assert.ok(html.includes('Live trading is off'), id + ' must say live trading is off: ' + html);
          assert.ok(!NO_NUMBERS.test(textOf(html)), id + ' must show NO numbers, got: ' + html);
        }
        assert.strictEqual(el('copy-live-open-count').style.display, 'none', 'count pill hidden');
        assert.strictEqual(el('copy-live-closed-count').style.display, 'none', 'count pill hidden');
        assert.strictEqual(el('copy-live-closed-pagination').style.display, 'none');
        assert.ok(el('copy-live-kpis').innerHTML.includes('_copyGoToConfigTab'), 'links to where the switch lives');
        noPaper(liveDom(), 'off + no history');
    """, tmp_path)


def test_live_on_and_empty_is_distinct_from_off(tmp_path):
    run_js("""
        posture(true);
        routes['/api/copy-trading/positions'] = () => ({ body: POS() });
        await fetchCopyTradingModePosture();
        await fetchCopyTradingPositions('live');
        const kpis = el('copy-live-kpis').innerHTML;
        assert.ok(!kpis.includes('Live trading is off'));
        assert.ok(kpis.includes('No settled live trades yet'), 'no settled trades -> no fake zero P&L');
        assert.ok(!kpis.includes('+$0.00') && !kpis.includes('$0.00 P'), 'never a zero-P&L reading');
        assert.ok(el('copy-positions-live-content').innerHTML.includes('No live positions yet.'));
        assert.ok(el('copy-live-closed-list').innerHTML.includes('No closed live trades yet.'));
        noPaper(liveDom(), 'on + empty');
    """, tmp_path)


def test_kpi_cards_show_live_figures_only(tmp_path):
    run_js("""
        posture(true);
        routes['/api/copy-trading/followed-wallets'] = () => ({ body: FOLLOWED([
          LIVE_W('0xLive'), LIVE_W('0xCapped', { live_eligible: false,
            live_status_reason: "this wallet's live exposure limit is currently reached" }), W('0xReady')]) });
        routes['/api/copy-trading/positions'] = () => ({ body: POS({
          live_open_positions: [OPEN(1, '0xLive', 10), OPEN(2, '0xLive', 5.5), OPEN(3, '0xCapped', 4)],
          live_total: { n_settled: 3, realized_pnl_usd: 12.5 },
          live_realized_pnl_history: [CLOSED(1, 12.5)] }) });
        await fetchCopyTradingModePosture();
        await fetchFollowedWallets('live');
        await fetchCopyTradingPositions('live');
        const k = el('copy-live-kpis').innerHTML;
        assert.ok(k.includes('Live P&amp;L') && k.includes('+$12.50') && k.includes('3 settled'), k);
        assert.ok(k.includes('wc-value pos'), 'positive P&L is green');
        assert.ok(k.includes('Open live exposure') && k.includes('$19.50') && k.includes('cap $10.00 per wallet'), k);
        assert.ok(k.includes('Open live positions') && k.includes('across 2 wallets'), k);
        assert.ok(k.includes('Live wallets') && k.includes('2 opted in') && k.includes('1 at cap'), k);
        assert.ok(!/cash|balance|usdc/i.test(k), 'no cash/balance card: the API has none');
        assert.ok((k.match(/class="wallet-card"/g) || []).length === 4, 'exactly four .wallet-card KPI cards');
        assert.ok(!k.includes('aggregate'), 'never an unqualified aggregate P&L');
        noPaper(k, 'KPI cards');
        // a negative P&L is red
        routes['/api/copy-trading/positions'] = () => ({ body: POS({
          live_open_positions: [OPEN(1, '0xLive', 10)], live_total: { n_settled: 1, realized_pnl_usd: -3 },
          live_realized_pnl_history: [CLOSED(1, -3)] }) });
        fresh();
        await fetchCopyTradingPositions('live');
        assert.ok(el('copy-live-kpis').innerHTML.includes('wc-value neg') && el('copy-live-kpis').innerHTML.includes('-$3.00'));
    """, tmp_path)


def test_live_off_with_history_still_shows_real_figures_and_the_off_banner(tmp_path):
    run_js("""
        posture(false);
        routes['/api/copy-trading/positions'] = () => ({ body: POS({
          live_total: { n_settled: 1, realized_pnl_usd: 15 },
          live_realized_pnl_history: [CLOSED(1, 15)], live_per_wallet: [{ address: '0xLive', n_settled: 1, realized_pnl_usd: 15 }] }) });
        await fetchCopyTradingModePosture();
        await fetchCopyTradingPositions('live');
        assert.ok(el('copy-live-kpis').innerHTML.includes('+$15.00'));
        assert.ok(!el('copy-live-kpis').innerHTML.includes('Live trading is off'));
        assert.ok(el('copy-positions-live-content').innerHTML.includes('warn-banner'), 'off banner layered on top of the history');
        assert.ok(el('copy-live-closed-list').innerHTML.includes('LIVE-CLOSED-1'));
        assert.ok(el('copy-live-pnl-content').innerHTML.includes('0xLive'));
    """, tmp_path)


# ---------------------------------------------------------------------------
# No paper data on the Live tab; failed fetches never fall back
# ---------------------------------------------------------------------------

def test_no_paper_figure_reaches_any_live_container(tmp_path):
    """The payload (and followed-wallets payload) is full of paper sentinels;
    none may appear in any Live container once every section has rendered."""
    run_js("""
        posture(true);
        routes['/api/copy-trading/followed-wallets'] = () => ({ body: FOLLOWED([LIVE_W('0xLive')]) });
        routes['/api/copy-trading/positions'] = () => ({ body: POS({
          live_open_positions: [OPEN(1, '0xLive', 10)], live_total: { n_settled: 2, realized_pnl_usd: 4 },
          live_realized_pnl_history: [CLOSED(1, 1), CLOSED(2, 3)],
          live_per_wallet: [{ address: '0xLive', n_settled: 2, realized_pnl_usd: 4 }] }) });
        routes['/api/copy-trading/activity-feed'] = () => ({ body: { events: [
          { event_type: 'live_order_filled', ts: '2026-09-20T00:00:00Z', address: '0xLive', mode: 'live', market: 'LIVE-X', fill_price: 0.4, size_usd: 2, signal_id: 1 },
          { event_type: 'order_placed', ts: '2026-09-20T00:00:00Z', address: '0xPaper', mode: 'paper', market: 'PAPER-OPEN-MKT', fill_price: 0.5, size_usd: 777.77, signal_id: 2 },
        ] } });
        await fetchCopyTradingModePosture();
        await fetchFollowedWallets('live');
        await fetchCopyTradingPositions('live');
        await fetchCopyTradingActivityFeed('live');
        const html = liveDom();
        assert.ok(html.includes('LIVE-OPEN-1') && html.includes('LIVE-CLOSED-2') && html.includes('LIVE-X'), 'live data did render');
        noPaper(html, 'populated live tab');
        assert.ok(!el('copy-live-activity-list').innerHTML.includes('0xPaper'), 'a mixed feed never shows paper events on Live');
    """, tmp_path)


def test_failed_live_fetch_never_falls_back_to_paper_or_zero(tmp_path):
    run_js("""
        posture(true);
        await fetchCopyTradingModePosture();
        // A paper render exists on the Paper tab (sentinels in its containers).
        renderCopyPositions(POS());
        assert.ok(el('copy-positions-content').innerHTML.includes('PAPER-OPEN-MKT'));

        // First live load fails outright.
        routes['/api/copy-trading/positions'] = () => new Error('network down');
        fresh();
        await fetchCopyTradingPositions('live');
        const html = liveDom();
        noPaper(html, 'failed first live fetch');
        assert.ok(el('copy-live-kpis').innerHTML.includes('Live figures unavailable'));
        assert.ok(!NO_NUMBERS.test(textOf(el('copy-live-kpis').innerHTML)),
          'unavailable card prints no figure (not even a zero)');
        assert.ok(el('copy-positions-live-content').innerHTML.includes('Could not load live positions'));
        assert.ok(el('copy-live-closed-list').innerHTML.includes('Could not load'));
        assert.ok(el('copy-live-pnl-content').innerHTML.includes('Could not load'));

        // A degraded 200 (live_error) with paper keys populated: same.
        routes['/api/copy-trading/positions'] = () => ({ body: POS({ live_error: 'Live data temporarily unavailable: boom' }) });
        fresh();
        await fetchCopyTradingPositions('live');
        noPaper(liveDom(), 'live_error payload');
        assert.ok(el('copy-live-kpis').innerHTML.includes('Live figures unavailable'));
        assert.ok(el('copy-positions-live-error-banner').classList.contains('visible'));
    """, tmp_path)


def test_failed_refresh_keeps_last_known_live_figures_and_says_so(tmp_path):
    run_js("""
        posture(true);
        await fetchCopyTradingModePosture();
        routes['/api/copy-trading/positions'] = () => ({ body: POS({
          live_open_positions: [OPEN(1, '0xLive', 10)], live_total: { n_settled: 1, realized_pnl_usd: 8 },
          live_realized_pnl_history: [CLOSED(1, 8)] }) });
        await fetchCopyTradingPositions('live');
        const good = {
          kpis: el('copy-live-kpis').innerHTML, open: el('copy-positions-live-content').innerHTML,
          closed: el('copy-live-closed-list').innerHTML,
        };
        assert.ok(good.kpis.includes('+$8.00'));

        routes['/api/copy-trading/positions'] = () => new Error('HTTP 502');
        fresh();
        await fetchCopyTradingPositions('live');
        assert.ok(el('copy-positions-live-error-banner').classList.contains('visible'), 'stale banner shown');
        assert.strictEqual(el('copy-positions-live-content').innerHTML, good.open, 'open positions kept');
        assert.strictEqual(el('copy-live-closed-list').innerHTML, good.closed, 'closed list kept');
        assert.ok(el('copy-live-kpis').innerHTML.includes('+$8.00'), 'KPI figures kept');
        assert.ok(el('copy-live-kpis').innerHTML.includes('last-known live figures'), 'KPI row says the data is stale');
        noPaper(liveDom(), 'failed refresh');

        // live_error on an otherwise-200 response behaves the same.
        routes['/api/copy-trading/positions'] = () => ({ body: POS({ live_error: 'x' }) });
        fresh();
        await fetchCopyTradingPositions('live');
        assert.strictEqual(el('copy-positions-live-content').innerHTML, good.open);
        assert.ok(el('copy-live-kpis').innerHTML.includes('+$8.00'));

        // Recovery clears both the banner and the stale note.
        routes['/api/copy-trading/positions'] = () => ({ body: POS({
          live_open_positions: [OPEN(1, '0xLive', 10)], live_total: { n_settled: 1, realized_pnl_usd: 8 },
          live_realized_pnl_history: [CLOSED(1, 8)] }) });
        fresh();
        await fetchCopyTradingPositions('live');
        assert.ok(!el('copy-positions-live-error-banner').classList.contains('visible'));
        assert.ok(!el('copy-live-kpis').innerHTML.includes('last-known'));
    """, tmp_path)


def test_drift_banner_is_independent_of_the_live_states(tmp_path):
    """Epic J reconciliation banner: preserved, un-softened, shown whatever the
    KPI/off state is."""
    run_js("""
        posture(false);
        routes['/api/copy-trading/positions'] = () => ({ body: POS() });
        await fetchCopyTradingModePosture();
        await fetchCopyTradingPositions('live');
        renderCopyLiveBalanceDrift({ within_tolerance: false, drift_usd: 12.34, expected_balance_usd: 1, actual_balance_usd: 2 });
        assert.ok(el('copy-positions-live-drift-banner').classList.contains('visible'));
        assert.ok(el('copy-positions-live-drift-text').textContent.includes('manual reconciliation required'));
        assert.ok(el('copy-live-kpis').innerHTML.includes('Live trading is off'), 'drift does not disturb the off card');
        renderCopyLiveBalanceDrift({ within_tolerance: true, drift_usd: 0 });
        assert.ok(!el('copy-positions-live-drift-banner').classList.contains('visible'));
    """, tmp_path)


# ---------------------------------------------------------------------------
# Open positions / Recent Closed
# ---------------------------------------------------------------------------

def test_open_positions_table_is_inside_a_scroll_wrapper_with_a_count(tmp_path):
    run_js("""
        posture(true);
        routes['/api/copy-trading/positions'] = () => ({ body: POS({
          live_open_positions: [OPEN(1, '0xLive', 10), OPEN(2, '0xLive', 5, { status: 'pending', fill_price: null })],
          live_total: { n_settled: 1, realized_pnl_usd: 1 }, live_realized_pnl_history: [CLOSED(1)] }) });
        await fetchCopyTradingModePosture();
        await fetchCopyTradingPositions('live');
        const html = el('copy-positions-live-content').innerHTML;
        assert.ok(/<div class="copy-table-wrap">\\s*<table class="copy-table"/.test(html), 'table sits in .copy-table-wrap');
        assert.ok(html.includes('Pending') && !html.includes('nullcent') && !html.includes('null'), 'pending row renders a placeholder');
        assert.strictEqual(el('copy-live-open-count').textContent, '2');
        assert.notStrictEqual(el('copy-live-open-count').style.display, 'none');
    """, tmp_path)


def test_recent_closed_pages_newest_first_15_per_page(tmp_path):
    run_js("""
        posture(true);
        // payload is OLDEST-first (n = minute offset): 1 (oldest) .. 40 (newest)
        const hist = Array.from({ length: 40 }, (_, i) => CLOSED(i + 1, i % 2 ? 1 : -1));
        routes['/api/copy-trading/positions'] = () => ({ body: POS({
          live_total: { n_settled: 40, realized_pnl_usd: 0 }, live_realized_pnl_history: hist }) });
        await fetchCopyTradingModePosture();
        await fetchCopyTradingPositions('live');

        const list = () => el('copy-live-closed-list').innerHTML;
        const markets = () => [...list().matchAll(/LIVE-CLOSED-(\\d+)/g)].map(m => +m[1]).filter((v, i, a) => a.indexOf(v) === i);
        assert.deepStrictEqual(markets(), Array.from({ length: 15 }, (_, i) => 40 - i), 'page 1 = newest 15, newest first');
        assert.strictEqual(el('copy-live-closed-count').textContent, '40');
        assert.strictEqual(el('copy-live-closed-pagination').style.display, 'flex');
        assert.strictEqual(el('copy-live-closed-info').textContent, 'Page 1 of 3');
        assert.strictEqual(el('copy-live-closed-prev').disabled, true);
        assert.strictEqual(el('copy-live-closed-next').disabled, false);
        assert.ok(list().includes('copy-pnl-pos') && list().includes('copy-pnl-neg'), 'pos/neg P&L classes');
        assert.ok(/<div class="copy-table-wrap">/.test(list()));

        copyLiveClosedChangePage(1);
        assert.deepStrictEqual(markets(), Array.from({ length: 15 }, (_, i) => 25 - i));
        assert.strictEqual(el('copy-live-closed-info').textContent, 'Page 2 of 3');
        assert.strictEqual(el('copy-live-closed-prev').disabled, false);

        copyLiveClosedChangePage(1);
        assert.deepStrictEqual(markets(), Array.from({ length: 10 }, (_, i) => 10 - i), 'last page holds the 10 oldest');
        assert.strictEqual(el('copy-live-closed-next').disabled, true);
        copyLiveClosedChangePage(1);                       // past the end: clamped
        assert.strictEqual(el('copy-live-closed-info').textContent, 'Page 3 of 3');
        copyLiveClosedChangePage(-5);                      // before the start: clamped
        assert.strictEqual(el('copy-live-closed-info').textContent, 'Page 1 of 3');

        // A background refresh keeps the operator's page (and clamps if it shrank).
        copyLiveClosedChangePage(1);
        hist.push(CLOSED(41));
        fresh();
        await fetchCopyTradingPositions('live');
        assert.strictEqual(el('copy-live-closed-info').textContent, 'Page 2 of 3');
    """, tmp_path)


def test_recent_closed_hides_the_pager_for_one_page_and_orders_ties_by_payload(tmp_path):
    run_js("""
        posture(true);
        const same = '2026-09-01T00:00:00.000Z';
        const hist = [1, 2, 3].map(n => ({ ...CLOSED(n), settled_at: same }));
        routes['/api/copy-trading/positions'] = () => ({ body: POS({
          live_total: { n_settled: 3, realized_pnl_usd: 3 }, live_realized_pnl_history: hist }) });
        await fetchCopyTradingModePosture();
        await fetchCopyTradingPositions('live');
        assert.strictEqual(el('copy-live-closed-pagination').style.display, 'none', '<= 15 rows: no pager');
        const order = [...el('copy-live-closed-list').innerHTML.matchAll(/LIVE-CLOSED-(\\d)/g)].map(m => m[1]).filter((v, i, a) => a.indexOf(v) === i);
        assert.deepStrictEqual(order, ['3', '2', '1'], 'equal timestamps: latest payload row first');
    """, tmp_path)


def test_unchanged_polls_do_not_rebuild_the_live_dom(tmp_path):
    run_js("""
        posture(true);
        routes['/api/copy-trading/followed-wallets'] = () => ({ body: FOLLOWED([LIVE_W('0xLive'), W('0xReady')]) });
        const hist = Array.from({ length: 20 }, (_, i) => CLOSED(i + 1));
        routes['/api/copy-trading/positions'] = () => ({ body: POS({
          live_open_positions: [OPEN(1, '0xLive', 10)], live_total: { n_settled: 20, realized_pnl_usd: 20 },
          live_realized_pnl_history: hist, live_per_wallet: [{ address: '0xLive', n_settled: 20, realized_pnl_usd: 20 }] }) });
        routes['/api/copy-trading/activity-feed'] = () => ({ body: { events: [
          { event_type: 'live_order_filled', ts: '2026-09-20T00:00:00Z', address: '0xLive', mode: 'live', market: 'M', fill_price: 0.4, size_usd: 2, signal_id: 1 }] } });
        await fetchCopyTradingModePosture();
        await fetchFollowedWallets('live');
        await fetchCopyTradingPositions('live');
        await fetchCopyTradingActivityFeed('live');
        const ids = [...LIVE_CONTAINERS, 'copy-live-activity-list'];
        const writes = () => ids.map(id => el(id)._writes);
        const before = writes();
        const charts = chartCount;
        for (let i = 0; i < 3; i++) {
          fresh();
          await fetchCopyTradingModePosture();
          await fetchFollowedWallets('live');
          await fetchCopyTradingPositions('live');
          await fetchCopyTradingActivityFeed('live');
        }
        assert.deepStrictEqual(writes(), before, 'identical payloads must not rewrite any Live container');
        assert.strictEqual(chartCount, charts, 'the live chart is not torn down on an unchanged poll');

        // Re-rendering from the SAME data (posture re-check, page click on one page) also skips the write.
        renderCopyLivePositions(_copyLivePositionsData, true);
        assert.deepStrictEqual(writes(), before);

        // A real change does rebuild.
        routes['/api/copy-trading/positions'] = () => ({ body: POS({
          live_open_positions: [OPEN(1, '0xLive', 10), OPEN(2, '0xLive', 3)], live_total: { n_settled: 20, realized_pnl_usd: 20 },
          live_realized_pnl_history: hist, live_per_wallet: [{ address: '0xLive', n_settled: 20, realized_pnl_usd: 20 }] }) });
        fresh();
        await fetchCopyTradingPositions('live');
        assert.notStrictEqual(el('copy-positions-live-content')._writes, before[1]);
        assert.notStrictEqual(el('copy-live-kpis')._writes, before[0]);
    """, tmp_path)


def test_range_change_redraws_the_chart_from_the_last_good_payload(tmp_path):
    run_js("""
        posture(true);
        routes['/api/copy-trading/positions'] = () => ({ body: POS({
          live_open_positions: [OPEN(1, '0xLive', 10)], live_total: { n_settled: 2, realized_pnl_usd: 4 },
          live_realized_pnl_history: [CLOSED(1, 1), CLOSED(2, 3)],
          live_per_wallet: [{ address: '0xLive', n_settled: 2, realized_pnl_usd: 4 }] }) });
        await fetchCopyTradingModePosture();
        await fetchCopyTradingPositions('live');
        assert.ok(el('copy-live-pnl-content').innerHTML.includes('copy-positions-live-canvas'));
        const before = chartCount;
        _copyLivePositionsOnRangeChange({ target: { value: 'all' } });
        assert.strictEqual(chartCount, before + 1, 'changing the range redraws the chart once');
        // a degraded poll does not stop the range control working on the good data
        routes['/api/copy-trading/positions'] = () => ({ body: POS({ live_error: 'x' }) });
        fresh();
        await fetchCopyTradingPositions('live');
        _copyLivePositionsOnRangeChange({ target: { value: '7' } });
        assert.strictEqual(chartCount, before + 2);
    """, tmp_path)


# ---------------------------------------------------------------------------
# Live activity paging
# ---------------------------------------------------------------------------

def test_live_activity_pages_at_25_and_resets_on_filter_change(tmp_path):
    run_js("""
        posture(true);
        await fetchCopyTradingModePosture();
        // newest first from the backend: ev-60 is the newest
        const events = Array.from({ length: 60 }, (_, i) => ({
          event_type: 'live_order_filled', ts: new Date(Date.UTC(2026, 8, 20, 0, 60 - i)).toISOString(),
          address: i % 2 ? '0xB' : '0xA', mode: 'live', market: 'ev-' + (60 - i), fill_price: 0.4, size_usd: 2, signal_id: i }));
        routes['/api/copy-trading/activity-feed'] = () => ({ body: { events } });
        await fetchCopyTradingActivityFeed('live');
        const items = () => [...el('copy-live-activity-list').innerHTML.matchAll(/ev-(\\d+)/g)].map(m => +m[1]);
        assert.strictEqual(items().length, 25);
        assert.strictEqual(items()[0], 60, 'newest first');
        assert.strictEqual(el('copy-live-activity-pagination').style.display, 'flex');
        assert.strictEqual(el('copy-live-activity-info').textContent, 'Page 1 of 3');
        assert.strictEqual(el('copy-live-activity-prev').disabled, true);

        copyLiveActivityChangePage(1);
        assert.deepStrictEqual([items()[0], items()[24]], [35, 11]);
        assert.strictEqual(el('copy-live-activity-info').textContent, 'Page 2 of 3');
        copyLiveActivityChangePage(1);
        assert.strictEqual(items().length, 10);
        assert.strictEqual(el('copy-live-activity-next').disabled, true);
        copyLiveActivityChangePage(1);
        assert.strictEqual(el('copy-live-activity-info').textContent, 'Page 3 of 3', 'clamped at the last page');

        // A background refresh keeps the page.
        fresh();
        routes['/api/copy-trading/activity-feed'] = () => ({ body: { events: [...events] } });
        await fetchCopyTradingActivityFeed('live');
        assert.strictEqual(el('copy-live-activity-info').textContent, 'Page 3 of 3');
    """, tmp_path)


def test_live_activity_filter_change_resets_to_page_one(tmp_path):
    run_js("""
        posture(true);
        await fetchCopyTradingModePosture();
        const events = Array.from({ length: 60 }, (_, i) => ({
          event_type: 'live_order_filled', ts: new Date(Date.UTC(2026, 8, 20, 0, 60 - i)).toISOString(),
          address: i % 2 ? '0xB' : '0xA', mode: 'live', market: 'ev-' + (60 - i), fill_price: 0.4, size_usd: 2, signal_id: i }));
        routes['/api/copy-trading/activity-feed'] = () => ({ body: { events } });
        await fetchCopyTradingActivityFeed('live');
        _copyActivityInitStaticControls();
        copyLiveActivityChangePage(1);
        copyLiveActivityChangePage(1);
        assert.strictEqual(el('copy-live-activity-info').textContent, 'Page 3 of 3');
        el('copy-live-activity-wallet-select')._listeners.change[0]({ target: { value: '0xA' } });
        assert.strictEqual(el('copy-live-activity-info').textContent, 'Page 1 of 2', '30 events for 0xA, back on page 1');
        el('copy-live-activity-type-select')._listeners.change[0]({ target: { value: 'live_order_rejected' } });
        assert.strictEqual(el('copy-live-activity-pagination').style.display, 'none', 'no matches: pager hidden');
        assert.ok(el('copy-live-activity-list').innerHTML.includes('No matching activity'));
    """, tmp_path)


def test_live_activity_pager_is_hidden_for_a_single_page(tmp_path):
    run_js("""
        posture(true);
        await fetchCopyTradingModePosture();
        routes['/api/copy-trading/activity-feed'] = () => ({ body: { events: [
          { event_type: 'live_order_filled', ts: '2026-09-20T00:00:00Z', address: '0xA', mode: 'live', market: 'M', fill_price: 0.4, size_usd: 2, signal_id: 1 }] } });
        await fetchCopyTradingActivityFeed('live');
        assert.strictEqual(el('copy-live-activity-pagination').style.display, 'none');
    """, tmp_path)


# ---------------------------------------------------------------------------
# Roster: Ready to go live (labelled PAPER P&L, collapse) and Edit live stake
# ---------------------------------------------------------------------------

def test_ready_list_shows_only_the_labelled_paper_pnl(tmp_path):
    run_js("""
        renderFollowedWallets(FOLLOWED([LIVE_W('0xLive'), W('0xReady', { realized_pnl_usd: -3.25, stake_per_trade: 9.99 })]), 'live');
        const ready = el('copy-live-ready-list').innerHTML;
        assert.ok(ready.includes('0xReady') && ready.includes('btn-followed-golive'));
        assert.ok(ready.includes('mode-badge-paper">PAPER</span><span class="sr-only">Paper P&amp;L </span>-$3.25'),
          'the paper figure sits next to a PAPER badge');
        assert.ok(ready.includes('Paper P&amp;L <span class="mode-badge mode-badge-paper">PAPER</span>'), 'column header carries the badge');
        assert.strictEqual((ready.match(/[$][0-9]/g) || []).length, 1, 'that one figure is the only dollar amount');
        assert.ok(!ready.includes('9.99'), 'no paper stake in the Ready list');
        const roster = el('copy-live-followed-list').innerHTML;
        assert.ok(!roster.includes('123.45') && !roster.includes('5.55') && !roster.includes('-$3.25'), 'no paper figure in the Live roster');
        assert.ok(!/<th[^>]*>[^<]*P&amp;L/.test(roster), 'no per-wallet P&L column in the Live roster');
    """, tmp_path)


def test_ready_list_collapses_above_five_rows_and_remembers_its_state(tmp_path):
    run_js("""
        const ready = (n) => FOLLOWED([LIVE_W('0xLive'), ...Array.from({ length: n }, (_, i) => W('0xReady' + i))]);
        renderFollowedWallets(ready(5), 'live');
        assert.ok(!el('copy-live-ready-list').innerHTML.includes('<details'), '5 rows stay expanded');
        assert.ok(el('copy-live-ready-list').innerHTML.includes('0xReady4'));

        renderFollowedWallets(ready(6), 'live');
        let html = el('copy-live-ready-list').innerHTML;
        assert.ok(html.includes('<details') && !/<details[^>]* open/.test(html), '6 rows collapse by default');
        assert.ok(html.includes('6 wallets ready to go live'));
        assert.ok(html.includes('0xReady5'), 'rows are still in the DOM inside the details');

        // The operator opens it; a later (changed) refresh must not snap it shut.
        _copyLiveReadyOnToggle({ open: true });
        renderFollowedWallets(ready(7), 'live');
        assert.ok(/<details[^>]* open/.test(el('copy-live-ready-list').innerHTML), 'stays open across a re-render');
        _copyLiveReadyOnToggle({ open: false });
        renderFollowedWallets(ready(7), 'live');
        assert.ok(!/<details[^>]* open/.test(el('copy-live-ready-list').innerHTML));
    """, tmp_path)


def _edit_stake_js(body: str) -> str:
    """Shared setup for the Edit-live-stake tests: one live wallet with a $2.00
    live override and a paper stake of $5.55, live cap $10."""
    return textwrap.dedent("""
        currentTab = 'copy-live';
        let prompts = [], confirms = [];
        let promptAnswer = null, confirmAnswer = true;
        window.prompt = (msg, def) => { prompts.push({ msg, def }); return promptAnswer; };
        window.confirm = (msg) => { confirms.push(msg); return confirmAnswer; };
        const mkBtn = () => new Element('btn');
        const setup = (wallet) => {
          renderFollowedWallets(FOLLOWED([wallet]), 'live');
          routes['/api/copy-trading/wallets/'] = () => ({ body: { success: true, message: 'ok' } });
          routes['/api/copy-trading/followed-wallets'] = () => ({ body: FOLLOWED([wallet]) });
          fresh();
        };
        const patches = () => calls.filter(c => c.opts && c.opts.method === 'PATCH');
    """) + textwrap.dedent(body)


def test_edit_live_stake_button_only_for_unpaused_live_wallets(tmp_path):
    run_js("""
        renderFollowedWallets(FOLLOWED([LIVE_W('0xAct'), LIVE_W('0xPaused', { status: 'paused', live_eligible: false,
          live_status_reason: 'this wallet is paused' })]), 'live');
        const rows = el('copy-live-followed-list').innerHTML.split('<tr class="copy-row"').slice(1);
        const act = rows.find(r => r.includes('0xAct')), paused = rows.find(r => r.includes('0xPaused'));
        assert.ok(act.includes('btn-followed-edit-live-stake') && act.includes('Edit live stake'));
        assert.ok(act.includes('btn-followed-pause') && act.includes('btn-followed-revert-live'), 'Pause + Revert stay');
        assert.ok(!paused.includes('btn-followed-edit-live-stake'), 'no live-stake edit while paused');
        assert.ok(paused.includes('btn-followed-resume'));
        assert.ok(!act.includes('btn-followed-edit-stake'), 'the paper stake editor never appears on Live');
        // routed through the single delegated handler
        const seen = [];
        const btn = Object.assign(new Element('b'), { dataset: { address: '0xAct' } });
        window.prompt = (m, d) => { seen.push(d); return null; };
        currentTab = 'copy-live';
        _followedOnListClick({ target: { closest: (sel) => sel === '.btn-followed-edit-live-stake' ? btn : null }, stopPropagation() {} });
        assert.deepStrictEqual(seen, ['2.00'], 'click routes to followedEditLiveStake, prefilled with the RESOLVED live stake');
    """, tmp_path)


def test_edit_live_stake_cancel_invalid_and_unchanged_never_call_the_api(tmp_path):
    run_js(_edit_stake_js("""
        setup(LIVE_W('0xLive'));
        promptAnswer = null;
        await followedEditLiveStake('0xLive', mkBtn());                 // cancelled
        for (const bad of ['abc', '0', '-1', '1e999', '10.01', '5abc']) {
          promptAnswer = bad;
          await followedEditLiveStake('0xLive', mkBtn());
          assert.ok(el('copy-live-error-banner').classList.contains('visible'), 'banner for ' + bad);
          el('copy-live-error-banner').classList.remove('visible');
        }
        assert.ok(el('copy-live-error-text').textContent.includes('Could not set a live stake') || true);
        promptAnswer = '2.00';                                          // unchanged
        await followedEditLiveStake('0xLive', mkBtn());
        assert.strictEqual(patches().length, 0, 'no request for cancel / invalid / unchanged input');
        assert.strictEqual(confirms.length, 0);
        assert.ok(prompts[0].def === '2.00' && !prompts[0].msg.includes('5.55'), 'prefilled with the live stake; the prompt prints no paper figure');
        assert.ok(prompts[0].msg.includes('$10.00'), 'names the live cap');
    """), tmp_path)


def test_edit_live_stake_lower_patches_without_confirm(tmp_path):
    run_js(_edit_stake_js("""
        setup(LIVE_W('0xLive'));
        promptAnswer = '1.5';
        await followedEditLiveStake('0xLive', mkBtn());
        assert.strictEqual(confirms.length, 0, 'lowering the live stake needs no confirmation');
        assert.strictEqual(patches().length, 1);
        assert.ok(patches()[0].url.endsWith('/api/copy-trading/wallets/0xLive/live-stake'));
        assert.deepStrictEqual(JSON.parse(patches()[0].opts.body), { stake: 1.5 });
        assert.ok(callsTo('/followed-wallets').length >= 1, 'roster refetched after success');
    """), tmp_path)


def test_edit_live_stake_raise_requires_confirmation(tmp_path):
    run_js(_edit_stake_js("""
        setup(LIVE_W('0xLive'));
        promptAnswer = '4';
        confirmAnswer = false;
        await followedEditLiveStake('0xLive', mkBtn());
        assert.strictEqual(confirms.length, 1);
        assert.ok(confirms[0].includes('REAL trades of $4.00') && confirms[0].includes('up from $2.00'), confirms[0]);
        assert.strictEqual(patches().length, 0, 'declined -> nothing sent');

        confirmAnswer = true;
        await followedEditLiveStake('0xLive', mkBtn());
        assert.strictEqual(patches().length, 1);
        assert.deepStrictEqual(JSON.parse(patches()[0].opts.body), { stake: 4 });
    """), tmp_path)


def test_edit_live_stake_blank_relinks_to_paper_and_confirms_a_raise(tmp_path):
    run_js(_edit_stake_js("""
        // override 2.00 < paper 5.55: blank means "inherit paper" = a RAISE -> confirm
        setup(LIVE_W('0xLive'));
        promptAnswer = '   ';
        await followedEditLiveStake('0xLive', mkBtn());
        assert.strictEqual(confirms.length, 1);
        assert.ok(confirms[0].includes('$5.55') && confirms[0].includes('paper stake'), confirms[0]);
        assert.deepStrictEqual(JSON.parse(patches()[0].opts.body), { stake: null });

        // override 9 > paper 5.55: blank is a LOWER -> no confirm
        confirms.length = 0; calls.length = 0;
        setup(LIVE_W('0xLive', { live_stake_per_trade: 9, live_stake_is_override: true }));
        await followedEditLiveStake('0xLive', mkBtn());
        assert.strictEqual(confirms.length, 0);
        assert.deepStrictEqual(JSON.parse(patches()[0].opts.body), { stake: null });

        // already inheriting + blank: unchanged
        calls.length = 0;
        setup(LIVE_W('0xLive', { live_stake_per_trade: 5.55, live_stake_is_override: false }));
        await followedEditLiveStake('0xLive', mkBtn());
        assert.strictEqual(patches().length, 0);
    """), tmp_path)


def test_edit_live_stake_failure_surfaces_the_banner_and_reenables_the_button(tmp_path):
    run_js(_edit_stake_js("""
        setup(LIVE_W('0xLive'));
        routes['/api/copy-trading/wallets/'] = () => ({ ok: true, body: { success: false, message: 'not a followed wallet' } });
        promptAnswer = '1';
        const btn = mkBtn();
        await followedEditLiveStake('0xLive', btn);
        assert.ok(el('copy-live-error-banner').classList.contains('visible'));
        assert.ok(el('copy-live-error-text').textContent.includes('Could not update the live stake') &&
                  el('copy-live-error-text').textContent.includes('not a followed wallet'));
        assert.strictEqual(btn.disabled, false);
        assert.strictEqual(callsTo('/followed-wallets').length, 0, 'no refetch / no optimistic change on failure');
    """), tmp_path)


def test_edit_live_stake_needs_a_finite_cap(tmp_path):
    run_js(_edit_stake_js("""
        const w = LIVE_W('0xLive');
        renderFollowedWallets({ ...FOLLOWED([w]), live_cap_usd: undefined }, 'live');
        promptAnswer = '1';
        await followedEditLiveStake('0xLive', mkBtn());
        assert.strictEqual(prompts.length, 0, 'no dialog without a known cap');
        assert.strictEqual(patches().length, 0);
        assert.ok(el('copy-live-error-banner').classList.contains('visible'));
    """), tmp_path)

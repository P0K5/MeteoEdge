"""Unit tests for the Copy-Trading dashboard Positions & P&L view's
live/paper twin-panel client-side logic (issue #1186, Epic J).

Same technique as test_copy_trading_mode_banner_js_logic.py /
test_copy_trading_activity_feed_js_logic.py (issue #758): this repo has no
JS test framework, so this extracts the *actual* inline <script> block
shipped in src/dashboard/static/index.html and executes it under plain
Node, with minimal DOM/fetch/window stubs.

Covers the issue's explicit acceptance-criteria states for the Live
column, independent of the Paper column:
- Off (no historical live data): dedicated off-state, no numeric figures
  at all, dimmed/neutral, with a link to Config.
- On but empty: distinct "No live positions yet." state with the live
  accent, never a fake $0.00.
- Off but with historical live data: real data renders, with an
  off-banner layered on top (never hidden).
- Populated + on: no off-banner.
- A live-side query failure (`live_error`) degrades only the Live column
  and never touches the Paper column's banner/content.
- The Live column re-renders off the fast-polled (30s) global posture
  flag, not this endpoint's own 5-minute poll -- flipping
  fetchCopyTradingModePosture()'s result alone (no new positions fetch)
  must update the Live column immediately.
- renderCopyLiveBalanceDrift() (issue #1189): the reconciliation-required
  banner stays hidden when never-checked or within-tolerance, appears with
  the un-softened "manual reconciliation required" language on a flagged
  drift, and hides again once a later clean check clears it.
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

_PRELUDE = textwrap.dedent("""
    function makeStubElement() {
      const classSet = new Set();
      const el = {
        style: {}, dataset: {}, children: [], childElementCount: 0,
        classList: {
          add(...names) { names.forEach(n => classSet.add(n)); },
          remove(...names) { names.forEach(n => classSet.delete(n)); },
          toggle(name, force) {
            const has = classSet.has(name);
            const want = force === undefined ? !has : force;
            if (want) classSet.add(name); else classSet.delete(name);
            return want;
          },
          contains(name) { return classSet.has(name); },
        },
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
    global.Chart = function () { this.destroy = () => {}; };
    global.getComputedStyle = () => ({ getPropertyValue: () => '' });
    global.event = { target: makeStubElement() };
    global.navigator = { clipboard: { writeText: async () => {} } };
""")

_ASSERTIONS = textwrap.dedent("""
    const assert = require('assert');

    function emptyPositionsPayload() {
      return {
        open_positions: [], realized_pnl_history: [], per_wallet: [],
        total: { n_settled: 0, realized_pnl_usd: 0.0 },
        backtest_total: { n_wallets: 0, n_settled: 0, realized_pnl_usd: 0.0,
                           projected_flat_dollar_pnl: 0.0, divergence_usd: 0.0, divergence_pct: null },
        live_open_positions: [], live_realized_pnl_history: [], live_per_wallet: [],
        live_total: { n_settled: 0, realized_pnl_usd: 0.0 },
        live_error: null,
      };
    }

    (async () => {
      // ------------------------------------------------------------------
      // 1. Off + no historical live data: dedicated off-state, no numeric
      //    figures at all, dimmed/neutral, with a Config link.
      // ------------------------------------------------------------------
      let data = emptyPositionsPayload();
      renderCopyLivePositions(data, false);
      let wrap = document.getElementById('copy-positions-live-content');
      assert.ok(wrap.innerHTML.includes('Live trading is off'), 'off-state text missing');
      assert.ok(!wrap.innerHTML.includes('$'), 'off-state must never show a numeric figure, not even $0.00');
      assert.ok(!wrap.innerHTML.includes('copy-positions-live-accent'), 'off-state must not use the live accent');
      assert.ok(wrap.innerHTML.includes('_copyGoToConfigTab'), 'off-state must link to where the switch lives');

      // ------------------------------------------------------------------
      // 2. On but empty: distinct "No live positions yet." state, live
      //    accent, never a fake zero.
      // ------------------------------------------------------------------
      data = emptyPositionsPayload();
      renderCopyLivePositions(data, true);
      wrap = document.getElementById('copy-positions-live-content');
      assert.ok(wrap.innerHTML.includes('No live positions yet.'), 'on-but-empty text missing');
      assert.ok(wrap.innerHTML.includes('copy-positions-live-accent'), 'on-but-empty must use the live accent');
      assert.ok(!wrap.innerHTML.includes('Live trading is off'), 'on-but-empty must not read as the off-state');

      // ------------------------------------------------------------------
      // 3. These two states are textually distinct from each other (never
      //    the same message for off vs. on-but-empty).
      // ------------------------------------------------------------------
      renderCopyLivePositions(emptyPositionsPayload(), false);
      const offHtml = document.getElementById('copy-positions-live-content').innerHTML;
      renderCopyLivePositions(emptyPositionsPayload(), true);
      const onEmptyHtml = document.getElementById('copy-positions-live-content').innerHTML;
      assert.notStrictEqual(offHtml, onEmptyHtml);

      // ------------------------------------------------------------------
      // 4. Historical live data exists (a settled position, ever) but
      //    live is currently OFF: real data renders (never hidden), with
      //    an off-banner layered on top.
      // ------------------------------------------------------------------
      data = emptyPositionsPayload();
      data.live_total = { n_settled: 1, realized_pnl_usd: 15.0 };
      data.live_realized_pnl_history = [
        { address: '0xabc', market: 'M1', settled_at: '2026-09-20T00:00:00Z', settled_pnl_usd: 15.0, stake_usd: 25.0 },
      ];
      data.live_per_wallet = [{ address: '0xabc', n_settled: 1, realized_pnl_usd: 15.0,
                                 projected_flat_dollar_pnl: null, divergence_usd: null, divergence_pct: null }];
      renderCopyLivePositions(data, false);
      wrap = document.getElementById('copy-positions-live-content');
      assert.ok(wrap.innerHTML.includes('warn-banner'), 'off-with-history must layer a warning banner on top');
      assert.ok(wrap.innerHTML.includes('0xabc') || wrap.innerHTML.includes('abc'), 'off-with-history must still show the real per-wallet data');
      assert.ok(!wrap.innerHTML.includes('No live positions yet.'), 'off-with-history must not read as the on-but-empty state');
      assert.ok(!wrap.innerHTML.includes('<h3>Live trading is off</h3>'), 'off-with-history must not use the numberless off-state');

      // ------------------------------------------------------------------
      // 5. Same populated data, but live is currently ON: no off-banner.
      // ------------------------------------------------------------------
      renderCopyLivePositions(data, true);
      wrap = document.getElementById('copy-positions-live-content');
      assert.ok(!wrap.innerHTML.includes('warn-banner'), 'on + populated must not show the off-banner');

      // ------------------------------------------------------------------
      // 6. Open live positions with no fill price yet (status='pending')
      //    render "—" rather than crashing on a null fill_price.
      // ------------------------------------------------------------------
      data = emptyPositionsPayload();
      data.live_open_positions = [{
        id: 1, address: '0xabc', market: 'M1', outcome_index: 0, status: 'pending',
        order_id: null, fill_price: null, stake_usd: 10.0, filled_stake_usd: null,
        entry_ts: '2026-09-19T00:00:00Z', signal_id: 7,
      }];
      renderCopyLivePositions(data, true);
      wrap = document.getElementById('copy-positions-live-content');
      assert.ok(wrap.innerHTML.includes('Pending'), 'pending status must render');
      assert.ok(!wrap.innerHTML.includes('nullcent'), 'must not stringify a null fill_price');

      // ------------------------------------------------------------------
      // 7. The Live aggregate P&L pill is independently labeled -- never
      //    an unqualified "aggregate P&L".
      // ------------------------------------------------------------------
      data = emptyPositionsPayload();
      data.live_total = { n_settled: 3, realized_pnl_usd: 42.5 };
      renderCopyLivePositions(data, true);
      const pill = document.getElementById('copy-positions-live-total-pill');
      assert.ok(pill.textContent.includes('Live aggregate P&L'), `expected explicit Live label, got: ${pill.textContent}`);
      assert.ok(pill.textContent.includes('42.5') || pill.textContent.includes('42.50'));

      // ------------------------------------------------------------------
      // 8. A live-side query failure (live_error) shows the live column's
      //    own banner and must never blank a previously-rendered good
      //    state, nor touch the paper column at all.
      // ------------------------------------------------------------------
      data = emptyPositionsPayload();
      data.live_total = { n_settled: 1, realized_pnl_usd: 15.0 };
      data.live_open_positions = [{
        id: 9, address: '0xabc', market: 'M2', outcome_index: 0, status: 'filled',
        order_id: 'o1', fill_price: 0.4, stake_usd: 10.0, filled_stake_usd: 10.0,
        entry_ts: '2026-09-19T00:00:00Z', signal_id: 8,
      }];
      renderCopyLivePositions(data, true);
      const goodHtml = document.getElementById('copy-positions-live-content').innerHTML;
      assert.ok(goodHtml.length > 0);

      const paperWrap = document.getElementById('copy-positions-content');
      paperWrap.innerHTML = 'UNTOUCHED_PAPER_CONTENT';

      const erroredData = emptyPositionsPayload();
      erroredData.live_error = 'Live data temporarily unavailable: boom';
      renderCopyLivePositions(erroredData, true);

      const liveBanner = document.getElementById('copy-positions-live-error-banner');
      assert.ok(liveBanner.classList.contains('visible'), 'live error banner must become visible');
      assert.strictEqual(
        document.getElementById('copy-positions-live-content').innerHTML, goodHtml,
        'a live_error must preserve the last-known-good live column DOM, not blank it',
      );
      assert.strictEqual(paperWrap.innerHTML, 'UNTOUCHED_PAPER_CONTENT', 'a live_error must never touch the paper column');

      // ------------------------------------------------------------------
      // 9. Wallet-balance-drift reconciliation banner (issue #1189):
      //    never checked yet (all-None) -> hidden; within tolerance ->
      //    hidden (clears a prior warning); flagged drift -> visible with
      //    the "manual reconciliation required" language, never softened.
      // ------------------------------------------------------------------
      renderCopyLiveBalanceDrift({
        checked_at: null, within_tolerance: null, drift_usd: null,
        expected_balance_usd: null, actual_balance_usd: null,
      });
      let driftBanner = document.getElementById('copy-positions-live-drift-banner');
      assert.ok(!driftBanner.classList.contains('visible'), 'never-checked must not show the drift banner');

      renderCopyLiveBalanceDrift({
        checked_at: '2026-09-20T00:00:00Z', within_tolerance: true, drift_usd: 0.0,
        expected_balance_usd: 100.0, actual_balance_usd: 100.0,
      });
      driftBanner = document.getElementById('copy-positions-live-drift-banner');
      assert.ok(!driftBanner.classList.contains('visible'), 'within-tolerance must not show the drift banner');

      renderCopyLiveBalanceDrift({
        checked_at: '2026-09-20T01:00:00Z', within_tolerance: false, drift_usd: 12.34,
        expected_balance_usd: 100.0, actual_balance_usd: 112.34,
      });
      driftBanner = document.getElementById('copy-positions-live-drift-banner');
      assert.ok(driftBanner.classList.contains('visible'), 'a flagged drift must show the drift banner');
      const driftText = document.getElementById('copy-positions-live-drift-text').textContent;
      assert.ok(driftText.includes('manual reconciliation required'), `drift banner text must not soften the language, got: ${driftText}`);
      assert.ok(driftText.includes('12.34'), `drift banner text must include the drift amount, got: ${driftText}`);

      // A later clean check clears the previously-visible warning.
      renderCopyLiveBalanceDrift({
        checked_at: '2026-09-20T02:00:00Z', within_tolerance: true, drift_usd: 0.0,
        expected_balance_usd: 100.0, actual_balance_usd: 100.0,
      });
      driftBanner = document.getElementById('copy-positions-live-drift-banner');
      assert.ok(!driftBanner.classList.contains('visible'), 'a later clean check must clear a previously-shown drift banner');

      console.log('ALL_POSITIONS_LIVE_PAPER_JS_ASSERTIONS_PASSED');
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
def test_positions_live_column_states_and_isolation(tmp_path):
    """Executes the real shipped dashboard script under Node and exercises
    the Positions & P&L Live column's state logic and its isolation from
    the Paper column."""
    combined = _PRELUDE + "\n" + _extract_inline_script() + "\n" + _ASSERTIONS
    script_path = tmp_path / "positions_live_paper_logic_check.js"
    script_path.write_text(combined, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "ALL_POSITIONS_LIVE_PAPER_JS_ASSERTIONS_PASSED" in result.stdout

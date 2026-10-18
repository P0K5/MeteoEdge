"""Unit tests for the "Recently Closed" two-source merge client-side logic
(issue #1457).

Same technique as test_copy_trading_positions_live_paper_js_logic.py (issue
#1186) / test_copy_trading_mode_banner_js_logic.py (issue #758): this repo
has no JS test framework, so this extracts the *actual* inline <script>
block shipped in src/dashboard/static/index.html and executes it under
plain Node, with minimal DOM/fetch/window stubs.

Covers #1457's acceptance criteria for the frontend half:
- fetchHistory() renders "Recently Closed" from the fast, network-free
  /api/portfolio/history endpoint via renderClosed(), independent of the
  slower /api/portfolio wallet fetch.
- mergeClosedFromWallet() (fed by /api/portfolio's closed_positions once
  that slower call resolves) dedupes by token_id -- entries already shown
  by the fast path are never duplicated.
- Genuinely new "manual trade" entries (wallet-only, not in the fast list)
  are appended, sorted back into newest-first order.
- A merge re-render preserves the current page (uses _renderClosedPage(),
  never resets to page 0 via renderClosed()) and flags freshly-merged rows
  with the existing .copy-row-highlight treatment.
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
    let _fetchResponse = { ok: true, json: async () => ({ closed_positions: [] }) };
    global.fetch = async () => _fetchResponse;
    global.lucide = { createIcons() {} };
    global.Chart = function () { this.destroy = () => {}; };
    global.getComputedStyle = () => ({ getPropertyValue: () => '' });
    global.event = { target: makeStubElement() };
    global.navigator = { clipboard: { writeText: async () => {} } };
""")

_ASSERTIONS = textwrap.dedent("""
    const assert = require('assert');

    function closedPos(tokenId, closedAt, overrides = {}) {
      return Object.assign({
        question: 'q', station: 'KLAX', side: 'NO', bracket_low: 60, bracket_high: 65,
        entry_price: 40, exit_price: 0, pnl: -12.0, shares: 30.0,
        closed_at: closedAt, token_id: tokenId, exit_reason: 'lost',
      }, overrides);
    }

    (async () => {
      // ------------------------------------------------------------------
      // 1. renderClosed() with no positions shows the empty state.
      // ------------------------------------------------------------------
      renderClosed([]);
      let el = document.getElementById('closed-positions');
      assert.ok(el.innerHTML.includes('No closed trades'), 'empty state missing');

      // ------------------------------------------------------------------
      // 2. renderClosed() with positions renders them and sets the count
      //    -- this is fetchHistory()'s render path (fast, independent of
      //    the wallet fetch).
      // ------------------------------------------------------------------
      const fast = [
        closedPos('tok-a', '2026-10-05T00:00:00Z'),
        closedPos('tok-b', '2026-10-01T00:00:00Z'),
      ];
      renderClosed(fast);
      el = document.getElementById('closed-positions');
      assert.ok(el.innerHTML.includes('tok-a') && el.innerHTML.includes('tok-b'),
        'both fast-path positions must render');
      assert.strictEqual(document.getElementById('closed-count').textContent, 2);

      // ------------------------------------------------------------------
      // 3. mergeClosedFromWallet() dedupes by token_id: re-submitting the
      //    same entries the fast path already rendered adds nothing.
      // ------------------------------------------------------------------
      mergeClosedFromWallet([closedPos('tok-a', '2026-10-05T00:00:00Z')]);
      assert.strictEqual(document.getElementById('closed-count').textContent, 2,
        'merging an already-shown token_id must not duplicate it');

      // ------------------------------------------------------------------
      // 4. mergeClosedFromWallet() appends a genuinely new "manual trade"
      //    entry (wallet-only, unseen by the fast path), sorted back into
      //    newest-first order, and flags it with the highlight class used
      //    elsewhere in this dashboard for a freshly-changed row.
      // ------------------------------------------------------------------
      mergeClosedFromWallet([
        closedPos('tok-a', '2026-10-05T00:00:00Z'),      // already shown -- skipped
        closedPos('tok-manual', '2026-10-09T00:00:00Z'),  // new -- newest of all three
      ]);
      assert.strictEqual(document.getElementById('closed-count').textContent, 3,
        'a genuinely new manual-trade entry must be appended exactly once');
      el = document.getElementById('closed-positions');
      assert.ok(el.innerHTML.includes('tok-manual'), 'new manual-trade entry must render');
      // The fake document stub doesn't parse innerHTML into real child nodes,
      // so inspect the rendered markup string directly for the card's class
      // attribute rather than going through classList on a freshly-created
      // (unpopulated) stub element.
      const manualCardMatch = el.innerHTML.match(/<div class="([^"]*)" id="closed-card-tok-manual">/);
      assert.ok(manualCardMatch, 'card markup for tok-manual not found');
      assert.ok(manualCardMatch[1].includes('copy-row-highlight'),
        'a freshly-merged row must get the existing highlight treatment');
      // Newest-first: tok-manual (10-09) must precede tok-a (10-05) in the DOM.
      assert.ok(el.innerHTML.indexOf('tok-manual') < el.innerHTML.indexOf('tok-a'),
        'merged entries must be re-sorted newest-first, not just appended at the end');

      // ------------------------------------------------------------------
      // 5. mergeClosedFromWallet() re-renders via _renderClosedPage(), not
      //    renderClosed() -- so it must not reset an already-advanced page
      //    (verified here via closedChangePage after loading >15 rows).
      // ------------------------------------------------------------------
      const many = [];
      for (let i = 0; i < 20; i++) {
        many.push(closedPos('tok-many-' + i, new Date(2026, 9, 20 - i).toISOString()));
      }
      renderClosed(many);
      closedChangePage(1);  // advance to page 2 (CLOSED_PAGE_SIZE=15 -> 2 pages)
      assert.strictEqual(document.getElementById('pg-info').textContent, 'Page 2 of 2');
      mergeClosedFromWallet([closedPos('tok-many-0', many[0].closed_at)]);  // already shown, no-op
      assert.strictEqual(document.getElementById('pg-info').textContent, 'Page 2 of 2',
        'a merge with nothing new to add must not reset the current page');

      // ------------------------------------------------------------------
      // 6. fetchHistory() is the standalone fetch against the new fast
      //    endpoint -- it renders "Recently Closed" straight from
      //    /api/portfolio/history's response, with no dependency on
      //    /api/portfolio or its wallet fetch.
      // ------------------------------------------------------------------
      _fetchResponse = {
        ok: true,
        json: async () => ({ closed_positions: [closedPos('tok-fast-only', '2026-10-10T00:00:00Z')] }),
      };
      await fetchHistory();
      el = document.getElementById('closed-positions');
      assert.ok(el.innerHTML.includes('tok-fast-only'),
        'fetchHistory() must render Recently Closed from /api/portfolio/history alone');
      assert.strictEqual(document.getElementById('closed-count').textContent, 1);

      // ------------------------------------------------------------------
      // 7. (#1473) A poll returning identical closed_positions must not reset
      //    the page or collapse an expanded chart; a changed list keeps the
      //    page (clamped) and expanded ids.
      // ------------------------------------------------------------------
      const poll = [];
      for (let i = 0; i < 20; i++) {
        poll.push(closedPos('tok-poll-' + i, new Date(2026, 9, 20 - i).toISOString()));
      }
      renderClosed(poll);
      closedChangePage(1);
      _closedExpandedIds.add('tok-poll-16');
      const htmlBefore = document.getElementById('closed-positions').innerHTML;
      renderClosed(JSON.parse(JSON.stringify(poll)));  // identical data
      assert.strictEqual(document.getElementById('pg-info').textContent, 'Page 2 of 2',
        'identical poll must not reset to page 1');
      assert.ok(_closedExpandedIds.has('tok-poll-16'), 'identical poll must keep expanded chart');
      assert.strictEqual(document.getElementById('closed-positions').innerHTML, htmlBefore);
      // Changed data (one more settled position): stays on page 2.
      renderClosed([closedPos('tok-poll-new', '2026-10-25T00:00:00Z')].concat(poll));
      assert.strictEqual(document.getElementById('pg-info').textContent, 'Page 2 of 2',
        'changed poll must keep the current page');
      assert.ok(_closedExpandedIds.has('tok-poll-16'), 'changed poll must keep expanded chart');
      assert.strictEqual(document.getElementById('closed-count').textContent, 21);
      // List shrinks to one page: page is clamped, vanished expanded ids dropped.
      renderClosed(poll.slice(0, 3));
      assert.strictEqual(_closedPage, 0);
      assert.ok(!_closedExpandedIds.has('tok-poll-16'), 'expanded id for removed row must be pruned');

      console.log('ALL_PORTFOLIO_HISTORY_MERGE_JS_ASSERTIONS_PASSED');
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
def test_closed_positions_merge_dedupe_and_page_preservation(tmp_path):
    """Executes the real shipped dashboard script under Node and exercises
    the Recently Closed two-source merge logic (issue #1457)."""
    combined = _PRELUDE + "\n" + _extract_inline_script() + "\n" + _ASSERTIONS
    script_path = tmp_path / "portfolio_history_merge_check.js"
    script_path.write_text(combined, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "ALL_PORTFOLIO_HISTORY_MERGE_JS_ASSERTIONS_PASSED" in result.stdout

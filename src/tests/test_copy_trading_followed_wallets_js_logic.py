"""Unit tests for the Copy-Trading dashboard Followed Wallets view's
client-side logic (epic F #1143, story F2 #1147).

Same technique as test_edge_tab_js_logic.py (issue #758): this repo has no
JS test framework (no package.json / jest / playwright), so this extracts
the *actual* inline <script> block shipped in
src/dashboard/static/index.html and executes it under plain Node (already
required by CI for the Edge tab's own JS-logic test), with minimal DOM/
fetch/window stubs.

Covers the two pieces the issue's acceptance criteria call out explicitly:
- The pause/resume toggle is genuinely *optimistic* (the status badge
  flips before the network round-trip resolves) and rolls back to the
  exact prior badge on a simulated backend failure.
- The unfollow action's window.confirm() dialog contains the required
  literal copy stating existing open positions will NOT be closed --
  checked as a literal substring, not just "some confirm dialog exists".

Also covers the second, independent live/paper badge added alongside the
paper status badge (epic J, issue #1187): the optimistic pause/resume
appliers must never leave a stale or falsely-LIVE badge showing in the
same status cell while a request is in flight.
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

# Minimal browser-global stubs, mirroring test_edge_tab_js_logic.py's
# _PRELUDE. getElementById returns the same stub instance for a given id on
# every call (a real DOM would too) -- needed so tests can render, then
# read back what a later getElementById(sameId) call sees (e.g. the status
# badge's innerHTML mid-request, before the fetch resolves).
_PRELUDE = textwrap.dedent("""
    function makeStubElement() {
      const el = {
        style: {}, classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
        dataset: {}, children: [], childElementCount: 0,
        addEventListener(){}, removeEventListener(){},
        querySelectorAll(){ return []; }, querySelector(){ return null; },
        appendChild(){}, remove(){}, disabled: false, title: '',
        focus(){}, setAttribute(){}, removeAttribute(){}, closest(){ return null; },
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

    function makeBtn() {
      const btn = makeStubElement();
      return btn;
    }

    (async () => {
      // ------------------------------------------------------------------
      // 1. Resume is genuinely optimistic: the status cell flips to
      //    "Active" synchronously (before the fetch promise resolves), and
      //    rolls back to the exact prior badge HTML on a simulated backend
      //    failure -- the acceptance criteria's explicit requirement.
      // ------------------------------------------------------------------
      let resolveFetch;
      global.fetch = (url, opts) => new Promise((resolve) => { resolveFetch = resolve; });

      const address = '0xResumeMe';
      const cell = document.getElementById('followed-status-cell-' + _copySafeId(address));
      cell.innerHTML = '<span class="followed-status-badge followed-status-paused"><i data-lucide="pause-circle"></i>Paused</span><span class="followed-paused-reason">unstable</span>'
        + '<span class="mode-badge mode-badge-paper" aria-label="Live status: paper only — this wallet is paused">PAPER</span>';
      const priorBadgeHtml = cell.innerHTML;

      const btn = makeBtn();
      const pending = followedResumeWallet(address, btn);
      // Not awaited yet -- optimistic flip must already be visible.
      assert.ok(cell.innerHTML.includes('Active'), 'resume must optimistically flip the badge before the fetch resolves');
      assert.ok(!cell.innerHTML.includes('Paused'), 'optimistic badge must not still say Paused');
      // Issue #1187: resuming can't derive the correct live badge
      // client-side (depends on server-side global switch + exposure), so
      // it must never optimistically guess LIVE -- it stays PAPER until
      // the refetch corrects it.
      assert.ok(cell.innerHTML.includes('PAPER'), 'resume must never optimistically show a LIVE badge');
      assert.ok(!cell.innerHTML.includes('mode-badge-live'), 'resume must never optimistically apply the live badge class');
      assert.strictEqual(btn.disabled, true, 'button must be disabled while the request is in flight');

      // Now the backend refuses (simulated failure).
      resolveFetch(jsonResp({ success: false, message: 'not paused' }));
      await pending;

      assert.strictEqual(cell.innerHTML, priorBadgeHtml, 'resume must roll back to the exact prior badge HTML on failure');
      assert.strictEqual(btn.disabled, false, 'button must be re-enabled after the failed request settles');

      // ------------------------------------------------------------------
      // 2. Pause is optimistic too, and requires a reason via window.prompt
      //    -- an empty/cancelled reason must never hit the network.
      // ------------------------------------------------------------------
      let fetchCallCount = 0;
      global.fetch = async () => { fetchCallCount += 1; return jsonResp({ success: true, message: 'ok' }); };
      window.prompt = () => null; // operator cancels
      await followedPauseWallet('0xPauseMe', makeBtn());
      assert.strictEqual(fetchCallCount, 0, 'cancelling the reason prompt must not call the API');

      window.prompt = () => '   '; // whitespace-only reason
      await followedPauseWallet('0xPauseMe', makeBtn());
      assert.strictEqual(fetchCallCount, 0, 'an empty/whitespace-only reason must not call the API');

      window.prompt = () => 'flagged by wallet-health job';
      let pauseCallCount = 0;
      let listRefetchCallCount = 0;
      let fetchBody = null;
      // A real success path also triggers onSuccess's full-list refetch
      // (fetchFollowedWallets()) -- the stub discriminates by URL so that
      // second call gets a shape renderFollowedWallets() can actually
      // consume, rather than the pause endpoint's {success,message} body.
      global.fetch = async (url, opts) => {
        if (String(url).endsWith('/pause')) {
          pauseCallCount += 1;
          fetchBody = opts && opts.body ? JSON.parse(opts.body) : null;
          return jsonResp({ success: true, message: 'ok' });
        }
        listRefetchCallCount += 1;
        return jsonResp({
          wallets: [], active_count: 0, paused_count: 0, aggregate_pnl_usd: 0, n_settled_total: 0,
          live_eligible_count: 0, paper_only_count: 0, live_aggregate_pnl_usd: 0, live_n_settled_total: 0,
          live_trading_enabled: false,
        });
      };
      const pauseCell = document.getElementById('followed-status-cell-' + _copySafeId('0xPauseMe'));
      pauseCell.innerHTML = '<span class="followed-status-badge followed-status-active"><i data-lucide="play-circle"></i>Active</span>'
        + '<span class="mode-badge mode-badge-live" aria-label="Live status: live — eligible for live execution">LIVE</span>';
      const pauseBtn = makeBtn();
      const pausePending = followedPauseWallet('0xPauseMe', pauseBtn);
      // followedPauseWallet reads window.prompt synchronously (a plain
      // function here, not a Promise) before the optimistic apply, so by
      // the time control returns to us post-call the badge should already
      // reflect the paused state optimistically, ahead of the fetch.
      assert.ok(pauseCell.innerHTML.includes('Paused'), 'pause must optimistically flip the badge before the fetch resolves');
      // Issue #1187: pausing a wallet is a deterministic PAPER override
      // (a paused wallet is always PAPER regardless of the global switch)
      // -- unlike resume, pause CAN flip the live badge correctly and
      // immediately, even though this row started out LIVE.
      assert.ok(pauseCell.innerHTML.includes('PAPER'), 'pause must optimistically flip a previously-LIVE badge to PAPER too');
      assert.ok(!pauseCell.innerHTML.includes('mode-badge-live'), 'pause must not leave a stale LIVE badge showing');
      await pausePending;
      assert.strictEqual(pauseCallCount, 1, 'a real, non-empty reason must call the pause API exactly once');
      assert.strictEqual(listRefetchCallCount, 1, 'a successful pause must trigger exactly one list refetch');
      assert.strictEqual(fetchBody.reason, 'flagged by wallet-health job', 'the trimmed reason must be sent as the request body');
      assert.ok(pauseCell.innerHTML.includes('Paused'), 'pause must still show the Paused badge after success');

      // ------------------------------------------------------------------
      // 3. Unfollow's window.confirm() must contain the required literal
      //    copy -- reviewers check this literally, not "some dialog
      //    exists" (PM task context, issue #1147).
      // ------------------------------------------------------------------
      let confirmText = null;
      window.confirm = (text) => { confirmText = text; return false; }; // operator cancels
      global.fetch = async () => { throw new Error('must not be called when confirm() is cancelled'); };
      await followedUnfollowWallet('0xUnfollowMe', makeBtn());
      assert.ok(confirmText !== null, 'unfollow must call window.confirm()');
      assert.ok(
        confirmText.includes('will NOT be closed'),
        `confirm() text must literally state open positions will NOT be closed, got: ${confirmText}`
      );
      assert.ok(
        confirmText.toLowerCase().includes('open positions'),
        `confirm() text must literally mention open positions, got: ${confirmText}`
      );

      // Confirmed unfollow: row is optimistically hidden, then a failure
      // rolls the row's visibility back.
      window.confirm = () => true;
      let resolveUnfollow;
      global.fetch = () => new Promise((resolve) => { resolveUnfollow = resolve; });
      const ufAddress = '0xUnfollowMe2';
      const row = document.getElementById('followed-row-' + _copySafeId(ufAddress));
      row.style.display = 'table-row';
      const ufPending = followedUnfollowWallet(ufAddress, makeBtn());
      assert.strictEqual(row.style.display, 'none', 'unfollow must optimistically hide the row before the fetch resolves');
      resolveUnfollow(jsonResp({ success: false, message: 'not a followed wallet' }));
      await ufPending;
      assert.strictEqual(row.style.display, 'table-row', 'unfollow must roll the row back to visible on failure');

      // ------------------------------------------------------------------
      // 4. Second, independent live/paper badge (issue #1187): correct
      //    class/text for both states, and the aria-label spells out the
      //    reason verbatim from the backend field -- never re-derived or
      //    guessed client-side, so it can't drift out of sync with it.
      // ------------------------------------------------------------------
      const liveHtml = _followedLiveBadgeHtml({ live_eligible: true, live_status_reason: 'eligible for live execution' });
      assert.ok(liveHtml.includes('mode-badge-live'), 'live-eligible wallet must use the mode-badge-live class');
      assert.ok(liveHtml.includes('>LIVE<'), 'live-eligible wallet badge text must be LIVE');
      assert.ok(
        liveHtml.includes('Live status: live — eligible for live execution'),
        'aria-label must spell out the reason, not just the state'
      );

      const paperHtml = _followedLiveBadgeHtml({ live_eligible: false, live_status_reason: 'live trading is currently off' });
      assert.ok(paperHtml.includes('mode-badge-paper'), 'non-eligible wallet must use the mode-badge-paper class');
      assert.ok(paperHtml.includes('>PAPER<'), 'non-eligible wallet badge text must be PAPER');
      assert.ok(
        paperHtml.includes('Live status: paper only — live trading is currently off'),
        'aria-label must spell out the off reason'
      );

      // ------------------------------------------------------------------
      // 5. renderFollowedWallets() populates the new live-eligible/
      //    paper-only counts, the split paper/live aggregate P&L pills
      //    (never a single combined "aggregate P&L"), and toggles the
      //    table-level "all wallets are paper-only" notice strictly off
      //    the response's own live_trading_enabled flag -- never inferred
      //    from row data client-side.
      // ------------------------------------------------------------------
      const offBanner = document.getElementById('followed-live-off-banner');
      const toggleCalls = [];
      offBanner.classList.toggle = (cls, force) => { toggleCalls.push({ cls, force }); };

      renderFollowedWallets({
        wallets: [], active_count: 0, paused_count: 0, aggregate_pnl_usd: 5, n_settled_total: 2,
        live_eligible_count: 3, paper_only_count: 1, live_aggregate_pnl_usd: -2, live_n_settled_total: 1,
        live_trading_enabled: false,
      });
      assert.strictEqual(document.getElementById('followed-live-eligible-pill').textContent, '3 live-eligible');
      assert.strictEqual(document.getElementById('followed-paper-only-pill').textContent, '1 paper-only');
      assert.ok(
        document.getElementById('followed-pnl-pill').textContent.includes('paper aggregate P&L'),
        'the paper pill must say "paper aggregate P&L", never a bare "aggregate P&L"'
      );
      assert.ok(document.getElementById('followed-live-pnl-pill').textContent.includes('live aggregate P&L'));
      assert.deepStrictEqual(
        toggleCalls[toggleCalls.length - 1], { cls: 'visible', force: true },
        'the off-banner must be shown when live_trading_enabled is false'
      );

      renderFollowedWallets({
        wallets: [], active_count: 0, paused_count: 0, aggregate_pnl_usd: 0, n_settled_total: 0,
        live_eligible_count: 0, paper_only_count: 0, live_aggregate_pnl_usd: 0, live_n_settled_total: 0,
        live_trading_enabled: true,
      });
      assert.deepStrictEqual(
        toggleCalls[toggleCalls.length - 1], { cls: 'visible', force: false },
        'the off-banner must be hidden when live_trading_enabled is true'
      );

      // ------------------------------------------------------------------
      // 6. Followed Wallets gets its own dedicated 30s poll, decoupled
      //    from the four-view 5-minute group and fetched unconditionally
      //    on every tab entry -- the same precedent PR #1192 established
      //    for the global posture banner: this badge is just as
      //    config-driven and trust-relevant, so it must not risk sitting
      //    stale for up to 5 minutes on a tab revisit.
      // ------------------------------------------------------------------
      let followedFetchCount = 0;
      fetchFollowedWallets = async () => { followedFetchCount++; };
      fetchCopyTradingModePosture = async () => {};
      fetchCopyTradingCandidates = async () => {};
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
      followedWalletsIntervalId = null;

      switchTab('copy-trading');
      assert.ok(followedFetchCount >= 1, 'first tab entry must fetch followed wallets immediately');
      const thirtySecCalls = setIntervalCalls.filter(c => c.delay === 30_000);
      assert.strictEqual(
        thirtySecCalls.length, 2,
        'the posture banner and followed wallets must each get their own dedicated 30s interval, not share one'
      );

      const clearedBeforeLeaving = clearIntervalCalls.length;
      switchTab('other-unrelated-tab');
      assert.ok(
        clearIntervalCalls.length >= clearedBeforeLeaving + 3,
        'leaving the tab must clear all three Copy-Trading intervals (view-data, posture, followed wallets)'
      );

      // Re-entry: the exact bug PR #1192 fixed for the posture banner --
      // copyTradingLoaded is already true here, so a naive first-activation
      // gate would silently skip this fetch, leaving the badge stale until
      // the next poll tick.
      followedFetchCount = 0;
      setIntervalCalls.length = 0;
      switchTab('copy-trading');
      assert.strictEqual(followedFetchCount, 1, 'followed wallets must refetch immediately on every tab re-entry, not just the first');
      assert.ok(setIntervalCalls.some(c => c.delay === 30_000), 'a fresh 30s followed-wallets interval must be created on re-entry too');

      console.log('ALL_FOLLOWED_WALLETS_JS_ASSERTIONS_PASSED');
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
def test_followed_wallets_optimistic_toggle_and_confirm_copy(tmp_path):
    """Executes the real shipped dashboard script under Node and exercises
    the Followed Wallets view's optimistic pause/resume rollback and the
    unfollow confirm-dialog copy (issue #1147).
    """
    combined = _PRELUDE + "\n" + _extract_inline_script() + "\n" + _ASSERTIONS
    script_path = tmp_path / "followed_wallets_logic_check.js"
    script_path.write_text(combined, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "ALL_FOLLOWED_WALLETS_JS_ASSERTIONS_PASSED" in result.stdout

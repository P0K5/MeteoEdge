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

Also covers the per-wallet live opt-in control (issues #1254, #1258,
#1259): the four-state live badge (LIVE / LIVE (switch off) /
LIVE (cap reached) / PAPER) and its conservative fallback for unrecognized
reasons; the two-line paper/live stake display; the "Go live"/"Revert to
paper" button's control states (available, opted-in, disabled-while-paused);
the enable-live two-dialog flow (window.prompt for the stake, then
window.confirm) with its three client-side validation states and its
stake-first/enable-last API call sequencing (including the redundant-write
skip and both partial-failure attributions); the deliberately asymmetric
optimistic UI (enabling never shows LIVE optimistically, disabling does);
and the server-computed "# opted into live" summary pill.
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

      const summaryPayload = {
        wallets: [], active_count: 2, paused_count: 1, aggregate_pnl_usd: 5, n_settled_total: 2,
        live_eligible_count: 3, paper_only_count: 1, live_aggregate_pnl_usd: -2, live_n_settled_total: 1,
        live_trading_enabled: false, live_opted_in_count: 4,
      };

      // PAPER render (issue #1275): paper counts + paper aggregate only --
      // never a live figure on the Paper tab.
      renderFollowedWallets(summaryPayload, 'paper');
      assert.strictEqual(document.getElementById('followed-paper-only-pill').textContent, '1 paper-only');
      assert.strictEqual(document.getElementById('followed-active-pill').textContent, '2 active');
      assert.ok(
        document.getElementById('followed-pnl-pill').textContent.includes('paper aggregate P&L'),
        'the paper pill must say "paper aggregate P&L", never a bare "aggregate P&L"'
      );
      assert.strictEqual(document.getElementById('live-followed-live-pnl-pill').textContent, '',
        'rendering the Paper tab must never write a live aggregate');
      assert.strictEqual(document.getElementById('live-followed-live-eligible-pill').textContent, '');
      assert.strictEqual(toggleCalls.length, 0, 'the Paper render never touches the live-off notice');

      // LIVE render: live-eligible count + live aggregate P&L, never a
      // single combined "aggregate P&L", and the table-level "live is
      // off" notice toggles strictly off the response's own
      // live_trading_enabled flag -- never inferred from row data.
      renderFollowedWallets(summaryPayload, 'live');
      assert.strictEqual(document.getElementById('live-followed-live-eligible-pill').textContent, '3 live-eligible');
      assert.strictEqual(document.getElementById('live-followed-opted-in-pill').textContent, '4 opted into live');
      assert.ok(document.getElementById('live-followed-live-pnl-pill').textContent.includes('live aggregate P&L'));
      assert.ok(!document.getElementById('live-followed-live-pnl-pill').textContent.toLowerCase().includes('paper'));
      assert.deepStrictEqual(
        toggleCalls[toggleCalls.length - 1], { cls: 'visible', force: true },
        'the off-banner must be shown when live_trading_enabled is false'
      );

      renderFollowedWallets({ ...summaryPayload, live_trading_enabled: true }, 'live');
      assert.deepStrictEqual(
        toggleCalls[toggleCalls.length - 1], { cls: 'visible', force: false },
        'the off-banner must be hidden when live_trading_enabled is true'
      );

      // ------------------------------------------------------------------
      // 6. (The per-tab 30s poll / refetch-on-entry wiring for the roster
      //    now lives in COPY_TAB_JOBS and is exercised against a faithful
      //    DOM in test_dashboard_copy_tab_controller_behavior.py.)
      // ------------------------------------------------------------------

      // ------------------------------------------------------------------
      // 7. Four-state live badge (issues #1254/#1258): LIVE,
      //    LIVE (switch off), LIVE (cap reached), PAPER -- driven off
      //    live_enabled + the backend's own live_status_reason string,
      //    never a client-side guess.
      // ------------------------------------------------------------------
      const liveEligibleHtml = _followedLiveBadgeHtml({ live_enabled: true, live_eligible: true, live_status_reason: 'eligible for live execution' });
      assert.ok(liveEligibleHtml.includes('mode-badge-live"'), 'eligible wallet must use the plain mode-badge-live class');
      assert.ok(liveEligibleHtml.includes('>LIVE<'));

      const switchOffHtml = _followedLiveBadgeHtml({ live_enabled: true, live_eligible: false, live_status_reason: 'live trading is currently off' });
      assert.ok(switchOffHtml.includes('mode-badge-live-pending'), 'opted-in wallet with the global switch off must use the muted-green pending class');
      assert.ok(switchOffHtml.includes('>LIVE (switch off)<'));
      assert.ok(switchOffHtml.includes('Live status: live, opted in but the global switch is off'));

      const capReachedHtml = _followedLiveBadgeHtml({ live_enabled: true, live_eligible: false, live_status_reason: "this wallet's live exposure limit is currently reached" });
      assert.ok(capReachedHtml.includes('mode-badge-live-pending'), 'opted-in wallet at its cap must use the muted-green pending class too');
      assert.ok(capReachedHtml.includes('>LIVE (cap reached)<'));
      assert.ok(capReachedHtml.includes("Live status: live, opted in but this wallet's live exposure limit is currently reached"));

      const neverOptedInHtml = _followedLiveBadgeHtml({ live_enabled: false, live_eligible: false, live_status_reason: 'live is not enabled for this wallet' });
      assert.ok(neverOptedInHtml.includes('mode-badge-paper'));
      assert.ok(neverOptedInHtml.includes('>PAPER<'));

      // A future/unrecognized reason with live_enabled=true must safely
      // fall through to plain PAPER -- never invent a third muted-green
      // label (design spec's conservative-fallback rule).
      const unknownReasonHtml = _followedLiveBadgeHtml({ live_enabled: true, live_eligible: false, live_status_reason: 'some future reason not yet known to the frontend' });
      assert.ok(unknownReasonHtml.includes('mode-badge-paper'), 'an unrecognized reason must fall through to plain PAPER, never a guessed label');
      assert.ok(unknownReasonHtml.includes('>PAPER<'));

      // ------------------------------------------------------------------
      // 8. Paper / live stake display (issues #1259, #1275): never blended
      //    -- the Paper roster renders only the paper stake, the Live
      //    roster only the resolved live stake.
      // ------------------------------------------------------------------
      const paperStakeHtml = _followedPaperStakeHtml({ stake_per_trade: 5, live_stake_per_trade: 2 });
      assert.ok(paperStakeHtml.includes('Paper: $5.00'));
      assert.ok(!paperStakeHtml.includes('$2.00') && !paperStakeHtml.includes('Live'), 'the Paper stake cell must never show a live figure');

      const overrideStakeHtml = _followedLiveStakeHtml({ stake_per_trade: 5, live_stake_per_trade: 2, live_stake_is_override: true });
      assert.ok(overrideStakeHtml.includes('Live: $2.00'));
      assert.ok(!overrideStakeHtml.includes('$5.00') && !overrideStakeHtml.includes('Paper'), 'the Live stake cell must never show the paper figure');
      assert.ok(!overrideStakeHtml.includes('inherits paper'), 'an explicit override must not show the "(inherits paper)" note');

      const inheritedStakeHtml = _followedLiveStakeHtml({ stake_per_trade: 5, live_stake_per_trade: 5, live_stake_is_override: false });
      assert.ok(inheritedStakeHtml.includes('Live: $5.00'));
      assert.ok(inheritedStakeHtml.includes('(inherits paper)'), 'an inherited (non-override) live stake must be annotated as such');

      // ------------------------------------------------------------------
      // 9. "Go live" / "Revert to paper" control states (issue #1254).
      // ------------------------------------------------------------------
      const goLiveAvailable = _followedGoLiveButtonHtml({ address: '0xGL', status: 'active', live_enabled: false });
      assert.ok(goLiveAvailable.includes('btn-followed-golive'));
      assert.ok(goLiveAvailable.includes('Go live'));
      assert.ok(goLiveAvailable.includes('aria-label="Go live for 0xGL"'));
      assert.ok(!goLiveAvailable.includes('disabled'));

      const revertAvailable = _followedGoLiveButtonHtml({ address: '0xRV', status: 'active', live_enabled: true });
      assert.ok(revertAvailable.includes('btn-followed-revert-live'));
      assert.ok(revertAvailable.includes('Revert to paper'));
      assert.ok(revertAvailable.includes('aria-label="Revert 0xRV to paper-only"'));
      assert.ok(!revertAvailable.includes('disabled'));

      const goLivePaused = _followedGoLiveButtonHtml({ address: '0xP1', status: 'paused', live_enabled: false });
      assert.ok(goLivePaused.includes('btn-followed-golive'), 'a paused wallet not yet opted in still renders the Go-live button shape, just disabled');
      assert.ok(goLivePaused.includes('disabled'));
      assert.ok(goLivePaused.includes('Live opt-in is unavailable while this wallet is paused'));

      const revertPaused = _followedGoLiveButtonHtml({ address: '0xP2', status: 'paused', live_enabled: true });
      assert.ok(revertPaused.includes('btn-followed-revert-live'), 'a paused, already-opted-in wallet renders the Revert button shape, just disabled');
      assert.ok(revertPaused.includes('disabled'));
      assert.ok(revertPaused.includes('Live opt-in is unavailable while this wallet is paused'));

      // ------------------------------------------------------------------
      // 10. followedGoLive: two-dialog flow, validation, API call
      //     sequencing (stake-first, enable-last), and the badge must never
      //     show LIVE optimistically (issues #1254, #1259).
      // ------------------------------------------------------------------
      function setWallet(w) { _followedWalletsData = { wallets: [w], live_cap_usd: 10 }; }
      const errBanner = document.getElementById('copy-paper-error-text');   // scope falls back to paper off-tab

      // 0) Defensive guard (AI review, PR #1268): a missing/non-finite
      //    live_cap_usd must degrade to a clean banner error instead of an
      //    uncaught TypeError from `.toFixed()` on undefined, and must
      //    never even open the stake prompt.
      _followedWalletsData = { wallets: [{ address: '0xNoCap', stake_per_trade: 5, live_stake_per_trade: 5, live_stake_is_override: false, live_enabled: false, status: 'active' }], live_cap_usd: undefined };
      let promptOpenedForNoCap = false;
      window.prompt = () => { promptOpenedForNoCap = true; return null; };
      let noCapFetchCount = 0;
      global.fetch = async () => { noCapFetchCount++; return jsonResp({ success: true }); };
      await followedGoLive('0xNoCap', makeBtn());
      assert.strictEqual(promptOpenedForNoCap, false, 'a missing live_cap_usd must abort before the stake prompt ever opens');
      assert.strictEqual(noCapFetchCount, 0, 'a missing live_cap_usd must not call any API');
      assert.ok(errBanner.textContent.includes('live exposure cap is unavailable'), 'a missing live_cap_usd must show a clean banner error, not throw');

      // a) Cancelling the stake prompt makes no API call at all.
      setWallet({ address: '0xGoLive1', stake_per_trade: 5, live_stake_per_trade: 5, live_stake_is_override: false, live_enabled: false, status: 'active' });
      window.prompt = () => null;
      let flowFetchCount = 0;
      global.fetch = async () => { flowFetchCount++; return jsonResp({ success: true }); };
      await followedGoLive('0xGoLive1', makeBtn());
      assert.strictEqual(flowFetchCount, 0, 'cancelling the stake prompt must not call any API');

      // b) Non-finite stake -> banner error, no API call.
      window.prompt = () => 'abc';
      await followedGoLive('0xGoLive1', makeBtn());
      assert.strictEqual(flowFetchCount, 0, 'a non-finite stake must not call any API');
      assert.ok(errBanner.textContent.includes('enter a live stake greater than $0'), 'non-finite stake must show the finite/positive validation copy');

      // c) Non-positive stake -> banner error, no API call.
      window.prompt = () => '0';
      await followedGoLive('0xGoLive1', makeBtn());
      assert.strictEqual(flowFetchCount, 0, 'a non-positive stake must not call any API');

      // d) Above-cap stake -> banner error, no API call.
      window.prompt = () => '999';
      await followedGoLive('0xGoLive1', makeBtn());
      assert.strictEqual(flowFetchCount, 0, 'a stake above the live cap must not call any API');
      assert.ok(errBanner.textContent.includes("exceeds this wallet's live exposure cap"), 'an above-cap stake must show the cap-specific validation copy');

      // e) Cancelling the final confirm() makes no API call either.
      window.prompt = () => '3';
      window.confirm = () => false;
      await followedGoLive('0xGoLive1', makeBtn());
      assert.strictEqual(flowFetchCount, 0, 'cancelling the final confirmation must not call any API');

      // f) Success path: stake differs from the current override -> PATCH
      //    .../live-stake fires BEFORE POST .../live (stake-first,
      //    enable-last), and the badge never shows LIVE optimistically.
      window.prompt = () => '3';
      window.confirm = () => true;
      const callOrder = [];
      let patchBody = null, postBody = null;
      global.fetch = async (url, opts) => {
        const u = String(url);
        if (u.endsWith('/live-stake')) {
          callOrder.push('live-stake');
          patchBody = JSON.parse(opts.body);
          return jsonResp({ success: true, message: 'ok' });
        }
        if (u.endsWith('/live')) {
          callOrder.push('live');
          postBody = JSON.parse(opts.body);
          return jsonResp({ success: true, message: 'ok' });
        }
        return jsonResp({
          wallets: [], active_count: 0, paused_count: 0, aggregate_pnl_usd: 0, n_settled_total: 0,
          live_eligible_count: 0, paper_only_count: 0, live_aggregate_pnl_usd: 0, live_n_settled_total: 0,
          live_trading_enabled: true, live_cap_usd: 10, live_opted_in_count: 1,
        });
      };
      let refetchCount = 0;
      fetchFollowedWallets = async () => { refetchCount++; };
      const safeIdGL1 = _copySafeId('0xGoLive1');
      const glCell = document.getElementById('followed-status-cell-' + safeIdGL1);
      glCell.innerHTML = _followedStatusBadgeHtml({ status: 'active' })
        + _followedLiveBadgeHtml({ live_enabled: false, live_eligible: false, live_status_reason: 'live is not enabled for this wallet' });
      const glMsgEl = document.getElementById('followed-live-msg-' + safeIdGL1);
      const glBtn = makeBtn();
      // Track loading-class churn across the step1 -> step2 boundary
      // (Designer nitpick, PR #1268): 'loading' must be added once per
      // step (harmless no-op re-add for step 2) but only ever REMOVED once,
      // at the very end -- never removed-then-re-added between the two
      // requests, which would be a visible idle flicker.
      const glLoadingAdds = [];
      const glLoadingRemoves = [];
      glBtn.classList.add = (cls) => { if (cls === 'loading') glLoadingAdds.push(cls); };
      glBtn.classList.remove = (cls) => { if (cls === 'loading') glLoadingRemoves.push(cls); };
      await followedGoLive('0xGoLive1', glBtn);
      assert.deepStrictEqual(callOrder, ['live-stake', 'live'], 'the live-stake PATCH must fire before the live-enable POST');
      assert.strictEqual(patchBody.stake, 3, 'the resolved override stake must be sent to the live-stake endpoint');
      assert.strictEqual(postBody.enabled, true);
      assert.ok(glCell.innerHTML.includes('PAPER'), 'the badge must never optimistically show LIVE while the enable request is in flight');
      assert.ok(!glCell.innerHTML.includes('mode-badge-live"'), 'the badge must not flip to the plain LIVE class before the refetch corrects it');
      assert.strictEqual(refetchCount, 1, 'a successful go-live must trigger exactly one list refetch');
      assert.ok(glMsgEl.textContent.includes('Live trading enabled'), 'the per-row status region must announce success for screen readers');
      assert.strictEqual(glLoadingRemoves.length, 1, 'the loading state must be held across the stake-then-enable boundary, only cleared once at the very end (Designer nitpick)');

      // g) Accepting the pre-filled resolved value as-is is an explicit
      //    override -- the stake PATCH still fires even though the number
      //    is numerically unchanged, per the design spec's "accepting the
      //    default is an explicit override" rule.
      setWallet({ address: '0xGoLive2', stake_per_trade: 5, live_stake_per_trade: 5, live_stake_is_override: false, live_enabled: false, status: 'active' });
      window.prompt = () => '5';
      window.confirm = () => true;
      callOrder.length = 0;
      await followedGoLive('0xGoLive2', makeBtn());
      assert.deepStrictEqual(callOrder, ['live-stake', 'live']);

      // Truly-unchanged case: an existing override, re-entered as itself --
      // the stake PATCH must be skipped entirely, avoiding a redundant
      // write.
      setWallet({ address: '0xGoLive3', stake_per_trade: 5, live_stake_per_trade: 2, live_stake_is_override: true, live_enabled: false, status: 'active' });
      window.prompt = () => '2';
      callOrder.length = 0;
      await followedGoLive('0xGoLive3', makeBtn());
      assert.deepStrictEqual(callOrder, ['live'], 'the stake PATCH must be skipped when nothing actually changed');

      // h) A failing stake PATCH stops the whole flow -- the enable POST is
      //    never called, and the error is attributed to the stake step.
      setWallet({ address: '0xGoLive4', stake_per_trade: 5, live_stake_per_trade: 5, live_stake_is_override: false, live_enabled: false, status: 'active' });
      window.prompt = () => '3';
      callOrder.length = 0;
      global.fetch = async (url) => {
        if (String(url).endsWith('/live-stake')) { callOrder.push('live-stake'); return jsonResp({ success: false, message: 'stake rejected' }); }
        callOrder.push('live'); return jsonResp({ success: true, message: 'ok' });
      };
      await followedGoLive('0xGoLive4', makeBtn());
      assert.deepStrictEqual(callOrder, ['live-stake'], 'a failing stake PATCH must stop the flow before the enable POST is called');
      assert.ok(errBanner.textContent.includes('Could not set a live stake'), 'the error must be attributed to the stake step, not the enable step');

      // i) Stake PATCH succeeds but the enable POST fails -- attributed to
      //    the enable step instead, per the spec's per-step error copy.
      setWallet({ address: '0xGoLive5', stake_per_trade: 5, live_stake_per_trade: 5, live_stake_is_override: false, live_enabled: false, status: 'active' });
      window.prompt = () => '3';
      callOrder.length = 0;
      global.fetch = async (url) => {
        if (String(url).endsWith('/live-stake')) { callOrder.push('live-stake'); return jsonResp({ success: true, message: 'ok' }); }
        callOrder.push('live'); return jsonResp({ success: false, message: 'wallet is paused' });
      };
      await followedGoLive('0xGoLive5', makeBtn());
      assert.deepStrictEqual(callOrder, ['live-stake', 'live']);
      assert.ok(errBanner.textContent.includes('Could not enable live trading'), 'a failing enable POST must be attributed to the enable step, not the stake step');

      // ------------------------------------------------------------------
      // 11. followedRevertToPaper: no confirmation dialog, optimistic flip
      //     to PAPER, rollback to the exact prior badge HTML on failure --
      //     disabling is the risk-reducing direction, so (unlike
      //     followedGoLive above) it follows the same optimistic
      //     convention as Pause/Resume (issue #1254).
      // ------------------------------------------------------------------
      let promptCalled = false, confirmCalled = false;
      window.prompt = () => { promptCalled = true; return null; };
      window.confirm = () => { confirmCalled = true; return true; };

      setWallet({ address: '0xRevert1', stake_per_trade: 5, live_stake_per_trade: 5, live_stake_is_override: false, live_enabled: true, status: 'active' });
      const safeIdRV = _copySafeId('0xRevert1');
      const rvCell = document.getElementById('followed-status-cell-' + safeIdRV);
      rvCell.innerHTML = _followedStatusBadgeHtml({ status: 'active' })
        + _followedLiveBadgeHtml({ live_enabled: true, live_eligible: true, live_status_reason: 'eligible for live execution' });
      const rvPrior = rvCell.innerHTML;
      const rvMsgEl = document.getElementById('followed-live-msg-' + safeIdRV);

      let resolveRevert;
      global.fetch = () => new Promise((resolve) => { resolveRevert = resolve; });
      const rvPending = followedRevertToPaper('0xRevert1', makeBtn());
      assert.ok(rvCell.innerHTML.includes('PAPER'), 'revert-to-paper must optimistically flip the badge before the fetch resolves');
      assert.ok(!rvCell.innerHTML.includes('mode-badge-live"'), 'the optimistic flip must not leave the plain LIVE class showing');
      resolveRevert(jsonResp({ success: false, message: 'not opted in' }));
      await rvPending;
      assert.strictEqual(rvCell.innerHTML, rvPrior, 'revert-to-paper must roll back to the exact prior badge on failure');
      assert.strictEqual(promptCalled, false, 'disabling live must never call window.prompt');
      assert.strictEqual(confirmCalled, false, 'disabling live must never call window.confirm');

      global.fetch = async () => jsonResp({ success: true, message: 'ok' });
      fetchFollowedWallets = async () => {};
      await followedRevertToPaper('0xRevert1', makeBtn());
      assert.ok(rvMsgEl.textContent.includes('reverted to paper'), 'the per-row status region must announce the revert for screen readers');

      // ------------------------------------------------------------------
      // 12. Header pill: # opted into live is server-computed, never a
      //     client-side count (issue #1259). Paper shows it only as a
      //     cross-link by count; the Live tab shows it plain.
      // ------------------------------------------------------------------
      const optedInPayload = {
        wallets: [], active_count: 0, paused_count: 0, aggregate_pnl_usd: 0, n_settled_total: 0,
        live_eligible_count: 0, paper_only_count: 0, live_aggregate_pnl_usd: 0, live_n_settled_total: 0,
        live_trading_enabled: true, live_cap_usd: 10, live_opted_in_count: 4,
      };
      renderFollowedWallets(optedInPayload, 'paper');
      assert.strictEqual(document.getElementById('followed-opted-in-pill').textContent, '4 opted into live → Live tab');
      renderFollowedWallets(optedInPayload, 'live');
      assert.strictEqual(document.getElementById('live-followed-opted-in-pill').textContent, '4 opted into live');

      // ------------------------------------------------------------------
      // 13. Roster split (issue #1275): the SAME payload renders two
      //     rosters that never show the other mode's numbers, with unique
      //     DOM ids per tab.
      // ------------------------------------------------------------------
      const rosterPayload = {
        wallets: [
          { address: '0xLiveOne', stake_per_trade: 5, status: 'active', paused_reason: null,
            added_at: '2026-09-01T00:00:00Z', n_settled: 3, realized_pnl_usd: 12.5,
            live_enabled: true, live_eligible: true, live_status_reason: 'eligible for live execution',
            live_stake_per_trade: 2, live_stake_is_override: true },
          { address: '0xPaperOne', stake_per_trade: 7, status: 'active', paused_reason: null,
            added_at: '2026-09-02T00:00:00Z', n_settled: 1, realized_pnl_usd: -3.25,
            live_enabled: false, live_eligible: false, live_status_reason: 'live is not enabled for this wallet',
            live_stake_per_trade: 7, live_stake_is_override: false },
          { address: '0xPausedOne', stake_per_trade: 9, status: 'paused', paused_reason: 'unstable',
            added_at: '2026-09-03T00:00:00Z', n_settled: 0, realized_pnl_usd: 0,
            live_enabled: false, live_eligible: false, live_status_reason: 'this wallet is paused',
            live_stake_per_trade: 9, live_stake_is_override: false },
        ],
        active_count: 2, paused_count: 1, aggregate_pnl_usd: 9.25, n_settled_total: 4,
        live_eligible_count: 1, paper_only_count: 2, live_aggregate_pnl_usd: 40, live_n_settled_total: 6,
        live_trading_enabled: true, live_cap_usd: 10, live_opted_in_count: 1,
      };
      renderFollowedWallets(rosterPayload, 'paper');
      renderFollowedWallets(rosterPayload, 'live');
      const paperHtmlAll = document.getElementById('copy-followed-list').innerHTML;
      const liveRosterHtml = document.getElementById('copy-live-followed-list').innerHTML;
      const readyHtml = document.getElementById('copy-live-ready-list').innerHTML;

      // Paper roster: every followed wallet, paper stake + paper P&L, paper
      // controls; no live stake / live money action.
      for (const a of ['0xLiveOne', '0xPaperOne', '0xPausedOne']) assert.ok(paperHtmlAll.includes(a), 'paper roster lists every followed wallet: ' + a);
      assert.ok(paperHtmlAll.includes('Paper: $5.00') && paperHtmlAll.includes('Paper: $7.00'));
      assert.ok(!paperHtmlAll.includes('Live: $'), 'the Paper roster must never show a live stake');
      assert.ok(paperHtmlAll.includes('+$12.50') && paperHtmlAll.includes('-$3.25'), 'paper running P&L renders');
      assert.ok(paperHtmlAll.includes('btn-followed-pause') && paperHtmlAll.includes('btn-followed-resume'));
      assert.ok(paperHtmlAll.includes('btn-followed-edit-stake') && paperHtmlAll.includes('btn-followed-unfollow'));
      assert.ok(!paperHtmlAll.includes('btn-followed-golive') && !paperHtmlAll.includes('btn-followed-revert-live'),
        'real-money actions (Go live / Revert) must not be on the Paper tab');
      assert.strictEqual((paperHtmlAll.match(/btn-followed-goto-live/g) || []).length, 1,
        'only the active, not-yet-opted-in wallet gets the "Go live ->" link to the Live tab');
      assert.ok(paperHtmlAll.includes('id="followed-row-' + _copySafeId('0xPaperOne') + '"'));

      // Live roster: opted-in wallets only; live stake; Pause + Revert; no
      // paper stake or paper P&L, no Edit-stake/Unfollow (paper controls).
      assert.ok(liveRosterHtml.includes('0xLiveOne'));
      assert.ok(!liveRosterHtml.includes('0xPaperOne') && !liveRosterHtml.includes('0xPausedOne'), 'only opted-in wallets are on the Live roster');
      assert.ok(liveRosterHtml.includes('Live: $2.00'));
      assert.ok(!liveRosterHtml.includes('Paper: $') && !liveRosterHtml.includes('$5.00') && !liveRosterHtml.includes('12.50'),
        'the Live roster must never show a paper stake or paper P&L');
      assert.ok(liveRosterHtml.includes('btn-followed-pause'), 'Pause is duplicated on the Live tab (PM decision)');
      assert.ok(liveRosterHtml.includes('btn-followed-revert-live'));
      assert.ok(!liveRosterHtml.includes('btn-followed-edit-stake') && !liveRosterHtml.includes('btn-followed-unfollow'));
      assert.ok(liveRosterHtml.includes('id="live-followed-row-' + _copySafeId('0xLiveOne') + '"'));

      // Ready to go live: active, not-yet-opted-in wallets with Go live.
      assert.ok(readyHtml.includes('0xPaperOne') && readyHtml.includes('btn-followed-golive'));
      assert.ok(!readyHtml.includes('0xLiveOne') && !readyHtml.includes('0xPausedOne'));
      assert.ok(!/[$][0-9]/.test(readyHtml), 'the first-cut Ready list renders no figures at all');

      // No DOM id is shared between the two tabs' rosters.
      const idsOf = (html) => [...html.matchAll(/ id="([^"]+)"/g)].map(m => m[1]);
      const paperIds = new Set(idsOf(paperHtmlAll));
      for (const id of [...idsOf(liveRosterHtml), ...idsOf(readyHtml)]) {
        assert.ok(!paperIds.has(id), 'duplicate DOM id across tabs: ' + id);
      }

      // Empty states: Paper points at the Wallets tab; Live prints no number.
      renderFollowedWallets({ ...rosterPayload, wallets: [] }, 'paper');
      assert.ok(document.getElementById('copy-followed-list').innerHTML.includes('Nothing followed yet'));
      renderFollowedWallets({ ...rosterPayload, wallets: [] }, 'live');
      assert.ok(document.getElementById('copy-live-followed-list').innerHTML.includes('No wallets are live'));

      // ------------------------------------------------------------------
      // 14. Row actions on the Live tab address the Live roster's own DOM
      //     ids (so the optimistic status flip lands on the visible row).
      // ------------------------------------------------------------------
      currentTab = 'copy-live';
      const liveCell = document.getElementById('live-followed-status-cell-' + _copySafeId('0xLiveOne'));
      liveCell.innerHTML = _followedStatusBadgeHtml({ status: 'active' })
        + _followedLiveBadgeHtml({ live_enabled: true, live_eligible: true, live_status_reason: 'eligible for live execution' });
      const paperCellUntouched = document.getElementById('followed-status-cell-' + _copySafeId('0xLiveOne'));
      paperCellUntouched.innerHTML = 'PAPER-ROW-UNTOUCHED';
      window.prompt = () => 'emergency stop';
      global.fetch = () => new Promise(() => {});   // stays in flight
      followedPauseWallet('0xLiveOne', makeBtn());
      assert.ok(liveCell.innerHTML.includes('Paused'), 'Pause on the Live tab flips the Live row optimistically');
      assert.strictEqual(paperCellUntouched.innerHTML, 'PAPER-ROW-UNTOUCHED');
      currentTab = 'portfolio';

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

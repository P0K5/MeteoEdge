"""Unit tests for the Edge tab's client-side gate/decision logic (issue #758).

The dashboard has no JS test framework (no package.json / jest / playwright
anywhere in this repo) -- introducing one is out of scope for a single-tab
frontend PR. Instead, this test extracts the *actual* inline <script> block
that ships in src/dashboard/static/index.html and executes it under plain
Node (pre-installed on the GitHub-hosted CI runners, no new dependency),
with a minimal DOM stub so the script can load without a browser. It then
asserts on the pure logic functions that decide what a live trading-status
gate chip says and which row gets visually emphasized -- the two pieces of
this PR the "AI / NVIDIA NIM review" flagged as safety-relevant and
untested.

Every assertion here mirrors a concrete rule from
docs/design/edge-tab-bracket-decisions.md §4/§5, so a regression that changes
gate-chip wording or row-emphasis precedence fails loudly instead of only
being caught by an operator staring at the live dashboard.

It also drives `loadEdgeStationData()` end-to-end (issue #787 / PR #783) to
cover the Today->D+1 auto-advance branch: a stubbed `fetch` keyed off the
`next_day` query param feeds each of the three cases the "AI / NVIDIA NIM
review" flagged as untested, and `renderEdgeDecisionTable`/`showEdgeError`
are reassigned to recording spies (same top-level-function-reassignment
trick as the DOM stubs) so the test can assert on what actually got
rendered without a browser.
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

# Minimal browser-global stubs so the dashboard's inline script can execute
# top-to-bottom under Node without throwing on DOM/localStorage/fetch access.
# Nothing in the extracted script runs a fetch/DOM call at *load* time -- the
# whole file is function/const declarations plus one DOMContentLoaded
# registration at the very bottom -- so these stubs never need to do
# anything beyond "not throw".
_PRELUDE = textwrap.dedent("""
    function makeStubElement() {
      const el = {
        style: {}, classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
        dataset: {}, children: [], childElementCount: 0,
        addEventListener(){}, removeEventListener(){},
        querySelectorAll(){ return []; }, querySelector(){ return null; },
        appendChild(){}, remove(){}, disabled: false, title: '',
      };
      Object.defineProperty(el, 'innerHTML', { get(){ return this._innerHTML || ''; }, set(v){ this._innerHTML = v; } });
      Object.defineProperty(el, 'textContent', { get(){ return this._textContent || ''; }, set(v){ this._textContent = v; } });
      return el;
    }
    global.document = {
      getElementById() { return makeStubElement(); },
      querySelectorAll() { return []; },
      querySelector() { return null; },
      createElement() { return makeStubElement(); },
      documentElement: { getAttribute(){ return 'dark'; }, setAttribute(){} },
      addEventListener() {},
    };
    global.window = { addEventListener() {} };
    global.localStorage = { getItem() { return null; }, setItem() {} };
    global.fetch = async () => ({ ok: true, json: async () => ({}) });
    global.lucide = { createIcons() {} };
    global.Chart = function () {};
    global.getComputedStyle = () => ({ getPropertyValue: () => '' });
    global.event = { target: makeStubElement() };
""")

# The 11 canonical gate-verdict enums, locked by the design spec and by
# src/strategy/scanner.py's GATE_VERDICTS frozenset -- kept in sync manually
# on both sides (see docs/design/edge-tab-bracket-decisions.md §5 open
# question 3, "no renaming requested").
EXPECTED_VERDICTS = [
    "traded_live", "entry_guard", "timeout_today", "shadow_only", "next_day_shadow",
    "below_min_edge", "above_max_edge", "below_min_price", "below_min_confidence",
    "margin_gate", "mae_gate",
]

_ASSERTIONS = textwrap.dedent("""
    const assert = require('assert');

    // GATE_CHIP_META must cover exactly the 11 locked verdicts, each with a label.
    const EXPECTED_VERDICTS = %(verdicts)r;
    for (const v of EXPECTED_VERDICTS) {
      assert.ok(GATE_CHIP_META[v], `GATE_CHIP_META missing verdict: ${v}`);
      assert.ok(GATE_CHIP_META[v].label, `GATE_CHIP_META[${v}] missing label`);
    }
    assert.strictEqual(Object.keys(GATE_CHIP_META).length, EXPECTED_VERDICTS.length);

    // gateTooltip: numeric-threshold verdicts format actual/threshold by gate_unit
    // exactly the way the scanner itself gated on the value (design spec §5 table).
    assert.strictEqual(
      gateTooltip({ gate_verdict: 'below_min_edge', gate_actual: 12.4, gate_threshold: 15, gate_unit: 'cents' }),
      'edge 12.4¢ < min 15.0¢'
    );
    assert.strictEqual(
      gateTooltip({ gate_verdict: 'mae_gate', gate_actual: 9.2, gate_threshold: 8.0, gate_unit: 'degrees_f' }),
      'rolling MAE 9.2°F > 8.0°F — recent accuracy too poor for a live NO entry'
    );
    assert.strictEqual(
      gateTooltip({ gate_verdict: 'below_min_confidence', gate_actual: 0.42, gate_threshold: 0.05, gate_unit: 'probability' }),
      'model p_yes 42%% < min confidence 5%% required for YES'
    );
    // Deliberately doesn't say "live" -- scan_decisions can't distinguish a
    // confirmed live fill from a paper-mode placeholder (Backlog #780).
    assert.strictEqual(
      gateTooltip({ gate_verdict: 'traded_live', side: 'YES' }),
      'Traded — cleared every gate on the YES side.'
    );
    assert.strictEqual(
      gateTooltip({ gate_verdict: 'entry_guard', gate_detail: 'duplicate-entry guard' }),
      'duplicate-entry guard'
    );

    // gateChipHTML: correct family class + label text present.
    const chip = gateChipHTML({ gate_verdict: 'traded_live', side: 'YES' });
    assert.ok(chip.includes('gate-traded_live'), 'chip missing gate-traded_live class');
    assert.ok(chip.includes('Traded'), 'chip missing label text');

    // Row-emphasis precedence (design spec §4): row-traded beats everything else.
    let emphasis = computeRowEmphasis([
      { gate_verdict: 'below_min_edge', ev_yes: -1, ev_no: -1 },
      { gate_verdict: 'traded_live', side: 'YES', ev_yes: 5, ev_no: -2 },
      { gate_verdict: 'entry_guard', side: 'NO', ev_yes: -1, ev_no: 8 },
    ]);
    assert.deepStrictEqual(emphasis, [null, 'row-traded', null]);

    // row-near-miss (entry_guard/timeout_today, own flagged side) beats row-best-signal
    // when there is no traded_live row.
    emphasis = computeRowEmphasis([
      { gate_verdict: 'below_min_edge', ev_yes: 20, ev_no: -1 },
      { gate_verdict: 'entry_guard', side: 'NO', ev_yes: -1, ev_no: 8 },
      { gate_verdict: 'timeout_today', side: 'YES', ev_yes: 3, ev_no: -1 },
    ]);
    assert.deepStrictEqual(emphasis, [null, 'row-near-miss', null]);

    // row-best-signal only applies when nothing traded/near-missed, and only when
    // the best EV is a real (positive) signal.
    emphasis = computeRowEmphasis([
      { gate_verdict: 'below_min_edge', ev_yes: -5, ev_no: -3 },
      { gate_verdict: 'below_min_confidence', ev_yes: 6, ev_no: -1 },
    ]);
    assert.deepStrictEqual(emphasis, [null, 'row-best-signal']);

    // Never manufacture a highlight from an all-negative table.
    emphasis = computeRowEmphasis([
      { gate_verdict: 'below_min_edge', ev_yes: -5, ev_no: -3 },
      { gate_verdict: 'below_min_edge', ev_yes: -1, ev_no: -8 },
    ]);
    assert.deepStrictEqual(emphasis, [null, null]);

    // isThinSignal: both sides near zero => thin; either side with real edge => not thin.
    assert.strictEqual(isThinSignal({ ev_yes: 0.2, ev_no: -0.3 }), true);
    assert.strictEqual(isThinSignal({ ev_yes: 5.0, ev_no: -0.3 }), false);
    assert.strictEqual(isThinSignal({ ev_yes: null, ev_no: null }), true);

    // fmtEv: sign + near-zero threshold (+/-0.5c).
    assert.deepStrictEqual(fmtEv(9.8), { text: '+9.8¢', cls: 'positive' });
    assert.deepStrictEqual(fmtEv(-4.0), { text: '−4.0¢', cls: 'negative' });
    assert.deepStrictEqual(fmtEv(0.2), { text: '+0.2¢', cls: 'near-zero' });
    assert.deepStrictEqual(fmtEv(null), { text: '—', cls: 'near-zero' });

    // fmtPollFreshness: stale at > 2x poll interval, fresh well within it.
    const freshResult = fmtPollFreshness(new Date().toISOString(), 60);
    assert.strictEqual(freshResult.isStale, false);
    const staleTs = new Date(Date.now() - 10 * 60 * 1000).toISOString();
    const staleResult = fmtPollFreshness(staleTs, 60);
    assert.strictEqual(staleResult.isStale, true);

    // edgeTempFmt: F passthrough, F->C conversion, null-safe.
    assert.strictEqual(edgeTempFmt(70, 'F'), '70.0°F');
    assert.strictEqual(edgeTempFmt(32, 'C'), '0.0°C');
    assert.strictEqual(edgeTempFmt(null, 'F'), '—');

    // bracketLabel: open-ended sentinel handling mirrors the backend's
    // _format_bracket_range (src/dashboard/api.py).
    assert.strictEqual(bracketLabel({ range: '68–70°F', bracket_low: 68, bracket_high: 70 }, 'F'), '68–70°F');
    assert.strictEqual(bracketLabel({ bracket_low: -50, bracket_high: 40 }, 'C'), '≤4°C');
    assert.strictEqual(bracketLabel({ bracket_low: 100, bracket_high: 200 }, 'C'), '≥38°C');
""") % {"verdicts": EXPECTED_VERDICTS}

# loadEdgeStationData() auto-advance-to-D+1 tests (issue #787 / PR #783). These
# drive the real function end-to-end with a stubbed fetch keyed off the
# `next_day` query param, then assert on the module-scope globals and
# render-function call args the function drives -- same "spy by reassigning
# the top-level function declaration" trick used for DOM stubs above, since
# nothing in this file has a mocking library available.
_D1_AUTOADVANCE_ASSERTIONS = textwrap.dedent("""
    function jsonResp(body) { return { ok: true, json: async () => body }; }
    const failedResp = { ok: false };

    let fetchResponses = { today: null, d1: null };
    let fetchCallCount = 0;
    global.fetch = async (url) => {
      fetchCallCount += 1;
      const isNextDay = /next_day=true/.test(url);
      return isNextDay ? fetchResponses.d1 : fetchResponses.today;
    };

    let renderTableCalls = [];
    renderEdgeDecisionTable = (...args) => { renderTableCalls.push(args); };
    let errorCalls = 0;
    showEdgeError = () => { errorCalls += 1; };

    const TODAY_EMPTY = jsonResp({ brackets: [] });
    const D1_WITH_DATA = jsonResp({ brackets: [{ range: '68-70', bracket_low: 68, bracket_high: 70 }] });
    const TODAY_WITH_DATA = jsonResp({ brackets: [{ range: '60-62', bracket_low: 60, bracket_high: 62 }] });

    (async () => {
      // Case 1: Today empty, D+1 has data -- auto-advance fires, reuses the
      // already-fetched d1Data local (no extra request beyond the initial
      // parallel pair), and does not surface the error banner.
      fetchResponses = { today: TODAY_EMPTY, d1: D1_WITH_DATA };
      fetchCallCount = 0;
      renderTableCalls = [];
      errorCalls = 0;
      await loadEdgeStationData('KTEST');
      assert.strictEqual(edgeIsNextDay, true, 'case 1: edgeIsNextDay should be true');
      assert.strictEqual(fetchCallCount, 2, 'case 1: only the initial parallel today+d1 fetch, no extra request');
      assert.strictEqual(renderTableCalls.length, 1, 'case 1: table rendered exactly once');
      assert.strictEqual(renderTableCalls[0][3], true, 'case 1: rendered with isNextDay=true');
      assert.strictEqual(renderTableCalls[0][0].length, 1, 'case 1: rendered the D+1 bracket');
      assert.strictEqual(errorCalls, 0, 'case 1: no error banner');

      // Case 2: Today has data -- auto-advance does NOT fire, behaves as before.
      fetchResponses = { today: TODAY_WITH_DATA, d1: D1_WITH_DATA };
      fetchCallCount = 0;
      renderTableCalls = [];
      errorCalls = 0;
      await loadEdgeStationData('KTEST');
      assert.strictEqual(edgeIsNextDay, false, 'case 2: edgeIsNextDay should stay false');
      assert.strictEqual(renderTableCalls.length, 1, 'case 2: table rendered exactly once');
      assert.strictEqual(renderTableCalls[0][3], false, 'case 2: rendered with isNextDay=false');
      assert.strictEqual(renderTableCalls[0][0].length, 1, 'case 2: rendered the Today bracket');
      assert.strictEqual(errorCalls, 0, 'case 2: no error banner');

      // Case 3: both empty and Today's fetch failed -- auto-advance does NOT
      // fire (no D+1 data to advance to either) and the existing error-banner
      // path still runs.
      fetchResponses = { today: failedResp, d1: jsonResp({ brackets: [] }) };
      fetchCallCount = 0;
      renderTableCalls = [];
      errorCalls = 0;
      await loadEdgeStationData('KTEST');
      assert.strictEqual(edgeIsNextDay, false, 'case 3: edgeIsNextDay should stay false');
      assert.strictEqual(renderTableCalls.length, 1, 'case 3: table rendered exactly once');
      assert.strictEqual(renderTableCalls[0][3], false, 'case 3: rendered with isNextDay=false');
      assert.strictEqual(renderTableCalls[0][0], null, 'case 3: rendered null brackets (fetch failed)');
      assert.strictEqual(errorCalls, 1, 'case 3: error banner shown');

      console.log('ALL_EDGE_TAB_JS_ASSERTIONS_PASSED');
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
def test_edge_tab_gate_and_emphasis_logic(tmp_path):
    """Executes the real shipped dashboard script under Node and exercises
    the gate-chip / row-emphasis / formatting logic backing the Edge tab
    (issue #758). Fails if index.html's script can't parse/load, or if any
    of the design-spec-derived behaviors above regress.
    """
    combined = (
        _PRELUDE
        + "\n"
        + _extract_inline_script()
        + "\n"
        + _ASSERTIONS
        + "\n"
        + _D1_AUTOADVANCE_ASSERTIONS
    )
    script_path = tmp_path / "edge_tab_logic_check.js"
    script_path.write_text(combined, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "ALL_EDGE_TAB_JS_ASSERTIONS_PASSED" in result.stdout

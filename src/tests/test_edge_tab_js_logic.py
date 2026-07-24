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

And, per the Designer's change request on PR #783: the persistent in-content
D+1 indicator (auto-advanced and manually-toggled cases) and the day-aware
"Best signal" caption fix, asserted against the actual rendered DOM text --
not the date-toggle button's `.active` class -- since the toggle tint alone
was judged insufficient signal for trading UI and a test that only checked
toggle state would pass even with the in-content indicator missing entirely.
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
    // getElementById returns the *same* stub instance for a given id on every
    // call (a real DOM would too) -- needed so tests can render, then read
    // back what a later getElementById(sameId) call sees, e.g. the D+1
    // indicator's visibility/text after loadEdgeStationData()/selectEdgeDate().
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

    // Issue #819: shadow-routed verdicts (shadow_only, next_day_shadow) must
    // have disambiguating prefixes to distinguish from other shadow-related
    // concepts (EMOS calibration mode, D+1 policy indicator, etc.). "Route:"
    // prefix connects to the family name "Shadow-routed by policy" from the
    // design spec (docs/design/edge-tab-bracket-decisions.md §5).
    assert.strictEqual(
      GATE_CHIP_META.shadow_only.label, 'Route: shadow',
      'shadow_only must have disambiguating "Route:" prefix'
    );
    assert.strictEqual(
      GATE_CHIP_META.next_day_shadow.label, 'Route: next-day',
      'next_day_shadow must have disambiguating "Route:" prefix'
    );
    // Both should be in the same family
    assert.strictEqual(
      GATE_CHIP_META.shadow_only.family, 'Shadow-routed by policy',
      'shadow_only should be in Shadow-routed family'
    );
    assert.strictEqual(
      GATE_CHIP_META.next_day_shadow.family, 'Shadow-routed by policy',
      'next_day_shadow should be in Shadow-routed family'
    );
    // Verify the prefixes start with 'Route:' for disambiguation
    assert.ok(
      GATE_CHIP_META.shadow_only.label.startsWith('Route:'),
      'shadow_only label must start with "Route:" prefix'
    );
    assert.ok(
      GATE_CHIP_META.next_day_shadow.label.startsWith('Route:'),
      'next_day_shadow label must start with "Route:" prefix'
    );

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
    // execution_mode (#780) distinguishes a confirmed live fill from the
    // scanner's paper-mode placeholder; a missing/unrecognized value reads
    // as 'paper', the conservative default.
    assert.strictEqual(
      gateTooltip({ gate_verdict: 'traded_live', side: 'YES', execution_mode: 'live' }),
      'Traded live — cleared every gate on the YES side and the order filled.'
    );
    assert.strictEqual(
      gateTooltip({ gate_verdict: 'traded_live', side: 'YES', execution_mode: 'paper' }),
      'Traded (paper) — cleared every gate on the YES side; no live trader was configured this poll, so no order was placed.'
    );
    assert.strictEqual(
      gateTooltip({ gate_verdict: 'traded_live', side: 'YES' }),
      'Traded (paper) — cleared every gate on the YES side; no live trader was configured this poll, so no order was placed.'
    );
    assert.strictEqual(
      gateTooltip({ gate_verdict: 'entry_guard', gate_detail: 'duplicate-entry guard' }),
      'duplicate-entry guard'
    );

    // gateChipHTML: correct family class + execution_mode-qualified label text.
    const liveChip = gateChipHTML({ gate_verdict: 'traded_live', side: 'YES', execution_mode: 'live' });
    assert.ok(liveChip.includes('gate-traded_live'), 'chip missing gate-traded_live class');
    assert.ok(liveChip.includes('Traded live'), 'live chip missing "Traded live" label');
    const paperChip = gateChipHTML({ gate_verdict: 'traded_live', side: 'YES', execution_mode: 'paper' });
    assert.ok(paperChip.includes('Traded (paper)'), 'paper chip missing "Traded (paper)" label');

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

# loadEdgeStationData() auto-advance-to-D+1 tests (issue #787 / PR #783), plus
# the Designer-requested persistent in-content D+1 indicator and the day-aware
# "Best signal" caption fix that shipped alongside it. These drive the real
# functions end-to-end with a stubbed fetch keyed off the `next_day` query
# param, then assert on the module-scope globals, the D+1 indicator's actual
# DOM text/visibility, and render-function call args -- same
# "spy by reassigning the top-level function declaration" trick used for DOM
# stubs above, since nothing in this file has a mocking library available.
#
# Per explicit re-review criterion: the indicator/caption assertions below
# read the DOM element's own text content (via the id-cached getElementById
# stub in _PRELUDE), not the date-toggle button's .active class -- asserting
# only the toggle state would pass even if the in-content indicator were
# missing entirely, which is exactly the failure mode this change exists to
# prevent.
_D1_AUTOADVANCE_ASSERTIONS = textwrap.dedent("""
    function jsonResp(body) { return { ok: true, json: async () => body }; }
    const failedResp = { ok: false };

    function d1IndicatorState() {
      const el = document.getElementById('edge-d1-indicator');
      const textEl = document.getElementById('edge-d1-indicator-text');
      return { visible: el.style.display !== 'none', text: textEl.textContent };
    }
    const D1_INDICATOR_TEXT = \"Today's markets closed \\u2014 showing D+1 (shadow-only)\";

    let fetchResponses = { today: null, d1: null };
    let fetchCallCount = 0;
    global.fetch = async (url) => {
      fetchCallCount += 1;
      const isNextDay = /next_day=true/.test(url);
      return isNextDay ? fetchResponses.d1 : fetchResponses.today;
    };

    const TODAY_EMPTY = jsonResp({ brackets: [] });
    const D1_WITH_DATA = jsonResp({ brackets: [{ range: '68-70', bracket_low: 68, bracket_high: 70 }] });
    const TODAY_WITH_DATA = jsonResp({ brackets: [{ range: '60-62', bracket_low: 60, bracket_high: 62 }] });

    // --- Day-aware "Best signal" caption (line ~3092) -- tested against the
    // *real* renderEdgeDecisionTable, before it gets reassigned to a spy
    // below, by reading the actually-rendered table HTML back off the DOM.
    const bestSignalBrackets = [
      { gate_verdict: 'below_min_edge', ev_yes: -5, ev_no: -3, range: '60-62', bracket_low: 60, bracket_high: 62 },
      { gate_verdict: 'below_min_confidence', ev_yes: 6, ev_no: -1, range: '68-70', bracket_low: 68, bracket_high: 70 },
    ];
    renderEdgeDecisionTable(bestSignalBrackets, 'F', 'KTEST', false);
    let tableHTML = document.getElementById('edge-table-body').innerHTML;
    assert.ok(tableHTML.includes('Best signal today'), 'caption should read \"Best signal today\" when isNextDay=false');
    assert.ok(!tableHTML.includes('Best signal D+1'), 'caption should not say D+1 when isNextDay=false');

    renderEdgeDecisionTable(bestSignalBrackets, 'F', 'KTEST', true);
    tableHTML = document.getElementById('edge-table-body').innerHTML;
    assert.ok(tableHTML.includes('Best signal D+1'), 'caption should read \"Best signal D+1\" when isNextDay=true');
    assert.ok(!tableHTML.includes('Best signal today'), 'caption should not say \"today\" when isNextDay=true');

    // --- loadEdgeStationData() auto-advance branch -- renderEdgeDecisionTable
    // is now reassigned to a recording spy so these cases can assert on what
    // it was called with, independent of its own (already tested above)
    // rendering logic.
    let renderTableCalls = [];
    renderEdgeDecisionTable = (...args) => { renderTableCalls.push(args); };
    let errorCalls = 0;
    showEdgeError = () => { errorCalls += 1; };

    (async () => {
      // Case 1: Today empty, D+1 has data -- auto-advance fires, reuses the
      // already-fetched d1Data local (no extra request beyond the initial
      // parallel pair), does not surface the error banner, and shows the
      // persistent D+1 indicator with the exact expected text.
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
      let indicator = d1IndicatorState();
      assert.strictEqual(indicator.visible, true, 'case 1: D+1 indicator should be visible');
      assert.strictEqual(indicator.text, D1_INDICATOR_TEXT, 'case 1: D+1 indicator text');

      // Case 2: Today has data -- auto-advance does NOT fire, behaves as
      // before, and the D+1 indicator stays hidden with no leftover text.
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
      indicator = d1IndicatorState();
      assert.strictEqual(indicator.visible, false, 'case 2: D+1 indicator should be hidden');
      assert.strictEqual(indicator.text, '', 'case 2: D+1 indicator text should be cleared');

      // Case 3: both empty and Today's fetch failed -- auto-advance does NOT
      // fire (no D+1 data to advance to either), the existing error-banner
      // path still runs, and the D+1 indicator stays hidden.
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
      indicator = d1IndicatorState();
      assert.strictEqual(indicator.visible, false, 'case 3: D+1 indicator should be hidden');
      assert.strictEqual(indicator.text, '', 'case 3: D+1 indicator text should be cleared');

      // Case 4: manual toggle via selectEdgeDate() -- the indicator must show
      // on a deliberate D+1 click too, not just on auto-advance, and must
      // clear again when the operator clicks back to Today.
      edgeSelectedStation = 'KTEST';
      edgeD1Cache = { station: null, data: null };  // force a fresh D+1 fetch, not a stale cache hit
      document.getElementById('edge-date-d1').disabled = false;
      fetchResponses = { today: TODAY_WITH_DATA, d1: D1_WITH_DATA };
      await selectEdgeDate('d1');
      assert.strictEqual(edgeIsNextDay, true, 'case 4: edgeIsNextDay should be true after manual D+1 toggle');
      indicator = d1IndicatorState();
      assert.strictEqual(indicator.visible, true, 'case 4: D+1 indicator should be visible on manual toggle');
      assert.strictEqual(indicator.text, D1_INDICATOR_TEXT, 'case 4: D+1 indicator text on manual toggle');

      await selectEdgeDate('today');
      assert.strictEqual(edgeIsNextDay, false, 'case 4: edgeIsNextDay should be false after toggling back to Today');
      indicator = d1IndicatorState();
      assert.strictEqual(indicator.visible, false, 'case 4: D+1 indicator should hide after toggling back to Today');
      assert.strictEqual(indicator.text, '', 'case 4: D+1 indicator text should be cleared after toggling back to Today');

      // Case 5: regression guard (Tech Lead PM review) -- loadEdgeStationData()
      // must reset the D+1 indicator *synchronously*, before the fetch
      // round-trip, not only in the post-fetch fallthrough. Otherwise, on a
      // station switch away from a D+1-showing station, the previous
      // station's "showing D+1" banner lingers on screen through the new
      // station's skeleton-loading state. A JS async function runs
      // synchronously up to its first `await`, so checking indicator state
      // right after calling (without awaiting) the function catches this
      // deterministically.
      renderEdgeD1Indicator(true);  // simulate leftover state from a prior station
      indicator = d1IndicatorState();
      assert.strictEqual(indicator.visible, true, 'case 5 setup: indicator should start visible');
      fetchResponses = { today: TODAY_WITH_DATA, d1: D1_WITH_DATA };
      const pending = loadEdgeStationData('KTEST');  // not awaited yet
      indicator = d1IndicatorState();
      assert.strictEqual(indicator.visible, false, 'case 5: indicator must reset before the fetch resolves, not after');
      assert.strictEqual(indicator.text, '', 'case 5: indicator text must clear synchronously too');
      await pending;

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

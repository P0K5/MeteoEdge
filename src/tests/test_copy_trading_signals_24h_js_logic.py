"""JS-logic tests for the per-wallet "Signals (24h)" column (issue #1389).

Same technique as test_copy_trading_positions_live_paper_js_logic.py: runs the
real inline <script> of index.html under Node with DOM stubs.
"""
from __future__ import annotations

import subprocess
import textwrap

import pytest

from src.tests.test_copy_trading_positions_live_paper_js_logic import (
    NODE, _PRELUDE, _extract_inline_script,
)

_ASSERTIONS = textwrap.dedent("""
    const assert = require('assert');
    const reason = (o) => Object.assign({ reason: 'k', meaning: 'm', class: 'routine',
      class_label: 'Routine', count: 1, trend: 'STEADY', accepted_ref: null }, o);
    const wallet = (o) => Object.assign({ address: '0xAAA', n_settled: 1, realized_pnl_usd: 1.0,
      projected_flat_dollar_pnl: null, divergence_usd: null, divergence_pct: null,
      signals_24h: { placed: 12, skipped: 5, reasons: [reason({ count: 5 })] } }, o);

    // 1. placed/skipped from payload; invented reason + meaning rendered verbatim (no JS vocabulary).
    let html = _copyPerWalletTableHtml([wallet({ signals_24h: { placed: 12, skipped: 5, reasons: [
      reason({ reason: 'invented_key_zz', meaning: 'Totally invented meaning', class: 'structural',
               class_label: 'Structural', count: 5, accepted_ref: '#1390' })] } })]);
    assert.ok(html.includes('12 placed · '), html);
    assert.ok(html.includes('5 skipped'));
    assert.ok(html.includes('Signals (24h)'));
    assert.ok(html.includes('Totally invented meaning') && html.includes('invented_key_zz'));
    assert.ok(html.includes('skip-class-structural') && html.includes('Accepted (#1390)'));
    assert.ok(html.includes('Why skipped?'));

    // 2. null signals_24h -> Unavailable, never zeros.
    html = _copyPerWalletTableHtml([wallet({ signals_24h: null })]);
    assert.ok(html.includes('Unavailable') && html.includes('could not be loaded'));
    assert.ok(!html.includes('0 placed') && !html.includes('Why skipped?'));

    // 3. No signals / zero skipped: no button.
    html = _copyPerWalletTableHtml([wallet({ signals_24h: { placed: 0, skipped: 0, reasons: [] } })]);
    assert.ok(html.includes('No signals in 24h') && !html.includes('Why skipped?'));
    html = _copyPerWalletTableHtml([wallet({ signals_24h: { placed: 8, skipped: 0, reasons: [] } })]);
    assert.ok(html.includes('8 placed · ') && html.includes('0 skipped') && !html.includes('Why skipped?'));

    // 4. Unexpected class -> unknown styling; SPIKE marker shown, STEADY not.
    html = _copyPerWalletTableHtml([wallet({ signals_24h: { placed: 1, skipped: 2, reasons: [
      reason({ class: 'brand_new_class', class_label: 'Whatever', trend: 'SPIKE', count: 2 })] } })]);
    assert.ok(html.includes('skip-class-unknown') && html.includes('copy-skip-trend') && html.includes('>SPIKE<'));
    assert.ok(!_copyPerWalletTableHtml([wallet()]).includes('copy-skip-trend'));

    // 5. Escaping.
    html = _copyPerWalletTableHtml([wallet({ signals_24h: { placed: 1, skipped: 1, reasons: [
      reason({ meaning: '<img src=x onerror=1>', reason: '<b>x</b>', class_label: '<i>L</i>', accepted_ref: '<u>' })] } })]);
    assert.ok(!html.includes('<img src=x') && !html.includes('<b>x</b>') && !html.includes('<i>L</i>') && !html.includes('<u>'));

    // 6. colspan follows the backtest toggle.
    _copyBacktestOn = false;
    assert.ok(_copyPerWalletTableHtml([wallet()]).includes('colspan="4"'));
    _copyBacktestOn = true;
    assert.ok(_copyPerWalletTableHtml([wallet()]).includes('colspan="6"'));
    _copyBacktestOn = false;

    // 7. >6 reasons: first 6 + "+N more", revealed in place.
    const many = Array.from({ length: 8 }, (_, i) => reason({ reason: 'r' + i, count: 8 - i }));
    const manyW = wallet({ signals_24h: { placed: 0, skipped: 36, reasons: many } });
    html = _copyPerWalletTableHtml([manyW]);
    assert.strictEqual((html.match(/copy-skip-item/g) || []).length, 6);
    assert.ok(html.includes('+2 more reasons'));
    _copySignalsMoreWallets.add('0xAAA');
    html = _copyPerWalletTableHtml([manyW]);
    assert.strictEqual((html.match(/copy-skip-item/g) || []).length, 8);
    assert.ok(!html.includes('more reasons'));
    _copySignalsMoreWallets.clear();

    // 8. Open state keyed by address survives a re-render (poll) and toggles via the handler.
    _copyPositionsData = { per_wallet: [wallet()] };
    const slot = document.getElementById('copy-positions-wallet-slot');
    html = _copyPerWalletTableHtml(_copyPositionsData.per_wallet);
    assert.ok(/copy-signals-detail"[^>]* hidden/.test(html), 'collapsed by default');
    _copyToggleSignalsDetail('0xAAA');
    assert.ok(_copySignalsOpenWallets.has('0xAAA'));
    assert.ok(!/copy-signals-detail"[^>]* hidden/.test(slot.innerHTML), 'open after toggle');
    assert.ok(slot.innerHTML.includes('aria-expanded="true"') && slot.innerHTML.includes('Hide reasons'));
    // simulated poll with identical data
    _copyRenderPerWalletSlot([wallet()]);
    assert.ok(!/copy-signals-detail"[^>]* hidden/.test(slot.innerHTML), 'poll must not collapse');
    _copySignalsOnKeydown({ key: 'Escape', target: { closest: () => ({ dataset: { address: '0xAAA' } }) } });
    assert.ok(!_copySignalsOpenWallets.has('0xAAA'), 'Esc collapses');

    console.log('SIGNALS_24H_JS_ASSERTIONS_PASSED');
""")


@pytest.mark.skipif(NODE is None, reason="node is not on PATH in this environment")
def test_per_wallet_signals_24h_js_logic(tmp_path):
    script = tmp_path / "signals_24h_check.js"
    script.write_text(_PRELUDE + "\n" + _extract_inline_script() + "\n" + _ASSERTIONS, encoding="utf-8")
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "SIGNALS_24H_JS_ASSERTIONS_PASSED" in result.stdout

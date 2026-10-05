"""Behavior tests for copyAddressToClipboard (issue #1273).

Extracts the real JS function (and _copyTruncateAddress) from index.html and runs
it under Node against a small but honest DOM stub: real parent/child semantics,
recursive querySelector, real event bubbling with stopPropagation, focus tracking
via document.activeElement, a lucide stub that REPLACES <i> with a NEW <svg>, and
a controllable fake-timer queue.
"""
from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = REPO_ROOT / "src" / "dashboard" / "static" / "index.html"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(not NODE, reason="requires node.js")


def _extract_function(source: str, signature: str) -> str:
    """Return the full source of the function starting at `signature` using
    balanced-brace matching (skips strings, template literals and comments)."""
    start = source.index(signature)
    i = source.index("{", start)
    depth = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if ch in "'\"`":
            quote = ch
            i += 1
            while source[i] != quote:
                if source[i] == "\\":
                    i += 1
                i += 1
        elif source.startswith("//", i):
            i = source.index("\n", i)
            continue
        elif source.startswith("/*", i):
            i = source.index("*/", i) + 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces for {signature}")


def _functions() -> str:
    html = INDEX_HTML.read_text()
    return (
        _extract_function(html, "function _copyTruncateAddress(")
        + "\n"
        + _extract_function(html, "async function copyAddressToClipboard(")
    )


_PRELUDE = textwrap.dedent("""
    const assert = require('assert');
    const order = [];

    class Element {
      constructor(tag) {
        this.tagName = tag;            // 'i' / 'svg' / 'BUTTON' ...
        this.className = '';
        this.style = {};
        this._attrs = {};
        this._listeners = {};
        this.parentNode = null;
        this.children = [];
        this.textContent = '';
        this.innerHTML = '';
        this.value = '';
      }
      get parentElement() { return this.parentNode; }
      get isConnected() {
        let n = this;
        while (n.parentNode) n = n.parentNode;
        return n === body;
      }
      get nextSibling() {
        const p = this.parentNode;
        if (!p) return null;
        return p.children[p.children.indexOf(this) + 1] || null;
      }
      setAttribute(k, v) { this._attrs[k] = String(v); }
      getAttribute(k) { return k in this._attrs ? this._attrs[k] : null; }
      appendChild(c) {
        if (c.parentNode) c.parentNode.removeChild(c);
        c.parentNode = this; this.children.push(c);
        if (c.id) document._ids[c.id] = c;
        return c;
      }
      insertBefore(c, ref) {
        if (c.parentNode) c.parentNode.removeChild(c);
        c.parentNode = this;
        const i = ref ? this.children.indexOf(ref) : -1;
        if (i >= 0) this.children.splice(i, 0, c); else this.children.push(c);
        return c;
      }
      removeChild(c) {
        const i = this.children.indexOf(c);
        if (i >= 0) { this.children.splice(i, 1); c.parentNode = null; }
        return c;
      }
      _matches(sel) {
        return sel.split(',').map(s => s.trim()).some(s => {
          if (s.startsWith('.')) return this.className.split(/\\s+/).includes(s.slice(1));
          return this.tagName.toLowerCase() === s.toLowerCase();
        });
      }
      querySelector(sel) {            // depth-first, descends into dynamic nodes
        for (const c of this.children) {
          if (c._matches(sel)) return c;
          const f = c.querySelector(sel);
          if (f) return f;
        }
        return null;
      }
      addEventListener(t, h) { (this._listeners[t] = this._listeners[t] || []).push(h); }
      removeEventListener(t, h) {
        const l = this._listeners[t] || [];
        const i = l.indexOf(h);
        if (i >= 0) l.splice(i, 1);
      }
      // Real semantics: all listeners on a node run; stopPropagation only
      // prevents bubbling to ancestors.
      dispatch(type, props = {}) {
        const evt = Object.assign({ type, target: this, _stopped: false,
          stopPropagation() { this._stopped = true; } }, props);
        let node = this;
        while (node && !evt._stopped) {
          for (const h of [...(node._listeners[type] || [])]) h(evt);
          node = node.parentNode;
        }
        return evt;
      }
      focus() {
        const prev = document.activeElement;
        order.push(this.tagName + ':focus');
        document.activeElement = this;
        if (prev && prev !== this) prev.dispatch('blur');
        this.dispatch('focus');
      }
      select() { order.push(this.tagName + ':select'); }
    }

    const body = new Element('BODY');
    global.document = {
      body,
      activeElement: body,
      _ids: {},
      getElementById(id) { return this._ids[id] || null; },
      createElement(t) {
        const e = new Element(t);
        if (document._noFocus && t === 'input') e.focus = () => {};
        return e;
      },
      execCommand: () => false,
    };
    global.window = { isSecureContext: true };
    Object.defineProperty(global, 'navigator', { value: { clipboard: { writeText: async () => {} } }, writable: true, configurable: true });
    console.warn = () => {};

    // lucide: replace every <i data-lucide> with a NEW <svg> node
    global.lucide = {
      createIcons() {
        (function walk(node) {
          node.children.slice().forEach((c) => {
            if (c.tagName === 'i' && c.getAttribute('data-lucide')) {
              const svg = new Element('svg');
              svg.setAttribute('data-lucide-name', c.getAttribute('data-lucide'));
              svg.className = c.className;
              node.insertBefore(svg, c);
              node.removeChild(c);
            } else walk(c);
          });
        })(body);
      },
    };

    // Controllable fake timers
    let _now = 0, _tid = 0;
    const _timers = new Map();
    global.setTimeout = (fn, d) => { const id = ++_tid; _timers.set(id, { fn, due: _now + d }); return id; };
    global.clearTimeout = (id) => { _timers.delete(id); };
    function tick(ms) {
      const end = _now + ms;
      for (;;) {
        const due = [..._timers.entries()].filter(([, t]) => t.due <= end).sort((a, b) => a[1].due - b[1].due)[0];
        if (!due) break;
        _timers.delete(due[0]);
        _now = Math.max(_now, due[1].due);
        due[1].fn();
      }
      _now = end;
    }
    const pending = () => _timers.size;

    // Row with button(<i>) + feedback label + address span (like the real templates)
    function makeRow(address) {
      const row = new Element('DIV');
      body.appendChild(row);
      const btn = new Element('BUTTON'); row.appendChild(btn);
      const i = new Element('i'); i.setAttribute('data-lucide', 'copy'); btn.appendChild(i);
      const label = new Element('SPAN'); label.className = 'copy-feedback-label'; row.appendChild(label);
      const span = new Element('SPAN'); span.className = 'copy-address-mono';
      span.setAttribute('data-address', address.replace(/&/g, '&amp;'));   // escaped, like the template
      span.textContent = _copyTruncateAddress(address); row.appendChild(span);
      return { row, btn, label, span };
    }
    const iconName = (btn) => {
      const n = btn.querySelector('svg, i');
      return n.tagName === 'svg' ? n.getAttribute('data-lucide-name') : n.getAttribute('data-lucide');
    };
    const ADDR = '0x123456789abcdef0123456789abcdef012345678';
""")


def run_js(body: str) -> None:
    code = (
        _PRELUDE
        + "\n"
        + _functions()
        + "\n(async () => {\n"
        + textwrap.dedent(body)
        + "\n})().then(() => console.log('OK'), e => { console.error(e); process.exit(1); });"
    )
    r = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0 and "OK" in r.stdout, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"


def test_clipboard_api_success_and_reset_after_1500ms():
    run_js("""
        let written = null;
        navigator.clipboard.writeText = async (t) => { written = t; };
        const { btn, label } = makeRow(ADDR);
        await copyAddressToClipboard(ADDR, btn);
        assert.strictEqual(written, ADDR);
        assert.strictEqual(iconName(btn), 'check');
        assert.strictEqual(label.textContent, 'Copied');
        assert.ok(label.className.includes('success') && label.className.includes('visible'));
        tick(1499);
        assert.strictEqual(iconName(btn), 'check', 'not reset before 1500ms');
        tick(1);
        assert.strictEqual(iconName(btn), 'copy', 'icon reset after 1500ms');
        assert.strictEqual(label.textContent, '');
        assert.strictEqual(label.className, 'copy-feedback-label');
    """)


def test_execcommand_fallback_when_clipboard_unavailable():
    run_js("""
        window.isSecureContext = false;
        navigator.clipboard = undefined;
        let cmd = null;
        document.execCommand = (c) => { cmd = c; return true; };
        const { btn, label } = makeRow(ADDR);
        await copyAddressToClipboard(ADDR, btn);
        assert.strictEqual(cmd, 'copy');
        assert.strictEqual(label.textContent, 'Copied');
        assert.strictEqual(iconName(btn), 'check');
    """)


def test_second_click_after_lucide_swap_gives_feedback_and_resets():
    """After createIcons() replaces <i> with a NEW <svg>, the next click must
    still show feedback and the timer must still reset the icon."""
    run_js("""
        const { btn, label } = makeRow(ADDR);
        await copyAddressToClipboard(ADDR, btn);
        const firstSvg = btn.querySelector('svg, i');
        assert.strictEqual(firstSvg.tagName, 'svg');
        tick(1500);
        assert.strictEqual(iconName(btn), 'copy');
        const secondSvg = btn.querySelector('svg, i');
        assert.notStrictEqual(secondSvg, firstSvg, 'icon node replaced again');
        await copyAddressToClipboard(ADDR, btn);
        assert.strictEqual(iconName(btn), 'check', 'second click shows feedback');
        assert.strictEqual(label.textContent, 'Copied');
        tick(1500);
        assert.strictEqual(iconName(btn), 'copy', 'second click resets too');
        assert.strictEqual(label.textContent, '');
    """)


def test_rapid_clicks_clear_first_timer_single_reset():
    run_js("""
        const { btn } = makeRow(ADDR);
        await copyAddressToClipboard(ADDR, btn);
        tick(0);   // flush the screen-reader announce timer (#1281)
        assert.strictEqual(pending(), 1);
        tick(1000);
        await copyAddressToClipboard(ADDR, btn);
        tick(0);
        assert.strictEqual(pending(), 1, 'first timer cleared; exactly one pending');
        tick(600);   // 1600ms after first click: the first timer would have fired
        assert.strictEqual(iconName(btn), 'check', 'first timer must not reset early');
        tick(900);
        assert.strictEqual(iconName(btn), 'copy');
        assert.strictEqual(pending(), 0);
    """)


def test_detached_button_does_not_throw():
    run_js("""
        const { row, btn, label } = makeRow(ADDR);
        body.removeChild(row);
        assert.ok(!btn.isConnected);
        await copyAddressToClipboard(ADDR, btn);
        assert.strictEqual(label.textContent, '');
        window.isSecureContext = false;
        await copyAddressToClipboard(ADDR, btn);   // failure path, detached
        assert.strictEqual(pending(), 0);
        // Detached while a reset timer is pending
        const r2 = makeRow(ADDR);
        window.isSecureContext = true;
        await copyAddressToClipboard(ADDR, r2.btn);
        body.removeChild(r2.row);
        tick(1500);
    """)


def test_failure_shows_visible_label_input_hint_and_focus_before_select():
    run_js("""
        window.isSecureContext = false;
        const { row, btn, label, span } = makeRow(ADDR);
        await copyAddressToClipboard(ADDR, btn);
        tick(0);
        assert.ok(label.innerHTML.includes('Copy failed — select manually'), label.innerHTML);
        assert.ok(label.className.includes('error') && label.className.includes('visible'));
        assert.strictEqual(document.getElementById('copy-sr-status').textContent, 'Could not copy address to clipboard');
        assert.strictEqual(iconName(btn), 'x');
        assert.strictEqual(span.style.display, 'none');
        const input = row.querySelector('input');
        assert.ok(input && input.value === ADDR && input.readOnly);
        const hint = row.querySelector('.copy-address-hint');
        assert.ok(hint && hint.textContent === 'Press Ctrl/Cmd+C');
        const kids = row.children;
        assert.strictEqual(kids[kids.indexOf(span) + 1], input);
        assert.strictEqual(hint.parentNode, label, 'hint stacks inside the failure label overlay');
        const f = order.indexOf('input:focus'), s = order.indexOf('input:select');
        assert.ok(f >= 0 && s > f, 'focus() before select(): ' + order);
        assert.strictEqual(document.activeElement, input);
    """)


def test_failure_after_clipboard_rejection_and_execcommand_failure():
    run_js("""
        navigator.clipboard.writeText = async () => { throw new Error('denied'); };
        document.execCommand = () => false;
        const { row, btn } = makeRow(ADDR);
        await copyAddressToClipboard(ADDR, btn);
        assert.ok(row.querySelector('input'));
        assert.strictEqual(iconName(btn), 'x');
    """)


def test_input_events_do_not_bubble_to_row():
    run_js("""
        window.isSecureContext = false;
        const { row, btn } = makeRow(ADDR);
        const seen = [];
        ['click', 'mousedown', 'keydown'].forEach(t => row.addEventListener(t, () => seen.push(t)));
        await copyAddressToClipboard(ADDR, btn);
        const input = row.querySelector('input');
        input.dispatch('click'); input.dispatch('mousedown'); input.dispatch('keydown', { key: 'ArrowRight' });
        assert.deepStrictEqual(seen, [], 'row listener must not see input events');
        btn.dispatch('click');   // sanity: the stub really bubbles
        assert.deepStrictEqual(seen, ['click']);
    """)


@pytest.mark.parametrize("how", ["escape", "blur", "timeout"])
def test_dismiss_restores_truncated_text_display_and_removes_hint(how):
    run_js("""
        window.isSecureContext = false;
        const how = '%s';
        if (how === 'timeout') document._noFocus = true;   // unfocused input -> 4s timer runs
        const addr = '0xAB&CD56789abcdef0123456789abcdef01234567';   // '&' is escaped in data-address
        const { row, btn, label, span } = makeRow(addr);
        span.style.display = 'inline-block';
        span.textContent = 'STALE';
        await copyAddressToClipboard(addr, btn);
        const input = row.querySelector('input');
        const hint = row.querySelector('.copy-address-hint');
        assert.strictEqual(span.style.display, 'none');
        tick(0);   // flush the screen-reader announce timer (#1281)
        if (how === 'escape') input.dispatch('keydown', { key: 'Escape' });
        else if (how === 'blur') input.dispatch('blur');
        else tick(4000);
        assert.ok(!input.isConnected && !hint.isConnected, 'input and hint removed together');
        assert.strictEqual(span.style.display, 'inline-block', 'original display restored');
        assert.strictEqual(span.textContent, '0xAB&C…' + addr.slice(-4), 'raw address truncated 6...4');
        assert.strictEqual(label.textContent, '');
        assert.strictEqual(iconName(btn), 'copy', 'error feedback cleared');
        assert.strictEqual(pending(), 0);
    """ % how)


def test_failure_timer_paused_while_input_focused_then_blur_dismisses():
    run_js("""
        window.isSecureContext = false;
        const { row, btn } = makeRow(ADDR);
        await copyAddressToClipboard(ADDR, btn);
        const input = row.querySelector('input');
        assert.strictEqual(document.activeElement, input);
        tick(10000);
        assert.ok(input.isConnected, 'no auto-dismiss while the input has focus');
        input.dispatch('blur');
        assert.ok(!input.isConnected, 'blur dismisses');
        assert.strictEqual(pending(), 0);
    """)


def test_failure_auto_dismisses_after_exactly_4000ms_when_not_focused():
    run_js("""
        window.isSecureContext = false;
        document._noFocus = true;
        const { row, btn } = makeRow(ADDR);
        await copyAddressToClipboard(ADDR, btn);
        const input = row.querySelector('input');
        tick(3999);
        assert.ok(input.isConnected);
        tick(1);
        assert.ok(!input.isConnected, 'dismissed after 4000ms');
        assert.strictEqual(pending(), 0);
    """)


def test_fallback_input_sized_to_hidden_span_before_hiding():
    run_js("""
        window.isSecureContext = false;
        const { row, btn, span } = makeRow(ADDR);
        span.offsetWidth = 123; span.offsetHeight = 18;
        await copyAddressToClipboard(ADDR, btn);
        const input = row.querySelector('input');
        assert.strictEqual(input.style.width, '123px');
        assert.strictEqual(input.style.height, '18px');
    """)


def test_repeat_copy_reannounces_by_clearing_then_setting_on_next_tick():
    run_js("""
        const { btn } = makeRow(ADDR);
        const sr = () => document.getElementById('copy-sr-status').textContent;
        await copyAddressToClipboard(ADDR, btn);
        assert.strictEqual(sr(), '', 'cleared synchronously, set on the next tick');
        tick(0);
        assert.strictEqual(sr(), 'Address copied to clipboard');
        // Same text again: must pass through '' so the live region re-announces.
        const seen = [];
        const node = document.getElementById('copy-sr-status');
        let v = node.textContent;
        Object.defineProperty(node, 'textContent', { get: () => v, set: (x) => { seen.push(x); v = x; } });
        await copyAddressToClipboard(ADDR, btn);
        tick(0);
        assert.deepStrictEqual(seen, ['', 'Address copied to clipboard']);
        assert.strictEqual(pending(), 1, 'only the 1500ms reset timer remains');
    """)


def test_rapid_repeat_copy_announces_once():
    run_js("""
        const { btn } = makeRow(ADDR);
        await copyAddressToClipboard(ADDR, btn);
        await copyAddressToClipboard(ADDR, btn);
        assert.strictEqual(pending(), 2, 'one announce + one reset timer');
        tick(0);
        assert.strictEqual(document.getElementById('copy-sr-status').textContent, 'Address copied to clipboard');
    """)


def test_icon_takes_success_and_error_state_and_resets():
    run_js("""
        const { btn } = makeRow(ADDR);
        const state = () => btn.getAttribute('data-copy-state');
        await copyAddressToClipboard(ADDR, btn);
        assert.strictEqual(state(), 'success');
        tick(1500);
        assert.strictEqual(state(), '');
        window.isSecureContext = false;
        document._noFocus = true;
        await copyAddressToClipboard(ADDR, btn);
        assert.strictEqual(state(), 'error');
        tick(4000);
        assert.strictEqual(state(), '', 'dismiss resets the icon colour state');
    """)


def test_failure_while_input_already_showing_restores_span_and_keeps_one_input():
    """A second failure used to capture display:'none' as the 'original', leaving
    the address permanently hidden after dismissal."""
    run_js("""
        window.isSecureContext = false;
        document._noFocus = true;
        const { row, btn, span } = makeRow(ADDR);
        span.style.display = 'inline-block';
        await copyAddressToClipboard(ADDR, btn);
        assert.strictEqual(span.style.display, 'none');
        span.textContent = '';                      // nothing cached can rescue it
        await copyAddressToClipboard(ADDR, btn);    // fails again with input showing
        assert.strictEqual(row.children.filter(c => c.tagName === 'input').length, 1, 'one input, not two');
        tick(4000);
        assert.ok(!row.querySelector('input') && !row.querySelector('.copy-address-hint'));
        assert.strictEqual(span.style.display, 'inline-block');
        assert.strictEqual(span.textContent, '0x1234…5678', 'truncated text re-derived from the address');
        assert.strictEqual(pending(), 0);
    """)


def test_hint_is_visible_text_in_overlay_not_only_a_title():
    run_js("""
        window.isSecureContext = false;
        const { row, btn, label } = makeRow(ADDR);
        await copyAddressToClipboard(ADDR, btn);
        const hint = row.querySelector('.copy-address-hint');
        assert.strictEqual(hint.textContent, 'Press Ctrl/Cmd+C');
        assert.strictEqual(hint.parentNode, label);
        assert.ok(label.className.includes('visible'));
    """)


def test_css_icon_colour_and_mobile_rules_exist():
    html = INDEX_HTML.read_text()
    assert ".copy-copy-btn[data-copy-state=success]" in html and "color:var(--yes)" in html
    assert ".copy-copy-btn[data-copy-state=error]" in html
    mobile = html[html.index("@media(max-width:600px){\n  .copy-feedback-label"):]
    mobile = mobile[:mobile.index("\n}")]
    assert "white-space:normal" in mobile and "max-width:min(240px" in mobile
    assert ".copy-address-fallback-input{min-width:0;max-width:100%;}" in mobile

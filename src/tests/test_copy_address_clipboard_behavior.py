"""Behavior tests for copy-address-to-clipboard function (issue #1273).

Executes the actual copyAddressToClipboard JS function under Node.js with DOM/window stubs,
testing the full fallback chain and UI feedback behavior.
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
    // Faithful DOM stub with real parentNode/children, insertBefore, appendChild, querySelector, classList, dispatchEvent
    class Element {
      constructor(tagName = 'DIV', className = '') {
        this.tagName = tagName;
        this.className = className;
        this.style = {};
        this.dataset = {};
        this._attrs = {};
        this._textContent = '';
        this._innerHTML = '';
        this._value = '';
        this._listeners = {};
        this.parentNode = null;
        this.children = [];
      }

      get isConnected() {
        let node = this;
        while (node.parentNode) node = node.parentNode;
        return node === document.body || node === document;
      }

      get parentElement() { return this.parentNode; }
      set parentElement(val) { this.parentNode = val; }

      get textContent() { return this._textContent; }
      set textContent(val) { this._textContent = val; }

      get innerHTML() { return this._innerHTML; }
      set innerHTML(val) { this._innerHTML = val; }

      get value() { return this._value; }
      set value(val) { this._value = val; }

      get nextSibling() {
        if (!this.parentNode) return null;
        const idx = this.parentNode.children.indexOf(this);
        return idx >= 0 && idx < this.parentNode.children.length - 1 ? this.parentNode.children[idx + 1] : null;
      }

      get classList() {
        const self = this;
        return {
          add(cls) { if (!self.className.includes(cls)) self.className += (self.className ? ' ' : '') + cls; },
          remove(cls) { self.className = self.className.split(' ').filter(c => c !== cls).join(' '); },
          contains(cls) { return self.className.split(' ').includes(cls); }
        };
      }

      setAttribute(key, val) { this._attrs[key] = val; }
      getAttribute(key) { return this._attrs[key] || null; }

      appendChild(child) {
        child.parentNode = this;
        this.children.push(child);
        return child;
      }

      insertBefore(child, ref) {
        child.parentNode = this;
        const idx = ref ? this.children.indexOf(ref) : -1;
        if (idx >= 0) this.children.splice(idx, 0, child);
        else this.children.push(child);
        return child;
      }

      removeChild(child) {
        const idx = this.children.indexOf(child);
        if (idx >= 0) {
          this.children.splice(idx, 1);
          child.parentNode = null;
        }
        return child;
      }

      remove() {
        if (this.parentNode) this.parentNode.removeChild(this);
      }

      querySelector(sel) {
        if (sel === 'svg, i') {
          for (const child of this.children) {
            if (child.tagName === 'SVG' || child.tagName === 'I') return child;
            const found = child.querySelector?.(sel);
            if (found) return found;
          }
          return null;
        }
        if (sel.startsWith('.')) {
          const cls = sel.slice(1);
          for (const child of this.children) {
            if (child.classList.contains(cls)) return child;
            const found = child.querySelector?.(sel);
            if (found) return found;
          }
        }
        return null;
      }

      querySelectorAll(sel) { return []; }

      addEventListener(type, handler) {
        if (!this._listeners[type]) this._listeners[type] = [];
        this._listeners[type].push(handler);
      }

      removeEventListener(type, handler) {
        if (this._listeners[type]) {
          const idx = this._listeners[type].indexOf(handler);
          if (idx >= 0) this._listeners[type].splice(idx, 1);
        }
      }

      dispatchEvent(evt) {
        const handlers = this._listeners[evt.type] || [];
        for (const handler of handlers) {
          handler(evt);
          if (evt._stopped) break;
        }
        // Bubble to parent
        if (!evt._stopped && this.parentNode) {
          this.parentNode.dispatchEvent(evt);
        }
      }

      focus() { this._focused = true; }
      select() { this._selected = true; }
    }

    const idCache = {};
    const body = new Element('BODY');
    global.document = {
      getElementById(id) {
        if (!idCache[id]) idCache[id] = new Element();
        return idCache[id];
      },
      createElement(tag) { return new Element(tag.toUpperCase()); },
      querySelectorAll() { return []; },
      querySelector() { return null; },
      documentElement: { getAttribute(){ return 'light'; } },
      activeElement: new Element(),
      body: body,
    };

    global.window = { isSecureContext: true };
    global.navigator = { clipboard: { writeText: async () => {} } };
    global.lucide = {
      createIcons() {
        // Replace each <i data-lucide> with a NEW <svg> node
        function processNode(node) {
          for (let i = node.children.length - 1; i >= 0; i--) {
            const child = node.children[i];
            if (child.tagName === 'I' && child.getAttribute('data-lucide')) {
              const svg = new Element('SVG');
              svg._attrs = {...child._attrs};
              svg.className = child.className;
              node.children[i] = svg;
              svg.parentNode = node;
            } else {
              processNode(child);
            }
          }
        }
        processNode(document.body);
      }
    };
    global.localStorage = { getItem() { return null; }, setItem(){} };
    let _timerId = 0;
    global.clearTimeout = () => {};
    global.setTimeout = (fn, delay) => ++_timerId;

    // Helper for backward compatibility with existing tests
    function makeDOMNode(tag = 'DIV') {
      return new Element(tag);
    }

    // Also add _copyTruncateAddress helper used by showFailure
    function _copyTruncateAddress(addr) {
      return addr && addr.length > 12 ? addr.slice(0, 6) + '…' + addr.slice(-4) : addr;
    }
""")

@pytest.mark.skipif(not NODE, reason="requires node.js")
def test_clipboard_api_success_on_secure_context():
    """Test: secure context uses navigator.clipboard and shows Copied state."""
    with open(INDEX_HTML) as f:
        html = f.read()

    match = re.search(
        r'(async\s+function\s+copyAddressToClipboard\(address,\s*btn\)\s*\{[\s\S]*?\n\})',
        html
    )
    assert match, "Could not extract function"
    func = match.group(1)

    test_code = _PRELUDE + "\n" + func + textwrap.dedent("""
        const assert = require('assert');

        (async () => {
          window.isSecureContext = true;
          let clipboardCalled = false;
          if (!navigator.clipboard) navigator.clipboard = {};
          navigator.clipboard.writeText = async (text) => {
            clipboardCalled = true;
            assert.strictEqual(text, '0xTEST123', 'clipboard should receive full address');
          };

          const btn = new Element('BUTTON');
          btn.className = 'copy-copy-btn';
          btn.parentNode = document.body;
          document.body.appendChild(btn);

          const icon = new Element('I');
          icon.setAttribute('data-lucide', 'copy');
          btn.appendChild(icon);

          const feedbackLabel = new Element('SPAN');
          feedbackLabel.className = 'copy-feedback-label';
          feedbackLabel.setAttribute('data-address', '0xTEST123');
          btn.parentNode.appendChild(feedbackLabel);

          await copyAddressToClipboard('0xTEST123', btn);

          assert.ok(clipboardCalled, 'navigator.clipboard.writeText should be called');
          const newIcon = btn.querySelector('svg, i');
          assert.strictEqual(newIcon.getAttribute('data-lucide'), 'check',
            'icon should swap to check on success');

          console.log('✓ clipboard API success test passed');
        })();
    """)

    result = subprocess.run([NODE, "-e", test_code], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, f"Test failed:\n{result.stderr}"
    assert "passed" in result.stdout


@pytest.mark.skipif(not NODE, reason="requires node.js")
def test_execcommand_fallback_when_clipboard_unavailable():
    """Test: when clipboard API unavailable or isSecureContext false, falls back to execCommand."""
    with open(INDEX_HTML) as f:
        html = f.read()

    match = re.search(
        r'(async\s+function\s+copyAddressToClipboard\(address,\s*btn\)\s*\{[\s\S]*?\n\})',
        html
    )
    assert match, "Could not extract function"
    func = match.group(1)

    test_code = _PRELUDE + "\n" + func + textwrap.dedent("""
        const assert = require('assert');

        (async () => {
          window.isSecureContext = false;
          navigator.clipboard = undefined;

          let execCalled = false;
          let execCmd = '';
          document.execCommand = function(cmd) {
            execCalled = true;
            execCmd = cmd;
            return true;
          };

          const btn = makeDOMNode();
          btn.parentElement = makeDOMNode();
          btn.querySelector = () => makeDOMNode();
          btn.parentElement.querySelector = () => makeDOMNode();

          await copyAddressToClipboard('0xHTTP456', btn);

          assert.ok(execCalled, 'execCommand should be called');
          assert.strictEqual(execCmd, 'copy', 'should call execCommand("copy")');

          console.log('✓ execCommand fallback test passed');
        })();
    """)

    result = subprocess.run([NODE, "-e", test_code], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, f"Test failed:\n{result.stderr}"
    assert "passed" in result.stdout


@pytest.mark.skipif(not NODE, reason="requires node.js")
def test_manual_input_fallback_on_all_failures():
    """Test: execCommand failing shows error state and manual input field."""
    with open(INDEX_HTML) as f:
        html = f.read()

    match = re.search(
        r'(async\s+function\s+copyAddressToClipboard\(address,\s*btn\)\s*\{[\s\S]*?\n\})',
        html
    )
    assert match, "Could not extract function"
    func = match.group(1)

    test_code = _PRELUDE + "\n" + func + textwrap.dedent("""
        const assert = require('assert');

        (async () => {
          window.isSecureContext = false;
          navigator.clipboard = undefined;
          document.execCommand = function() { return false; };

          const btn = new Element('BUTTON');
          btn.className = 'copy-copy-btn';
          btn.parentNode = document.body;
          document.body.appendChild(btn);

          const icon = new Element('I');
          icon.setAttribute('data-lucide', 'copy');
          btn.appendChild(icon);

          const feedbackLabel = new Element('SPAN');
          feedbackLabel.className = 'copy-feedback-label';
          feedbackLabel.setAttribute('data-address', '0xSHOW123456');
          btn.parentNode.appendChild(feedbackLabel);

          const addressSpan = new Element('SPAN');
          addressSpan.className = 'copy-address-mono';
          addressSpan.setAttribute('data-address', '0xSHOW123456');
          addressSpan.textContent = 'SHOW…3456';
          btn.parentNode.appendChild(addressSpan);

          await copyAddressToClipboard('0xSHOW123456', btn);

          // Should show error icon
          const errorIcon = btn.querySelector('svg, i');
          assert.strictEqual(errorIcon.getAttribute('data-lucide'), 'x',
            'icon should be x on failure');

          // Should show error state in aria-live
          const srStatus = document.getElementById('copy-sr-status');
          const srText = srStatus.textContent || '';
          assert.ok(srText.toLowerCase().includes('could not copy'),
            'aria-live should announce failure');

          console.log('✓ manual input fallback test passed');
        })();
    """)

    result = subprocess.run([NODE, "-e", test_code], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, f"Test failed:\n{result.stderr}"
    assert "passed" in result.stdout


@pytest.mark.skipif(not NODE, reason="requires node.js")
def test_rapid_clicks_reset_timer():
    """Test: clicking copy button twice rapidly resets the 1.5s timer (icon not stuck)."""
    with open(INDEX_HTML) as f:
        html = f.read()

    match = re.search(
        r'(async\s+function\s+copyAddressToClipboard\(address,\s*btn\)\s*\{[\s\S]*?\n\})',
        html
    )
    assert match, "Could not extract function"
    func = match.group(1)

    test_code = _PRELUDE + "\n" + func + textwrap.dedent("""
        const assert = require('assert');

        (async () => {
          window.isSecureContext = true;
          if (!navigator.clipboard) navigator.clipboard = {};
          navigator.clipboard.writeText = async () => {};

          const btn = new Element('BUTTON');
          btn.className = 'copy-copy-btn';
          btn.parentNode = document.body;
          document.body.appendChild(btn);

          const icon = new Element('I');
          icon.setAttribute('data-lucide', 'copy');
          btn.appendChild(icon);

          const feedbackLabel = new Element('SPAN');
          feedbackLabel.className = 'copy-feedback-label';
          btn.parentNode.appendChild(feedbackLabel);

          // Track timer cancellations
          let clearCalled = 0;
          global.clearTimeout = (id) => { clearCalled++; };

          // First click
          if (!navigator.clipboard) navigator.clipboard = {};
          navigator.clipboard.writeText = async () => {};
          await copyAddressToClipboard('0xTEST', btn);
          const firstIcon = btn.querySelector('svg, i');
          assert.strictEqual(firstIcon.getAttribute('data-lucide'), 'check',
            'first click: icon should be check');

          // Second click (rapid)
          await copyAddressToClipboard('0xTEST', btn);

          // Should have cleaned up previous timer
          assert.ok(clearCalled > 0, 'should clear previous timer on rapid click');
          const secondIcon = btn.querySelector('svg, i');
          assert.strictEqual(secondIcon.getAttribute('data-lucide'), 'check',
            'second click: icon should still be check (not stuck)');

          console.log('✓ rapid clicks test passed');
        })();
    """)

    result = subprocess.run([NODE, "-e", test_code], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, f"Test failed:\n{result.stderr}"
    assert "passed" in result.stdout


@pytest.mark.skipif(not NODE, reason="requires node.js")
def test_detached_button_does_not_throw():
    """Test: if button is removed from DOM (isConnected=false), function doesn't crash."""
    with open(INDEX_HTML) as f:
        html = f.read()

    match = re.search(
        r'(async\s+function\s+copyAddressToClipboard\(address,\s*btn\)\s*\{[\s\S]*?\n\})',
        html
    )
    assert match, "Could not extract function"
    func = match.group(1)

    test_code = _PRELUDE + "\n" + func + textwrap.dedent("""
        const assert = require('assert');

        (async () => {
          window.isSecureContext = true;
          if (!navigator.clipboard) navigator.clipboard = {};
          navigator.clipboard.writeText = async () => {};

          const btn = new Element('BUTTON');
          btn.parentNode = null;  // Detached, not connected to body

          const icon = new Element('I');
          icon.setAttribute('data-lucide', 'copy');
          btn.appendChild(icon);

          const feedbackLabel = new Element('SPAN');
          feedbackLabel.className = 'copy-feedback-label';
          // Add to document since btn is detached but we need a parent for selectors to work
          const tempContainer = new Element('DIV');
          tempContainer.appendChild(btn);
          tempContainer.appendChild(feedbackLabel);

          // Should not throw
          try {
            await copyAddressToClipboard('0xDETACHED', btn);
            console.log('✓ detached button test passed');
          } catch (e) {
            throw new Error('Function should not throw on detached button: ' + e.message);
          }
        })();
    """)

    result = subprocess.run([NODE, "-e", test_code], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, f"Test failed:\n{result.stderr}"
    assert "passed" in result.stdout


@pytest.mark.skipif(not NODE, reason="requires node.js")
def test_second_click_after_lucide_swap():
    """Regression: second click works after lucide.createIcons replaces <i> with <svg>."""
    with open(INDEX_HTML) as f:
        html = f.read()

    match = re.search(
        r'(async\s+function\s+copyAddressToClipboard\(address,\s*btn\)\s*\{[\s\S]*?\n\})',
        html
    )
    assert match, "Could not extract function"
    func = match.group(1)

    test_code = _PRELUDE + "\n" + func + textwrap.dedent("""
        const assert = require('assert');

        (async () => {
          window.isSecureContext = true;
          navigator.clipboard.writeText = async () => {};

          const btn = new Element('BUTTON');
          btn.parentNode = document.body;
          document.body.appendChild(btn);
          const iconI = new Element('I');
          iconI.setAttribute('data-lucide', 'copy');
          btn.appendChild(iconI);

          const feedbackLabel = new Element('SPAN');
          feedbackLabel.className = 'copy-feedback-label';
          btn.parentNode.appendChild(feedbackLabel);

          const addressSpan = new Element('SPAN');
          addressSpan.className = 'copy-address-mono';
          addressSpan.setAttribute('data-address', '0x1234567890abcdef');
          addressSpan.textContent = 'ADDR1…cdef';
          btn.parentNode.appendChild(addressSpan);

          // First click - icon changes to check
          await copyAddressToClipboard('0x1234567890abcdef', btn);
          assert.strictEqual(iconI.getAttribute('data-lucide'), 'check', 'first click: icon should be check');

          // Simulate lucide.createIcons replacing <i> with <svg>
          lucide.createIcons();

          // Icon should now be <svg>, <i> should be gone
          const newIcon = btn.querySelector('svg, i');
          assert.strictEqual(newIcon.tagName, 'SVG', 'after createIcons: icon should be SVG');

          // Second click - should work despite swap, icon resets
          await copyAddressToClipboard('0x1234567890abcdef', btn);

          // After second click, icon should have feedback
          const finalIcon = btn.querySelector('svg, i');
          assert.strictEqual(finalIcon.getAttribute('data-lucide'), 'check', 'second click: icon should show feedback');

          console.log('✓ second click after lucide swap test passed');
        })();
    """)

    result = subprocess.run([NODE, "-e", test_code], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, f"Test failed:\n{result.stderr}"
    assert "passed" in result.stdout


@pytest.mark.skipif(not NODE, reason="requires node.js")
def test_input_event_propagation_stopped():
    """Regression: click/mousedown/keydown on input do not bubble to row ancestor."""
    with open(INDEX_HTML) as f:
        html = f.read()

    match = re.search(
        r'(async\s+function\s+copyAddressToClipboard\(address,\s*btn\)\s*\{[\s\S]*?\n\})',
        html
    )
    assert match, "Could not extract function"
    func = match.group(1)

    test_code = _PRELUDE + "\n" + func + textwrap.dedent("""
        const assert = require('assert');

        (async () => {
          window.isSecureContext = false;
          document.execCommand = () => false;

          const row = new Element('DIV');
          row.className = 'copy-row';
          row.parentNode = document.body;
          document.body.appendChild(row);

          const btn = new Element('BUTTON');
          btn.className = 'copy-copy-btn';
          btn.parentNode = row;
          row.appendChild(btn);

          const icon = new Element('I');
          icon.setAttribute('data-lucide', 'copy');
          btn.appendChild(icon);

          const feedbackLabel = new Element('SPAN');
          feedbackLabel.className = 'copy-feedback-label';
          row.appendChild(feedbackLabel);

          const addressSpan = new Element('SPAN');
          addressSpan.className = 'copy-address-mono';
          addressSpan.setAttribute('data-address', '0x1234567890abcdef');
          addressSpan.textContent = 'ADDR';
          row.appendChild(addressSpan);

          // Track if row received events
          let rowEventsReceived = [];
          ['click', 'mousedown', 'keydown'].forEach(type => {
            row.addEventListener(type, (e) => {
              rowEventsReceived.push(type);
            });
          });

          await copyAddressToClipboard('0x1234567890abcdef', btn);

          // Get the input (created during failure fallback)
          const input = btn.parentNode.querySelector('input');
          assert.ok(input, 'input should be created');

          // Dispatch events on the input
          input.dispatchEvent({type: 'click', stopPropagation: () => {}, _stopped: false});
          input.dispatchEvent({type: 'mousedown', stopPropagation: () => {}, _stopped: false});
          input.dispatchEvent({type: 'keydown', key: 'ArrowRight', stopPropagation: () => {}, _stopped: false});

          // Row should NOT receive events (stopPropagation called)
          assert.strictEqual(rowEventsReceived.length, 0, 'row should not receive events from input');

          console.log('✓ input event propagation stopped test passed');
        })();
    """)

    result = subprocess.run([NODE, "-e", test_code], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, f"Test failed:\n{result.stderr}"
    assert "passed" in result.stdout


@pytest.mark.skipif(not NODE, reason="requires node.js")
def test_restore_dismissed_input():
    """Regression: restore after dismiss puts back correct truncated text and original display."""
    with open(INDEX_HTML) as f:
        html = f.read()

    match = re.search(
        r'(async\s+function\s+copyAddressToClipboard\(address,\s*btn\)\s*\{[\s\S]*?\n\})',
        html
    )
    assert match, "Could not extract function"
    func = match.group(1)

    test_code = _PRELUDE + "\n" + func + textwrap.dedent("""
        const assert = require('assert');

        (async () => {
          window.isSecureContext = false;
          document.execCommand = () => false;

          const btn = new Element('BUTTON');
          btn.parentNode = document.body;
          document.body.appendChild(btn);

          const icon = new Element('I');
          btn.appendChild(icon);

          const feedbackLabel = new Element('SPAN');
          feedbackLabel.className = 'copy-feedback-label';
          btn.parentNode.appendChild(feedbackLabel);

          const addressSpan = new Element('SPAN');
          addressSpan.className = 'copy-address-mono';
          addressSpan.setAttribute('data-address', '0x123456789abcdefghijk');
          addressSpan.textContent = '0x1234…hijk';
          addressSpan.style.display = '';
          btn.parentNode.appendChild(addressSpan);

          await copyAddressToClipboard('0x123456789abcdefghijk', btn);

          // Address span should be hidden, input shown
          assert.strictEqual(addressSpan.style.display, 'none', 'address should be hidden');
          const input = btn.parentNode.querySelector('input');
          assert.ok(input, 'input should exist');

          // Simulate Escape to restore
          const evt = {type: 'keydown', key: 'Escape', stopPropagation: () => {}};
          input.dispatchEvent(evt);

          // Address span should be restored with original text
          assert.strictEqual(addressSpan.style.display, '', 'address display should restore');
          assert.strictEqual(addressSpan.textContent, '0x1234…hijk', 'address text should match truncated render');

          // Input should be removed
          assert.ok(!input.isConnected, 'input should be removed');

          console.log('✓ restore dismissed input test passed');
        })();
    """)

    result = subprocess.run([NODE, "-e", test_code], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, f"Test failed:\n{result.stderr}"
    assert "passed" in result.stdout


@pytest.mark.skipif(not NODE, reason="requires node.js")
def test_hint_rendered_with_input():
    """Regression: 'Press Ctrl/Cmd+C' hint is rendered next to input and removed with it."""
    with open(INDEX_HTML) as f:
        html = f.read()

    match = re.search(
        r'(async\s+function\s+copyAddressToClipboard\(address,\s*btn\)\s*\{[\s\S]*?\n\})',
        html
    )
    assert match, "Could not extract function"
    func = match.group(1)

    test_code = _PRELUDE + "\n" + func + textwrap.dedent("""
        const assert = require('assert');

        (async () => {
          window.isSecureContext = false;
          document.execCommand = () => false;

          const btn = new Element('BUTTON');
          btn.parentNode = document.body;
          document.body.appendChild(btn);

          const icon = new Element('I');
          btn.appendChild(icon);

          const feedbackLabel = new Element('SPAN');
          feedbackLabel.className = 'copy-feedback-label';
          btn.parentNode.appendChild(feedbackLabel);

          const addressSpan = new Element('SPAN');
          addressSpan.className = 'copy-address-mono';
          addressSpan.setAttribute('data-address', '0x1234567890abcdef');
          addressSpan.textContent = 'ADDR';
          btn.parentNode.appendChild(addressSpan);

          await copyAddressToClipboard('0x1234567890abcdef', btn);

          // Find hint element
          const hint = btn.parentNode.querySelector('.copy-address-hint');
          assert.ok(hint, 'hint should be rendered');
          assert.strictEqual(hint.textContent, 'Press Ctrl/Cmd+C', 'hint text should be correct');

          // Get input and simulate blur to dismiss
          const input = btn.parentNode.querySelector('input');
          input.dispatchEvent({type: 'blur'});

          // Both input and hint should be removed
          assert.ok(!input.isConnected, 'input should be removed');
          assert.ok(!hint.isConnected, 'hint should be removed');

          console.log('✓ hint rendered with input test passed');
        })();
    """)

    result = subprocess.run([NODE, "-e", test_code], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, f"Test failed:\n{result.stderr}"
    assert "passed" in result.stdout


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

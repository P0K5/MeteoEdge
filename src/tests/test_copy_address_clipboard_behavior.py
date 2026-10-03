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
    // Minimal DOM/window stubs
    function makeDOMNode() {
      const node = {
        style: {},
        classList: { add(){}, remove(){}, contains(){ return false; } },
        dataset: {},
        isConnected: true,
        parentElement: null,
        addEventListener(){},
        removeEventListener(){},
        querySelector(){ return null; },
        querySelectorAll(){ return []; },
        appendChild(){},
        insertBefore(){},
        removeChild(){},
        remove(){},
        setAttribute(k, v){ if(!this._attrs) this._attrs = {}; this._attrs[k] = v; },
        getAttribute(k){ return this._attrs?.[k] || null; },
        focus(){},
        select(){},
      };
      Object.defineProperty(node, 'innerHTML', {
        get(){ return this._innerHTML || ''; },
        set(v){ this._innerHTML = v; }
      });
      Object.defineProperty(node, 'textContent', {
        get(){ return this._textContent || ''; },
        set(v){ this._textContent = v; }
      });
      Object.defineProperty(node, 'value', {
        get(){ return this._value || ''; },
        set(v){ this._value = v; }
      });
      return node;
    }

    const idCache = {};
    global.document = {
      getElementById(id) {
        if (!idCache[id]) idCache[id] = makeDOMNode();
        return idCache[id];
      },
      createElement() { return makeDOMNode(); },
      querySelectorAll() { return []; },
      querySelector() { return null; },
      documentElement: { getAttribute(){ return 'light'; } },
      activeElement: { focus(){} },
      body: { appendChild(){}, removeChild(){} },
    };

    global.window = { isSecureContext: true };
    global.navigator = { clipboard: { writeText: async () => {} } };
    global.lucide = { createIcons() {} };
    global.localStorage = { getItem() { return null; }, setItem(){} };
    global.clearTimeout = () => {};
    global.setTimeout = (fn, delay) => Math.random();
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

          const btn = makeDOMNode();
          btn.parentElement = makeDOMNode();
          const icon = makeDOMNode();
          btn.querySelector = () => icon;
          btn.parentElement.querySelector = () => makeDOMNode();

          await copyAddressToClipboard('0xTEST123', btn);

          assert.ok(clipboardCalled, 'navigator.clipboard.writeText should be called');
          assert.strictEqual(icon._attrs?.['data-lucide'], 'check',
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

          const btn = makeDOMNode();
          btn.parentElement = makeDOMNode();
          const icon = makeDOMNode();
          const feedbackLabel = makeDOMNode();
          const addressSpan = makeDOMNode();
          addressSpan._originalContent = 'SHOW12…3456';

          btn.querySelector = () => icon;
          btn.parentElement.querySelector = (sel) => {
            if (sel === '.copy-feedback-label') return feedbackLabel;
            if (sel === '.copy-address-mono') return addressSpan;
            return null;
          };
          addressSpan.parentElement = btn.parentElement;

          await copyAddressToClipboard('0xSHOW123456', btn);

          // Should show error icon
          assert.strictEqual(icon._attrs?.['data-lucide'], 'x',
            'icon should be x on failure');

          // Should show error state in aria-live
          const srStatus = document.getElementById('copy-sr-status');
          const srText = srStatus._textContent || '';
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

          const btn = makeDOMNode();
          btn.parentElement = makeDOMNode();
          const icon = makeDOMNode();
          btn.querySelector = () => icon;
          btn.parentElement.querySelector = () => makeDOMNode();

          // Track timer cancellations
          let timerIds = [];
          let clearCalled = 0;
          global.clearTimeout = (id) => { clearCalled++; };
          global.setTimeout = function(fn, delay) {
            const id = Math.random();
            timerIds.push(id);
            return id;
          };

          // First click
          await copyAddressToClipboard('0xTEST', btn);
          const timersAfterFirst = timerIds.length;
          assert.strictEqual(icon._attrs?.['data-lucide'], 'check',
            'first click: icon should be check');

          // Second click (rapid)
          await copyAddressToClipboard('0xTEST', btn);

          // Should have cleaned up previous timer
          assert.ok(clearCalled > 0, 'should clear previous timer on rapid click');
          assert.strictEqual(icon._attrs?.['data-lucide'], 'check',
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

          const btn = makeDOMNode();
          btn.isConnected = false;  // Detached
          btn.parentElement = makeDOMNode();
          const icon = makeDOMNode();
          icon.isConnected = false;
          btn.querySelector = () => icon;
          btn.parentElement.querySelector = () => makeDOMNode();

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


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

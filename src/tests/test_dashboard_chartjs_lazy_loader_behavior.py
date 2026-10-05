"""Behavior tests for the lazy Chart.js loader (issue #808).

Runs the REAL inline <script> of src/dashboard/static/index.html under Node
against a tiny DOM stub (same technique as
test_dashboard_copy_live_tab_behavior.py). `document.head.appendChild` captures
the injected <script> so each test can fire `onload` / `onerror` by hand.

Covered:

* `_ensureChartJs` injects ONE version-pinned script, resolves
  once it loads, and `Chart` is then defined;
* a load error clears the memoised promise, so a retry injects a fresh script
  and succeeds;
* the stale-token guard in the copy chart renderers: two renders requested
  while Chart.js is still loading produce ONE chart (the newest payload), not
  two;
* when Chart.js is already loaded the renderer stays synchronous.

Skipped when node is not installed (repo convention).
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

pytestmark = pytest.mark.skipif(not NODE, reason="requires node.js")

_PRELUDE = textwrap.dedent(r"""
    const assert = require('assert');

    class Element {
      constructor(tag) { this.tag = tag; this.style = {}; this.removed = false; }
      remove() { this.removed = true; }
      addEventListener() {}
    }
    const _byId = {};
    const el = (id) => _byId[id] || (_byId[id] = new Element('div'));
    const appended = [];                       // <script> elements injected into <head>
    global.document = {
      hidden: false,
      head: { appendChild(e) { appended.push(e); } },
      getElementById: (id) => el(id),
      querySelectorAll: () => [],
      querySelector: () => null,
      createElement: (tag) => new Element(tag),
      documentElement: { getAttribute() { return 'dark'; }, setAttribute() {} },
      addEventListener() {},
    };
    global.window = { addEventListener() {}, isSecureContext: true };
    global.localStorage = { getItem() { return null; }, setItem() {} };
    global.lucide = { createIcons() {} };
    global.getComputedStyle = () => ({ getPropertyValue: () => '' });
    console.error = () => {};
    console.warn = () => {};
    // Chart is deliberately NOT defined: tests install it when "the CDN script loads".
    const created = [];                        // data of every chart constructed
    const installChart = () => {
      global.Chart = function (canvas, cfg) { created.push(cfg.data); this.destroy = () => {}; };
    };
    const tick = () => new Promise(r => setImmediate(r));
""")


def _run(body: str, tmp_path: Path) -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r"<script>([\s\S]*?)</script>", html)
    assert m, "Could not find the dashboard's inline <script> block"
    code = (
        _PRELUDE + "\n" + m.group(1) + "\n(async () => {\n" + textwrap.dedent(body)
        + "\n})().then(() => console.log('OK'), e => { console.log('FAIL'); console.log(e && e.stack || e); process.exit(1); });"
    )
    path = tmp_path / "chartjs_loader_check.js"
    path.write_text(code, encoding="utf-8")
    r = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "OK" in r.stdout, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"


def test_head_has_no_blocking_chartjs_tag():
    html = INDEX_HTML.read_text(encoding="utf-8")
    head = html.split("</head>", 1)[0]
    assert "chart.umd" not in head and "chart.js@" not in head
    assert re.search(r'<link rel="preconnect" href="https://unpkg.com"', head)
    assert re.search(r'<script defer src="https://unpkg.com/lucide@\d+\.\d+\.\d+/[^"]*"></script>', head)
    assert "integrity" not in html, "SRI intentionally not used (hashes unverifiable against unpkg; see #808)"


def test_ensure_chartjs_injects_pinned_script_and_resolves(tmp_path):
    _run("""
        assert.strictEqual(typeof Chart, 'undefined');
        const p = _ensureChartJs();
        assert.strictEqual(appended.length, 1, 'one script injected');
        const s = appended[0];
        assert.match(s.src, /chart\\.js@\\d+\\.\\d+\\.\\d+\\/dist\\/chart\\.umd\\.min\\.js$/);
        assert.strictEqual(s.integrity, undefined, 'no SRI attribute (deliberate)');
        assert.strictEqual(_ensureChartJs(), p, 'concurrent callers share one promise');
        assert.strictEqual(appended.length, 1);
        installChart();
        s.onload();
        await p;
        assert.strictEqual(typeof Chart, 'function');
        // Already loaded: resolves without injecting anything else.
        await _ensureChartJs();
        assert.strictEqual(appended.length, 1);
    """, tmp_path)


def test_load_error_clears_memo_and_retry_succeeds(tmp_path):
    _run("""
        const first = _ensureChartJs();
        appended[0].onerror();
        await assert.rejects(first, /failed to load/);
        assert.ok(appended[0].removed, 'failed script tag is removed');
        const second = _ensureChartJs();
        assert.notStrictEqual(second, first);
        assert.strictEqual(appended.length, 2, 'retry injects a fresh script');
        installChart();
        appended[1].onload();
        await second;
        assert.strictEqual(typeof Chart, 'function');
    """, tmp_path)


def test_stale_token_guard_prevents_double_render(tmp_path):
    _run("""
        const pts = (n) => [{ day: '2026-09-01', cumulative: n }];
        _renderCopyLivePositionsChart(pts(1));    // Chart.js not loaded yet -> starts load
        _renderCopyLivePositionsChart(pts(2));    // newer request supersedes the first
        assert.strictEqual(created.length, 0, 'nothing drawn before the script loads');
        assert.strictEqual(appended.length, 1, 'still a single script injection');
        installChart();
        appended[0].onload();
        await tick();
        assert.strictEqual(created.length, 1, 'exactly one chart drawn, not two');
        assert.strictEqual(created[0].datasets[0].data[0], 2, 'the newest payload wins');
    """, tmp_path)


def test_renderer_is_synchronous_once_chartjs_is_loaded(tmp_path):
    _run("""
        installChart();
        _renderCopyLivePositionsChart([{ day: '2026-09-01', cumulative: 3 }]);
        assert.strictEqual(created.length, 1, 'drawn synchronously, no extra await tick');
        assert.strictEqual(appended.length, 0);
    """, tmp_path)

"""Chromium check for issue #1290: at a 375px viewport the Copy · Live roster's
actions (Edit live stake, Revert to paper) are reachable without horizontal
scrolling, and a failed posture fetch shows the neutral "Live status
unavailable" badge/banner (never "off"/PAPER).

Skipped when playwright or a Chromium build is not available (repo convention
for browser-dependent tests).
"""
from __future__ import annotations

import functools
import glob
import http.server
import threading
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

STATIC = Path(__file__).resolve().parents[1] / "dashboard" / "static"


def _chromium_path() -> str | None:
    for pattern in (
        "/opt/pw-browsers/chromium-*/chrome-linux/chrome",
        "/opt/pw-browsers/chromium_headless_shell-*/chrome-linux/headless_shell",
    ):
        found = sorted(glob.glob(pattern))
        if found:
            return found[-1]
    return None


def _wallet(addr: str, **over) -> dict:
    base = dict(
        address=addr, stake_per_trade=5.0, status="active", paused_reason=None,
        added_at="2026-09-01T00:00:00Z", n_settled=3, realized_pnl_usd=1.5,
        live_enabled=True, live_eligible=True, live_status_reason="eligible for live execution",
        live_stake_per_trade=2.0, live_stake_is_override=True,
    )
    return base | over


READY = "0x" + "c" * 40
FOLLOWED = dict(
    wallets=[
        _wallet("0x" + "a" * 40),
        _wallet("0x" + "b" * 40, live_stake_is_override=False, live_stake_per_trade=5.0),
        _wallet(READY, live_enabled=False, live_eligible=False, live_status_reason="live is not enabled for this wallet",
                live_stake_is_override=False, live_stake_per_trade=5.0),
    ],
    active_count=2, paused_count=0, aggregate_pnl_usd=3, n_settled_total=6, live_eligible_count=2,
    paper_only_count=0, live_aggregate_pnl_usd=0, live_n_settled_total=0, live_trading_enabled=True,
    live_cap_usd=10, live_opted_in_count=2,
)

MEASURE_JS = """async () => {
  const de = document.documentElement;
  const wrap = document.querySelector('#copy-live-followed-list .copy-table-wrap');
  const btns = [...document.querySelectorAll('#copy-live-followed-list .btn-followed-action')].map(b => {
    const r = b.getBoundingClientRect();
    return { cls: b.className, right: r.right, height: r.height };
  });
  const badge = document.getElementById('copy-live-mode-banner');
  const go = document.querySelector('#copy-live-ready-list .btn-followed-golive');
  const goRect = go ? go.getBoundingClientRect() : null;
  const colorOf = (v) => {
    const t = document.createElement('i'); t.style.color = v; document.body.appendChild(t);
    const c = getComputedStyle(t).color; t.remove(); return c;
  };
  // Trigger a validation failure through the real handler (prompt -> "abc").
  window.prompt = () => 'abc';
  const addr = '0x' + 'a'.repeat(40);
  await followedEditLiveStake(addr, document.createElement('button'));
  const msgEl = document.getElementById('live-followed-live-msg-' + _copySafeId(addr));
  return {
    goHeight: goRect && goRect.height, goRight: goRect && goRect.right,
    errText: msgEl.textContent, errColor: getComputedStyle(msgEl).color,
    noColor: colorOf('var(--no)'), mutedColor: colorOf('var(--muted)'),
    docScrollWidth: de.scrollWidth, innerWidth: window.innerWidth,
    wrapClient: wrap.clientWidth, wrapScroll: wrap.scrollWidth, buttons: btns,
    badgeText: badge.textContent.trim(), badgeClass: badge.className,
    infoDisplay: getComputedStyle(document.getElementById('copy-live-live-status-banner')).display,
  };
}"""


def _measure(config_fails: bool) -> dict:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(STATIC))
    handler.log_message = lambda *a, **k: None  # type: ignore[attr-defined]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def api(route):
        url = route.request.url
        if "/api/config" in url:
            if config_fails:
                return route.fulfill(status=500, body="{}")
            return route.fulfill(json={"copy_trading": {"COPY_LIVE_TRADING_ENABLED": {"value": True}}})
        if "followed-wallets" in url:
            return route.fulfill(json=FOLLOWED)
        return route.fulfill(json={})

    def any_route(route):
        if "/api/" in route.request.url:
            return api(route)
        if route.request.url.startswith("http://127.0.0.1"):
            return route.continue_()
        return route.fulfill(status=200, body="", content_type="application/javascript")

    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=_chromium_path(), args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 375, "height": 812})
            page.route("**/*", any_route)
            page.add_init_script("window.lucide={createIcons(){}};window.Chart=function(){this.destroy=()=>{}};")
            page.goto(f"http://127.0.0.1:{port}/index.html")
            page.wait_for_timeout(500)
            page.evaluate("document.getElementById('tab-btn-copy-live').click()")
            page.wait_for_timeout(1500)
            out = page.evaluate(MEASURE_JS)
            browser.close()
            return out
    finally:
        srv.shutdown()


@pytest.fixture(scope="module")
def _need_chromium():
    if not _chromium_path():
        pytest.skip("requires a Chromium build under /opt/pw-browsers")


def test_live_roster_actions_fit_375px_without_horizontal_scroll(_need_chromium):
    m = _measure(config_fails=False)
    assert m["innerWidth"] == 375
    assert m["docScrollWidth"] <= 375, m
    assert m["wrapScroll"] <= m["wrapClient"], f"roster scrolls horizontally: {m}"
    assert len(m["buttons"]) == 6
    for b in m["buttons"]:
        assert b["right"] <= 375, f"action off-screen: {b}"
        assert b["height"] >= 44, f"touch target below 44px: {b}"


def test_failed_posture_fetch_shows_unavailable_in_a_real_browser(_need_chromium):
    m = _measure(config_fails=True)
    assert m["badgeText"] == "Live status unavailable"
    assert "mode-badge-unknown" in m["badgeClass"]
    assert "mode-badge-paper" not in m["badgeClass"]
    assert m["infoDisplay"] != "none"


def test_row_stake_error_is_rendered_in_the_no_colour_and_ready_go_live_fits(_need_chromium):
    m = _measure(config_fails=False)
    assert m["errText"].startswith("Could not set live stake: ")
    assert m["noColor"] != m["mutedColor"]
    assert m["errColor"] == m["noColor"], f"error line must use --no, got {m['errColor']}"
    assert m["goRight"] <= 375, m  # height is 40px (pre-existing, Ready table untouched by #1290)

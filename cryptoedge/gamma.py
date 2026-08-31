"""Thin Gamma API client for the crypto up/down series.

Deliberately standalone (stdlib only) rather than importing MeteoEdge's
``src.http_client`` -- see README.md, "Isolation invariant". Keeping the
dependency surface at zero is what makes the isolation test cheap to enforce.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
UA = {"User-Agent": "cryptoedge/1.0 (research collector)"}

# btc-updown-5m-1788268500  ->  ('btc', 5, 1788268500)
SLUG_RE = re.compile(r"^([a-z0-9]+)-updown-(\d+)m-(\d+)$")


def parse_slug(slug: str) -> "tuple[str, int, int] | None":
    """(asset, window_minutes, window_start_epoch_seconds) or None."""
    m = SLUG_RE.match(slug or "")
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3))


def _get(url: str, timeout: int = 20, retries: int = 4):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                json.JSONDecodeError, OSError) as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"gamma GET failed after {retries} attempts: {url}") from last


def fetch_live_updown(limit: int = 100) -> "list[dict]":
    """DEPRECATED -- cannot reach the live window. See ``window_slugs``.

    Kept only to document the dead end: list-pagination returns either
    ~24h-ahead placeholder listings or months-old never-closed markets, never
    the window currently trading. Use ``window_slugs`` + ``fetch_slugs``.
    """
    url = (f"{GAMMA_EVENTS}?closed=false&limit={limit}"
           f"&order=startDate&ascending=false&tag_slug=crypto")
    out = []
    for ev in _get(url):
        parsed = parse_slug(ev.get("slug", ""))
        if not parsed:
            continue
        asset, window_min, start_epoch = parsed
        for m in ev.get("markets", []) or []:
            prices = m.get("outcomePrices")
            if isinstance(prices, str):
                try:
                    prices = json.loads(prices)
                except json.JSONDecodeError:
                    prices = None
            tokens = m.get("clobTokenIds")
            if isinstance(tokens, str):
                try:
                    tokens = json.loads(tokens)
                except json.JSONDecodeError:
                    tokens = None
            out.append({
                "slug": ev["slug"], "asset": asset, "window_min": window_min,
                "window_start_ms": start_epoch * 1000,
                "window_end_ms": _iso_ms(m.get("endDate") or ev.get("endDate")),
                "market_id": str(m.get("id")),
                "best_bid": _f(m.get("bestBid")), "best_ask": _f(m.get("bestAsk")),
                "spread": _f(m.get("spread")),
                "price_up": _f(prices[0]) if prices else None,
                "price_down": _f(prices[1]) if prices and len(prices) > 1 else None,
                "liquidity": _f(m.get("liquidity")), "volume": _f(m.get("volume")),
                "token_up": tokens[0] if tokens else None,
                "token_down": tokens[1] if tokens and len(tokens) > 1 else None,
                "closed": bool(m.get("closed")),
            })
    return out


def window_slugs(assets, windows, now_epoch: int, offsets=(-2, -1, 0, 1, 2)) -> "list[str]":
    """Deterministic slugs for the windows around *now*.

    The slug encodes the window-start epoch (``btc-updown-5m-1788268500`` starts
    at 1788268500), so the live market can be addressed directly by the clock.

    This replaces list-pagination, which cannot reach the live window:
    ``order=startDate&ascending=false`` returns the furthest-FUTURE listings
    (~24h ahead, all quoted at a placeholder 0.50/0.51), and
    ``order=endDate&ascending=true`` returns stale never-closed markets from
    months ago (bid 0 / ask 1 / liquidity 0). Neither surfaces the window that
    is actually trading. Verified against the live API 2026-08-31.

    Negative offsets are included so a just-ended window is re-read after Gamma
    marks it closed -- that is where the settled ``outcomePrices`` come from.
    """
    out = []
    for w in windows:
        span = w * 60
        base = (now_epoch // span) * span
        for off in offsets:
            for a in assets:
                out.append(f"{a}-updown-{w}m-{base + off * span}")
    return out


def fetch_slugs(slugs: "list[str]") -> "list[dict]":
    """Fetch many slugs in ONE request (Gamma accepts repeated ``slug=``),
    flattened the same way as the rows the collector stores.

    Batching verified 2026-08-31: 4 slugs requested -> 4 events returned. This
    keeps polling cost at one request per cycle no matter how many series are
    tracked.
    """
    if not slugs:
        return []
    q = "&".join(f"slug={s}" for s in slugs)
    events = _get(f"{GAMMA_EVENTS}?limit={max(20, len(slugs) * 2)}&{q}")
    return _flatten(events)


def _flatten(events) -> "list[dict]":
    out = []
    for ev in events:
        parsed = parse_slug(ev.get("slug", ""))
        if not parsed:
            continue
        asset, window_min, start_epoch = parsed
        for m in ev.get("markets", []) or []:
            prices = _jlist(m.get("outcomePrices"))
            tokens = _jlist(m.get("clobTokenIds"))
            out.append({
                "slug": ev["slug"], "asset": asset, "window_min": window_min,
                "window_start_ms": start_epoch * 1000,
                "window_end_ms": _iso_ms(m.get("endDate") or ev.get("endDate")),
                "market_id": str(m.get("id")),
                "best_bid": _f(m.get("bestBid")), "best_ask": _f(m.get("bestAsk")),
                "spread": _f(m.get("spread")),
                "price_up": _f(prices[0]) if prices else None,
                "price_down": _f(prices[1]) if prices and len(prices) > 1 else None,
                "liquidity": _f(m.get("liquidity")), "volume": _f(m.get("volume")),
                "token_up": tokens[0] if tokens else None,
                "token_down": tokens[1] if tokens and len(tokens) > 1 else None,
                "closed": bool(m.get("closed")),
            })
    return out


def _jlist(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return None
    return v

def fetch_event(slug: str) -> "dict | None":
    r = _get(f"{GAMMA_EVENTS}?limit=1&slug={slug}")
    return r[0] if r else None


def _f(v):
    """Float or None. NEVER substitutes a placeholder -- #1028's lesson: a
    fabricated price is indistinguishable from a real one once stored."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _iso_ms(s: "str | None") -> "int | None":
    if not s:
        return None
    from datetime import datetime, timezone
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00"))
                   .astimezone(timezone.utc).timestamp() * 1000)
    except ValueError:
        return None

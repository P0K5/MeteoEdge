"""Polymarket API client. No authentication required for read-only access."""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.http_client import cached_fetch_json, fetch

log = logging.getLogger(__name__)
from src.config import (
    POLYMARKET_GAMMA_API, POLYMARKET_CLOB_API,
    POLYMARKET_WEATHER_TAG_ID, HTTP_TIMEOUT_SECONDS,
)


def get_weather_markets() -> list[dict]:
    """Fetch all active weather-tagged markets via Polymarket Gamma API.

    Paginated (100 per page). Each page URL is cached for 5 min so back-to-back
    polls don't re-fetch. Markets open/close infrequently within a session.
    """
    all_markets: list[dict] = []
    seen_ids: set[str] = set()

    for offset in range(0, 5000, 100):
        url = (
            f"{POLYMARKET_GAMMA_API}/markets"
            f"?limit=100&active=true&closed=false"
            f"&tag_id={POLYMARKET_WEATHER_TAG_ID}&offset={offset}"
        )
        data = cached_fetch_json(url, ttl_minutes=5)
        if data is None:
            log.warning("[polymarket] offset=%s fetch failed, stopping pagination", offset)
            break
        batch = data if isinstance(data, list) else data.get("markets", [])
        if not batch:
            break
        for m in batch:
            mid = m.get("conditionId") or m.get("condition_id") or m.get("id")
            if mid and mid not in seen_ids:
                seen_ids.add(mid)
                all_markets.append(m)
        if len(batch) < 100:
            break

    return all_markets


def fetch_market_final_price(ticker: str) -> int | None:
    """Fetch the final resolved YES price (in cents) for a closed market.

    Queries the Polymarket Gamma API for the market identified by *ticker*
    (the 0x condition ID). Returns the YES ``outcomePrices`` value rounded to
    the nearest integer cent, or ``None`` if the market is not found, not yet
    resolved, or the API call fails.

    The ``outcomePrices`` field is a JSON-encoded list of decimal strings where
    index 0 is the YES price and index 1 is the NO price, e.g.
    ``'["0.97", "0.03"]'``.  A resolved-YES market shows YES ~= 1.00 (100c)
    and a resolved-NO market shows YES ~= 0.00 (0c).
    """
    import json as _json

    url = f"{POLYMARKET_GAMMA_API}/markets?condition_ids={ticker}&closed=true"
    try:
        r = fetch(url, timeout=HTTP_TIMEOUT_SECONDS)
        r.raise_for_status()
        result = r.json()
        if not result:
            log.warning("[polymarket] fetch_market_final_price(%s...): empty response", ticker[:14])
            return None
        market = result[0]
    except Exception as e:
        log.warning("[polymarket] fetch_market_final_price(%s...): %s", ticker[:14], e)
        return None

    raw = market.get("outcomePrices")
    if raw is None:
        return None

    # outcomePrices may arrive as a JSON string or already a list
    if isinstance(raw, str):
        try:
            prices = _json.loads(raw)
        except Exception:
            return None
    else:
        prices = raw

    if not prices:
        return None

    try:
        yes_price = float(prices[0])
    except (ValueError, TypeError, IndexError):
        return None

    return round(yes_price * 100)


def get_orderbook(token_id: str) -> dict:
    """Fetch live CLOB order book for a single token (YES or NO side).

    Not cached — the orderbook changes tick by tick.
    Returns dict with 'bids' and 'asks' lists of {price: str, size: str}.
    """
    url = f"{POLYMARKET_CLOB_API}/book?token_id={token_id}"
    try:
        r = fetch(url, timeout=HTTP_TIMEOUT_SECONDS)
        return r.json()
    except Exception as e:
        raise RuntimeError(f"Polymarket CLOB request failed for token {token_id}: {e}") from e


def fetch_orderbooks_batch(
    token_ids: list,
    max_workers: int = 8,
) -> dict:
    """Fetch orderbooks for multiple tokens in parallel.

    Each token is fetched independently using a thread pool bounded by
    ``max_workers``. A failed fetch for any single token returns ``{}`` for
    that token so the poll can continue with the remaining results.

    Args:
        token_ids: List of CLOB token IDs to fetch.
        max_workers: Maximum concurrent HTTP requests (default 8).

    Returns:
        dict mapping token_id → orderbook dict. Missing/failed tokens map
        to an empty dict ``{}``.
    """
    if not token_ids:
        return {}

    # Deduplicate while preserving order so we make exactly one request per
    # unique token regardless of how many markets share a token.
    unique_ids = list(dict.fromkeys(token_ids))

    results: dict = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(get_orderbook, tid): tid for tid in unique_ids}
        for fut in as_completed(futures):
            tid = futures[fut]
            try:
                results[tid] = fut.result()
            except Exception as e:
                log.warning("[clob-batch] %s...: %s", tid[:14], e)
                results[tid] = {}
    return results

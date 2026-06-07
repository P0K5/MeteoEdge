"""Polymarket API client. No authentication required for read-only access."""
from src.http_client import cached_fetch_json, fetch
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
            print(f"[polymarket] offset={offset} fetch failed, stopping pagination")
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

"""
Polymarket Gamma API client. Read-only, no auth. Identical in shape to the
live spike's client — kept as its own module here so the shadow loop is a
self-contained deployment.
"""
import httpx

from config import (
    POLYMARKET_GAMMA_API,
    POLYMARKET_WEATHER_TAG_ID,
    HTTP_TIMEOUT_SECONDS,
    USER_AGENT,
)

_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}


def get_weather_markets() -> list[dict]:
    """Fetch all active markets under Polymarket's weather tag (id=84)."""
    all_markets: list[dict] = []
    seen_ids: set[str] = set()

    for offset in range(0, 5000, 100):
        url = f"{POLYMARKET_GAMMA_API}/markets"
        params = {
            "limit": 100,
            "active": "true",
            "closed": "false",
            "tag_id": POLYMARKET_WEATHER_TAG_ID,
            "offset": offset,
        }
        try:
            r = httpx.get(url, params=params, headers=_HEADERS,
                          timeout=HTTP_TIMEOUT_SECONDS)
            r.raise_for_status()
            data = r.json()
            batch = data if isinstance(data, list) else data.get("markets", [])
        except Exception as e:
            print(f"[polymarket] offset={offset} fetch error: {e}")
            break

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

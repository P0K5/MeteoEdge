"""Polymarket trader-activity client (copy-trading hypothesis spike).

Wraps the public, unauthenticated Data API (``data-api.polymarket.com``) the
same way ``src/data/polymarket.py`` wraps the Gamma/CLOB APIs: no API key,
per-domain rate limiting via ``src.http_client``. This is a *different* host
than Gamma/CLOB, so it is a separate module rather than added to
``polymarket.py`` -- it has nothing to do with weather markets specifically,
it tracks wallets across all of Polymarket.

Two entry points, both keyed off a wallet ("proxy") address:

- ``get_leaderboard()`` -- Polymarket's own ranking of top wallets by
  realized profit over a time window. This is the "who is a profitable
  trader" discovery step.
- ``get_wallet_trades()`` -- the full trade tape (maker + taker fills) for
  one wallet, paginated. This is the raw data the backtest
  (``src/scripts/copy_trade_backtest.py``) replays.

**Unverified against live traffic.** Outbound access to polymarket.com
hosts was unavailable in the environment this module was written in, so
none of the request/response shapes below have been exercised against a
real wallet. Field names are read defensively (multiple aliases tried,
mirroring ``polymarket.py``'s ``_select_matching_market`` pattern) and every
public function fails soft (``[]``/``None``, never raises on a bad
response) so a wrong guess here degrades to "no data" rather than a crash.
Before trusting backtest output, run this once against a known wallet
address and confirm ``get_wallet_trades`` returns non-empty, realistic
records.
"""
import logging

from src.config import HTTP_TIMEOUT_SECONDS, POLYMARKET_DATA_API
from src.http_client import fetch

log = logging.getLogger(__name__)

LEADERBOARD_WINDOWS = {"day", "week", "month", "all"}

#: Hard cap on pagination depth so a misbehaving response (e.g. an API that
#: ignores offset and always returns the same page) can't loop forever or
#: hammer the host -- mirrors get_weather_markets()'s bounded range in
#: polymarket.py.
DEFAULT_TRADE_PAGE_SIZE = 500
MAX_TRADE_PAGES = 40


def get_leaderboard(window: str = "month", limit: int = 50) -> list[dict]:
    """Best-effort fetch of Polymarket's top-wallets-by-profit ranking.

    Returns ``[]`` (never raises) on any failure -- an empty result means
    "leaderboard unavailable this run", not "no profitable traders exist".
    Callers should fall back to an explicit wallet list when this returns
    ``[]`` (the backtest script's ``--wallets`` flag does exactly that).
    """
    if window not in LEADERBOARD_WINDOWS:
        raise ValueError(f"window must be one of {sorted(LEADERBOARD_WINDOWS)}, got {window!r}")

    url = f"{POLYMARKET_DATA_API}/leaderboard?window={window}&limit={limit}"
    try:
        r = fetch(url, timeout=HTTP_TIMEOUT_SECONDS)
        data = r.json()
    except Exception as exc:
        log.warning("[polymarket-traders] get_leaderboard(window=%s): %s", window, exc)
        return []

    if isinstance(data, dict):
        data = data.get("leaderboard") or data.get("results") or data.get("data") or []
    return data if isinstance(data, list) else []


def wallet_address(entry: dict) -> "str | None":
    """Extract a wallet/proxy address from a leaderboard (or trade) record,
    trying the field-name aliases different Data API versions have used."""
    for key in ("proxyWallet", "proxy_wallet", "wallet", "address", "user"):
        val = entry.get(key)
        if val:
            return val
    return None


def get_wallet_trades(
    address: str,
    max_pages: int = MAX_TRADE_PAGES,
    page_size: int = DEFAULT_TRADE_PAGE_SIZE,
) -> list[dict]:
    """Fetch *address*'s trade tape (maker + taker fills), oldest-first
    pagination via limit/offset, up to *max_pages*.

    A failed page stops pagination and logs a warning -- whatever was
    collected on earlier pages is still returned rather than discarded, so
    a transient mid-run failure degrades to "partial history" instead of
    "no history".
    """
    trades: list[dict] = []
    for page in range(max_pages):
        offset = page * page_size
        url = f"{POLYMARKET_DATA_API}/trades?user={address}&limit={page_size}&offset={offset}"
        try:
            r = fetch(url, timeout=HTTP_TIMEOUT_SECONDS)
            batch = r.json()
        except Exception as exc:
            log.warning(
                "[polymarket-traders] get_wallet_trades(%s...): page %s failed: %s",
                address[:10], page, exc,
            )
            break
        if not batch:
            break
        trades.extend(batch)
        if len(batch) < page_size:
            break
    return trades


def normalize_trade(raw: dict) -> "dict | None":
    """Normalize one raw trade record to the fields the backtest needs:
    ``market`` (condition ID), ``side`` ("BUY"/"SELL"), ``price`` (0-1
    probability), ``size`` (shares), ``timestamp`` (unix seconds),
    ``outcome`` ("Yes"/"No" label).

    Returns ``None`` if a required field is missing or unparseable -- an
    unnormalizable record is dropped, never guessed into a value.
    """
    try:
        market = raw.get("conditionId") or raw.get("condition_id") or raw.get("market")
        side = (raw.get("side") or "").upper()
        price = float(raw.get("price"))
        size = float(raw.get("size"))
        ts = int(raw.get("timestamp"))
        outcome = raw.get("outcome")
    except (TypeError, ValueError):
        return None

    if not market or side not in ("BUY", "SELL"):
        return None

    return {
        "market": market,
        "side": side,
        "price": price,
        "size": size,
        "timestamp": ts,
        "outcome": outcome,
        "asset": raw.get("asset") or raw.get("token_id"),
    }

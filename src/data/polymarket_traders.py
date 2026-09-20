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

**Verified against live traffic 2026-09-18.** ``get_wallet_trades`` (the
``/trades`` endpoint) and ``get_leaderboard`` (``/v1/leaderboard`` -- note
the ``/v1`` prefix, absent from the first best-guess URL) both confirmed
returning real, non-empty records with the field names read below. One
correctness gap found and fixed in that pass: a trade's ``outcome`` field
is the market's own label text (``"Up"``/``"Down"``, team names, etc. --
*not* reliably ``"Yes"``/``"No"``), so callers that need to know which side
of a binary market a trade is on must use ``outcome_index`` (0 == the same
index-0/"YES" price slot ``fetch_market_final_price`` in ``polymarket.py``
reads), never a text match against ``outcome``. See
``copy_trade_backtest.resolve_payout``. Field names are still read
defensively (multiple aliases tried, mirroring ``polymarket.py``'s
``_select_matching_market`` pattern) and every public function still fails
soft (``[]``/``None``, never raises on a bad response).
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

    url = f"{POLYMARKET_DATA_API}/v1/leaderboard?window={window}&limit={limit}"
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
    """Fetch *address*'s full trade tape (maker + taker fills), paginated
    via limit/offset, up to *max_pages*.

    **Pagination order -- corrected 2026-09-20 (issue #1123 research).**
    This docstring previously claimed oldest-first pagination; that was
    never actually verified against live traffic (only field names were,
    per the module docstring's 2026-09-18 note). Direct verification
    against ``data-api.polymarket.com/trades`` on 2026-09-20 (two
    independent wallets, 100+ trades each, offset pages checked for
    contiguity) shows the endpoint returns **newest-first** by default
    (strictly descending ``timestamp``, no ordering param requested or
    needed) and does **not** honor an explicit since/after/min-timestamp
    query filter (an unrecognized ``after=<ts>`` parameter is silently
    ignored -- confirmed byte-identical results with and without it).
    This function's own behavior (collect every page in whatever order
    the server returns them, up to *max_pages*) is unaffected by this
    correction -- full-history callers (``copy_trade_backtest.py``,
    ``copy_wallet_screening.py``) aggregate the whole result set
    regardless of order. Callers that only want trades newer than a
    known point (e.g. a poll loop) should use
    ``get_wallet_trades_since()`` instead, which exploits the confirmed
    newest-first default to stop paginating early rather than walking
    the full history every cycle.

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


def get_wallet_trades_since(
    address: str,
    since_ts: int,
    max_pages: int = MAX_TRADE_PAGES,
    page_size: int = DEFAULT_TRADE_PAGE_SIZE,
) -> list[dict]:
    """Fetch *address*'s trades strictly newer than *since_ts* (unix
    seconds), for a poll loop that only wants "what's new since last
    check" (issue #1123).

    Exploits ``get_wallet_trades()``'s confirmed newest-first default
    (see its docstring for the live-traffic verification): walks pages
    front-to-back and stops as soon as a trade at or before *since_ts* is
    seen, rather than walking the full history every cycle. For a
    normal-cadence follow set this typically costs a single page --
    ``COPY_MAX_WALLETS_FOLLOWED`` defaults to 10 and Epic A's stability
    screening already filters out the highest-volume/unstable wallets, so
    the followed set is expected to be small and moderate-volume, though
    that isn't enforced here. **Known limitation:** a wallet that trades
    more than *page_size* times between two poll cycles still needs
    multiple pages, bounded by *max_pages* exactly like
    ``get_wallet_trades()`` -- a pathologically high-volume followed
    wallet would still see a slow poll on the cycle it catches up.

    ``since_ts=0`` (a wallet's first-ever poll, before
    ``last_seen_trade_ts`` is set) has no early-stop point and walks up
    to the same ``max_pages`` cap as ``get_wallet_trades()`` -- there is
    no cheaper way to bootstrap a new follow given the endpoint's lack of
    a server-side tail filter.

    Never raises -- a failed page stops pagination and logs a warning,
    exactly like ``get_wallet_trades()``, degrading to a partial result
    rather than none.
    """
    since_ts = int(since_ts)
    trades: list[dict] = []
    for page in range(max_pages):
        offset = page * page_size
        url = f"{POLYMARKET_DATA_API}/trades?user={address}&limit={page_size}&offset={offset}"
        try:
            r = fetch(url, timeout=HTTP_TIMEOUT_SECONDS)
            batch = r.json()
        except Exception as exc:
            log.warning(
                "[polymarket-traders] get_wallet_trades_since(%s...): page %s failed: %s",
                address[:10], page, exc,
            )
            break
        if not batch:
            break

        reached_boundary = False
        for raw in batch:
            try:
                ts = int(raw.get("timestamp"))
            except (TypeError, ValueError):
                # Unparseable timestamp -- keep it (normalize_trade() will
                # drop it later); we just can't use it to decide the
                # early-stop boundary.
                trades.append(raw)
                continue
            if ts <= since_ts:
                reached_boundary = True
                break
            trades.append(raw)

        if reached_boundary or len(batch) < page_size:
            break
    return trades


def normalize_trade(raw: dict) -> "dict | None":
    """Normalize one raw trade record to the fields the backtest needs:
    ``market`` (condition ID), ``side`` ("BUY"/"SELL"), ``price`` (0-1
    probability), ``size`` (shares), ``timestamp`` (unix seconds),
    ``outcome`` (the market's own outcome label -- e.g. "Yes"/"No", but
    also "Up"/"Down", a team name, etc., so never text-match it to decide
    a binary win/lose), ``outcome_index`` (0 or 1 -- the positional slot
    that lines up with the Gamma API's ``outcomePrices``/``outcomes``
    arrays, i.e. the reliable way to know which side of a binary market
    this trade is on), ``source_trade_id`` (the on-chain ``transactionHash``,
    verified present on live trade records 2026-09-19 -- see
    ``copy_signals.source_trade_id`` in ``src/data/db.py`` for why the
    copy-trading signal loop needs it: crash-recovery dedup against
    re-signaling an already-processed fill. ``None`` if the field is
    absent from the raw record).

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

    try:
        raw_index = raw.get("outcomeIndex")
        outcome_index = int(raw_index) if raw_index is not None else None
    except (TypeError, ValueError):
        outcome_index = None

    return {
        "market": market,
        "side": side,
        "price": price,
        "size": size,
        "timestamp": ts,
        "outcome": outcome,
        "outcome_index": outcome_index,
        "asset": raw.get("asset") or raw.get("token_id"),
        "source_trade_id": raw.get("transactionHash") or raw.get("transaction_hash"),
    }

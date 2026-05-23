"""MeteoEdge web dashboard API server.

Endpoints:
    GET /api/health    — liveness probe
    GET /api/portfolio — open + closed positions, sourced from CLOB trade history
    GET /             — serves static/index.html (mounted last)

Data source (priority order):
    1. Polymarket CLOB trade history — positions and fills
    2. live_state.json — optional enrichment for my_prob / edge / station / bracket
    3. Gamma API — market question strings
    4. CLOB orderbook — live mark-to-market per open position
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from py_clob_client_v2.clob_types import BookParams

from src.config import POLYMARKET_GAMMA_API
from src.data.polymarket import get_orderbook
from src.execution.live_trader import STATE_PATH

logger = logging.getLogger(__name__)

app = FastAPI(title="MeteoEdge Dashboard", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

STATIC = Path(__file__).parent / "static"

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class PositionOut(BaseModel):
    question: str = ""
    station: str
    side: Literal["YES", "NO"]
    bracket_low: float
    bracket_high: float
    entry_price: int     # cents — avg fill price
    market_prob: int     # cents — live CLOB midpoint
    my_prob: int         # cents — model prediction (from enrichment, or entry_price)
    edge: float          # my_prob - market_prob
    shares: float
    invested: float
    current_value: float
    target_value: float


class ClosedPositionOut(BaseModel):
    question: str = ""
    station: str = ""
    side: Literal["YES", "NO"] = "YES"
    bracket_low: float = 0.0
    bracket_high: float = 0.0
    entry_price: int     # cents — avg buy price
    exit_price: int      # cents — sell price
    pnl: float           # realised P&L in USD
    shares: float
    closed_at: str = ""


class PortfolioOut(BaseModel):
    cash_usdc: float
    open_positions: list[PositionOut]
    closed_positions: list[ClosedPositionOut]
    updated_at: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_state() -> dict:
    """Read live_state.json; return empty state if missing or corrupt."""
    try:
        if STATE_PATH.exists():
            return {"open_trades": [], **__import__("json").loads(STATE_PATH.read_text())}
    except Exception:
        pass
    return {"updated_at": "", "open_trades": []}


def _state_enrichment() -> dict[str, dict]:
    """Return live_state.json open trades keyed by token_id for quick lookup."""
    return {t["token_id"]: t for t in (_read_state().get("open_trades") or []) if t.get("token_id")}


def _midpoint_cents(token_id: str, fallback_cents: int) -> int:
    """Single-token midpoint — used only in the live_state.json fallback path."""
    try:
        ob = get_orderbook(token_id)
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        if not bids or not asks:
            return fallback_cents
        best_bid = max(float(b["price"]) for b in bids)
        best_ask = min(float(a["price"]) for a in asks)
        return max(1, min(99, round((best_bid + best_ask) / 2 * 100)))
    except Exception:
        return fallback_cents


def _batch_midpoints(client, token_ids: list[str], fallbacks: dict[str, int]) -> dict[str, int]:
    """Fetch midpoints for all token_ids in a single CLOB request."""
    if not token_ids:
        return {}
    try:
        params = [BookParams(token_id=t) for t in token_ids]
        resp = client.get_midpoints(params)
        # Response is a dict keyed by token_id with mid value as string or float
        result: dict[str, int] = {}
        for token_id in token_ids:
            raw = resp.get(token_id)
            if raw is not None:
                result[token_id] = max(1, min(99, round(float(raw) * 100)))
            else:
                result[token_id] = fallbacks.get(token_id, 50)
        return result
    except Exception as e:
        logger.warning("Batch midpoints failed (%s) — using entry prices", e)
        return {t: fallbacks.get(t, 50) for t in token_ids}


_question_cache: dict[str, str] = {}
_weather_tokens: set[str] = set()
_weather_tokens_ts: float = 0.0


def _weather_token_ids() -> set[str]:
    """Return token IDs for all weather markets (active + recently closed). Cached 10 min."""
    import time
    import json as _json
    global _weather_tokens, _weather_tokens_ts
    if _weather_tokens and time.monotonic() - _weather_tokens_ts < 600:
        return _weather_tokens
    tokens: set[str] = set()
    for closed in ("false", "true"):
        for offset in range(0, 10000, 100):
            url = (
                f"{POLYMARKET_GAMMA_API}/markets"
                f"?limit=100&tag_id=84&closed={closed}&offset={offset}"
            )
            try:
                r = httpx.get(url, timeout=10)
                batch = r.json()
                if isinstance(batch, dict):
                    batch = batch.get("markets") or []
                if not batch:
                    break
                for m in batch:
                    raw = m.get("clobTokenIds") or "[]"
                    ids = raw if isinstance(raw, list) else _json.loads(raw)
                    for t in ids:
                        tokens.add(str(t))
                if len(batch) < 100:
                    break
            except Exception as e:
                logger.warning("Weather token fetch failed (closed=%s offset=%d): %s", closed, offset, e)
                break
    if tokens:
        _weather_tokens = tokens
        _weather_tokens_ts = time.monotonic()
        logger.info("Loaded %d weather token IDs", len(tokens))
    return _weather_tokens


def _market_question(condition_id: str) -> str:
    """Fetch market question from Gamma API by condition_id, cached in-process."""
    if not condition_id:
        return ""
    if condition_id in _question_cache:
        return _question_cache[condition_id]
    try:
        url = f"{POLYMARKET_GAMMA_API}/markets?conditionId={condition_id}&limit=1"
        r = httpx.get(url, timeout=8)
        data = r.json()
        batch = data if isinstance(data, list) else data.get("markets") or []
        question = ""
        if batch:
            m = batch[0]
            question = m.get("question") or m.get("groupItemTitle") or ""
        _question_cache[condition_id] = question
        return question
    except Exception:
        return ""


def _cash_usdc() -> float:
    try:
        from src.execution.auth import get_clob_client
        from src.execution.live_trader import LiveTrader
        return LiveTrader(get_clob_client()).get_usdc_balance()
    except Exception:
        logger.warning("Could not fetch USDC balance — returning 0.0")
        return 0.0


def _positions_from_clob() -> tuple[list[PositionOut], list[ClosedPositionOut]]:
    """Build open and closed positions from CLOB authenticated trade history."""
    from src.execution.auth import get_clob_client
    client = get_clob_client()
    trades = client.get_trades(only_first_page=True)

    weather_tokens = _weather_token_ids()

    # Group fills by token
    buys: dict[str, list[dict]] = defaultdict(list)
    sells: dict[str, list[dict]] = defaultdict(list)
    condition_ids: dict[str, str] = {}
    outcomes: dict[str, str] = {}  # token_id -> "Yes"/"No"

    for t in trades:
        token_id = str(t.get("asset_id") or t.get("assetId") or "")
        if not token_id:
            continue
        if weather_tokens and token_id not in weather_tokens:
            continue
        side = (t.get("side") or "").upper()
        size = float(t.get("size") or 0)
        price = float(t.get("price") or 0)
        condition_ids[token_id] = str(t.get("market") or "")
        outcomes[token_id] = str(t.get("outcome") or "Yes")
        fill = {"size": size, "price": price, "ts": str(t.get("match_time") or "")}
        if side == "BUY":
            buys[token_id].append(fill)
        elif side == "SELL":
            sells[token_id].append(fill)

    enrichment = _state_enrichment()

    # Pre-compute per-token aggregates
    all_tokens = set(buys) | set(sells)
    token_data: dict[str, dict] = {}
    for token_id in all_tokens:
        buy_list = buys.get(token_id, [])
        sell_list = sells.get(token_id, [])
        total_bought = sum(b["size"] for b in buy_list)
        total_sold = sum(s["size"] for s in sell_list)
        avg_buy_price = (
            sum(b["size"] * b["price"] for b in buy_list) / total_bought
            if total_bought > 0 else 0.0
        )
        token_data[token_id] = {
            "buy_list": buy_list,
            "sell_list": sell_list,
            "total_bought": total_bought,
            "total_sold": total_sold,
            "net_shares": round(total_bought - total_sold, 4),
            "avg_buy_price": avg_buy_price,
            "avg_entry_cents": max(1, min(99, round(avg_buy_price * 100))) if avg_buy_price else 50,
        }

    # Batch-fetch midpoints for all open tokens in a single request
    open_tokens = [t for t, d in token_data.items() if d["net_shares"] > 0.01]
    fallbacks = {t: token_data[t]["avg_entry_cents"] for t in open_tokens}
    midpoints = _batch_midpoints(client, open_tokens, fallbacks)

    open_positions: list[PositionOut] = []
    closed_positions: list[ClosedPositionOut] = []

    for token_id, d in token_data.items():
        enrich = enrichment.get(token_id, {})
        outcome_str = outcomes.get(token_id, "Yes")
        side: Literal["YES", "NO"] = enrich.get("side") or ("YES" if outcome_str.upper() == "YES" else "NO")
        question = _market_question(condition_ids.get(token_id, ""))
        avg_entry_cents = d["avg_entry_cents"]
        avg_buy_price = d["avg_buy_price"]

        if d["net_shares"] > 0.01:
            market_prob = midpoints.get(token_id, avg_entry_cents)
            my_prob = int(enrich.get("predicted_price", avg_entry_cents))
            open_positions.append(PositionOut(
                question=question,
                station=str(enrich.get("station", "")),
                side=side,
                bracket_low=float(enrich.get("bracket_low", 0.0)),
                bracket_high=float(enrich.get("bracket_high", 0.0)),
                entry_price=avg_entry_cents,
                market_prob=market_prob,
                my_prob=my_prob,
                edge=round(my_prob - market_prob, 2),
                shares=d["net_shares"],
                invested=round(d["net_shares"] * avg_buy_price, 2),
                current_value=round(d["net_shares"] * (market_prob / 100), 2),
                target_value=round(d["net_shares"] * 1.00, 2),
            ))

        if d["sell_list"]:
            total_sold = d["total_sold"]
            avg_sell_price = sum(s["size"] * s["price"] for s in d["sell_list"]) / total_sold
            avg_sell_cents = max(1, min(99, round(avg_sell_price * 100)))
            pnl = round((avg_sell_price - avg_buy_price) * total_sold, 2)
            latest_ts = max((s["ts"] for s in d["sell_list"]), default="")
            closed_positions.append(ClosedPositionOut(
                question=question,
                station=str(enrich.get("station", "")),
                side=side,
                bracket_low=float(enrich.get("bracket_low", 0.0)),
                bracket_high=float(enrich.get("bracket_high", 0.0)),
                entry_price=avg_entry_cents,
                exit_price=avg_sell_cents,
                pnl=pnl,
                shares=round(total_sold, 4),
                closed_at=latest_ts,
            ))

    # Sort: open by invested desc, closed by time desc
    open_positions.sort(key=lambda p: p.invested, reverse=True)
    closed_positions.sort(key=lambda p: p.closed_at, reverse=True)

    return open_positions, closed_positions


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/api/portfolio", response_model=PortfolioOut)
def portfolio() -> PortfolioOut:
    try:
        open_pos, closed_pos = _positions_from_clob()
    except Exception as e:
        logger.warning("CLOB trade fetch failed (%s) — falling back to live_state.json", e)
        # Fallback: reconstruct open positions from live_state.json only
        state = _read_state()
        open_trades = state.get("open_trades") or []
        open_pos = []
        for trade in open_trades:
            entry_price = int(trade.get("entry_price", 0))
            token_id = str(trade.get("token_id", ""))
            size_usdc = float(trade.get("size_usdc", 0.0))
            my_prob = int(trade.get("predicted_price", entry_price))
            market_prob = _midpoint_cents(token_id, entry_price)
            shares = round(size_usdc / (entry_price / 100), 4) if entry_price > 0 else 0.0
            open_pos.append(PositionOut(
                question="",
                station=str(trade.get("station", "")),
                side=trade.get("side", "YES"),
                bracket_low=float(trade.get("bracket_low", 0.0)),
                bracket_high=float(trade.get("bracket_high", 0.0)),
                entry_price=entry_price,
                market_prob=market_prob,
                my_prob=my_prob,
                edge=round(my_prob - market_prob, 2),
                shares=shares,
                invested=round(size_usdc, 2),
                current_value=round(shares * (market_prob / 100), 2),
                target_value=round(shares * 1.00, 2),
            ))
        closed_pos = []

    cash = _cash_usdc()
    return PortfolioOut(
        cash_usdc=round(cash, 2),
        open_positions=open_pos,
        closed_positions=closed_pos,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


# Mount static files last so /api routes take priority
if STATIC.exists():
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")

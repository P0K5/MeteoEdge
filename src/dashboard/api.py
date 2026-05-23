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


def _is_weather_question(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in ("temperature", "degrees", "highest temp", "daily high"))


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


def _positions_from_wallet() -> list[PositionOut]:
    """Fetch current open positions directly from Polymarket Data API by wallet address."""
    import os
    wallet = os.environ.get("POLYMARKET_DEPOSIT_WALLET", "")
    if not wallet:
        raise RuntimeError("POLYMARKET_DEPOSIT_WALLET not set")

    url = f"https://data-api.polymarket.com/positions?user_address={wallet}&sizeThreshold=.01&limit=100"
    r = httpx.get(url, timeout=15)
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list):
        rows = rows.get("positions") or rows.get("data") or []

    enrichment = _state_enrichment()

    from src.execution.auth import get_clob_client
    client = get_clob_client()
    token_ids = [str(p.get("asset") or p.get("tokenId") or p.get("token_id") or "") for p in rows]
    token_ids = [t for t in token_ids if t]
    fallbacks = {t: 50 for t in token_ids}
    midpoints = _batch_midpoints(client, token_ids, fallbacks)

    open_positions: list[PositionOut] = []
    for row in rows:
        token_id = str(row.get("asset") or row.get("tokenId") or row.get("token_id") or "")
        if not token_id:
            continue
        shares = float(row.get("size") or row.get("shares") or 0)
        if shares < 0.01:
            continue

        avg_price = float(row.get("avgPrice") or row.get("avg_price") or 0)
        avg_entry_cents = max(1, min(99, round(avg_price * 100))) if avg_price else 50

        outcome_str = str(row.get("outcome") or row.get("side") or "Yes")
        side: Literal["YES", "NO"] = "YES" if outcome_str.upper() in ("YES", "1") else "NO"

        market_obj = row.get("market") or {}
        question = (
            market_obj.get("question")
            or market_obj.get("groupItemTitle")
            or _market_question(str(row.get("conditionId") or market_obj.get("conditionId") or ""))
        )

        enrich = enrichment.get(token_id, {})
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
            shares=round(shares, 4),
            invested=round(shares * avg_price, 2),
            current_value=round(shares * (market_prob / 100), 2),
            target_value=round(shares * 1.00, 2),
        ))

    open_positions.sort(key=lambda p: p.invested, reverse=True)
    return open_positions


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/api/portfolio", response_model=PortfolioOut)
def portfolio() -> PortfolioOut:
    try:
        open_pos = _positions_from_wallet()
    except Exception as e:
        logger.warning("Wallet positions fetch failed (%s) — returning empty", e)
        open_pos = []

    cash = _cash_usdc()
    return PortfolioOut(
        cash_usdc=round(cash, 2),
        open_positions=open_pos,
        closed_positions=[],
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


# Mount static files last so /api routes take priority
if STATIC.exists():
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")

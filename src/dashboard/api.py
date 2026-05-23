"""MeteoEdge web dashboard API server.

Endpoints:
    GET /api/health    — liveness probe
    GET /api/portfolio — open positions with live CLOB mark-to-market
    GET /             — serves static/index.html (mounted last)

Usage:
    python run_dashboard.py
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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
    station: str
    side: Literal["YES", "NO"]
    bracket_low: float
    bracket_high: float
    entry_price: int     # cents — actual fill price
    market_prob: int     # cents — live CLOB midpoint
    my_prob: int         # cents — predicted_price at entry
    edge: float          # my_prob - market_prob
    shares: float
    invested: float      # USD cost basis
    current_value: float # USD mark-to-market
    target_value: float  # USD if wins (shares × $1.00)


class PortfolioOut(BaseModel):
    cash_usdc: float
    open_positions: list[PositionOut]
    updated_at: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_state() -> dict:
    """Read live_state.json; return empty state if missing or corrupt."""
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return {"updated_at": "", "open_trades": []}


def _midpoint_cents(token_id: str, fallback_cents: int) -> int:
    """Return CLOB midpoint in cents, falling back to fallback_cents on error."""
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
        logger.warning("Orderbook fetch failed for %s… — using entry_price fallback", token_id[:14])
        return fallback_cents


def _cash_usdc() -> float:
    """Return CLOB USDC balance; return 0.0 on any error."""
    try:
        from src.execution.auth import get_clob_client
        from src.execution.live_trader import LiveTrader
        return LiveTrader(get_clob_client()).get_usdc_balance()
    except Exception:
        logger.warning("Could not fetch USDC balance — returning 0.0")
        return 0.0


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/api/portfolio", response_model=PortfolioOut)
def portfolio() -> PortfolioOut:
    state = _read_state()
    open_trades = state.get("open_trades") or []
    updated_at = state.get("updated_at") or ""

    cash = _cash_usdc()

    positions: list[PositionOut] = []
    for trade in open_trades:
        entry_price: int = int(trade.get("entry_price", 0))
        token_id: str = str(trade.get("token_id", ""))
        size_usdc: float = float(trade.get("size_usdc", 0.0))
        my_prob: int = int(trade.get("predicted_price", entry_price))

        market_prob = _midpoint_cents(token_id, entry_price)

        if entry_price > 0:
            shares = round(size_usdc / (entry_price / 100), 4)
        else:
            shares = 0.0

        positions.append(PositionOut(
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

    return PortfolioOut(
        cash_usdc=round(cash, 2),
        open_positions=positions,
        updated_at=updated_at,
    )


# Mount static files last so /api routes take priority
if STATIC.exists():
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")

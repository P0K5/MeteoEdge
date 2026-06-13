"""MeteoEdge web dashboard API server — single consolidated dashboard.

Endpoints:
    GET /health         — liveness probe (uptime, last_poll)
    GET /status         — capital, today PnL, win rate, open positions count
    GET /trades         — last 50 trade records (newest first)
    GET /stations       — per-station trade count, win rate, total PnL
    GET /api/health     — liveness probe (ISO timestamp)
    GET /api/bot-log    — tail of logs/bot.log
    GET /api/portfolio  — open + closed positions, sourced from CLOB trade history
    GET /api/cities/{city}/taf      — TAF windows with disruption flag
    GET /api/cities/{city}/deb      — DEB model weights and RMSE
    GET /api/cities/{city}/analysis — intraday correction analysis
    GET /api/positions/{token_id}/snapshots — per-position price/model snapshots
    GET /               — serves static/index.html (mounted last)

Data source (priority order):
    1. Polymarket CLOB trade history — positions and fills
    2. live_state.json — optional enrichment for my_prob / edge / station / bracket
    3. Gamma API — market question strings
    4. CLOB orderbook — live mark-to-market per open position

Usage (standalone):
    uvicorn src.dashboard.api:app --port 8000

Usage (embedded in run.py via bridge stub):
    from src.monitoring.dashboard import start_dashboard
    start_dashboard()   # fires a daemon thread, returns immediately
"""
from __future__ import annotations

import logging
import time
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from py_clob_client_v2.clob_types import BookParams

from src.config import (
    POLYMARKET_GAMMA_API, STATIONS, LIVE_TRADES_JSONL, SNAPSHOTS_JSONL,
    POSITION_SNAPSHOTS_JSONL, LOG_DIR, STARTING_CAPITAL_EUR,
)
from src.data.db import Database
from src.data.nws import fetch_nws_forecast_high
from src.data.polymarket import get_orderbook
from src.data.taf_disruption import check_taf_disruption
from src.dashboard.data import read_jsonl, load_live_trades, load_snapshots, load_position_snapshots

STATE_PATH = Path("logs/live_state.json")

_db = Database()

# Module-level start time so /health can report uptime.
_START_TIME = time.monotonic()

# Last poll timestamp written here by run.py after each poll cycle.
# Stored as an ISO string or None.  Also writeable via the bridge stub in
# src/monitoring/dashboard.py so run.py does not need to be changed.
last_poll_ts: str | None = None

logger = logging.getLogger(__name__)


def set_db(db) -> None:
    """Inject the Database instance from run.py so endpoints read from SQLite."""
    global _db
    _db = db


# ---------------------------------------------------------------------------
# Monitoring helpers (ported from src/monitoring/dashboard.py)
# ---------------------------------------------------------------------------

def _today_utc() -> str:
    """Return today's date as YYYY-MM-DD in UTC."""
    return datetime.now(timezone.utc).date().isoformat()


def _dashboard_load_trades() -> list[dict]:
    """Return all trade records, newest first. Prefers DB when available."""
    if _db is not None:
        try:
            rows = _db.get_trades(limit=None, mode=None)
            if rows:
                return rows
        except Exception:
            logger.warning("[dashboard] failed to load trades from DB", exc_info=True)
    records = load_live_trades()
    return list(reversed(records))


def _compute_win_rate(trades: list[dict], n: int = 50) -> float:
    """Compute win rate over the last *n* settled trades.

    Only trades with a non-zero pnl are considered settled.
    Returns 0.0 when no settled trades exist.
    """
    settled = [
        t for t in trades
        if t.get("outcome") == "filled" and float(t.get("pnl") or 0) != 0.0
    ][:n]
    if not settled:
        return 0.0
    wins = sum(1 for t in settled if float(t.get("pnl") or 0) > 0)
    return wins / len(settled)


def _today_pnl(trades: list[dict]) -> float:
    """Sum PnL for trades whose timestamp falls on today (UTC)."""
    today = _today_utc()
    total = 0.0
    for t in trades:
        ts = t.get("ts", "")
        if isinstance(ts, str) and ts.startswith(today):
            total += float(t.get("pnl", 0))
    return total


def _today_trade_count(trades: list[dict]) -> int:
    """Count trades whose timestamp falls on today (UTC)."""
    today = _today_utc()
    return sum(1 for t in trades if isinstance(t.get("ts", ""), str) and t["ts"].startswith(today))


def _latest_capital(snapshots: list[dict]) -> float:
    """Return the most recent capital value. Prefers DB; falls back to snapshots."""
    if _db is not None:
        try:
            rows = _db.get_trades(limit=1, mode=None)
            if rows and rows[0].get("capital_after") is not None:
                return float(rows[0]["capital_after"])
        except Exception:
            logger.warning("[dashboard] failed to read latest capital from DB", exc_info=True)
    if not snapshots:
        return STARTING_CAPITAL_EUR
    last = snapshots[-1]
    return float(last.get("capital", STARTING_CAPITAL_EUR))


def _open_positions_count() -> int:
    """Return today's open position count from risk_state, or 0."""
    if _db is None:
        return 0
    try:
        cur = _db._conn.execute(
            "SELECT open_positions FROM risk_state WHERE trade_date=?",
            (_today_utc(),),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception:
        logger.warning("[dashboard] failed to read open_positions count", exc_info=True)
        return 0


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
    my_prob: int         # cents — model prediction at entry time (from enrichment)
    my_prob_now: int | None = None  # cents — current model prediction (from latest snapshot)
    edge: float          # my_prob - market_prob (uses entry-time model, not live)
    shares: float
    invested: float
    current_value: float
    target_value: float
    forecast_high_f: float | None = None  # NWS forecast high °F for today
    token_id: str = ""   # NO token id — used to fetch position snapshots for charting


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
    token_id: str = ""   # NO token id — used to fetch snapshot history for charting
    exit_reason: Literal["take_profit", "stop_loss", "won", "lost"] = "won"


class PortfolioOut(BaseModel):
    cash_usdc: float
    open_positions: list[PositionOut]
    closed_positions: list[ClosedPositionOut]
    updated_at: str


class DebWeightOut(BaseModel):
    model: str
    weight: float
    rmse_f: float
    n_samples: int = 0


class DebOut(BaseModel):
    city: str
    updated_at: str
    weights: list[DebWeightOut]


class AnalysisOut(BaseModel):
    city: str
    corrected_mu_f: float | None = None
    bias_delta_f: float | None = None
    decay_factor: float | None = None
    obs_temp_f: float | None = None
    model_temp_at_obs_f: float | None = None
    last_correction_time: str | None = None


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


def _latest_model_probs() -> dict[tuple[str, float, float], float]:
    """Latest model p_yes per (station, bracket_low, bracket_high).

    snapshots.jsonl is append-only and chronologically ordered, so iterating
    forward and overwriting the dict yields the most-recent p_yes for each
    (station, bracket) pair.  Per-poll snapshots cover every bracket evaluated
    by scan_markets(), so any open position with a matching key has a live
    model probability available here.
    """
    if not SNAPSHOTS_JSONL.exists():
        return {}
    result: dict[tuple[str, float, float], float] = {}
    try:
        import json as _json
        with open(SNAPSHOTS_JSONL) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = _json.loads(line)
                except Exception:
                    continue
                station = r.get("station") or ""
                bl = r.get("bracket_low")
                bh = r.get("bracket_high")
                py = r.get("p_yes")
                if station and bl is not None and bh is not None and py is not None:
                    result[(station, float(bl), float(bh))] = float(py)
    except OSError as e:
        logger.warning("snapshots.jsonl read error: %s", e)
    return result


def _trades_file_enrichment() -> dict[str, dict]:
    """Read live_trades.jsonl and return the most recent filled record per asset_id.

    Used as a durable fallback when live_state.json is missing or stale — the JSONL
    file persists across trader restarts and always carries predicted_price.
    """
    if not LIVE_TRADES_JSONL.exists():
        return {}
    result: dict[str, dict] = {}
    try:
        import json as _json
        with open(LIVE_TRADES_JSONL) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = _json.loads(line)
                except Exception:
                    continue
                if r.get("outcome") != "filled":
                    continue
                asset_id = r.get("asset_id") or r.get("no_token_id") or ""
                if asset_id:
                    result[asset_id] = r  # last filled record wins
    except Exception as e:
        logger.warning("live_trades.jsonl read error: %s", e)
    return result


# City → (lat, lon) from STATIONS config — built once at import time.
_CITY_COORDS: dict[str, tuple[float, float]] = {
    city: (lat, lon) for _, lat, lon, city, *_ in STATIONS
}


def _stopped_positions() -> list[ClosedPositionOut]:
    """Return positions closed before settlement (METAR stop-loss or take-profit).

    Both exit paths write outcome='sold' records to live_trades.jsonl with
    entry_price_cents, shares, and pnl already computed.  Take-profit exits
    have positive pnl; METAR stop-losses are typically negative.  The trigger
    field on the source record distinguishes them.
    """
    if not LIVE_TRADES_JSONL.exists():
        return []
    result: list[ClosedPositionOut] = []
    try:
        import json as _json
        with open(LIVE_TRADES_JSONL) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = _json.loads(line)
                except Exception:
                    continue
                if r.get("outcome") != "sold":
                    continue
                exit_cents = int(r.get("price_cents") or 0)
                entry_cents = int(r.get("entry_price_cents") or exit_cents)
                shares = float(r.get("shares") or 0)
                pnl = float(r.get("pnl") or 0)
                # Determine exit reason from trigger field
                trigger = str(r.get("trigger") or "")
                if trigger.startswith("take_profit@"):
                    exit_reason = "take_profit"
                elif trigger.startswith("stop_loss@"):
                    exit_reason = "stop_loss"
                else:
                    exit_reason = "won"  # fallback
                result.append(ClosedPositionOut(
                    question=str(r.get("question") or ""),
                    station=str(r.get("station") or ""),
                    side="NO",  # METAR exits are always NO positions
                    bracket_low=float(r.get("bracket_low") or 0.0),
                    bracket_high=float(r.get("bracket_high") or 0.0),
                    entry_price=entry_cents,
                    exit_price=exit_cents,
                    pnl=pnl,
                    shares=round(shares, 4),
                    closed_at=str(r.get("ts") or ""),
                    token_id=str(r.get("no_token_id") or r.get("asset_id") or ""),
                    exit_reason=exit_reason,
                ))
    except Exception as e:
        logger.warning("live_trades.jsonl stop-loss read error: %s", e)
    return result


def _settled_jsonl_positions() -> list[ClosedPositionOut]:
    """Return settled hold-to-expiry trades from live_trades.jsonl.

    settle.py writes pnl/actual_high/yes_won back to outcome='filled' records
    after each market resolves.  The presence of a 'pnl' field is the signal
    that settlement has been recorded.  This catches positions that were
    redeemed on Polymarket and therefore disappeared from the wallet API.

    Records that also have an outcome='sold' sibling (stop-loss / take-profit
    exits) are already captured by _stopped_positions() — settle.py skips
    writing pnl back to those filled records, so they won't appear here.
    """
    if not LIVE_TRADES_JSONL.exists():
        return []
    result: list[ClosedPositionOut] = []
    try:
        import json as _json
        with open(LIVE_TRADES_JSONL) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = _json.loads(line)
                except Exception:
                    continue
                if r.get("outcome") != "filled":
                    continue
                if "pnl" not in r:
                    continue  # not yet settled by settle.py
                token_id = str(r.get("no_token_id") or r.get("asset_id") or "")
                entry_cents = int(r.get("price_cents") or r.get("entry_price_cents") or 50)
                pnl = float(r["pnl"])
                is_win = pnl > 0
                exit_cents = 100 if is_win else 0
                size_eur = float(r.get("size_eur") or 0)
                shares = float(r.get("shares") or (
                    size_eur / (entry_cents / 100) if entry_cents else 0
                ))
                # Determine exit reason: won if pnl > 0, lost if pnl <= 0
                exit_reason = "won" if pnl > 0 else "lost"
                result.append(ClosedPositionOut(
                    question=str(r.get("question") or ""),
                    station=str(r.get("station") or ""),
                    side=str(r.get("side") or "NO"),
                    bracket_low=float(r.get("bracket_low") or 0.0),
                    bracket_high=float(r.get("bracket_high") or 0.0),
                    entry_price=entry_cents,
                    exit_price=exit_cents,
                    pnl=pnl,
                    shares=round(shares, 4),
                    closed_at=str(r.get("end_date") or r.get("ts") or ""),
                    token_id=token_id,
                    exit_reason=exit_reason,
                ))
    except Exception as e:
        logger.warning("live_trades.jsonl settled read error: %s", e)
    return result


def _nws_forecast_for_title(title: str) -> float | None:
    """Return today's NWS forecast high (°F) for the city mentioned in *title*."""
    t = title.lower()
    for city, (lat, lon) in _CITY_COORDS.items():
        if city.lower() in t:
            try:
                return fetch_nws_forecast_high(lat, lon)
            except Exception:
                return None
    return None


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


def _positions_from_wallet() -> tuple[list[PositionOut], list[ClosedPositionOut]]:
    """Fetch positions directly from Polymarket Data API by wallet address."""
    import os
    wallet = os.environ.get("POLYMARKET_DEPOSIT_WALLET", "")
    if not wallet:
        raise RuntimeError("POLYMARKET_DEPOSIT_WALLET not set")

    base_url = f"https://data-api.polymarket.com/positions?user={wallet}&sizeThreshold=0.01&limit=100"
    rows: list = []
    offset = 0
    while True:
        r = httpx.get(f"{base_url}&offset={offset}", timeout=15)
        r.raise_for_status()
        page = r.json()
        if not isinstance(page, list):
            page = page.get("positions") or page.get("data") or []
        rows.extend(page)
        if len(page) < 100:
            break
        offset += 100

    enrichment = _state_enrichment()
    jsonl_enrichment = _trades_file_enrichment()
    snap_probs = _latest_model_probs()

    # Batch-fetch live midpoints for non-resolved positions only
    from src.execution.auth import get_clob_client
    client = get_clob_client()
    active_token_ids = [
        str(row.get("asset") or "")
        for row in rows
        if not row.get("redeemable") and row.get("asset")
    ]
    avg_prices = {
        str(row.get("asset") or ""): float(row.get("avgPrice") or 0)
        for row in rows
    }
    fallbacks = {t: max(1, min(99, round(avg_prices.get(t, 0) * 100))) for t in active_token_ids}
    midpoints = _batch_midpoints(client, active_token_ids, fallbacks)

    open_positions: list[PositionOut] = []
    closed_positions: list[ClosedPositionOut] = []

    for row in rows:
        token_id = str(row.get("asset") or "")
        if not token_id:
            continue
        shares = float(row.get("size") or 0)
        if shares < 0.01:
            continue

        avg_price = float(row.get("avgPrice") or 0)
        avg_entry_cents = max(1, min(99, round(avg_price * 100))) if avg_price else 50
        side: Literal["YES", "NO"] = "YES" if str(row.get("outcome") or "").upper() == "YES" else "NO"
        question = str(row.get("title") or "")
        enrich = enrichment.get(token_id) or jsonl_enrichment.get(token_id, {})
        my_prob = int(enrich.get("predicted_price", avg_entry_cents))

        if row.get("redeemable"):
            # Market resolved. Both winning ($1) and losing ($0) tokens are "redeemable"
            # in Polymarket's CTF — the API sets curPrice=1 for the winning side only.
            initial_value = float(row.get("initialValue") or shares * avg_price)
            cur_price = float(row.get("curPrice") or 0)
            is_win = cur_price >= 0.99
            pnl = round(shares * 1.0 - initial_value if is_win else -initial_value, 2)
            exit_cents = 100 if is_win else 0
            closed_positions.append(ClosedPositionOut(
                question=question,
                station=str(enrich.get("station", "")),
                side=side,
                bracket_low=float(enrich.get("bracket_low", 0.0)),
                bracket_high=float(enrich.get("bracket_high", 0.0)),
                entry_price=avg_entry_cents,
                exit_price=exit_cents,
                pnl=pnl,
                shares=round(shares, 4),
                closed_at=str(row.get("endDate") or ""),
                token_id=token_id,
            ))
        else:
            # Active position
            market_prob = midpoints.get(token_id, avg_entry_cents)
            station_key = str(enrich.get("station", ""))
            bracket_low_key = float(enrich.get("bracket_low", 0.0))
            bracket_high_key = float(enrich.get("bracket_high", 0.0))
            my_prob_now: int | None = None
            snap_key = (station_key, bracket_low_key, bracket_high_key)
            if station_key and snap_key in snap_probs:
                py = snap_probs[snap_key]
                raw_now = py * 100 if side == "YES" else (1 - py) * 100
                my_prob_now = max(1, min(99, round(raw_now)))
            open_positions.append(PositionOut(
                question=question,
                station=station_key,
                side=side,
                bracket_low=bracket_low_key,
                bracket_high=bracket_high_key,
                entry_price=avg_entry_cents,
                market_prob=market_prob,
                my_prob=my_prob,
                my_prob_now=my_prob_now,
                edge=round(my_prob - market_prob, 2),
                shares=round(shares, 4),
                invested=round(shares * avg_price, 2),
                current_value=round(float(row.get("currentValue") or shares * avg_price / 100), 2),
                target_value=round(shares * 1.00, 2),
                forecast_high_f=_nws_forecast_for_title(question),
                token_id=token_id,
            ))

    closed_positions.extend(_stopped_positions())

    # Merge settled JSONL positions — catches trades already redeemed from the wallet.
    # Deduplicate by token_id: wallet API record takes priority when both exist.
    seen_token_ids = {p.token_id for p in closed_positions if p.token_id}
    for p in _settled_jsonl_positions():
        if not p.token_id or p.token_id not in seen_token_ids:
            closed_positions.append(p)
            if p.token_id:
                seen_token_ids.add(p.token_id)

    open_positions.sort(key=lambda p: p.invested, reverse=True)
    closed_positions.sort(key=lambda p: p.closed_at, reverse=True)
    return open_positions, closed_positions


# ---------------------------------------------------------------------------
# Monitoring endpoints (ported from src/monitoring/dashboard.py)
# These use the root path prefix (/health, /status, /trades, /stations)
# ---------------------------------------------------------------------------

@app.get("/health")
def health_monitor() -> dict:
    """Liveness probe. Always returns 200 even when log files are empty.

    Reports uptime in seconds and the timestamp of the last polling cycle.
    """
    uptime = int(time.monotonic() - _START_TIME)
    return {
        "status": "ok",
        "last_poll": last_poll_ts,
        "uptime_seconds": uptime,
    }


@app.get("/status")
def status() -> dict:
    """Summary of current capital, today's PnL, trade count, win rate, and open positions."""
    trades = _dashboard_load_trades()
    snapshots = load_snapshots()

    capital = _latest_capital(snapshots)
    today_pnl = _today_pnl(trades)
    today_count = _today_trade_count(trades)
    win_rate = _compute_win_rate(trades, n=50)
    open_count = _open_positions_count()

    return {
        "capital": round(capital, 2),
        "today_pnl": round(today_pnl, 2),
        "today_trade_count": today_count,
        "win_rate": round(win_rate, 4),
        "open_positions_count": open_count,
        "last_poll": last_poll_ts,
    }


@app.get("/trades")
def trades_list() -> list[dict]:
    """Last 50 trade records, newest first."""
    return _dashboard_load_trades()[:50]


@app.get("/stations")
def stations() -> dict[str, Any]:
    """Per-station trade count, win rate, and total PnL."""
    all_trades = _dashboard_load_trades()
    by_station: dict[str, list[dict]] = defaultdict(list)
    for t in all_trades:
        station = t.get("station", "UNKNOWN")
        by_station[station].append(t)

    result: dict[str, Any] = {}
    for station, station_trades in sorted(by_station.items()):
        filled = [t for t in station_trades if t.get("outcome") == "filled"]
        wins = sum(1 for t in filled if float(t.get("pnl", 0)) > 0)
        win_rate = wins / len(filled) if filled else 0.0
        total_pnl = sum(float(t.get("pnl", 0)) for t in station_trades)
        result[station] = {
            "trade_count": len(station_trades),
            "filled_count": len(filled),
            "win_rate": round(win_rate, 4),
            "total_pnl": round(total_pnl, 2),
        }
    return result


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/api/bot-log")
def bot_log(lines: int = 200) -> dict:
    """Return the last *lines* lines of logs/bot.log (capped at 1000)."""
    n = max(1, min(1000, lines))
    path = LOG_DIR / "bot.log"
    if not path.exists():
        return {"path": str(path), "lines": [], "error": "log file not found"}
    try:
        with open(path, "rb") as fh:
            try:
                fh.seek(0, 2)
                size = fh.tell()
                block = 8192
                data = b""
                while size > 0 and data.count(b"\n") <= n:
                    step = min(block, size)
                    size -= step
                    fh.seek(size)
                    data = fh.read(step) + data
                text = data.decode("utf-8", errors="replace")
            except OSError:
                fh.seek(0)
                text = fh.read().decode("utf-8", errors="replace")
        tail = text.splitlines()[-n:]
        return {"path": str(path), "lines": tail}
    except Exception as e:
        logger.warning("bot.log read error: %s", e)
        return {"path": str(path), "lines": [], "error": str(e)}


@app.get("/api/portfolio", response_model=PortfolioOut)
def portfolio() -> PortfolioOut:
    try:
        open_pos, closed_pos = _positions_from_wallet()
    except Exception as e:
        logger.warning("Wallet positions fetch failed (%s) — returning empty", e)
        open_pos, closed_pos = [], []

    cash = _cash_usdc()
    return PortfolioOut(
        cash_usdc=round(cash, 2),
        open_positions=open_pos,
        closed_positions=closed_pos,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


_DISRUPTION_GROUP = "Temporary Fluctuation"
_DISRUPTION_CODES = frozenset({"TS", "SH", "FG"})


def _window_has_disruption(window: dict) -> bool:
    if window.get("group_type") != _DISRUPTION_GROUP:
        return False
    sig = set((window.get("sig_wx") or "").split())
    return bool(sig & _DISRUPTION_CODES)


@app.get("/api/cities/{city}/taf")
def get_city_taf(city: str, hours: int = 24) -> list[dict]:
    """Return TAF windows for *city* for the next *hours* hours (max 48).

    Each window includes a computed `taf_disruption` boolean.
    Returns 404 when no TAF data is available for the city.
    """
    hours = min(hours, 48)
    now = datetime.now(timezone.utc)
    from_ts = now.isoformat()
    to_ts = (now + timedelta(hours=hours)).isoformat()

    windows = _db.get_taf_windows(city, from_ts=from_ts, to_ts=to_ts)
    if not windows:
        raise HTTPException(status_code=404, detail=f"No TAF data for city: {city}")

    return [
        {**w, "taf_disruption": _window_has_disruption(w)}
        for w in windows
    ]


@app.get("/api/cities/{city}/deb", response_model=DebOut)
def get_city_deb(city: str) -> DebOut:
    """Return DEB model weights and RMSE for *city*.

    City name is normalised to title-case before the DB lookup so the endpoint
    is case-insensitive.  Returns 404 when no model_weights rows exist for the
    city.

    ``updated_at`` is the most-recent ``date`` value across all weight rows,
    formatted as an ISO date string.  ``n_samples`` is set to 0 because the
    model_weights table does not carry a sample count; callers that need the
    full forecast-log count should query the forecast-log endpoint directly.
    """
    normalised = city.title()
    rows = _db.get_model_weights(normalised)
    if not rows:
        raise HTTPException(status_code=404, detail="no DEB data for city")

    # updated_at = max date across all rows (rows are already ordered DESC)
    updated_at = rows[0]["date"]

    weights = [
        DebWeightOut(
            model=row["model"],
            weight=round(float(row["weight"]), 4),
            rmse_f=round(float(row["rmse"]), 4),
            n_samples=0,
        )
        for row in rows
    ]
    return DebOut(city=normalised, updated_at=updated_at, weights=weights)


@app.get("/api/cities/{city}/analysis", response_model=AnalysisOut)
def get_city_analysis(city: str) -> AnalysisOut:
    """Return intraday correction analysis for *city*.

    Fetches the latest intraday correction for today's date and populates
    the response with the most recent correction values. If no corrections
    exist or any error occurs, all correction fields are returned as None.

    City name is normalised to title-case before the DB lookup so the endpoint
    is case-insensitive.
    """
    normalised = city.title()
    today_date = datetime.now(timezone.utc).date().isoformat()

    corrected_mu_f = None
    bias_delta_f = None
    decay_factor = None
    obs_temp_f = None
    model_temp_at_obs_f = None
    last_correction_time = None

    try:
        corrections = _db.get_intraday_corrections(normalised, today_date)
        if corrections:
            # Get the most recent correction (last item in list)
            latest = corrections[-1]
            corrected_mu_f = float(latest["corrected_mu_f"])
            bias_delta_f = float(latest["delta_f"])
            decay_factor = float(latest["decay_factor"])
            obs_temp_f = float(latest["obs_temp_f"])
            model_temp_at_obs_f = float(latest["model_temp_f"])
            last_correction_time = str(latest["obs_time"])
    except Exception as e:
        logger.warning("Could not fetch intraday corrections for %s: %s", normalised, e)

    return AnalysisOut(
        city=normalised,
        corrected_mu_f=corrected_mu_f,
        bias_delta_f=bias_delta_f,
        decay_factor=decay_factor,
        obs_temp_f=obs_temp_f,
        model_temp_at_obs_f=model_temp_at_obs_f,
        last_correction_time=last_correction_time,
    )


@app.get("/api/positions/{token_id}/snapshots")
def position_snapshots(token_id: str) -> list[dict]:
    """Return time-series price/model snapshots for a position identified by its NO token_id.

    Each record contains: ts, market_bid, fair_value, current_high, latest_temp.
    Sourced from position_snapshots.jsonl written every poll by run.py.
    """
    if not POSITION_SNAPSHOTS_JSONL.exists():
        return []
    import json as _json
    result = []
    try:
        with open(POSITION_SNAPSHOTS_JSONL) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = _json.loads(line)
                except Exception:
                    continue
                if r.get("no_token_id") == token_id:
                    result.append({
                        "ts": r.get("ts", ""),
                        "market_bid": r.get("no_best_bid"),
                        "fair_value": r.get("fair_value_now"),
                        "current_high": r.get("current_high"),
                        "latest_temp": r.get("latest_temp"),
                    })
    except OSError as e:
        logger.warning("position_snapshots.jsonl read error: %s", e)
    return result


# Mount static files last so /api routes take priority
if STATIC.exists():
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")

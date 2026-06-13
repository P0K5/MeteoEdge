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
    STATION_ACTIVE_HOURS, DISABLED_STATIONS, EMOS_DEFAULT_MODE,
    CONFIG_DEFAULTS, get_live_config,
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
    allow_methods=["GET", "POST"],
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


class StationOverviewOut(BaseModel):
    metar: str
    city: str
    lat: float
    lon: float
    unit: str
    timezone: str
    active_hours_utc: list[int]
    enabled: bool
    trade_count: int
    filled_count: int
    win_rate: float
    total_pnl: float
    last_trade_ts: str | None
    open_positions_count: int
    last_obs_ts: str | None
    status: Literal["active", "outside_hours", "no_data", "disabled"]


class EmosCoefficients(BaseModel):
    a: float
    b: float
    c: float
    d: float
    crps_score: float | None = None
    trained_at: str | None = None
    ready_for_promotion: bool


class EmosCityStatus(BaseModel):
    city: str
    metar: str
    effective_mode: str
    shadow: EmosCoefficients | None = None
    primary: EmosCoefficients | None = None
    settled_days_available: int
    min_settled_days_required: int


# ---------------------------------------------------------------------------
# EMOS helpers
# ---------------------------------------------------------------------------

_EMOS_MIN_SETTLED_DAYS = 60

_CITY_TO_STATION: dict[str, str] = {
    city: station for station, _lat, _lon, city, *_ in STATIONS
}

_STATION_TO_CITY: dict[str, str] = {v: k for k, v in _CITY_TO_STATION.items()}


def _emos_row_to_coefficients(row: dict) -> EmosCoefficients:
    return EmosCoefficients(
        a=float(row["a"]),
        b=float(row["b"]),
        c=float(row["c"]),
        d=float(row["d"]),
        crps_score=float(row["crps_score"]) if row.get("crps_score") is not None else None,
        trained_at=row.get("trained_at"),
        ready_for_promotion=bool(row.get("ready_for_promotion", 0)),
    )


def _get_emos_city_status(city: str, station: str, calibration_by_city: dict) -> EmosCityStatus:
    city_rows = calibration_by_city.get(city, {})
    shadow_row = city_rows.get("emos_shadow")
    primary_row = city_rows.get("emos_primary")
    shadow = _emos_row_to_coefficients(shadow_row) if shadow_row else None
    primary = _emos_row_to_coefficients(primary_row) if primary_row else None
    settled = _db.get_settled_days_available(station) if _db is not None else 0
    override = _db.get_emos_effective_mode(city) if _db is not None else None
    effective_mode = override if override is not None else EMOS_DEFAULT_MODE
    return EmosCityStatus(
        city=city,
        metar=station,
        effective_mode=effective_mode,
        shadow=shadow,
        primary=primary,
        settled_days_available=settled,
        min_settled_days_required=_EMOS_MIN_SETTLED_DAYS,
    )


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


_stations_overview_cache: dict = {"ts": 0.0, "data": None}
_STATIONS_OVERVIEW_TTL = 60.0  # seconds


def _derive_station_status(
    metar: str,
    enabled: bool,
    last_obs_ts: "str | None",
) -> str:
    """Derive the station status string from config and latest observation timestamp.

    Logic:
      - disabled  → station is in DISABLED_STATIONS
      - outside_hours → enabled AND current UTC hour outside active_hours_utc
      - no_data   → enabled AND in active hours AND (no obs or obs > 2h ago)
      - active    → enabled AND in active hours AND obs within 2h
    """
    if not enabled:
        return "disabled"

    active_hours = STATION_ACTIVE_HOURS.get(metar)
    now_utc = datetime.now(timezone.utc)
    current_hour = now_utc.hour

    if active_hours is not None:
        start_h, end_h = active_hours
        in_active_hours = start_h <= current_hour < end_h
    else:
        in_active_hours = True  # treat unknown as always active

    if not in_active_hours:
        return "outside_hours"

    # In active hours — check observation freshness (within 2h)
    if last_obs_ts is None:
        return "no_data"

    try:
        # ISO timestamp may or may not have tz info
        obs_dt = datetime.fromisoformat(last_obs_ts)
        if obs_dt.tzinfo is None:
            obs_dt = obs_dt.replace(tzinfo=timezone.utc)
        age_seconds = (now_utc - obs_dt).total_seconds()
        if age_seconds > 7200:  # 2h = 7200s
            return "no_data"
        return "active"
    except (ValueError, TypeError):
        return "no_data"


@app.get("/api/stations/overview", response_model=list[StationOverviewOut])
def stations_overview() -> list[StationOverviewOut]:
    """Return config metadata and trade stats for all configured stations.

    Response is cached for 60 seconds.  Fields per station:
    - Config: metar, city, lat, lon, unit, timezone, active_hours_utc, enabled
    - DB: trade_count, filled_count, win_rate, total_pnl, last_trade_ts,
          open_positions_count, last_obs_ts
    - Derived: status (active | outside_hours | no_data | disabled)
    """
    now = time.monotonic()
    cached = _stations_overview_cache
    if cached["data"] is not None and (now - cached["ts"]) < _STATIONS_OVERVIEW_TTL:
        return cached["data"]

    # Fetch all DB data in three batch queries (avoids N+1 per station)
    last_obs_map: dict = {}
    open_pos_map: dict = {}
    trade_stats_map: dict = {}
    if _db is not None:
        try:
            last_obs_map = _db.get_stations_last_obs_ts()
        except Exception:
            logger.warning("[stations/overview] failed to fetch last_obs_ts", exc_info=True)
        try:
            open_pos_map = _db.get_stations_open_positions_count()
        except Exception:
            logger.warning("[stations/overview] failed to fetch open_positions_count", exc_info=True)
        try:
            trade_stats_map = _db.get_stations_trade_stats()
        except Exception:
            logger.warning("[stations/overview] failed to fetch trade_stats", exc_info=True)

    # Read DISABLED_STATIONS at request time (env var may change between restarts)
    disabled = DISABLED_STATIONS

    result: list[StationOverviewOut] = []
    for station_cfg in STATIONS:
        metar, lat, lon, city, _res_station, unit, tz = station_cfg
        enabled = metar not in disabled
        last_obs_ts = last_obs_map.get(metar)
        open_positions_count = open_pos_map.get(metar, 0)
        stats = trade_stats_map.get(metar, {})

        status = _derive_station_status(metar, enabled, last_obs_ts)

        active_hours = STATION_ACTIVE_HOURS.get(metar, (0, 24))

        result.append(StationOverviewOut(
            metar=metar,
            city=city,
            lat=lat,
            lon=lon,
            unit=unit,
            timezone=tz,
            active_hours_utc=list(active_hours),
            enabled=enabled,
            trade_count=stats.get("trade_count", 0),
            filled_count=stats.get("filled_count", 0),
            win_rate=stats.get("win_rate", 0.0),
            total_pnl=stats.get("total_pnl", 0.0),
            last_trade_ts=stats.get("last_trade_ts"),
            open_positions_count=open_positions_count,
            last_obs_ts=last_obs_ts,
            status=status,
        ))

    _stations_overview_cache["ts"] = now
    _stations_overview_cache["data"] = result
    return result


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


# ---------------------------------------------------------------------------
# EMOS management endpoints
# ---------------------------------------------------------------------------

@app.get("/api/emos/status", response_model=list[EmosCityStatus])
def emos_status() -> list[EmosCityStatus]:
    """Return EMOS calibration state for all cities defined in STATIONS.

    For each city, reports the effective mode, shadow/primary calibration rows,
    settled day count, and the minimum required before promotion.
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    # Load all emos_calibration rows once and group by city + model_mode
    all_rows = _db.get_all_emos_calibration()
    calibration_by_city: dict[str, dict[str, dict]] = {}
    for row in all_rows:
        city_key = row["city"]
        mode_key = row["model_mode"]
        calibration_by_city.setdefault(city_key, {})[mode_key] = row

    result = []
    for station, _lat, _lon, city, *_ in STATIONS:
        status = _get_emos_city_status(city, station, calibration_by_city)
        result.append(status)
    return result


def _resolve_city(city: str) -> tuple[str, str]:
    """Resolve a URL city name to (canonical_city, station).

    Performs a case-insensitive lookup against STATIONS. Raises 404 if not found.
    The URL may use URL-encoded spaces (FastAPI decodes them automatically).
    """
    city_lower = city.lower()
    for station, _lat, _lon, cfg_city, *_ in STATIONS:
        if cfg_city.lower() == city_lower:
            return cfg_city, station
    raise HTTPException(status_code=404, detail=f"Unknown city: {city!r}")


@app.post("/api/emos/{city}/promote", response_model=EmosCityStatus)
def emos_promote(city: str) -> EmosCityStatus:
    """Promote the emos_shadow row to emos_primary for a city.

    Preconditions (409 if not met):
    - A shadow row exists in emos_calibration for the city
    - ready_for_promotion = 1

    Action: copies shadow coefficients to a new/updated emos_primary row,
    then sets effective mode to 'emos_primary'.

    Returns the updated EmosCityStatus for the city.
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    canonical_city, station = _resolve_city(city)

    shadow = _db.get_emos_coefficients(canonical_city, "emos_shadow")
    if shadow is None:
        raise HTTPException(
            status_code=409,
            detail=f"No shadow calibration row for city '{canonical_city}'",
        )
    if not shadow.get("ready_for_promotion"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Shadow row for '{canonical_city}' is not ready for promotion "
                f"(ready_for_promotion=0). Use POST /api/emos/{city}/mark-ready first."
            ),
        )

    # Copy shadow → primary
    _db.upsert_emos_coefficients(
        city=canonical_city,
        model_mode="emos_primary",
        a=shadow["a"],
        b=shadow["b"],
        c=shadow["c"],
        d=shadow["d"],
        crps_score=shadow.get("crps_score"),
        trained_at=shadow.get("trained_at"),
        ready_for_promotion=0,
    )
    _db.set_emos_effective_mode(canonical_city, "emos_primary")

    # Build and return updated status
    all_rows = _db.get_all_emos_calibration()
    calibration_by_city: dict[str, dict[str, dict]] = {}
    for row in all_rows:
        calibration_by_city.setdefault(row["city"], {})[row["model_mode"]] = row
    return _get_emos_city_status(canonical_city, station, calibration_by_city)


@app.post("/api/emos/{city}/demote", response_model=EmosCityStatus)
def emos_demote(city: str) -> EmosCityStatus:
    """Roll back to legacy mode for a city (idempotent).

    Sets the effective mode to 'legacy'. Does not delete any calibration rows.
    Safe to call regardless of current effective mode.

    Returns the updated EmosCityStatus for the city.
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    canonical_city, station = _resolve_city(city)

    _db.set_emos_effective_mode(canonical_city, "legacy")

    all_rows = _db.get_all_emos_calibration()
    calibration_by_city: dict[str, dict[str, dict]] = {}
    for row in all_rows:
        calibration_by_city.setdefault(row["city"], {})[row["model_mode"]] = row
    return _get_emos_city_status(canonical_city, station, calibration_by_city)


@app.post("/api/emos/{city}/mark-ready", response_model=EmosCityStatus)
def emos_mark_ready(city: str) -> EmosCityStatus:
    """Toggle ready_for_promotion (0 ↔ 1) on the shadow row for a city.

    This is an administrative flag the operator sets after reviewing CRPS scores.
    Returns 409 if no shadow row exists for the city.

    Returns the updated EmosCityStatus for the city.
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    canonical_city, station = _resolve_city(city)

    new_val = _db.toggle_emos_ready_for_promotion(canonical_city)
    if new_val is None:
        raise HTTPException(
            status_code=409,
            detail=f"No shadow calibration row for city '{canonical_city}'",
        )

    all_rows = _db.get_all_emos_calibration()
    calibration_by_city: dict[str, dict[str, dict]] = {}
    for row in all_rows:
        calibration_by_city.setdefault(row["city"], {})[row["model_mode"]] = row
    return _get_emos_city_status(canonical_city, station, calibration_by_city)


# ---------------------------------------------------------------------------
# Config API — DB-backed parameter store
# ---------------------------------------------------------------------------

# Full parameter metadata: description, type, group, optional bounds.
# Keys match CONFIG_DEFAULTS in src/config.py exactly.
_CONFIG_META: dict[str, dict] = {
    "MIN_EDGE_CENTS": {
        "description": "Minimum required edge to enter a trade (¢)",
        "type": "float",
        "group": "strategy",
        "min": 1.0,
        "max": 50.0,
    },
    "MAX_EDGE_CENTS": {
        "description": "Maximum edge — above this, adverse selection risk",
        "type": "float",
        "group": "strategy",
        "min": 1.0,
        "max": 50.0,
    },
    "MIN_PRICE_CENTS": {
        "description": "Minimum market price to enter a trade (¢)",
        "type": "int",
        "group": "strategy",
        "min": 1,
        "max": 99,
    },
    "MIN_CONFIDENCE_YES": {
        "description": "Minimum model confidence for YES-side entries",
        "type": "float",
        "group": "strategy",
        "min": 0.5,
        "max": 1.0,
    },
    "MAX_CONFIDENCE_YES_FOR_NO": {
        "description": "Maximum p(YES) allowed for NO-side entries",
        "type": "float",
        "group": "strategy",
        "min": 0.0,
        "max": 0.5,
    },
    "ENABLE_YES_TRADES": {
        "description": "Allow YES-side entries",
        "type": "bool",
        "group": "strategy",
    },
    "MIN_FORECAST_BRACKET_MARGIN_F": {
        "description": "Minimum margin (°F) between forecast high and bracket boundary",
        "type": "float",
        "group": "strategy",
        "min": 0.0,
        "max": 10.0,
    },
    "EMOS_DEFAULT_MODE": {
        "description": "EMOS deployment mode fallback when no calibration row exists",
        "type": "enum",
        "group": "strategy",
        "options": ["legacy", "emos_shadow", "emos_primary"],
    },
    "DAILY_LOSS_LIMIT_EUR": {
        "description": "Maximum daily loss before trading halts (€)",
        "type": "float",
        "group": "risk",
        "min": 1.0,
        "max": 500.0,
    },
    "MAX_OPEN_POSITIONS": {
        "description": "Maximum number of simultaneous open positions",
        "type": "int",
        "group": "risk",
        "min": 1,
        "max": 50,
    },
    "DRAWDOWN_STOP_PCT": {
        "description": "Drawdown fraction that halts trading for the day",
        "type": "float",
        "group": "risk",
        "min": 0.01,
        "max": 1.0,
    },
    "MIN_MARKET_LIQUIDITY_SHARES": {
        "description": "Minimum shares at best bid/ask required to enter",
        "type": "float",
        "group": "risk",
        "min": 1.0,
        "max": 500.0,
    },
    "POSITION_SIZE_EUR": {
        "description": "Notional size per trade (€)",
        "type": "float",
        "group": "position",
        "min": 1.0,
        "max": 100.0,
    },
    "TAKE_PROFIT_BUFFER_CENTS": {
        "description": "Exit when bid reaches predicted_price minus this buffer (¢)",
        "type": "int",
        "group": "position",
        "min": 0,
        "max": 20,
    },
    "STOP_LOSS_MIN_BID_CENTS": {
        "description": "Only stop-loss sell when bid is at or above this floor (¢)",
        "type": "int",
        "group": "position",
        "min": 1,
        "max": 99,
    },
    "STOP_LOSS_CONSECUTIVE_POLLS": {
        "description": "Number of consecutive polls fair_value < entry required to trigger stop-loss",
        "type": "int",
        "group": "position",
        "min": 1,
        "max": 10,
    },
    "STOP_LOSS_MIN_DEPTH_SHARES": {
        "description": "Minimum shares at best bid to avoid spoof-triggered stop-loss",
        "type": "float",
        "group": "position",
        "min": 1.0,
        "max": 100.0,
    },
    "POLL_INTERVAL_SECONDS": {
        "description": "Seconds between poll cycles",
        "type": "int",
        "group": "timing",
        "min": 30,
        "max": 3600,
    },
    "MAX_MINUTES_TO_SETTLEMENT": {
        "description": "Skip markets with more than this many minutes to settlement",
        "type": "int",
        "group": "timing",
        "min": 60,
        "max": 2880,
    },
    "MIN_MINUTES_TO_SETTLEMENT": {
        "description": "Skip markets with fewer than this many minutes to settlement",
        "type": "int",
        "group": "timing",
        "min": 1,
        "max": 60,
    },
}

_EMOS_VALID_MODES = frozenset({"legacy", "emos_shadow", "emos_primary"})


class ConfigPatchRequest(BaseModel):
    key: str
    value: Any


def _validate_config_value(key: str, raw_value: Any) -> "tuple[str, str | None]":
    """Validate and coerce *raw_value* for *key*.

    Returns ``(serialised_str, None)`` on success or ``('', error_message)`` on
    failure.
    """
    meta = _CONFIG_META.get(key)
    if meta is None:
        return "", f"Unknown config key: {key!r}"

    param_type = meta["type"]
    try:
        if param_type == "bool":
            if isinstance(raw_value, bool):
                coerced: Any = raw_value
            elif isinstance(raw_value, str):
                coerced = raw_value.lower() in ("true", "1", "yes")
            else:
                coerced = bool(raw_value)
            return str(coerced).lower(), None

        if param_type == "int":
            coerced = int(raw_value)
            lo = meta.get("min")
            hi = meta.get("max")
            if lo is not None and coerced < lo:
                return "", f"{key} must be >= {lo}, got {coerced}"
            if hi is not None and coerced > hi:
                return "", f"{key} must be <= {hi}, got {coerced}"
            return str(coerced), None

        if param_type == "float":
            coerced = float(raw_value)
            lo = meta.get("min")
            hi = meta.get("max")
            if lo is not None and coerced < lo:
                return "", f"{key} must be >= {lo}, got {coerced}"
            if hi is not None and coerced > hi:
                return "", f"{key} must be <= {hi}, got {coerced}"
            return str(coerced), None

        if param_type == "enum":
            val = str(raw_value)
            allowed = set(meta.get("options", []))
            if key == "EMOS_DEFAULT_MODE":
                allowed = _EMOS_VALID_MODES
            if val not in allowed:
                return "", f"{key} must be one of {sorted(allowed)}, got {val!r}"
            return val, None

    except (ValueError, TypeError) as exc:
        return "", f"Invalid value for {key} ({param_type}): {exc}"

    return "", f"Unsupported type {param_type!r} for {key}"


def _typed_value(key: str, raw: str) -> Any:
    """Cast raw DB string to the correct Python type for the API response."""
    meta = _CONFIG_META.get(key, {})
    param_type = meta.get("type", "str")
    if param_type == "bool":
        return raw.lower() in ("true", "1", "yes")
    if param_type == "int":
        try:
            return int(raw)
        except (ValueError, TypeError):
            return raw
    if param_type == "float":
        try:
            return float(raw)
        except (ValueError, TypeError):
            return raw
    return raw


def _build_param_entry(key: str, raw: str) -> dict:
    """Build the parameter object returned by GET /api/config."""
    meta = _CONFIG_META.get(key, {})
    entry: dict = {
        "value": _typed_value(key, raw),
        "description": meta.get("description", ""),
        "type": meta.get("type", "str"),
    }
    if "min" in meta:
        entry["min"] = meta["min"]
    if "max" in meta:
        entry["max"] = meta["max"]
    if "options" in meta:
        entry["options"] = meta["options"]
    return entry


@app.get("/api/config")
def get_config() -> dict:
    """Return all editable bot parameters with their current DB values, grouped by category."""
    live = get_live_config(_db)
    # Build nested dict grouped by category
    result: dict[str, dict] = {}
    for key in CONFIG_DEFAULTS:
        meta = _CONFIG_META.get(key, {})
        group = meta.get("group", "other")
        raw = str(live.get(key, CONFIG_DEFAULTS[key]))
        if group not in result:
            result[group] = {}
        result[group][key] = _build_param_entry(key, raw)
    return result


@app.patch("/api/config")
def patch_config(req: ConfigPatchRequest) -> dict:
    """Update a single bot parameter.

    Validates key existence, type, and bounds. Writes to the bot_config table.
    Returns the updated parameter object or a 400 error on validation failure.
    """
    key = req.key
    if key not in CONFIG_DEFAULTS:
        raise HTTPException(status_code=400, detail=f"Unknown config key: {key!r}")

    serialised, err = _validate_config_value(key, req.value)
    if err:
        raise HTTPException(status_code=400, detail=err)

    _db.set_config(key, serialised)

    return _build_param_entry(key, serialised)


# Mount static files last so /api routes take priority
if STATIC.exists():
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")

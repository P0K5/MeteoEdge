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
    POST /api/positions/{token_id}/sell — operator-triggered immediate sell of a position
    POST /api/stations/{metar}/toggle — toggle station enable/disable (DB-persisted)
    GET /analytics/intraday — intra-day snapshot series for station × date from analytics.db
    GET /               — serves static/index.html (mounted last)

Data source (priority order):
    1. Polymarket CLOB trade history — positions and fills
    2. DB open_positions table — enrichment for station / bracket / predicted_price
    3. live_trades.jsonl — fallback enrichment for tokens settled/removed from open_positions
    4. Gamma API — market question strings
    5. CLOB orderbook — live mark-to-market per open position

Usage (standalone):
    uvicorn src.dashboard.api:app --port 8000

Usage (embedded in run.py via bridge stub):
    from src.monitoring.dashboard import start_dashboard
    start_dashboard()   # fires a daemon thread, returns immediately
"""
from __future__ import annotations

import io
import json
import logging
import math
import os
import time
import threading
from collections import defaultdict
from contextlib import redirect_stdout
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel

from py_clob_client_v2.clob_types import BookParams

from src.config import (
    POLYMARKET_GAMMA_API, STATIONS, LIVE_TRADES_JSONL, SNAPSHOTS_JSONL,
    POSITION_SNAPSHOTS_JSONL, LOG_DIR, STARTING_CAPITAL_EUR,
    STATION_ACTIVE_HOURS, DISABLED_STATIONS, SHADOW_STATIONS_YES, SHADOW_STATIONS_NO,
    EMOS_DEFAULT_MODE, CONFIG_DEFAULTS, get_live_config, station_city,
    MODEL_PROB_CAP, METAR_SKIP_STATIONS,
)
from src.model.residual_correction import (
    compute_residual_stats,
    compute_residual_stats_per_pair,
)
from src.data.archive_db import ArchiveDatabase
from src.data.db import Database, compute_win_rate
from src.data.nws import fetch_nws_forecast_high
from src.data.polymarket import get_orderbook
from src.data.taf_disruption import check_taf_disruption
from src.execution.live_trader import LiveTrader
from src.execution.order_manager import order_manager
from src.dashboard.data import read_jsonl, load_live_trades, load_snapshots, load_position_snapshots
from src.utils.log_rotation import iter_rotated_jsonl, rotated_sources

_db = Database()

# Module-level start time so /health can report uptime.
_START_TIME = time.monotonic()

# Last poll timestamp written here by run.py after each poll cycle.
# Stored as an ISO string or None.  Also writeable via the bridge stub in
# src/monitoring/dashboard.py so run.py does not need to be changed.
last_poll_ts: str | None = None

# Per-station weather-feed health from the most recent poll's _build_weather().
# A list of {station, status, reason}; written via the bridge stub in
# src/monitoring/dashboard.py so run.py does not need to import api directly.
weather_health: list | None = None

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# mtime-keyed JSONL cache (issue #170)
# Re-parses a JSONL file only when its mtime or size has changed since the
# last call.  Thread-safe via a per-cache lock so concurrent requests don't
# trigger redundant parses.
# ---------------------------------------------------------------------------

@dataclass
class _JsonlCache:
    mtime: float = 0.0
    size: int = 0
    data: Any = dc_field(default_factory=lambda: None)
    lock: threading.Lock = dc_field(default_factory=threading.Lock)


_JSONL_CACHES: dict[str, _JsonlCache] = {}
_JSONL_CACHES_LOCK = threading.Lock()


def _jsonl_cache_get(path: "Path | str", parse_fn):
    """Return cached result, re-parsing only when file mtime/size changes.

    Args:
        path: Path to the JSONL file.
        parse_fn: Callable(path) → data.  Called when cache is stale.
            When the file does not exist, returns ``parse_fn.__annotations__``
            default or an empty structure inferred from the first call's result.

    Returns:
        Cached (or freshly parsed) data from parse_fn.
    """
    key = str(path)
    # Lazily create cache entry (one per file).
    with _JSONL_CACHES_LOCK:
        if key not in _JSONL_CACHES:
            _JSONL_CACHES[key] = _JsonlCache()
    entry = _JSONL_CACHES[key]

    try:
        st = os.stat(path)
        mtime, size = st.st_mtime, st.st_size
    except OSError:
        # File does not exist — return empty-ish result without caching.
        return parse_fn(path)

    with entry.lock:
        if entry.mtime == mtime and entry.size == size and entry.data is not None:
            return entry.data
        data = parse_fn(path)
        entry.mtime = mtime
        entry.size = size
        entry.data = data
        return data


def _jsonl_cache_get_rotated(base: "Path", parse_fn):
    """Cache wrapper for a multi-file (rotated) JSONL source.

    Reads via the rotation helpers so the legacy plain file AND every dated
    .jsonl / .jsonl.gz are considered.  Cache key is the combined max-mtime
    plus total size across all source files — re-parses when ANY source
    changes.

    Args:
        base: The bare (legacy) path; rotation helpers expand it to all sources.
        parse_fn: Callable(iter_records) → data.  Receives an iterator that
            yields parsed JSON dicts in chronological order across all sources.
    """
    key = f"rotated::{base}"
    with _JSONL_CACHES_LOCK:
        if key not in _JSONL_CACHES:
            _JSONL_CACHES[key] = _JsonlCache()
    entry = _JSONL_CACHES[key]

    sources = rotated_sources(base)
    if not sources:
        return parse_fn(iter([]))

    max_mtime = 0.0
    total_size = 0
    for p in sources:
        try:
            st = os.stat(p)
        except OSError:
            continue
        if st.st_mtime > max_mtime:
            max_mtime = st.st_mtime
        total_size += st.st_size

    with entry.lock:
        if (entry.mtime == max_mtime
                and entry.size == total_size
                and entry.data is not None):
            return entry.data
        data = parse_fn(iter_rotated_jsonl(base))
        entry.mtime = max_mtime
        entry.size = total_size
        entry.data = data
        return data


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
    """Return all trade records (excluding shadow), newest first. Prefers DB when available."""
    if _db is not None:
        try:
            rows = _db.get_trades(limit=None, mode=None)
            if rows:
                # Filter out shadow rows for live metrics
                rows = [t for t in rows if t.get("mode") != "shadow"]
                return rows
        except Exception:
            logger.warning("[dashboard] failed to load trades from DB", exc_info=True)
    records = load_live_trades()
    # Filter out shadow rows from JSONL fallback
    records = [t for t in records if t.get("mode") != "shadow"]
    return list(reversed(records))


def _compute_win_rate(trades: list[dict], n: int = 50) -> float:
    """Compute win rate over the last *n* settled trades (excluding shadow).

    Only trades with a non-zero pnl are considered settled.
    Returns 0.0 when no settled trades exist.
    """
    settled = [
        t for t in trades
        if t.get("mode") != "shadow" and t.get("outcome") in ("filled", "sold") and float(t.get("pnl") or 0) != 0.0
    ][:n]
    if not settled:
        return 0.0
    wins = sum(1 for t in settled if float(t.get("pnl") or 0) > 0)
    return wins / len(settled)


def _today_pnl(trades: list[dict]) -> float:
    """Sum PnL for trades whose timestamp falls on today (UTC), excluding shadow rows."""
    today = _today_utc()
    total = 0.0
    for t in trades:
        ts = t.get("ts", "")
        if t.get("mode") != "shadow" and isinstance(ts, str) and ts.startswith(today):
            total += float(t.get("pnl", 0))
    return total


def _today_trade_count(trades: list[dict]) -> int:
    """Count trades whose timestamp falls on today (UTC)."""
    today = _today_utc()
    return sum(1 for t in trades if isinstance(t.get("ts", ""), str) and t["ts"].startswith(today))


def _latest_capital(snapshots: list[dict]) -> float:
    """Return the most recent capital value (excluding shadow). Prefers DB; falls back to snapshots."""
    if _db is not None:
        try:
            rows = _db.get_trades(limit=None, mode=None)
            if rows:
                # Find most recent non-shadow row with capital_after
                for row in rows:
                    if row.get("mode") != "shadow" and row.get("capital_after") is not None:
                        return float(row["capital_after"])
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

app.add_middleware(GZipMiddleware, minimum_size=1000)

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
    my_prob_now_ts: str | None = None  # ISO timestamp of snapshot when my_prob_now was captured
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
    exit_reason: Literal["take_profit", "stop_loss", "manual", "won", "lost"] = "won"


class PortfolioOut(BaseModel):
    cash_usdc: float
    open_positions: list[PositionOut]
    closed_positions: list[ClosedPositionOut]
    updated_at: str


class SellPositionOut(BaseModel):
    token_id: str
    status: Literal["sold", "no_fill"]
    order_id: str | None = None
    sell_price_cents: int | None = None
    shares: float | None = None
    pnl: float | None = None
    detail: str = ""


class DebWeightOut(BaseModel):
    model: str
    weight: float
    rmse_f: float
    n_samples: int  # sample_count from database: >= MIN_SAMPLES indicates calibrated, < MIN_SAMPLES indicates cold-start


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
    active_hours_local: list[int]
    enabled: bool
    yes_enabled: bool
    no_enabled: bool
    trade_count: int
    filled_count: int
    win_rate: float | None
    total_pnl: float
    last_trade_ts: str | None
    open_positions_count: int
    last_obs_ts: str | None
    status: Literal["active", "outside_hours", "no_data", "disabled"]


class PerfQuadrantOut(BaseModel):
    """Metrics for one (mode, side) quadrant of the performance matrix."""
    count: int
    win_rate: float | None
    pnl: float
    avg_entry_price: float | None
    days_of_data: int


class StationSidePerfOut(BaseModel):
    """Per-side breakdown for one mode (real or shadow)."""
    YES: PerfQuadrantOut
    NO: PerfQuadrantOut


class StationPerfOut(BaseModel):
    """2×2 performance matrix for a single station: {real, shadow} × {YES, NO}."""
    real: StationSidePerfOut
    shadow: StationSidePerfOut


class PromotionPrerequisitesOut(BaseModel):
    """Promotion gate status for a shadow station."""
    station: str
    city: str
    climb_rate: bool
    model_count: bool
    taf_coverage: bool
    secondary_obs: bool
    has_settled_loss: bool
    promotable: bool
    reason: str = ""


class PromotionBarOut(BaseModel):
    """Statistical promotion bar status for one shadow station+side (issue #559).

    Advisory only — reflects the settled shadow-trade data against the bar;
    it does not auto-promote and has no effect on the live entry gate.
    """
    station: str
    side: str
    n: int
    wins: int
    win_rate: float
    wilson_lower_bound: float
    breakeven_win_rate: float
    avg_entry_price_cents: float
    days_coverage: int
    price_valid: bool
    eligible: bool
    status: str
    reason: str
    excluded_certainty_shortcut_count: int = 0


class CopyCandidateOut(BaseModel):
    """One screened wallet's latest run, for the Candidates view table
    (epic F, issue #1146). Isolated from the weather strategy's own
    PositionOut/PromotionBarOut models — copy-trading is a separate
    dashboard tab (docs/design/copy-trading-architecture.md isolation
    requirement).
    """
    address: str
    window: str
    screened_at: str
    n_buy_trades: int
    n_resolved: int
    win_rate: float | None = None
    mean_roi: float | None = None
    median_roi: float | None = None
    mirrored_dollar_pnl: float | None = None
    flat_dollar_pnl: float | None = None
    flat_stake: float | None = None
    # Latest run's own stability check (stored at screening time — see
    # copy_wallet_screening.py::check_stability). Gates the Follow control.
    eligible_to_follow: bool
    # Independently recomputed instability flag (design spec — must be
    # visible in the table row, not just the detail panel): True whenever
    # check_stability() disagrees across the wallet's last two runs, which
    # by construction matches `not eligible_to_follow` for the latest run —
    # kept as its own field since the two are conceptually distinct (one
    # gates the Follow button, the other drives the warning badge) even
    # though they're computed from the same two rows.
    unstable: bool
    # Whether this wallet has a screening run before its latest one
    # (Designer review, PR #1152): a first-ever run has nothing to have
    # "swung" against yet, so `unstable` is conservatively True but the
    # table badge must not say "Unstable" for it -- that implies proven
    # instability, not "not yet tested twice". False only for a wallet on
    # its very first screening run.
    has_prior_run: bool
    followed: bool
    follow_status: str | None = None


class CopyCandidatesOut(BaseModel):
    """Single response for the Candidates view — one round-trip, not
    several (issue #1146 acceptance criteria)."""
    candidates: list[CopyCandidateOut]
    slots_remaining: int
    max_followed: int
    active_follow_count: int


class CopyWalletScreeningRunOut(BaseModel):
    """One historical screening run, for the detail-panel sparkline."""
    window: str
    screened_at: str
    n_resolved: int
    median_roi: float | None = None
    eligible_to_follow: bool


class CopyWalletHistoryOut(BaseModel):
    address: str
    runs: list[CopyWalletScreeningRunOut]  # newest first


class CopyFollowRequest(BaseModel):
    stake: float | None = None  # defaults to COPY_DEFAULT_FLAT_STAKE_USD


class CopyFollowResultOut(BaseModel):
    success: bool
    message: str


class CopyFollowedWalletOut(BaseModel):
    """One row of the Followed Wallets view's table (epic F, issue #1147)."""
    address: str
    stake_per_trade: float
    status: Literal["active", "paused"]
    paused_reason: str | None = None
    added_at: str
    n_settled: int
    realized_pnl_usd: float


class CopyFollowedWalletsOut(BaseModel):
    """Single response for the Followed Wallets view — table rows plus the
    summary-strip aggregates, one round-trip (mirrors CopyCandidatesOut's
    issue #1146 design)."""
    wallets: list[CopyFollowedWalletOut]
    active_count: int
    paused_count: int
    aggregate_pnl_usd: float
    n_settled_total: int


class CopyPauseRequest(BaseModel):
    reason: str


class CopyStakeUpdateRequest(BaseModel):
    stake: float


class CopyOpenPositionOut(BaseModel):
    """One open copy-trading position, for the Positions & P&L view's open
    positions table (epic F, issue #1148). Isolated from the weather
    strategy's own PositionOut (issue #1100 isolation requirement) --
    deliberately a different, copy-trading-specific shape, not a shared
    model."""
    id: int
    address: str
    market: str
    outcome_index: int
    entry_price: float
    stake_usd: float
    entry_ts: str
    signal_id: int


class CopySettledPnlPointOut(BaseModel):
    """One settled copy-trading position, for the realized-P&L-over-time
    chart. Raw rows, not a server-side date-bucketed aggregate -- see
    ``copy_trading_positions()``'s docstring for why."""
    address: str
    market: str
    settled_at: str
    settled_pnl_usd: float
    stake_usd: float


class CopyWalletPnlBreakdownOut(BaseModel):
    """One wallet's row in the per-wallet P&L breakdown table. Carries both
    the realized figures (always present) and the backtest-comparison
    figures (``None`` when the wallet has no screening row to compare
    against) in a single row, so the frontend's backtest-comparison toggle
    (issue #1148 acceptance criteria) just shows/hides columns rather than
    re-fetching."""
    address: str
    n_settled: int
    realized_pnl_usd: float
    projected_flat_dollar_pnl: float | None = None
    divergence_usd: float | None = None
    divergence_pct: float | None = None


class CopyPnlTotalOut(BaseModel):
    n_settled: int
    realized_pnl_usd: float


class CopyBacktestComparisonTotalOut(BaseModel):
    n_wallets: int
    n_settled: int
    realized_pnl_usd: float
    projected_flat_dollar_pnl: float
    divergence_usd: float
    divergence_pct: float | None = None


class CopyPositionsOut(BaseModel):
    """Single response for the Positions & P&L view — one round-trip,
    mirroring CopyCandidatesOut/CopyFollowedWalletsOut's design (issue
    #1148 acceptance criteria)."""
    open_positions: list[CopyOpenPositionOut]
    realized_pnl_history: list[CopySettledPnlPointOut]  # settled rows, oldest first
    per_wallet: list[CopyWalletPnlBreakdownOut]
    total: CopyPnlTotalOut
    backtest_total: CopyBacktestComparisonTotalOut


class CopySignalOut(BaseModel):
    """One copy_signals row, for a position's source-signal click-through
    (issue #1148 acceptance criteria)."""
    id: int
    address: str
    market: str
    outcome_index: int | None = None
    source_price: float
    source_trade_id: str | None = None
    detected_at: str
    order_placed: bool
    fill_price: float | None = None
    size_usd: float | None = None
    skip_reason: str | None = None


class CopyActivityEventOut(BaseModel):
    """One normalized Activity Feed event (epic F, story F4, issue #1149)
    -- shaped server-side so the frontend never has to reconcile two
    different row shapes (``copy_signals`` vs ``copy_wallets_followed``)
    itself, matching this file's established shaping-happens-in-the-
    endpoint pattern (see ``copy_trading_positions()``'s docstring).

    ``event_type`` is one of ``"order_placed"``, ``"order_skipped"``, or
    ``"wallet_paused"`` -- a ``copy_signals`` row is always a terminal
    outcome (``order_placed`` is set once, at insert time; there is no
    separate "signal detected then later placed/skipped" transition to
    represent, see ``copy_signal_loop.py``), so it maps to exactly one of
    the first two. ``market``/``outcome_index``/``source_price``/
    ``fill_price``/``size_usd``/``skip_reason``/``signal_id`` are only
    populated for signal-derived events; ``paused_reason`` only for
    ``wallet_paused`` events.
    """
    event_type: str
    ts: str
    address: str
    market: str | None = None
    outcome_index: int | None = None
    source_price: float | None = None
    fill_price: float | None = None
    size_usd: float | None = None
    skip_reason: str | None = None
    paused_reason: str | None = None
    signal_id: int | None = None


class CopyActivityFeedOut(BaseModel):
    """Single response for the Activity Feed view — reverse-chronological
    (issue #1149 acceptance criteria)."""
    events: list[CopyActivityEventOut]


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
    readiness_date: str | None = None


# ---------------------------------------------------------------------------
# EMOS helpers
# ---------------------------------------------------------------------------

_EMOS_MIN_SETTLED_DAYS = 60

_CITY_TO_STATION: dict[str, str] = {
    city: station for station, _lat, _lon, city, *_ in STATIONS
}

_STATION_TO_CITY: dict[str, str] = {v: k for k, v in _CITY_TO_STATION.items()}

# EMOS promotion-cohort bookkeeping (issue #765): stations whose METAR feed is
# chronically dead (METAR_SKIP_STATIONS, issue #732) are excluded here so they
# stop surfacing as phantom "EMOS regressions" in the cohort listing/ledger —
# they aren't being evaluated at all, just frozen at a stale calibration. This
# is narrower than DISABLED_STATIONS/training_eligible=false: those also cover
# cities with a healthy feed held for data-quality reasons (e.g. Shenzhen),
# which must stay visible in the cohort pending their own promotion decision.
_EMOS_COHORT_STATIONS = [s for s in STATIONS if s[0] not in METAR_SKIP_STATIONS]


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

    readiness_date = None
    if settled < _EMOS_MIN_SETTLED_DAYS and _db is not None:
        reset_ts_str = _db.get_config("model_forecast_log_reset_at")
        if reset_ts_str:
            try:
                from datetime import datetime, timedelta, timezone
                reset_ts = datetime.fromisoformat(reset_ts_str)
                forecast_rows = _db.get_forecast_log_by_lead(station, city, since_date="2000-01-01", lead_hours=24)
                forecast_count = len(forecast_rows)
                days_needed = max(_EMOS_MIN_SETTLED_DAYS - forecast_count, _EMOS_MIN_SETTLED_DAYS - settled)
                readiness_date = (reset_ts + timedelta(days=days_needed)).strftime("%Y-%m-%d")
            except Exception:
                pass

    return EmosCityStatus(
        city=city,
        metar=station,
        effective_mode=effective_mode,
        shadow=shadow,
        primary=primary,
        settled_days_available=settled,
        min_settled_days_required=_EMOS_MIN_SETTLED_DAYS,
        readiness_date=readiness_date,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db_open_positions_enrichment() -> dict[str, dict]:
    """Return DB open_positions keyed by token_id for quick lookup.

    Calls _db.get_open_positions() and returns a mapping of
    {token_id: {station, bracket_low, bracket_high, predicted_price}}.
    Returns an empty dict if _db is None or if the query fails.
    """
    if _db is None:
        return {}
    try:
        rows = _db.get_open_positions()
    except Exception:
        logger.warning("_db_open_positions_enrichment: DB query failed", exc_info=True)
        return {}
    return {
        row["token_id"]: {
            "station": row.get("station", ""),
            "bracket_low": row.get("bracket_low", 0.0),
            "bracket_high": row.get("bracket_high", 0.0),
            "predicted_price": row.get("predicted_price"),
        }
        for row in rows
        if row.get("token_id")
    }


def _parse_snapshots(records) -> "dict[tuple[str, float, float], tuple[float, str]]":
    """Parse snapshots into a (station, bracket_low, bracket_high) → (p_yes, ts) dict.

    snapshots.jsonl is append-only and chronologically ordered, so iterating
    forward and overwriting the dict yields the most-recent p_yes and timestamp
    for each (station, bracket) pair.  *records* is an iterator over already-parsed
    JSON dicts spanning every rotated source file.
    """
    result: dict[tuple[str, float, float], tuple[float, str]] = {}
    try:
        for r in records:
            station = r.get("station") or ""
            bl = r.get("bracket_low")
            bh = r.get("bracket_high")
            py = r.get("p_yes")
            ts = r.get("ts") or ""
            if station and bl is not None and bh is not None and py is not None:
                result[(station, float(bl), float(bh))] = (float(py), ts)
    except OSError as e:
        logger.warning("snapshots.jsonl read error: %s", e)
    return result


def _latest_model_probs() -> "dict[tuple[str, float, float], tuple[float, str]]":
    """Latest model p_yes per (station, bracket_low, bracket_high).

    Reads across every rotated snapshots file (legacy plain + dated) and
    re-parses only when any source's mtime or size changes.
    """
    return _jsonl_cache_get_rotated(SNAPSHOTS_JSONL, _parse_snapshots)


def _parse_position_snap_probs(records) -> "dict[tuple[str, float, float, str], tuple[float, str]]":
    """Parse position_snapshots.jsonl into a prob map keyed by (station, bl, bh, side).

    Used as a dashboard fallback for held positions when the station is outside
    its STATION_ACTIVE_HOURS scanner window — the always-on re-pricer (issue #425)
    writes to position_snapshots.jsonl 24/7, so this cache stays fresh overnight.
    """
    result: dict[tuple[str, float, float, str], tuple[float, str]] = {}
    try:
        for r in records:
            station = r.get("station") or ""
            bl = r.get("bracket_low")
            bh = r.get("bracket_high")
            py = r.get("p_yes_now")
            ts = r.get("ts") or ""
            side = str(r.get("side") or "NO").upper()
            if station and bl is not None and bh is not None and py is not None:
                result[(station, float(bl), float(bh), side)] = (float(py), ts)
    except OSError as e:
        logger.warning("position_snapshots.jsonl read error (prob parse): %s", e)
    return result


def _latest_position_snap_probs() -> "dict[tuple[str, float, float, str], tuple[float, str]]":
    """Cached read of position_snapshots.jsonl for dashboard fallback (issue #425)."""
    return _jsonl_cache_get_rotated(POSITION_SNAPSHOTS_JSONL, _parse_position_snap_probs)


# ---------------------------------------------------------------------------
# live_trades.jsonl — single-pass parser + mtime cache (issue #170)
# Three callers (_trades_file_enrichment, _stopped_positions,
# _settled_jsonl_positions) previously each opened and scanned the entire
# file independently.  _parse_live_trades() does one pass, building all three
# data structures, and the result is cached by mtime/size.
# ---------------------------------------------------------------------------

def _parse_live_trades(records) -> "tuple[dict, list, list]":
    """Single pass over live_trades records across every rotated source.

    Args:
        records: iterator of already-parsed JSON dicts (chronological order).

    Returns:
        (enrichment_dict, stopped_list, settled_list) where:
        - enrichment_dict: most recent filled record per asset_id (for _trades_file_enrichment)
        - stopped_list: list of ClosedPositionOut for outcome='sold' records
        - settled_list: list of ClosedPositionOut for settled filled records (pnl present)
    """
    enrichment: dict[str, dict] = {}
    stopped: list[ClosedPositionOut] = []
    settled: list[ClosedPositionOut] = []
    try:
        for r in records:
            outcome = r.get("outcome")

            if outcome == "filled":
                # enrichment dict: last filled record per asset_id wins
                asset_id = r.get("asset_id") or r.get("no_token_id") or ""
                if asset_id:
                    enrichment[asset_id] = r

                # settled positions: filled + pnl present
                if "pnl" in r:
                    token_id = str(r.get("no_token_id") or r.get("asset_id") or "")
                    entry_cents = int(r.get("price_cents") or r.get("entry_price_cents") or 50)
                    pnl = float(r["pnl"])
                    is_win = pnl > 0
                    exit_cents = 100 if is_win else 0
                    size_eur = float(r.get("size_eur") or 0)
                    shares = float(r.get("shares") or (
                        size_eur / (entry_cents / 100) if entry_cents else 0
                    ))
                    settled.append(ClosedPositionOut(
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
                        exit_reason="won" if pnl > 0 else "lost",
                    ))

            elif outcome == "sold":
                exit_cents = int(r.get("price_cents") or 0)
                entry_cents = int(r.get("entry_price_cents") or exit_cents)
                shares = float(r.get("shares") or 0)
                pnl = float(r.get("pnl") or 0)
                trigger = str(r.get("trigger") or "")
                if trigger.startswith("take_profit@"):
                    exit_reason = "take_profit"
                elif trigger.startswith("stop_loss@"):
                    exit_reason = "stop_loss"
                elif trigger.startswith("manual@"):
                    exit_reason = "manual"
                else:
                    exit_reason = "won" if pnl > 0 else "lost"
                entry_side = str(r.get("entry_side") or "NO").upper()
                side = "YES" if entry_side == "YES" else "NO"
                stopped.append(ClosedPositionOut(
                    question=str(r.get("question") or ""),
                    station=str(r.get("station") or ""),
                    side=side,
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
        logger.warning("live_trades.jsonl parse error: %s", e)
    return enrichment, stopped, settled


def _cached_live_trades() -> "tuple[dict, list, list]":
    """Return cached (enrichment, stopped, settled) from live_trades.

    Reads across every rotated source for live_trades.jsonl (legacy plain
    file + dated .jsonl / .jsonl.gz) and re-parses whenever any source's
    mtime or size changes.
    """
    return _jsonl_cache_get_rotated(LIVE_TRADES_JSONL, _parse_live_trades)


def _trades_file_enrichment() -> dict[str, dict]:
    """Return the most recent filled record per asset_id from live_trades.jsonl.

    Used as a durable fallback when a token is settled and removed from open_positions
    but still in the wallet during the redemption window — the JSONL file persists
    across trader restarts and always carries predicted_price.

    Reads from the mtime-keyed cache: live_trades.jsonl is parsed at most once
    per file change across all three callers in _positions_from_wallet().
    """
    enrichment, _, _ = _cached_live_trades()
    return enrichment


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

    Reads from the mtime-keyed cache: live_trades.jsonl is parsed at most once
    per file change across all three callers in _positions_from_wallet().
    """
    _, stopped, _ = _cached_live_trades()
    return stopped


def _settled_jsonl_positions() -> list[ClosedPositionOut]:
    """Return settled hold-to-expiry trades from live_trades.jsonl.

    NOTE (issue #617): superseded by _db_settled_positions() for the live
    dashboard render path — _positions_from_wallet() no longer calls this.
    settle_live_trades() (#609) writes pnl/settled_at directly onto the trades
    row and no longer patches it back into live_trades.jsonl (the write-back
    helper, _enrich_jsonl_with_settlements(), was removed from settle.py), so
    this will return an empty list for any trade settled after that change.
    Kept only so historical/rotated JSONL files with pre-#617 enrichment can
    still be parsed if ever needed; not otherwise load-bearing.

    settle.py used to write pnl/actual_high/yes_won back to outcome='filled'
    records after each market resolves.  The presence of a 'pnl' field was the
    signal that settlement had been recorded.

    Records that also have an outcome='sold' sibling (stop-loss / take-profit
    exits) are already captured by _stopped_positions() — settle.py skipped
    writing pnl back to those filled records, so they never appeared here.

    Reads from the mtime-keyed cache: live_trades.jsonl is parsed at most once
    per file change across all three callers in _positions_from_wallet().
    """
    _, _, settled = _cached_live_trades()
    return settled


def _db_settled_positions() -> list[ClosedPositionOut]:
    """Return held-to-expiry settled live positions from the trades table (#617).

    Replaces _settled_jsonl_positions() as the source feeding
    _positions_from_wallet()'s closed_positions list: settle_live_trades()
    (#609) writes pnl/settled_at directly onto the trades row, so this reads
    that instead of depending on live_trades.jsonl.

    Early exits (outcome='sold', stop-loss/take-profit/manual) are NOT covered
    here — those remain sourced from live_trades.jsonl via _stopped_positions()
    / _cached_live_trades(), because order_manager writes those JSONL records
    directly at sell time, independently of settle.py, so they are unaffected
    by the removal of the settle.py JSONL write-back and still carry a usable
    token_id for the dashboard's per-position chart.

    Known limitation: the matching open_positions row is deleted by settle.py
    at settlement time (db.close_position(order_id)), so these entries never
    carry a token_id (no chart link) or the original market `question` text —
    the frontend already falls back to a synthesized question string from
    station/side/bracket when `question` is empty (see buildQuestion() in
    static/index.html), and to a plain (non-expandable) closed-position row
    when token_id is empty, both pre-existing fallbacks unaffected by this
    change. Deliberately does not call the Gamma API to look up the real
    question text here, to avoid adding synchronous per-row HTTP calls to the
    /api/portfolio request path.

    Returns:
        (positions, condition_ids) where condition_ids is the set of ``0x...``
        market condition IDs (the trades.ticker value for real markets) among
        the settled rows. _positions_from_wallet() uses this in place of a
        token_id lookup (unavailable post-settlement, see above) to skip
        Polymarket wallet rows that are still ``redeemable`` for a market we
        have already settled from the DB — otherwise that market would be
        double-counted via the wallet API's unreliable curPrice-based
        win/loss guess (see the comment at the wallet loop's redeemable
        branch in _positions_from_wallet()).
    """
    if _db is None:
        return [], set()
    try:
        rows = _db.get_settled_live_trades()
    except Exception:
        logger.warning("_db_settled_positions: DB query failed", exc_info=True)
        return [], set()

    result: list[ClosedPositionOut] = []
    condition_ids: set[str] = set()
    for r in rows:
        try:
            entry_cents = int(r.get("actual_price") or 50)
            pnl = float(r["pnl"]) if r.get("pnl") is not None else 0.0
            is_win = pnl > 0
            size_eur = r.get("size_eur")
            if size_eur is None:
                size_eur = r.get("capital_before") or 0
            shares = float(size_eur) / (entry_cents / 100) if entry_cents else 0.0
            result.append(ClosedPositionOut(
                question="",
                station=str(r.get("station") or ""),
                side=str(r.get("side") or "NO"),
                bracket_low=float(r.get("bracket_low") or 0.0),
                bracket_high=float(r.get("bracket_high") or 0.0),
                entry_price=entry_cents,
                exit_price=100 if is_win else 0,
                pnl=pnl,
                shares=round(shares, 4),
                closed_at=str(r.get("end_date") or r.get("ts") or ""),
                token_id="",
                exit_reason="won" if is_win else "lost",
            ))
            ticker = str(r.get("ticker") or "")
            if ticker.startswith("0x"):
                condition_ids.add(ticker)
        except Exception:
            logger.warning(
                "_db_settled_positions: skipping malformed row id=%s",
                r.get("id"), exc_info=True,
            )
    return result, condition_ids


def _nws_forecast_for_title(title: str) -> float | None:
    """Return today's NWS forecast high (°F) for the city mentioned in *title*.

    NOTE: This function is preserved for backward compatibility.  New callers
    should prefer ``_nws_for_question()`` which deduplicates NWS HTTP calls
    across multiple positions within a single request via a per-request cache.
    """
    t = title.lower()
    for city, (lat, lon) in _CITY_COORDS.items():
        if city.lower() in t:
            try:
                return fetch_nws_forecast_high(lat, lon)
            except Exception:
                return None
    return None


def _midpoint_cents(token_id: str, fallback_cents: int) -> int:
    """Single-token midpoint for a given token."""
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

    enrichment = _db_open_positions_enrichment()
    # Single cached pass over live_trades.jsonl for enrichment + early-exit
    # (sold) positions, which are still written directly by order_manager at
    # sell time and unaffected by issue #617. Held-to-expiry settlements no
    # longer come from this parse — see _db_settled_positions() below.
    jsonl_enrichment, stopped_list, _unused_settled = _cached_live_trades()
    # Issue #617: held-to-expiry settlements now come from the trades table
    # directly (settle_live_trades() writes pnl/settled_at onto the row) --
    # settle.py no longer patches these back into live_trades.jsonl.
    settled_list, db_settled_condition_ids = _db_settled_positions()
    snap_probs = _latest_model_probs()
    # Fallback prob source for stations outside scanner active-hours window (#425).
    # The always-on re-pricer writes position_snapshots.jsonl 24/7; we use it
    # here so the dashboard shows a live model fair-value line overnight.
    pos_snap_probs = _latest_position_snap_probs()

    # Per-request NWS city cache: fetch each city's forecast at most once per
    # request regardless of how many open positions mention that city (issue #170).
    _nws_city_cache: dict[str, "float | None"] = {}

    def _nws_for_question(question: str) -> "float | None":
        t = question.lower()
        for city, (lat, lon) in _CITY_COORDS.items():
            if city.lower() in t:
                if city not in _nws_city_cache:
                    try:
                        _nws_city_cache[city] = fetch_nws_forecast_high(lat, lon)
                    except Exception:
                        _nws_city_cache[city] = None
                return _nws_city_cache[city]
        return None

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

    # JSONL/DB-driven outcomes (stopped + settled) are authoritative for
    # win/loss because settle.py records the realised pnl against actual_high
    # vs the bracket.  The Polymarket Data API reports curPrice=0 for BOTH
    # winning and losing redeemable tokens once the orderbook is gone, which
    # would otherwise label every unredeemed winner as a -initial_value loss.
    # Build the covered-position sets up front so the wallet redeemable
    # branch can defer to them.
    #
    # stopped_list (early exits) still carries a real token_id -- those JSONL
    # records are written directly by order_manager at sell time, unaffected
    # by issue #617.  settled_list (held-to-expiry, now DB-sourced) no longer
    # carries a token_id (the open_positions row is deleted at settlement
    # time) -- it's matched instead by market condition ID via
    # db_settled_condition_ids, since trades.ticker stores the same 0x
    # condition ID the wallet API reports as `conditionId` for a position.
    jsonl_settled_tokens: set[str] = {
        p.token_id for p in stopped_list if p.token_id
    }

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
            # Resolved market.  Skip if JSONL/DB already carries an
            # authoritative outcome for this position — that record will be
            # appended below and gets the correct win/loss + pnl from
            # settle.py.  Early exits are matched by token_id (jsonl_settled_
            # tokens); DB-sourced held-to-expiry settlements no longer carry
            # a token_id post-settlement, so those are matched by market
            # condition ID instead (db_settled_condition_ids) — see the
            # comment above where these sets are built.
            condition_id = str(row.get("conditionId") or "")
            if token_id in jsonl_settled_tokens or (
                condition_id and condition_id in db_settled_condition_ids
            ):
                continue
            # No JSONL/DB record (e.g. manual trade not made by the bot): fall
            # back to Polymarket Data API.  Note: curPrice is unreliable for
            # resolved markets without an active orderbook — it's 0 even for
            # winning tokens — so this branch may still misclassify rare
            # non-bot winners.  Logging the limitation here in case future
            # work needs a better signal (resolution event or redemption).
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
                exit_reason="won" if is_win else "lost",
            ))
        else:
            # Active position
            market_prob = midpoints.get(token_id, avg_entry_cents)
            station_key = str(enrich.get("station", ""))
            bracket_low_key = float(enrich.get("bracket_low", 0.0))
            bracket_high_key = float(enrich.get("bracket_high", 0.0))
            my_prob_now: int | None = None
            my_prob_now_ts: str | None = None
            snap_key = (station_key, bracket_low_key, bracket_high_key)
            if station_key and snap_key in snap_probs:
                py, ts = snap_probs[snap_key]
                raw_now = py * 100 if side == "YES" else (1 - py) * 100
                my_prob_now = max(1, min(99, round(raw_now)))
                my_prob_now_ts = ts
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
                my_prob_now_ts=my_prob_now_ts,
                edge=round(my_prob - market_prob, 2),
                shares=round(shares, 4),
                invested=round(shares * avg_price, 2),
                current_value=round(float(row.get("currentValue") or shares * avg_price / 100), 2),
                target_value=round(shares * 1.00, 2),
                forecast_high_f=_nws_for_question(question),
                token_id=token_id,
            ))

    # Append JSONL/DB-authoritative outcomes.  The wallet loop above already
    # skipped any redeemable row whose token_id is in jsonl_settled_tokens or
    # whose market condition ID is in db_settled_condition_ids, so there are
    # no collisions with the wallet-produced entries.  stopped_list entries
    # always carry a token_id (JSONL, unaffected by #617) and are deduped
    # defensively against each other; settled_list entries (DB-sourced, #617)
    # never carry a token_id post-settlement, so they are always appended --
    # dedup for those already happened above via db_settled_condition_ids.
    seen_token_ids = {p.token_id for p in closed_positions if p.token_id}
    for p in stopped_list:
        if not p.token_id or p.token_id not in seen_token_ids:
            closed_positions.append(p)
            if p.token_id:
                seen_token_ids.add(p.token_id)
    for p in settled_list:
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
    """Per-station trade count, win rate, and total PnL (excluding shadow rows)."""
    all_trades = _dashboard_load_trades()
    by_station: dict[str, list[dict]] = defaultdict(list)
    for t in all_trades:
        station = t.get("station", "UNKNOWN")
        by_station[station].append(t)

    result: dict[str, Any] = {}
    for station, station_trades in sorted(by_station.items()):
        filled = [t for t in station_trades if t.get("outcome") in ("filled", "sold") and t.get("pnl") is not None]
        wins = sum(1 for t in filled if float(t.get("pnl", 0)) > 0)
        wr = compute_win_rate(len(filled), wins)
        win_rate = round(wr, 4) if wr is not None else None
        # Exclude shadow rows from total_pnl
        total_pnl = sum(float(t.get("pnl", 0)) for t in station_trades if t.get("mode") != "shadow")
        result[station] = {
            "trade_count": len(station_trades),
            "filled_count": len(filled),
            "win_rate": win_rate,
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


@app.post("/api/positions/{token_id}/sell", response_model=SellPositionOut)
def sell_position(token_id: str) -> SellPositionOut:
    """Operator-triggered immediate sell of an open position.

    Sells the full remaining size at the current best bid (immediate-or-cancel),
    mirroring the bot's take-profit / stop-loss exit recording.  On a fill the
    position is closed in the DB, a 'sold' trade is recorded (so it moves to the
    closed list), and the bot's exit loops skip the token for the rest of the
    session.  Returns ``status='no_fill'`` (HTTP 200) when the order did not
    cross at market so the UI can offer a retry rather than surface an error.
    """
    if not token_id:
        raise HTTPException(status_code=400, detail="token_id required")
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    try:
        from src.execution.auth import get_clob_client
        trader = LiveTrader(get_clob_client(), _db)
    except Exception as e:
        logger.error("[manual-sell] trading client unavailable: %s", e)
        raise HTTPException(status_code=503, detail="Trading client unavailable")

    ts = datetime.now(timezone.utc).isoformat()
    try:
        result = order_manager.manual_sell_position(trader, token_id, ts, db=_db)
    except Exception as e:
        logger.warning("[manual-sell] failed for %s...: %s", token_id[:14], e)
        raise HTTPException(status_code=502, detail=f"Sell failed: {e}")

    status = result.get("status")
    if status == "not_found":
        raise HTTPException(status_code=404, detail=result.get("detail", "No open position found"))
    if status == "already_sold":
        raise HTTPException(status_code=409, detail=result.get("detail", "Position already sold"))
    if status == "no_fill":
        return SellPositionOut(token_id=token_id, status="no_fill", detail=result.get("detail", ""))

    return SellPositionOut(
        token_id=token_id,
        status="sold",
        order_id=result.get("order_id"),
        sell_price_cents=result.get("sell_price_cents"),
        shares=result.get("shares"),
        pnl=result.get("pnl"),
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
    formatted as an ISO date string.  ``n_samples`` is the sample count from
    the model_weights table: >= MIN_SAMPLES indicates calibrated weights with
    real RMSE, < MIN_SAMPLES indicates cold-start with rmse=0.0.
    """
    normalised = city.title()
    rows = _db.get_model_weights(normalised)
    if not rows:
        raise HTTPException(status_code=404, detail="no DEB data for city")

    # updated_at = max date across all rows (rows are already ordered DESC)
    updated_at = rows[0]["date"]

    # model_weights stores one row per (model, date), so a city accumulates a
    # fresh row per model every day.  The panel is a single current snapshot, so
    # collapse to the most-recent row per model.  Rows are ordered by date DESC,
    # so the first occurrence of each model is its newest.  (See #607.)
    seen: set[str] = set()
    latest = []
    for row in rows:
        if row["model"] in seen:
            continue
        seen.add(row["model"])
        latest.append(row)

    weights = [
        DebWeightOut(
            model=row["model"],
            weight=round(float(row["weight"]), 4),
            rmse_f=round(float(row["rmse"]), 4),
            n_samples=row.get("sample_count", 0) or 0,  # coerce None to 0
        )
        for row in latest
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
    station_tz: str = "UTC",
) -> str:
    """Derive the station status string from config and latest observation timestamp.

    Logic:
      - disabled  → station is in DISABLED_STATIONS
      - outside_hours → enabled AND current local hour outside active_hours_local
      - no_data   → enabled AND in active hours AND (no obs or obs > 2h ago)
      - active    → enabled AND in active hours AND obs within 2h
    """
    if not enabled:
        return "disabled"

    active_hours = STATION_ACTIVE_HOURS.get(metar)
    now_utc = datetime.now(timezone.utc)
    try:
        import zoneinfo
        local_now = now_utc.astimezone(zoneinfo.ZoneInfo(station_tz))
    except Exception:
        local_now = now_utc
    current_hour = local_now.hour

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

    # Read DISABLED_STATIONS at request time (env var may change between restarts).
    # Merge with DB overrides: a station is disabled when it is in DISABLED_STATIONS
    # OR when station_overrides.yes_enabled=0 AND no_enabled=0.  A DB override with
    # yes_enabled=True or no_enabled=True lifts the env-var disable for that side.
    db_overrides: "dict[str, dict]" = {}
    if _db is not None:
        try:
            db_overrides = _db.get_all_station_overrides()
        except Exception:
            logger.warning("[stations/overview] failed to fetch station_overrides", exc_info=True)

    result: list[StationOverviewOut] = []
    for station_cfg in STATIONS:
        metar, lat, lon, city, _res_station, unit, tz = station_cfg
        if metar in db_overrides:
            # "enabled" for the overview = at least one side is live
            override = db_overrides[metar]
            yes_enabled = override["yes_enabled"]
            no_enabled = override["no_enabled"]
            enabled = yes_enabled or no_enabled
        else:
            yes_enabled = metar not in DISABLED_STATIONS
            no_enabled = yes_enabled
            enabled = yes_enabled
        last_obs_ts = last_obs_map.get(metar)
        open_positions_count = open_pos_map.get(metar, 0)
        stats = trade_stats_map.get(metar, {})

        status = _derive_station_status(metar, enabled, last_obs_ts, tz)

        active_hours = STATION_ACTIVE_HOURS.get(metar, (0, 24))

        result.append(StationOverviewOut(
            metar=metar,
            city=city,
            lat=lat,
            lon=lon,
            unit=unit,
            timezone=tz,
            active_hours_local=list(active_hours),
            enabled=enabled,
            yes_enabled=yes_enabled,
            no_enabled=no_enabled,
            trade_count=stats.get("trade_count", 0),
            filled_count=stats.get("filled_count", 0),
            win_rate=stats.get("win_rate"),
            total_pnl=stats.get("total_pnl", 0.0),
            last_trade_ts=stats.get("last_trade_ts"),
            open_positions_count=open_positions_count,
            last_obs_ts=last_obs_ts,
            status=status,
        ))

    _stations_overview_cache["ts"] = now
    _stations_overview_cache["data"] = result
    return result


def _build_perf_quadrant(trades: list[dict]) -> PerfQuadrantOut:
    """Compute the 5 metrics for a single (mode, side) quadrant.

    Args:
        trades: Pre-filtered list of trade dicts for this quadrant.

    Returns:
        PerfQuadrantOut with count, win_rate, pnl, avg_entry_price, days_of_data.
    """
    count = len(trades)
    if count == 0:
        return PerfQuadrantOut(
            count=0,
            win_rate=None,
            pnl=0.0,
            avg_entry_price=None,
            days_of_data=0,
        )

    total_pnl = sum(float(t.get("pnl") or 0.0) for t in trades)

    # win_rate: fraction of settled trades (outcome='filled'/'sold' and pnl != 0) where pnl > 0
    settled = [t for t in trades if t.get("outcome") in ("filled", "sold") and float(t.get("pnl") or 0.0) != 0.0]
    if settled:
        wins = sum(1 for t in settled if float(t.get("pnl") or 0.0) > 0)
        win_rate: float | None = wins / len(settled)
    else:
        win_rate = None

    # avg_entry_price: average actual_price for settled trades
    settled_prices = [float(t["actual_price"]) for t in settled if t.get("actual_price") is not None]
    avg_entry_price: float | None = sum(settled_prices) / len(settled_prices) if settled_prices else None

    # days_of_data: distinct calendar days (UTC date of ts)
    days: set[str] = set()
    for t in trades:
        ts = t.get("ts", "")
        if isinstance(ts, str) and len(ts) >= 10:
            days.add(ts[:10])
    days_of_data = len(days)

    return PerfQuadrantOut(
        count=count,
        win_rate=win_rate,
        pnl=total_pnl,
        avg_entry_price=avg_entry_price,
        days_of_data=days_of_data,
    )


@app.get("/api/stations/perf", response_model=dict[str, StationPerfOut])
def stations_perf() -> dict[str, StationPerfOut]:
    """Return a 2×2 performance matrix per station: {real, shadow} × {YES, NO}.

    Real quadrant contains trades where mode != 'shadow'.
    Shadow quadrant contains trades where mode == 'shadow'.
    Each quadrant reports: count, win_rate, pnl, avg_entry_price, days_of_data.

    Only stations with at least one trade in ANY quadrant are included.
    Falls back to load_live_trades() (JSONL) when the database is unavailable.
    """
    # Load all trades from DB (all modes), fall back to JSONL
    if _db is not None:
        try:
            all_trades = _db.get_trades(limit=None, mode=None)
        except Exception:
            logger.warning("[stations/perf] failed to load trades from DB", exc_info=True)
            all_trades = list(reversed(load_live_trades()))
    else:
        all_trades = list(reversed(load_live_trades()))

    # Group trades by station then by mode-bucket then by side
    # mode-bucket: 'shadow' if mode=='shadow', else 'real'
    by_station: dict[str, dict[str, dict[str, list[dict]]]] = defaultdict(
        lambda: {
            "real":   {"YES": [], "NO": []},
            "shadow": {"YES": [], "NO": []},
        }
    )

    for trade in all_trades:
        station = trade.get("station")
        if not station:
            continue
        side = trade.get("side", "").upper()
        if side not in ("YES", "NO"):
            continue
        mode_val = trade.get("mode", "")
        bucket = "shadow" if mode_val == "shadow" else "real"
        by_station[station][bucket][side].append(trade)

    result: dict[str, StationPerfOut] = {}
    for station, buckets in by_station.items():
        result[station] = StationPerfOut(
            real=StationSidePerfOut(
                YES=_build_perf_quadrant(buckets["real"]["YES"]),
                NO=_build_perf_quadrant(buckets["real"]["NO"]),
            ),
            shadow=StationSidePerfOut(
                YES=_build_perf_quadrant(buckets["shadow"]["YES"]),
                NO=_build_perf_quadrant(buckets["shadow"]["NO"]),
            ),
        )

    return result


@app.get("/api/positions/{token_id}/snapshots")
def position_snapshots(token_id: str) -> list[dict]:
    """Return time-series price/model snapshots for a position identified by its NO token_id.

    Each record contains: ts, market_bid, fair_value, current_high, latest_temp.
    Reads across every rotated source for position_snapshots.jsonl (legacy
    plain file + dated .jsonl / .jsonl.gz) so charts keep working after log
    rotation creates the dated daily files.
    """
    result = []
    try:
        for r in iter_rotated_jsonl(POSITION_SNAPSHOTS_JSONL):
            if r.get("no_token_id") == token_id or r.get("yes_token_id") == token_id:
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


@app.get("/analytics/intraday")
def analytics_intraday(station: str, date: str) -> list[dict]:
    """Return the intra-day snapshot series for *station* × *date* from analytics.db.

    Query params:
        station: METAR code (e.g. KORD)
        date: ISO date YYYY-MM-DD (e.g. 2026-06-15)

    Returns [] when no data exists for the requested station/date (200, not 404).
    Sources data exclusively from data/analytics.db via ArchiveDatabase.
    """
    with ArchiveDatabase() as db:
        return db.get_snapshot_series(station, date)


@app.get("/api/weather-health")
def weather_health_status() -> dict:
    """Report the weather feed health captured by the most recent poll.

    Lets the dashboard show a banner naming which stations are degraded and
    why (outside active window, no METAR, parse error...).  When every station
    is degraded, _build_weather() returns empty and the per-position model
    (fair-value) line pauses while the market-bid line keeps updating.
    """
    health = weather_health or []
    degraded = [h for h in health if h.get("status") != "ok"]
    ok_count = len(health) - len(degraded)
    return {
        "ok_count": ok_count,
        "degraded": degraded,
        "all_degraded": bool(health) and ok_count == 0,
        "last_poll_ts": last_poll_ts,
    }


@app.get("/api/forecast-capture-health")
def forecast_capture_health() -> dict:
    """Dashboard health tile for the forecast-capture staleness watchdog (issue #717).

    Reports whether MAX(model_forecast_log.logged_at) is older than the
    configurable FORECAST_CAPTURE_STALENESS_THRESHOLD_HOURS threshold. This is
    a read-only, on-demand check of the DB -- the same underlying logic that
    src/scripts/run.py calls each poll tick to emit an ERROR into logs/bot.log
    (see src/monitoring/capture_staleness.py).
    """
    from src.monitoring.capture_staleness import get_capture_health
    return get_capture_health(_db)


# ---------------------------------------------------------------------------
# EMOS management endpoints
# ---------------------------------------------------------------------------

@app.get("/api/emos/status", response_model=list[EmosCityStatus])
def emos_status() -> list[EmosCityStatus]:
    """Return EMOS calibration state for all cities in the EMOS cohort.

    For each city, reports the effective mode, shadow/primary calibration rows,
    settled day count, and the minimum required before promotion. Stations with
    a chronically dead METAR feed (METAR_SKIP_STATIONS, issue #765) are excluded
    — they aren't being evaluated, so they're not part of the cohort.
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
    for station, _lat, _lon, city, *_ in _EMOS_COHORT_STATIONS:
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

    # Promotion-time guardrail: the interim overconfidence cap (issue #305) keeps
    # clamping p_yes even after EMOS is primary, throttling the calibrated
    # probabilities EMOS just produced. Surface it loudly at the moment of
    # promotion so the post-EMOS cleanup (issue #420) is not forgotten.
    if MODEL_PROB_CAP < 1.0:
        logger.warning(
            "[emos] %s promoted to emos_primary while MODEL_PROB_CAP=%.3f still "
            "clamps p_yes to [%.3f, %.3f]. Review/loosen this interim guardrail "
            "(#305) now that EMOS is primary — see post-EMOS cleanup issue #420.",
            canonical_city, MODEL_PROB_CAP,
            round(1.0 - MODEL_PROB_CAP, 4), MODEL_PROB_CAP,
        )

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
def emos_mark_ready(city: str, all_tracks: bool = False) -> EmosCityStatus:
    """Toggle ready_for_promotion (0 ↔ 1) on the shadow row for a city.

    This is an administrative flag the operator sets after reviewing CRPS scores.
    Returns 409 if no shadow row exists for the city.

    By default (issue #696) this scopes to the ACTIVE track — the current
    forecast_source/sigma_source (from FORECAST_STACK/EMOS_SIGMA_SOURCE
    bot_config) at the default lead_hours=24 bin the promotion gate actually
    reads. Pass ?all_tracks=true to reproduce the pre-#696 city-wide behavior
    (every forecast_source/sigma_source/lead_hours row for the city flips
    together) — an explicit operator opt-in, not the default.

    Returns the updated EmosCityStatus for the city.
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    canonical_city, station = _resolve_city(city)

    new_val = _db.toggle_emos_ready_for_promotion(canonical_city, all_tracks=all_tracks)
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
# EMOS shadow scaffolding status endpoint
# ---------------------------------------------------------------------------

@app.get("/api/emos-shadow/status")
def emos_shadow_status() -> list[dict]:
    """Per-city EMOS shadow status: sample count, mean CRPS, ready_for_promotion.

    ``legacy_mean_crps`` and ``crps_delta`` (issue #667) surface the
    EMOS-vs-legacy promotion evidence: ``crps_delta`` = legacy_mean_crps -
    mean_crps, positive meaning EMOS is beating the legacy baseline (lower
    CRPS is better). Either field is None until both model_modes have at
    least one logged row for the city.

    Stations with a chronically dead METAR feed (METAR_SKIP_STATIONS, issue
    #765) are excluded from this cohort listing — they aren't being
    evaluated, so they no longer surface as phantom "EMOS regressions".
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")
    try:
        results = []
        for station_cfg in _EMOS_COHORT_STATIONS:
            city = station_city(station_cfg)
            n_samples = _db.get_emos_crps_count(city)
            status = _db.get_emos_shadow_city_status(city)
            # ready_for_promotion is ALWAYS False for automated queries (never set to 1)
            results.append({
                "city": city,
                "n_samples": n_samples,
                "mean_crps": status["mean_crps"],
                "legacy_mean_crps": status["legacy_mean_crps"],
                "crps_delta": status["crps_delta"],
                "model_weights_snapshot": status["model_weights_snapshot"],
                "ready_for_promotion": False,
            })
        return results
    except Exception as e:
        logger.warning("[emos-shadow] status query failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Station toggle — enable/disable via DB-persisted overrides
# ---------------------------------------------------------------------------

# Build a quick set of valid METAR codes from STATIONS for 404 checks.
_KNOWN_METARS: frozenset[str] = frozenset(s[0] for s in STATIONS)


@app.post("/api/stations/{metar}/toggle")
def station_toggle(metar: str) -> dict:
    """Toggle both yes_enabled and no_enabled for *metar* (DB-persisted, back-compat).

    - Returns 404 for METAR codes not present in the STATIONS config.
    - Reads the current effective enabled state (env baseline merged with DB
      overrides), flips BOTH sides together, and writes the new value.
    - Invalidates the stations overview cache so the next GET sees the new state.

    Response: {"metar": str, "enabled": bool}
    """
    metar_upper = metar.upper()
    if metar_upper not in _KNOWN_METARS:
        raise HTTPException(status_code=404, detail=f"Unknown METAR code: {metar!r}")

    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    # Determine current effective enabled state (both sides)
    db_override = _db.get_station_override(metar_upper)
    if db_override is not None:
        current_enabled = db_override["yes_enabled"] and db_override["no_enabled"]
    else:
        current_enabled = metar_upper not in DISABLED_STATIONS

    new_enabled = not current_enabled
    _db.set_station_override(metar_upper, yes_enabled=new_enabled, no_enabled=new_enabled)

    # Invalidate the overview cache so the next request reflects the change.
    _stations_overview_cache["ts"] = 0.0
    _stations_overview_cache["data"] = None

    return {"metar": metar_upper, "enabled": new_enabled}


@app.post("/api/stations/{metar}/toggle/yes")
def station_toggle_yes(metar: str) -> dict:
    """Toggle yes_enabled for *metar* (DB-persisted).

    Flips only the YES side, leaving NO side unchanged.

    Response: {"metar": str, "yes_enabled": bool, "no_enabled": bool}
    """
    metar_upper = metar.upper()
    if metar_upper not in _KNOWN_METARS:
        raise HTTPException(status_code=404, detail=f"Unknown METAR code: {metar!r}")

    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    db_override = _db.get_station_override(metar_upper)
    if db_override is not None:
        current_yes = db_override["yes_enabled"]
        current_no = db_override["no_enabled"]
    else:
        base_enabled = metar_upper not in DISABLED_STATIONS
        current_yes = base_enabled and (metar_upper not in SHADOW_STATIONS_YES)
        current_no = base_enabled and (metar_upper not in SHADOW_STATIONS_NO)

    new_yes = not current_yes
    _db.set_station_override(metar_upper, yes_enabled=new_yes, no_enabled=current_no)

    _stations_overview_cache["ts"] = 0.0
    _stations_overview_cache["data"] = None

    return {"metar": metar_upper, "yes_enabled": new_yes, "no_enabled": current_no}


@app.post("/api/stations/{metar}/toggle/no")
def station_toggle_no(metar: str) -> dict:
    """Toggle no_enabled for *metar* (DB-persisted).

    Flips only the NO side, leaving YES side unchanged.

    Response: {"metar": str, "yes_enabled": bool, "no_enabled": bool}
    """
    metar_upper = metar.upper()
    if metar_upper not in _KNOWN_METARS:
        raise HTTPException(status_code=404, detail=f"Unknown METAR code: {metar!r}")

    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    db_override = _db.get_station_override(metar_upper)
    if db_override is not None:
        current_yes = db_override["yes_enabled"]
        current_no = db_override["no_enabled"]
    else:
        base_enabled = metar_upper not in DISABLED_STATIONS
        current_yes = base_enabled and (metar_upper not in SHADOW_STATIONS_YES)
        current_no = base_enabled and (metar_upper not in SHADOW_STATIONS_NO)

    new_no = not current_no
    _db.set_station_override(metar_upper, yes_enabled=current_yes, no_enabled=new_no)

    _stations_overview_cache["ts"] = 0.0
    _stations_overview_cache["data"] = None

    return {"metar": metar_upper, "yes_enabled": current_yes, "no_enabled": new_no}


# ---------------------------------------------------------------------------
# Promotion prerequisites API
# ---------------------------------------------------------------------------

@app.get("/api/promotion-prerequisites", response_model=list[PromotionPrerequisitesOut])
def promotion_prerequisites() -> list[PromotionPrerequisitesOut]:
    """Return promotion gate status for all shadow stations.

    Checks each shadow station for data-coverage prerequisites before promotion to live:
    1. Climb-rate history for current month
    2. ≥2 distinct forecast models (trailing window)
    3. ≥60 TAF windows (trailing 30 days, configurable)
    4. Secondary observation source (non-metar)
    5. At least one settled loss (rejects pure wins)

    Returns a list of stations with their gate status and promotability.
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    from src.model.promotion_gate import check_promotion_prerequisites

    result = []

    for station_tuple in STATIONS:
        station = station_tuple[0]
        city = station_tuple[3]

        gate_status = check_promotion_prerequisites(_db, station, city)
        result.append(PromotionPrerequisitesOut(
            station=station,
            city=city,
            climb_rate=gate_status['climb_rate'],
            model_count=gate_status['model_count'],
            taf_coverage=gate_status['taf_coverage'],
            secondary_obs=gate_status['secondary_obs'],
            has_settled_loss=gate_status['has_settled_loss'],
            promotable=gate_status['promotable'],
            reason=gate_status['reason'],
        ))

    return result


@app.get("/api/promotion-bar", response_model=list[PromotionBarOut])
def promotion_bar() -> list[PromotionBarOut]:
    """Statistical promotion bar (issue #559) — advisory only, never auto-promotes.

    Per shadow station+side, reports the settled trade count, win rate, Wilson
    score lower bound (95% by default, configurable via
    PROMOTION_WILSON_CONFIDENCE), and the break-even win rate implied by the
    average entry price plus the fee model (src/strategy/fee.py). A row is
    "eligible" iff n >= PROMOTION_MIN_SETTLED_TRADES AND the Wilson lower
    bound clears break-even.

    Supersedes the ad-hoc thresholds proposed in issue #80 (>=5 trades / 100%
    win rate / >=3 days) as the sole promotion path. This endpoint and the
    underlying computation never write to station_overrides or any other
    live-trading config — a human must act on the "eligible"/"status" signal.
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    from src.model.promotion_gate import compute_promotion_bar

    rows = compute_promotion_bar(_db)
    return [PromotionBarOut(**row) for row in rows]


# ---------------------------------------------------------------------------
# Copy-trading API — Candidates view (epic F #1143, story F1 #1146)
# ---------------------------------------------------------------------------

@app.get("/api/copy-trading/candidates", response_model=CopyCandidatesOut)
def copy_trading_candidates() -> CopyCandidatesOut:
    """Every screened wallet's latest run, plus follow/instability/slots
    context — one response model so the frontend needs a single round-trip
    (issue #1146 acceptance criteria), not several.

    Default sort is `median_roi` descending, matching the backtest report's
    own methodology — never `mirrored_dollar_pnl`/`flat_dollar_pnl` (the
    exact mistake the spike already made once, see the design spec).
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    from src.scripts.copy_wallet_promotion import active_follow_count
    from src.scripts.copy_wallet_screening import check_stability

    live_cfg = get_live_config(_db)
    max_followed = live_cfg["COPY_MAX_WALLETS_FOLLOWED"]
    active_count = active_follow_count(_db)
    slots_remaining = max(max_followed - active_count, 0)

    followed_status = {w["address"]: w["status"] for w in _db.get_followed_wallets()}
    # One batched query for every address's previous run, instead of an
    # N+1 get_recent_wallet_screenings() call per candidate row — this
    # endpoint is polled every 5 minutes by every open dashboard tab.
    previous_runs = _db.get_previous_wallet_screenings()

    candidates = []
    for row in _db.get_latest_wallet_screenings():
        # Reuse check_stability() against this wallet's own last two runs
        # rather than re-deriving the sign/tolerance logic (acceptance
        # criteria).
        previous = previous_runs.get(row["address"])
        unstable = not check_stability(row, previous)

        candidates.append(CopyCandidateOut(
            address=row["address"],
            window=row["window"],
            screened_at=row["screened_at"],
            n_buy_trades=row["n_buy_trades"],
            n_resolved=row["n_resolved"],
            win_rate=row["win_rate"],
            mean_roi=row["mean_roi"],
            median_roi=row["median_roi"],
            mirrored_dollar_pnl=row["mirrored_dollar_pnl"],
            flat_dollar_pnl=row["flat_dollar_pnl"],
            flat_stake=row["flat_stake"],
            eligible_to_follow=bool(row["eligible_to_follow"]),
            unstable=unstable,
            has_prior_run=previous is not None,
            followed=row["address"] in followed_status,
            follow_status=followed_status.get(row["address"]),
        ))

    candidates.sort(
        key=lambda c: c.median_roi if c.median_roi is not None else float("-inf"),
        reverse=True,
    )

    return CopyCandidatesOut(
        candidates=candidates,
        slots_remaining=slots_remaining,
        max_followed=max_followed,
        active_follow_count=active_count,
    )


@app.get(
    "/api/copy-trading/wallets/{address}/history",
    response_model=CopyWalletHistoryOut,
)
def copy_trading_wallet_history(address: str, limit: int = 10) -> CopyWalletHistoryOut:
    """A single wallet's recent screening runs, newest first — feeds the
    Candidates detail panel's median-ROI sparkline (issue #1146)."""
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    rows = _db.get_recent_wallet_screenings(address, limit=limit)
    return CopyWalletHistoryOut(
        address=address,
        runs=[
            CopyWalletScreeningRunOut(
                window=r["window"],
                screened_at=r["screened_at"],
                n_resolved=r["n_resolved"],
                median_roi=r["median_roi"],
                eligible_to_follow=bool(r["eligible_to_follow"]),
            )
            for r in rows
        ],
    )


@app.post(
    "/api/copy-trading/wallets/{address}/follow",
    response_model=CopyFollowResultOut,
)
def copy_trading_follow_wallet(address: str, req: CopyFollowRequest) -> CopyFollowResultOut:
    """Follow a screened wallet — wraps copy_wallet_promotion.py::follow()
    directly rather than re-deriving its roster-full / ineligible /
    already-followed refusal checks (issue #1146 acceptance criteria).

    follow() only communicates success/refusal via a print() + return code
    (it's a CLI function first) — captured here via redirect_stdout instead
    of changing its signature, since that signature is covered by
    src/tests/test_copy_wallet_promotion.py's own return-code assertions.
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    from src.scripts.copy_wallet_promotion import follow

    live_cfg = get_live_config(_db)
    max_followed = live_cfg["COPY_MAX_WALLETS_FOLLOWED"]
    max_stake = live_cfg["COPY_MAX_EXPOSURE_PER_WALLET_USD"]
    stake = req.stake if req.stake is not None else live_cfg["COPY_DEFAULT_FLAT_STAKE_USD"]

    # Trading-safety guardrail: an operator-submitted stake is otherwise
    # unbounded above (follow() itself only rejects <=0 / non-finite).
    # Reuse the existing per-wallet exposure cap rather than inventing a
    # new config key for this.
    if stake > max_stake:
        return CopyFollowResultOut(
            success=False,
            message=(
                f"Refusing to follow {address}: stake ${stake:.2f} exceeds "
                f"COPY_MAX_EXPOSURE_PER_WALLET_USD (${max_stake:.2f})."
            ),
        )

    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            code = follow(_db, address, stake, max_followed)
    except Exception as e:  # noqa: BLE001 — surface as a structured refusal, never a bare 500
        logger.exception("copy_trading_follow_wallet: follow() raised for %s", address)
        return CopyFollowResultOut(success=False, message=f"Follow failed: {e}")

    return CopyFollowResultOut(success=(code == 0), message=buf.getvalue().strip())


# ---------------------------------------------------------------------------
# Copy-trading API — Followed Wallets view (epic F #1143, story F2 #1147)
# ---------------------------------------------------------------------------

@app.get("/api/copy-trading/followed-wallets", response_model=CopyFollowedWalletsOut)
def copy_trading_followed_wallets() -> CopyFollowedWalletsOut:
    """Every followed wallet plus its running realized P&L, plus aggregate
    figures for the summary strip (issue #1147 acceptance criteria) — one
    response model, mirroring the Candidates view's single-round-trip
    design (CopyCandidatesOut, issue #1146).
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    pnl_by_wallet = {row["address"]: row for row in _db.get_copy_realized_pnl_by_wallet()}
    total = _db.get_copy_realized_pnl_total()

    wallets = []
    active_count = 0
    paused_count = 0
    for w in _db.get_followed_wallets():
        if w["status"] == "active":
            active_count += 1
        elif w["status"] == "paused":
            paused_count += 1

        pnl_row = pnl_by_wallet.get(w["address"])
        wallets.append(CopyFollowedWalletOut(
            address=w["address"],
            stake_per_trade=w["stake_per_trade"],
            status=w["status"],
            paused_reason=w.get("paused_reason"),
            added_at=w["added_at"],
            n_settled=pnl_row["n_settled"] if pnl_row else 0,
            realized_pnl_usd=pnl_row["total_pnl_usd"] if pnl_row else 0.0,
        ))

    # Most-recently-followed first — an operator managing the roster cares
    # most about wallets they just added, unlike the Candidates view (which
    # sorts by performance, per its own design spec).
    wallets.sort(key=lambda w: w.added_at, reverse=True)

    return CopyFollowedWalletsOut(
        wallets=wallets,
        active_count=active_count,
        paused_count=paused_count,
        aggregate_pnl_usd=total["total_pnl_usd"],
        n_settled_total=total["n_settled"],
    )


@app.post(
    "/api/copy-trading/wallets/{address}/pause",
    response_model=CopyFollowResultOut,
)
def copy_trading_pause_wallet(address: str, req: CopyPauseRequest) -> CopyFollowResultOut:
    """Pause a followed wallet — wraps copy_wallet_promotion.py::pause()
    directly rather than re-deriving its "must already be followed" guard
    (issue #1147 acceptance criteria, mirroring the follow endpoint's
    approach to reusing the CLI's own refusal logic).
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    from src.scripts.copy_wallet_promotion import pause

    reason = (req.reason or "").strip()
    if not reason:
        return CopyFollowResultOut(success=False, message="Pause requires a non-empty reason.")

    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            code = pause(_db, address, reason)
    except Exception as e:  # noqa: BLE001 — surface as a structured refusal, never a bare 500
        logger.exception("copy_trading_pause_wallet: pause() raised for %s", address)
        return CopyFollowResultOut(success=False, message=f"Pause failed: {e}")

    return CopyFollowResultOut(success=(code == 0), message=buf.getvalue().strip())


@app.post(
    "/api/copy-trading/wallets/{address}/resume",
    response_model=CopyFollowResultOut,
)
def copy_trading_resume_wallet(address: str) -> CopyFollowResultOut:
    """Resume a paused wallet — wraps copy_wallet_promotion.py::resume()
    directly, including its "must be paused" and roster-full refusal
    checks (issue #1147 acceptance criteria).
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    from src.scripts.copy_wallet_promotion import resume

    live_cfg = get_live_config(_db)
    max_followed = live_cfg["COPY_MAX_WALLETS_FOLLOWED"]

    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            code = resume(_db, address, max_followed)
    except Exception as e:  # noqa: BLE001 — surface as a structured refusal, never a bare 500
        logger.exception("copy_trading_resume_wallet: resume() raised for %s", address)
        return CopyFollowResultOut(success=False, message=f"Resume failed: {e}")

    return CopyFollowResultOut(success=(code == 0), message=buf.getvalue().strip())


@app.post(
    "/api/copy-trading/wallets/{address}/unfollow",
    response_model=CopyFollowResultOut,
)
def copy_trading_unfollow_wallet(address: str) -> CopyFollowResultOut:
    """Unfollow a wallet — permanently DELETEs its copy_wallets_followed row
    (see Database.delete_followed_wallet's docstring for why DELETE rather
    than a terminal status was chosen, issue #1147 acceptance criteria).

    copy_wallet_promotion.py has no CLI equivalent to wrap (only
    follow/pause/resume exist there), so this endpoint owns its own
    "must already be followed" refusal check directly, following this
    section's existing style rather than duplicating it into the CLI
    module for a single caller.

    Never touches copy_positions — any open positions for this wallet are
    left exactly as they are (surfaced to the operator via the frontend's
    confirm-dialog copy, not silently implied here).
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    known = {w["address"] for w in _db.get_followed_wallets()}
    if address not in known:
        return CopyFollowResultOut(
            success=False,
            message=f"Refusing to unfollow {address}: not a followed wallet.",
        )

    try:
        _db.delete_followed_wallet(address)
    except Exception as e:  # noqa: BLE001 — surface as a structured refusal, never a bare 500
        logger.exception("copy_trading_unfollow_wallet: delete failed for %s", address)
        return CopyFollowResultOut(success=False, message=f"Unfollow failed: {e}")

    return CopyFollowResultOut(
        success=True,
        message=f"Unfollowed {address}. Existing open positions are not affected.",
    )


@app.patch(
    "/api/copy-trading/wallets/{address}/stake",
    response_model=CopyFollowResultOut,
)
def copy_trading_update_wallet_stake(
    address: str, req: CopyStakeUpdateRequest
) -> CopyFollowResultOut:
    """Edit a followed wallet's per-trade stake (issue #1147 acceptance
    criteria's "edit-stake control"). No CLI equivalent exists to wrap
    (copy_wallet_promotion.py's --stake only sets the *initial* stake at
    --follow time), so this reuses --follow's own
    COPY_MAX_EXPOSURE_PER_WALLET_USD guard directly rather than leaving the
    new stake unbounded above.
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    known = {w["address"] for w in _db.get_followed_wallets()}
    if address not in known:
        return CopyFollowResultOut(
            success=False,
            message=f"Refusing to update stake for {address}: not a followed wallet.",
        )

    if not math.isfinite(req.stake) or req.stake <= 0:
        return CopyFollowResultOut(
            success=False,
            message=f"Stake must be a finite positive number in USD, got {req.stake!r}.",
        )

    live_cfg = get_live_config(_db)
    max_stake = live_cfg["COPY_MAX_EXPOSURE_PER_WALLET_USD"]
    if req.stake > max_stake:
        return CopyFollowResultOut(
            success=False,
            message=(
                f"Refusing to set stake ${req.stake:.2f} for {address}: exceeds "
                f"COPY_MAX_EXPOSURE_PER_WALLET_USD (${max_stake:.2f})."
            ),
        )

    _db.update_followed_wallet_stake(address, req.stake)
    return CopyFollowResultOut(
        success=True,
        message=f"Updated {address} stake to ${req.stake:.2f}/trade.",
    )


# ---------------------------------------------------------------------------
# Copy-trading API — Positions & P&L view (epic F #1143, story F3 #1148)
# ---------------------------------------------------------------------------

@app.get("/api/copy-trading/positions", response_model=CopyPositionsOut)
def copy_trading_positions() -> CopyPositionsOut:
    """Open positions, realized-P&L history, per-wallet breakdown (with
    backtest-comparison figures), and both totals for the Positions & P&L
    view (issue #1148 acceptance criteria) — one response, mirroring
    CopyCandidatesOut/CopyFollowedWalletsOut's single-round-trip design.

    Every figure here is read through the existing, already-tested
    ``get_open_copy_positions`` / ``get_copy_realized_pnl_by_wallet`` /
    ``get_copy_realized_pnl_total`` / ``get_settled_copy_positions`` /
    ``copy_backtest_comparison.get_wallet_backtest_comparison`` /
    ``get_backtest_comparison_total`` methods — no new aggregation SQL.

    Date-bucketed chart data (acceptance criteria's open question): no
    server-side "realized P&L across ALL wallets, bucketed by date" method
    exists — ``get_copy_realized_pnl_total_for_date`` only covers a single
    UTC day (built for the daily-loss circuit breaker) and adding a ranged
    version would be new aggregation SQL duplicating
    ``get_settled_copy_positions``'s existing per-row read. So this returns
    raw settled-position rows (oldest first) via that method, and the
    frontend buckets them by day for the chart (see
    ``_renderCopyPositionsChart`` in index.html).

    Per-wallet breakdown covers every wallet with an active follow OR any
    settled history — not just currently-followed wallets — because
    unfollowing a wallet does not delete its ``copy_positions`` rows (see
    ``copy_trading_unfollow_wallet``'s docstring), and those positions'
    realized P&L should stay visible here.
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    from src.data.copy_backtest_comparison import (
        get_backtest_comparison_total,
        get_wallet_backtest_comparison,
    )

    open_positions = [
        CopyOpenPositionOut(
            id=row["id"],
            address=row["address"],
            market=row["market"],
            outcome_index=row["outcome_index"],
            entry_price=row["entry_price"],
            stake_usd=row["stake_usd"],
            entry_ts=row["entry_ts"],
            signal_id=row["signal_id"],
        )
        for row in _db.get_open_copy_positions()
    ]

    # Union of currently-followed addresses and any address with settled
    # history, so a wallet unfollowed after settling positions still shows
    # up in the breakdown (see docstring above).
    addresses = {row["address"] for row in _db.get_copy_realized_pnl_by_wallet()}
    addresses |= {w["address"] for w in _db.get_followed_wallets()}

    realized_pnl_history: list[CopySettledPnlPointOut] = []
    per_wallet: list[CopyWalletPnlBreakdownOut] = []
    for address in addresses:
        for row in _db.get_settled_copy_positions(address):
            realized_pnl_history.append(CopySettledPnlPointOut(
                address=row["address"],
                market=row["market"],
                settled_at=row["settled_at"],
                settled_pnl_usd=row["settled_pnl_usd"],
                stake_usd=row["stake_usd"],
            ))

        comparison = get_wallet_backtest_comparison(_db, address)
        per_wallet.append(CopyWalletPnlBreakdownOut(**comparison))

    realized_pnl_history.sort(key=lambda p: p.settled_at)
    # Best-performing wallet first, matching the Candidates view's
    # performance-first default sort (never a $ PnL-only sort there, but
    # this table *is* the P&L table, so realized_pnl_usd is the natural key).
    per_wallet.sort(key=lambda w: w.realized_pnl_usd, reverse=True)

    total = _db.get_copy_realized_pnl_total()
    backtest_total = get_backtest_comparison_total(_db)

    return CopyPositionsOut(
        open_positions=open_positions,
        realized_pnl_history=realized_pnl_history,
        per_wallet=per_wallet,
        total=CopyPnlTotalOut(
            n_settled=total["n_settled"],
            realized_pnl_usd=total["total_pnl_usd"],
        ),
        backtest_total=CopyBacktestComparisonTotalOut(**backtest_total),
    )


@app.get("/api/copy-trading/signals/{signal_id}", response_model=CopySignalOut)
def copy_trading_signal(signal_id: int) -> CopySignalOut:
    """One ``copy_signals`` row, for the Positions & P&L view's
    position → source-signal click-through (issue #1148 acceptance
    criteria: "shows its source signal (which wallet, which trade it
    copied)", joined via ``copy_positions.signal_id``).
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    row = _db.get_copy_signal(signal_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No signal with id={signal_id}")

    return CopySignalOut(
        id=row["id"],
        address=row["address"],
        market=row["market"],
        outcome_index=row["outcome_index"],
        source_price=row["source_price"],
        source_trade_id=row["source_trade_id"],
        detected_at=row["detected_at"],
        order_placed=bool(row["order_placed"]),
        fill_price=row["fill_price"],
        size_usd=row["size_usd"],
        skip_reason=row["skip_reason"],
    )


# ---------------------------------------------------------------------------
# Copy-trading API — Activity Feed view (epic F #1143, story F4 #1149)
# ---------------------------------------------------------------------------

_ACTIVITY_EVENT_TYPES = {"order_placed", "order_skipped", "wallet_paused"}


@app.get("/api/copy-trading/activity-feed", response_model=CopyActivityFeedOut)
def copy_trading_activity_feed(
    address: "str | None" = None, event_type: "str | None" = None
) -> CopyActivityFeedOut:
    """Reverse-chronological feed of signal-detection and wallet-pause
    events (issue #1149 acceptance criteria), merged server-side from two
    different-shaped sources into one normalized ``CopyActivityEventOut``
    shape -- matching this file's established shape-it-in-the-endpoint
    pattern (see ``copy_trading_positions()``'s docstring), not a
    frontend-side reconciliation.

    Sources:
    - ``copy_signals`` (via ``get_copy_signals``): every detected signal,
      each a terminal ``order_placed`` or ``order_skipped`` outcome (see
      ``CopyActivityEventOut``'s docstring for why there is no separate
      "signal detected" event).
    - ``copy_wallets_followed`` rows with ``status='paused'``: surfaced as
      a synthetic ``wallet_paused`` event at ``paused_at``. A row with
      ``paused_at IS NULL`` (a pre-#1145 legacy pause, before the
      timestamp column existed) has no real timestamp to place it at, so
      it is silently skipped rather than crashing or guessing a time --
      exactly the acceptance criteria's required behavior.

    *address* and *event_type* both filter the merged result (query
    params); an *event_type* outside ``_ACTIVITY_EVENT_TYPES`` simply
    yields no matches rather than a 400, matching this dashboard's
    generally lenient filter-param handling elsewhere.
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    events: list[CopyActivityEventOut] = []

    for row in _db.get_copy_signals(address=address):
        if row["order_placed"]:
            events.append(CopyActivityEventOut(
                event_type="order_placed",
                ts=row["detected_at"],
                address=row["address"],
                market=row["market"],
                outcome_index=row["outcome_index"],
                source_price=row["source_price"],
                fill_price=row["fill_price"],
                size_usd=row["size_usd"],
                signal_id=row["id"],
            ))
        else:
            events.append(CopyActivityEventOut(
                event_type="order_skipped",
                ts=row["detected_at"],
                address=row["address"],
                market=row["market"],
                outcome_index=row["outcome_index"],
                source_price=row["source_price"],
                skip_reason=row["skip_reason"],
                signal_id=row["id"],
            ))

    paused_wallets = _db.get_followed_wallets(status="paused")
    if address is not None:
        paused_wallets = [w for w in paused_wallets if w["address"] == address]
    for w in paused_wallets:
        if w.get("paused_at") is None:
            continue  # pre-#1145 legacy row -- no real timestamped source, skip it
        events.append(CopyActivityEventOut(
            event_type="wallet_paused",
            ts=w["paused_at"],
            address=w["address"],
            paused_reason=w.get("paused_reason"),
        ))

    if event_type is not None:
        events = [e for e in events if e.event_type == event_type]

    events.sort(key=lambda e: e.ts, reverse=True)

    return CopyActivityFeedOut(events=events)


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
    "MIN_FORECAST_BRACKET_MARGIN_F": {
        "description": "Minimum margin (°F) between forecast high and bracket boundary",
        "type": "float",
        "group": "strategy",
        "min": 0.0,
        "max": 10.0,
    },
    "ENVELOPE_SIGMA_CLIMB_FRACTION": {
        "description": "Model stddev floor as a fraction of the climb still to come — blocks near-certain claims early in the day (#652)",
        "type": "float",
        "group": "strategy",
        "min": 0.0,
        "max": 1.0,
    },
    "EMOS_DEFAULT_MODE": {
        "description": "EMOS deployment mode fallback when no calibration row exists",
        "type": "enum",
        "group": "strategy",
        "options": ["legacy", "emos_shadow", "emos_primary"],
    },
    "EMOS_MIN_SAMPLES_PROMOTION": {
        "description": "Minimum CRPS-logged shadow days before a city may serve emos_primary",
        "type": "int",
        "group": "strategy",
        "min": 10,
        "max": 200,
    },
    "RESIDUAL_CORRECTION_ENABLED": {
        "description": "Apply residual bias correction to model probability estimates",
        "type": "bool",
        "group": "strategy",
    },
    "SHADOW_MIN_EDGE_CENTS_YES": {
        "description": "Minimum YES edge for shadow-log entry (live YES uses MIN_EDGE_CENTS)",
        "type": "float", "group": "strategy", "min": 0.5, "max": 15.0,
    },
    "SHADOW_MIN_CONFIDENCE_YES": {
        "description": "Minimum p(YES) for shadow-log entry (live YES uses MIN_CONFIDENCE_YES)",
        "type": "float", "group": "strategy", "min": 0.5, "max": 0.95,
    },
    "SHADOW_MIN_PRICE_CENTS_YES": {
        "description": "Minimum YES ask for shadow-log entry (live YES uses MIN_PRICE_CENTS)",
        "type": "int", "group": "strategy", "min": 1, "max": 60,
    },
    "RANK_ON_RAW_PROB": {
        "description": "Rank/prioritize candidates by uncapped model probability instead of scan order (issue #551, stage 1). Entry gates are unaffected.",
        "type": "bool",
        "group": "strategy",
    },
    "ENABLE_LOW_MARKETS": {
        "description": "Scan LOW-direction ('lowest temperature in') markets, shadow-only. Off by default (issue #733 rollback: the bot focuses on daily-HIGH markets); flipping on restores the previous shadow-only LOW scan.",
        "type": "bool",
        "group": "strategy",
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
    "SIZING_MODE": {
        "description": "Position sizing mode: flat (fixed size) or kelly (quarter-Kelly, experimental)",
        "type": "str",
        "group": "position",
        "options": ["flat", "kelly"],
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
    "DEB_ENABLED": {
        "description": "Master switch — enable DEB weight computation and consumption (requires restart for envelope.py)",
        "type": "bool",
        "group": "forecast",
    },
    "USE_ENSEMBLE_SIGMA": {
        "description": "Use per-station GEFS ensemble spread (WeatherState.ensemble_sigma_f) instead of the fixed FORECAST_STDDEV_F in true_probability_yes and EMOS serving, when available (issue #448). Also the single source of truth for which emos_calibration sigma_source track ('fixed'/'ensemble') a retrain writes to and a live reader reads from (issue #799) — flipping this on an existing deployment requires an 'ensemble' retrain to have already produced calibration rows (EMOS_SIGMA_SOURCE is deprecated/no-op; do not use it).",
        "type": "bool",
        "group": "forecast",
    },
    "DEB_HRRR_COLD_START_FRACTION": {
        "description": "Weight multiplier for HRRR during cold-start (< DEB_MIN_SAMPLES pairs)",
        "type": "float",
        "group": "forecast",
        "min": 0.1,
        "max": 1.0,
    },
    "DEB_NBM_COLD_START_FRACTION": {
        "description": "Weight multiplier for NBM during cold-start (< DEB_MIN_SAMPLES pairs)",
        "type": "float",
        "group": "forecast",
        "min": 0.1,
        "max": 1.0,
    },
    "DEB_GROUP_WEIGHT_CAP": {
        "description": "Max combined DEB weight for the noaa_us channel group (NWS + HRRR + NBM)",
        "type": "float",
        "group": "forecast",
        "min": 0.3,
        "max": 1.0,
    },
    "DEB_ECMWF_COLD_START_FRACTION": {
        "description": "Weight multiplier for ECMWF during cold-start (< DEB_MIN_SAMPLES pairs)",
        "type": "float",
        "group": "forecast",
        "min": 0.1,
        "max": 1.0,
    },
    "DEB_ICON_COLD_START_FRACTION": {
        "description": "Weight multiplier for ICON-EU during cold-start (< DEB_MIN_SAMPLES pairs)",
        "type": "float",
        "group": "forecast",
        "min": 0.1,
        "max": 1.0,
    },
    "FORECAST_STACK": {
        "description": "Active forecast stack — controls which ingestion channels are live",
        "type": "enum",
        "group": "forecast",
        "options": ["baseline", "hrrr_nbm", "intl_ecmwf_icon", "full"],
    },
    "EMOS_SIGMA_SOURCE": {
        "description": "DEPRECATED (issue #799) — no longer read by the resolver; the active EMOS sigma track now derives directly from USE_ENSEMBLE_SIGMA. Setting this key has no effect. Kept only for backward compatibility with already-seeded bot_config rows.",
        "type": "enum",
        "group": "forecast",
        "options": ["fixed", "ensemble"],
        "hidden": True,
    },
    "PROMOTION_MIN_SETTLED_TRADES": {
        "description": "Minimum settled shadow trades required for promotion eligibility (issue #559, supersedes #80)",
        "type": "int",
        "group": "promotion",
        "min": 5,
        "max": 200,
    },
    "PROMOTION_WILSON_CONFIDENCE": {
        "description": "Confidence level for the Wilson score lower bound used by the promotion bar",
        "type": "float",
        "group": "promotion",
        "min": 0.5,
        "max": 0.999,
    },
    "NEXT_DAY_EVALUATION": {
        "description": "Evaluate a station's next-day market once today's own market is past MIN_MINUTES_TO_SETTLEMENT or absent (issue #687). Shadow-logged only (is_next_day=1) -- no live entries from next-day evaluation. Default off -- no live behaviour change until enabled.",
        "type": "bool",
        "group": "strategy",
    },
    "NEXT_DAY_SIGMA_MULTIPLIER": {
        "description": "Fallback sigma multiplier for next-day evaluation when no EMOS lead bin covers the market's lead time: sigma = FORECAST_STDDEV_F * this (issue #687). Unfitted starting estimate -- a matched calibration bin's own sigma is used unchanged.",
        "type": "float",
        "group": "forecast",
        "min": 1.0,
        "max": 5.0,
    },
    "FORECAST_CAPTURE_STALENESS_THRESHOLD_HOURS": {
        "description": "Alert when the forecast-capture job's most recent model_forecast_log write is older than this many hours (issue #717)",
        "type": "float",
        "group": "monitoring",
        "min": 1.0,
        "max": 48.0,
    },
    "COPY_TRADING_ENABLED": {
        "description": "Master switch — enable copy-trading wallet-following and order placement (issue #1115)",
        "type": "bool",
        "group": "copy_trading",
    },
    "COPY_DEFAULT_FLAT_STAKE_USD": {
        "description": "Default flat stake per copy-trade ($) — matches copy_wallet_screening.py's --flat-stake CLI default",
        "type": "float",
        "group": "copy_trading",
        "min": 1.0,
        "max": 100.0,
    },
    "COPY_MAX_WALLETS_FOLLOWED": {
        "description": "Maximum number of wallets simultaneously followed",
        "type": "int",
        "group": "copy_trading",
        "min": 1,
        "max": 50,
    },
    "COPY_MAX_EXPOSURE_PER_WALLET_USD": {
        "description": "Maximum total exposure ($) to a single followed wallet",
        "type": "float",
        "group": "copy_trading",
        "min": 1.0,
        "max": 1000.0,
    },
    "COPY_MAX_TOTAL_EXPOSURE_USD": {
        "description": "Maximum total exposure ($) across all followed wallets combined",
        "type": "float",
        "group": "copy_trading",
        "min": 1.0,
        "max": 5000.0,
    },
    "COPY_SIGNAL_POLL_INTERVAL_SECONDS": {
        "description": "Seconds between copy-signal poll cycles (src/scripts/copy_signal_loop.py, issue #1123) -- live-read every cycle, unlike Epic A's screening cadence",
        "type": "int",
        "group": "copy_trading",
        "min": 30,
        "max": 3600,
    },
    "COPY_DAILY_LOSS_LIMIT_USD": {
        "description": "Block new copy-signal execution for the rest of the UTC day once today's realized copy-trading P&L is <= -this value (issue #1139 circuit breaker)",
        "type": "float",
        "group": "copy_trading",
        "min": 1.0,
        "max": 1000.0,
    },
    "COPY_DRAWDOWN_STOP_PCT": {
        "description": "Block new copy-signal execution until manually cleared once cumulative realized P&L is a drawdown of at least this fraction of COPY_TRADING_CAPITAL_USD (issue #1139 circuit breaker)",
        "type": "float",
        "group": "copy_trading",
        "min": 0.01,
        "max": 1.0,
    },
    "COPY_LIVE_TRADING_ENABLED": {
        "description": "Master switch — enable LIVE copy-trading order placement (issue #1163). Independent of COPY_TRADING_ENABLED (paper); built ahead of the phase-7 go/no-go gate, defaults off",
        "type": "bool",
        "group": "copy_trading",
    },
    "COPY_LIVE_MAX_EXPOSURE_PER_WALLET_USD": {
        "description": "Maximum total LIVE exposure ($) to a single followed wallet — separate from COPY_MAX_EXPOSURE_PER_WALLET_USD (paper)",
        "type": "float",
        "group": "copy_trading",
        "min": 1.0,
        "max": 1000.0,
    },
    "COPY_LIVE_MAX_TOTAL_EXPOSURE_USD": {
        "description": "Maximum total LIVE exposure ($) across all followed wallets combined — separate from COPY_MAX_TOTAL_EXPOSURE_USD (paper)",
        "type": "float",
        "group": "copy_trading",
        "min": 1.0,
        "max": 5000.0,
    },
    "COPY_LIVE_DAILY_LOSS_LIMIT_USD": {
        "description": "Block new LIVE copy-signal execution for the rest of the UTC day once today's realized LIVE copy-trading P&L is <= -this value (issue #1175 live circuit breaker) — separate from COPY_DAILY_LOSS_LIMIT_USD (paper)",
        "type": "float",
        "group": "copy_trading",
        "min": 1.0,
        "max": 1000.0,
    },
    "COPY_LIVE_DRAWDOWN_STOP_PCT": {
        "description": "Block new LIVE copy-signal execution until manually cleared once cumulative realized LIVE P&L is a drawdown of at least this fraction of COPY_LIVE_CAPITAL_USD (issue #1175 live circuit breaker) — separate from COPY_DRAWDOWN_STOP_PCT (paper)",
        "type": "float",
        "group": "copy_trading",
        "min": 0.01,
        "max": 1.0,
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
    """Return all editable bot parameters with their current DB values, grouped by category.

    Hidden parameters (marked with "hidden": True in _CONFIG_META) are excluded from
    the response but remain in the system for backward compatibility.
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")
    live = get_live_config(_db)
    # Build nested dict grouped by category
    result: dict[str, dict] = {}
    for key in CONFIG_DEFAULTS:
        meta = _CONFIG_META.get(key, {})
        # Skip hidden parameters (e.g., deprecated/no-op fields)
        if meta.get("hidden", False):
            continue
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
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")
    key = req.key
    if key not in CONFIG_DEFAULTS:
        raise HTTPException(status_code=400, detail=f"Unknown config key: {key!r}")

    serialised, err = _validate_config_value(key, req.value)
    if err:
        raise HTTPException(status_code=400, detail=err)

    _db.set_config(key, serialised)

    return _build_param_entry(key, serialised)


# ---------------------------------------------------------------------------
# Residual stats endpoint (issue #307)
# ---------------------------------------------------------------------------

class ResidualStatsOut(BaseModel):
    """Rolling residual bias statistics for a single city."""
    city: str
    mean_signed_error: float | None
    rolling_mae: float | None
    sample_count: int
    correction_applied: bool
    live_suppressed: bool


@app.get("/api/residual-stats", response_model=list[ResidualStatsOut])
def residual_stats() -> list[ResidualStatsOut]:
    """Return rolling residual bias stats per city.

    For each configured city, returns:
    - mean_signed_error: mean(delta_f) over the trailing window (positive = warm bias)
    - rolling_mae: mean(|delta_f|) over the trailing window
    - sample_count: number of correction rows used
    - correction_applied: True when bias term was applied to corrected_mu_f
    - live_suppressed: True when MAE exceeds MAX_RESIDUAL_MAE_F_FOR_LIVE

    Returns an entry for every city, with null mean_signed_error/rolling_mae
    when insufficient data is available (< RESIDUAL_MIN_SAMPLES corrections).
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    result: list[ResidualStatsOut] = []
    seen_cities: set[str] = set()

    for _station, _lat, _lon, city, *_ in STATIONS:
        if city in seen_cities:
            continue
        seen_cities.add(city)

        try:
            stats = compute_residual_stats(city, _db)
        except Exception as exc:
            logger.warning("[residual-stats] failed for city=%s: %s", city, exc)
            stats = None

        if stats is not None:
            result.append(ResidualStatsOut(
                city=city,
                mean_signed_error=round(stats.mean_signed_error, 3),
                rolling_mae=round(stats.rolling_mae, 3),
                sample_count=stats.sample_count,
                correction_applied=stats.correction_applied,
                live_suppressed=stats.live_suppressed,
            ))
        else:
            result.append(ResidualStatsOut(
                city=city,
                mean_signed_error=None,
                rolling_mae=None,
                sample_count=0,
                correction_applied=False,
                live_suppressed=False,
            ))

    return result


# ---------------------------------------------------------------------------
# Per-station residual endpoint (issue #340)
# ---------------------------------------------------------------------------

class StationResidualOut(BaseModel):
    """Residual bias stats for one (station, source) pair within a METAR city."""
    station: str
    source: str
    mean_signed_error: float
    rolling_mae: float
    sample_count: int
    clamped_correction: float
    correction_applied: bool
    live_suppressed: bool
    last_obs_time: "str | None"
    scope: str


@app.get("/api/stations/{metar}/residual", response_model=list[StationResidualOut])
def station_residual(metar: str) -> list[StationResidualOut]:
    """Return per-(station, source) residual bias stats for the city behind *metar*.

    Finds the city name from the STATIONS config, calls
    ``compute_residual_stats_per_pair``, and enriches each entry with the most
    recent ``obs_time`` seen for that pair in the last 30 days.

    Returns 404 when the METAR code is not in the STATIONS config.
    Returns an empty list when no qualified pairs exist (no data / below
    min_samples).
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    # Resolve city from METAR
    city: "str | None" = None
    for row in STATIONS:
        if row[0] == metar:
            city = row[3]
            break
    if city is None:
        raise HTTPException(status_code=404, detail=f"Unknown METAR: {metar}")

    try:
        pairs = compute_residual_stats_per_pair(city, _db)
    except Exception as exc:
        logger.warning("[station-residual] failed for city=%s: %s", city, exc)
        pairs = []

    if not pairs:
        return []

    # Fetch the most recent obs_time per (station, source) pair in the last 30 days
    from datetime import date as _date, timedelta as _timedelta
    since_date = (_date.today() - _timedelta(days=30)).isoformat()
    last_obs_map: dict[tuple[str, str], "str | None"] = {}
    try:
        rows = _db._conn.execute(
            "SELECT station, source, MAX(obs_time) AS last_obs "
            "FROM intraday_corrections "
            "WHERE city=? AND date>=? "
            "GROUP BY station, source",
            (city, since_date),
        ).fetchall()
        for r in rows:
            last_obs_map[(r[0], r[1])] = r[2]
    except Exception as exc:
        logger.warning("[station-residual] last_obs query failed for city=%s: %s", city, exc)

    result: list[StationResidualOut] = []
    for stats in pairs:
        result.append(StationResidualOut(
            station=stats.station,
            source=stats.source,
            mean_signed_error=round(stats.mean_signed_error, 3),
            rolling_mae=round(stats.rolling_mae, 3),
            sample_count=stats.sample_count,
            clamped_correction=round(stats.clamped_correction, 3),
            correction_applied=stats.correction_applied,
            live_suppressed=stats.live_suppressed,
            last_obs_time=last_obs_map.get((stats.station, stats.source)),
            scope=stats.scope,
        ))
    return result


# ---------------------------------------------------------------------------
# Close-reason stats endpoint (issue #304)
# ---------------------------------------------------------------------------

@app.get("/api/close-reason-stats")
def close_reason_stats() -> list[dict]:
    """Return P&L, win rate, count, avg, and worst PnL grouped by close reason.

    Close reasons: take_profit, forced_exit, stop_loss, settled (legacy rows
    where close_reason IS NULL are grouped under 'settled').

    Reproduces the live version of the table from the 2026-06-16 analysis:
      | Close type | n | Win rate | Total P&L | Avg | Worst |
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")
    try:
        return _db.get_close_reason_stats()
    except Exception as e:
        logger.warning("[close-reason-stats] query failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/trade-costs/summary")
def trade_cost_summary(days: int = 30) -> dict:
    """Return cost-accounting totals for live closed trades over the trailing *days*.

    Fields:
      - period_days, trade_count, fee_populated_count
      - total_fee_eur, avg_fee_eur  (estimated_fee_cents summed/averaged, converted to EUR)
      - total_size_eur, total_pnl
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")
    try:
        return _db.get_trade_cost_summary(days=days)
    except Exception as e:
        logger.warning("[trade-costs] query failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/guardrail-events")
def guardrail_events() -> dict:
    """Return summary counts for forced-exit, cap, bias-correction, and
    entry-guard (issue #611) guardrail events.

    Forced exits come from trades.close_reason='forced_exit'; cap, correction,
    and entry-guard events come from the guardrail_events table.  All counts
    are zero-safe.
    """
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")
    try:
        stats = _db.get_guardrail_stats()
        fe = _db.get_forced_exit_stats()

        return {
            "forced_exits": {
                "total": fe["total"],
                "last_7d": fe["last_7d"],
                "by_station": fe["by_station"],
            },
            "cap_events": {
                "total": stats["cap_events"]["total"],
                "last_7d": stats["cap_events"]["last_7d"],
                "avg_delta_p": stats["cap_events"]["avg_delta"],
            },
            "correction_events": {
                "total": stats["correction_events"]["total"],
                "last_7d": stats["correction_events"]["last_7d"],
                "avg_delta_f": stats["correction_events"]["avg_delta"],
            },
            "entry_guard_blocks": {
                "total": stats["entry_guard_blocks"]["total"],
                "last_7d": stats["entry_guard_blocks"]["last_7d"],
            },
        }
    except Exception as e:
        logger.warning("[guardrail-events] query failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Edge Tab — GET /api/analysis/{station}
#
# Issue #757: repointed to read the persisted scan_decisions table (#756) --
# the numbers the scanner actually traded on -- instead of a parallel
# get_ensemble_distribution()/get_bracket_analysis() recompute that could
# disagree with the live decision. That recompute is demoted, not retired:
# get_ensemble_distribution() still has a live caller (src.strategy.scanner,
# which is how ensemble_mean/members/range land in scan_decisions in the
# first place); get_bracket_analysis() loses its only caller here and is left
# in place per the issue's "don't delete unless clearly dead" instruction.
# ---------------------------------------------------------------------------

def _format_bracket_range(low_f: float, high_f: float) -> str:
    """Human-readable bracket label, e.g. ``"82–84°F"``.

    Mirrors the open-ended-bracket convention from the retired
    ``get_bracket_analysis()`` (src/model/bracket_analysis.py) so the label
    text is unchanged for operators: sentinel bounds outside [-49, 199] mean
    "no lower/upper bound".
    """
    if low_f <= -49:
        return f"≤{int(high_f)}°F"
    if high_f >= 199:
        return f"≥{int(low_f)}°F"
    return f"{int(low_f)}–{int(high_f)}°F"


class BracketOut(BaseModel):
    range: str
    bracket_low: float
    bracket_high: float
    market_yes_ask: int | None
    market_no_ask: int | None
    p_yes: float | None
    raw_p_yes: float | None
    ev_yes: float | None
    ev_no: float | None
    emos_mode: str | None
    forecast_high: float | None
    current_high: float | None
    minutes_to_settlement: float | None
    gate_verdict: str | None
    side: Literal["YES", "NO"] | None
    gate_actual: float | None
    gate_threshold: float | None
    gate_unit: str | None
    gate_detail: str | None
    execution_mode: Literal["live", "paper"] | None
    poll_ts: str | None


class ForecastInputsOut(BaseModel):
    """Demoted secondary block (design spec §3) -- the ensemble summary the
    scanner read while producing this poll's p_yes, sourced read-only from
    the persisted scan_decisions snapshot, never a live recompute.
    """
    ensemble_mean: float | None
    ensemble_range_low: float | None
    ensemble_range_high: float | None
    ensemble_members: int | None


class AnalysisStationOut(BaseModel):
    station: str
    date: str
    is_next_day: bool
    poll_ts: str | None
    poll_interval_seconds: int
    forecast_inputs: ForecastInputsOut | None
    brackets: list[BracketOut]


@app.get("/api/analysis/{station}", response_model=AnalysisStationOut)
def analysis_station(
    station: str,
    date: str | None = None,
    next_day: bool = False,
) -> AnalysisStationOut:
    """Return the bot's-eye per-bracket decision view for a station (issue #757).

    Serves the persisted ``scan_decisions`` table (#756) -- the same
    p_yes/ev_yes/ev_no/emos_mode numbers the scanner actually traded on, plus
    the per-bracket gate verdict -- rather than a parallel ensemble recompute.

    Path param:
        station: METAR code (e.g. KORD). Case-insensitive; normalised to upper.

    Query params:
        date: optional YYYY-MM-DD settlement-date string. Defaults to today
            (UTC) when ``next_day`` is false, tomorrow (UTC) when true.
        next_day: day selector — false selects the today (``is_next_day=0``)
            partition, true selects the D+1 (``is_next_day=1``) partition of
            ``scan_decisions`` rows. Rows are additionally filtered by
            ``is_next_day`` to defend against a caller-supplied ``date`` that
            disagrees with ``next_day``.

    Returns:
        200 — per-bracket decision rows (empty list + last poll_ts, if any,
              when there is no recent scan for the station/date)
        404 — station not in configured station list
        422 — date is not a valid YYYY-MM-DD string (FastAPI auto-handles malformed
              types; the explicit check below covers semantically invalid dates)
    """
    import datetime as _dt

    station_upper = station.upper()

    # 404 — unknown station
    if station_upper not in _KNOWN_METARS:
        raise HTTPException(status_code=404, detail=f"Unknown station: {station!r}")

    # Resolve / validate date
    if date is None:
        today_utc = _dt.datetime.now(_dt.timezone.utc).date()
        resolved_date = today_utc + _dt.timedelta(days=1) if next_day else today_utc
        resolved_date_str = resolved_date.isoformat()
    else:
        try:
            _dt.date.fromisoformat(date)
            resolved_date_str = date
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid date format: {date!r}. Expected YYYY-MM-DD.")

    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")

    # Source of truth: the persisted scan_decisions snapshot (#756), never a
    # live get_ensemble_distribution()/get_bracket_analysis() recompute.
    try:
        rows = _db.get_scan_decisions(station_upper, resolved_date_str)
    except Exception as exc:
        logger.warning("[analysis/%s] scan_decisions fetch failed: %s", station_upper, exc)
        rows = []

    # Partition on is_next_day (belt-and-braces alongside the date resolution
    # above -- a row's date and is_next_day are written together by the
    # scanner, but filtering here means a caller-supplied date/next_day
    # mismatch degrades to an empty result rather than a wrong partition).
    want_next_day = 1 if next_day else 0
    rows = [r for r in rows if int(r.get("is_next_day") or 0) == want_next_day]

    poll_interval_seconds = get_live_config(_db).get("POLL_INTERVAL_SECONDS", CONFIG_DEFAULTS["POLL_INTERVAL_SECONDS"])

    if not rows:
        return AnalysisStationOut(
            station=station_upper,
            date=resolved_date_str,
            is_next_day=next_day,
            poll_ts=None,
            poll_interval_seconds=poll_interval_seconds,
            forecast_inputs=None,
            brackets=[],
        )

    brackets = [
        BracketOut(
            range=_format_bracket_range(r["bracket_low"], r["bracket_high"]),
            bracket_low=r["bracket_low"],
            bracket_high=r["bracket_high"],
            market_yes_ask=r.get("yes_ask"),
            market_no_ask=r.get("no_ask"),
            p_yes=r.get("p_yes"),
            raw_p_yes=r.get("raw_p_yes"),
            ev_yes=r.get("ev_yes"),
            ev_no=r.get("ev_no"),
            emos_mode=r.get("emos_mode"),
            forecast_high=r.get("forecast_high"),
            current_high=r.get("current_high"),
            minutes_to_settlement=r.get("minutes_to_settlement"),
            gate_verdict=r.get("gate_verdict"),
            side=r.get("side"),
            gate_actual=r.get("gate_actual"),
            gate_threshold=r.get("gate_threshold"),
            gate_unit=r.get("gate_unit"),
            gate_detail=r.get("gate_detail"),
            execution_mode=r.get("execution_mode"),
            poll_ts=r.get("poll_ts"),
        )
        for r in rows
    ]

    # All rows come from the same poll_once() call, so poll_ts is identical
    # across brackets in practice; max() is a defensive tie-break, not a
    # meaningful aggregation.
    top_poll_ts = max((r.get("poll_ts") for r in rows if r.get("poll_ts")), default=None)

    # Forecast inputs (design spec §3) -- same ensemble_* values repeated on
    # every row for a given poll (scanner attaches them once per poll, not
    # per bracket); take the first row's copy as the scan-level summary.
    first = rows[0]
    forecast_inputs = None
    if any(first.get(k) is not None for k in ("ensemble_mean", "ensemble_members", "ensemble_range_low", "ensemble_range_high")):
        forecast_inputs = ForecastInputsOut(
            ensemble_mean=first.get("ensemble_mean"),
            ensemble_range_low=first.get("ensemble_range_low"),
            ensemble_range_high=first.get("ensemble_range_high"),
            ensemble_members=first.get("ensemble_members"),
        )

    return AnalysisStationOut(
        station=station_upper,
        date=resolved_date_str,
        is_next_day=next_day,
        poll_ts=top_poll_ts,
        poll_interval_seconds=poll_interval_seconds,
        forecast_inputs=forecast_inputs,
        brackets=brackets,
    )


# Mount static files last so /api routes take priority
if STATIC.exists():
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")

"""FastAPI status dashboard for MeteoEdge.

Exposes lightweight read-only endpoints by parsing JSONL/CSV log files.
Designed to run in a background thread alongside the main polling loop or
as a standalone service via uvicorn.

Usage (standalone):
    uvicorn src.monitoring.dashboard:app --port 8000

Usage (embedded in run.py):
    from src.monitoring.dashboard import start_dashboard
    start_dashboard()   # fires a daemon thread, returns immediately
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# FastAPI is an optional dependency — import lazily so that the rest of the
# codebase does not break when the package is missing.
try:
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "fastapi is required for the dashboard. "
        "Install it with: pip install fastapi uvicorn"
    ) from exc

from src.config import LOG_DIR, LIVE_TRADES_JSONL, SNAPSHOTS_JSONL, STARTING_CAPITAL_EUR

app = FastAPI(title="MeteoEdge Dashboard", version="1.0.0")

# Module-level start time so /health can report uptime.
_START_TIME = time.monotonic()

# Last poll timestamp is written here by run.py after each poll cycle.
# Stored as an ISO string or None.
last_poll_ts: str | None = None

# Database instance injected by run.py via set_db(). None until set.
_db = None


def set_db(db) -> None:
    """Inject the Database instance from run.py so endpoints read from SQLite."""
    global _db
    _db = db


# ---------------------------------------------------------------------------
# Log-parsing helpers
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file and return a list of dicts. Returns [] if missing/corrupt."""
    if not path.exists():
        return []
    records: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        return []
    return records


def _today_utc() -> str:
    """Return today's date as YYYY-MM-DD in UTC."""
    return datetime.now(timezone.utc).date().isoformat()


def _load_trades() -> list[dict]:
    """Return all trade records, newest first. Prefers DB when available."""
    if _db is not None:
        try:
            return _db.get_trades(limit=None, mode=None)
        except Exception:
            pass
    records = _read_jsonl(LIVE_TRADES_JSONL)
    return list(reversed(records))


def _compute_win_rate(trades: list[dict], n: int = 50) -> float:
    """Compute win rate over the last *n* settled trades.

    Only trades with a non-zero pnl are considered settled — live trades have
    pnl=0.0 at fill time and are updated only after Polymarket resolves the
    market. Unsettled trades are excluded from both numerator and denominator
    so they cannot artificially depress the win rate.

    Returns a float 0.0–1.0, or 1.0 when no settled trades exist yet
    (sentinel value that does not trigger the win-rate alert).
    """
    settled = [
        t for t in trades
        if t.get("outcome") == "filled" and float(t.get("pnl", 0)) != 0.0
    ][:n]
    if not settled:
        return 1.0  # No settlement data yet — suppress alert
    wins = sum(1 for t in settled if float(t.get("pnl", 0)) > 0)
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
            pass
    if not snapshots:
        return STARTING_CAPITAL_EUR
    last = snapshots[-1]
    return float(last.get("capital", STARTING_CAPITAL_EUR))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    """Liveness probe. Always returns 200 even when log files are empty."""
    uptime = int(time.monotonic() - _START_TIME)
    return {
        "status": "ok",
        "last_poll": last_poll_ts,
        "uptime_seconds": uptime,
    }


@app.get("/status")
def status() -> dict:
    """Summary of current capital, today's PnL, trade count, and win rate."""
    trades = _load_trades()
    snapshots = _read_jsonl(SNAPSHOTS_JSONL)

    capital = _latest_capital(snapshots)
    today_pnl = _today_pnl(trades)
    today_count = _today_trade_count(trades)
    win_rate = _compute_win_rate(trades, n=50)

    return {
        "capital": round(capital, 2),
        "today_pnl": round(today_pnl, 2),
        "today_trade_count": today_count,
        "win_rate": round(win_rate, 4),
        "last_poll": last_poll_ts,
    }


@app.get("/trades")
def trades() -> list[dict]:
    """Last 50 trade records, newest first."""
    return _load_trades()[:50]


@app.get("/stations")
def stations() -> dict[str, Any]:
    """Per-station trade count, win rate, and total PnL."""
    all_trades = _load_trades()
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
# Embedded launcher (for run.py integration)
# ---------------------------------------------------------------------------

def start_dashboard(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Start the FastAPI dashboard in a background daemon thread.

    Returns immediately. The dashboard runs until the process exits.
    If the port is already bound (e.g., the portfolio dashboard at
    src/dashboard/api.py is running on the same port), logs a notice and
    skips silently rather than letting uvicorn raise inside the daemon
    thread.
    """
    try:
        import uvicorn
    except ImportError:
        print("[dashboard] uvicorn not installed — dashboard disabled")
        return

    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("" if host == "0.0.0.0" else host, port))
    except OSError:
        print(f"[dashboard] port {port} already in use — embedded monitor skipped")
        return
    finally:
        probe.close()

    def _run() -> None:
        uvicorn.run(app, host=host, port=port, log_level="warning")

    thread = threading.Thread(target=_run, name="dashboard", daemon=True)
    thread.start()
    print(f"[dashboard] started at http://{host}:{port}")

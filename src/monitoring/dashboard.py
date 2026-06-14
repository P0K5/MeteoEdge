"""Bridge stub — delegates to the consolidated dashboard at src/dashboard/api.py.

run.py imports from this module for backward compatibility.  All actual
implementation lives in src/dashboard/api.py and src/dashboard/data.py.

Exported symbols (used by run.py and tests):
    last_poll_ts  — ISO timestamp of the last poll cycle (read/write, kept in sync with api)
    _db           — Database instance (kept in sync with api via set_db)
    set_db(db)    — inject the Database singleton into api
    start_dashboard() — start the FastAPI server in a background daemon thread
    _load_trades()    — return all trade records (for AlertManager)
    _compute_win_rate(trades, n) — compute win rate over last n settled trades
    _latest_capital(snapshots)  — return the most recent capital value
    _read_jsonl(path) — read a JSONL file (for tests)
    app           — the FastAPI app (forwarded from src.dashboard.api)

Usage (same as before, no changes to run.py required):
    from src.monitoring.dashboard import start_dashboard
    start_dashboard()
    import src.monitoring.dashboard as _dashboard
    _dashboard.last_poll_ts = ts   # kept in sync with src.dashboard.api.last_poll_ts
    _dashboard.set_db(db)          # injects into src.dashboard.api
"""
from __future__ import annotations

import threading
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def _get_api():
    """Deferred import to avoid circular deps at module load time."""
    import src.dashboard.api as _api
    return _api


# ---------------------------------------------------------------------------
# Real module-level attributes (owned by THIS module).
# These are the authoritative values that run.py and tests read/write.
# set_db() mirrors changes into src.dashboard.api so the HTTP endpoints
# that live in api.py also see the current database instance.
# ---------------------------------------------------------------------------

# Last poll timestamp written by run.py after each poll cycle.
last_poll_ts: str | None = None

# Per-station weather-feed health from the most recent _build_weather() call.
# Mirrored into src.dashboard.api so the /api/weather-health endpoint sees it.
weather_health: list | None = None

# Database instance injected by run.py via set_db().
_db = None


# ---------------------------------------------------------------------------
# Functions that keep src.dashboard.api in sync and delegate computation
# ---------------------------------------------------------------------------

def set_db(db) -> None:
    """Inject the Database instance into both this module and src.dashboard.api."""
    global _db
    _db = db
    _get_api().set_db(db)


def _load_trades() -> list[dict]:
    """Return all trade records, newest first.

    Delegates to src.dashboard.api._dashboard_load_trades() which prefers DB
    over JSONL fallback.
    """
    api = _get_api()
    return api._dashboard_load_trades()


def _compute_win_rate(trades: list[dict], n: int = 50) -> float:
    """Compute win rate over the last *n* settled trades."""
    return _get_api()._compute_win_rate(trades, n=n)


def _latest_capital(snapshots: list[dict]) -> float:
    """Return the most recent capital value."""
    return _get_api()._latest_capital(snapshots)


def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file; delegates to the shared data layer."""
    from src.dashboard.data import read_jsonl
    return read_jsonl(path)


def start_dashboard(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Start the consolidated FastAPI dashboard in a background daemon thread.

    Returns immediately.  If the port is already bound, logs a notice and
    skips silently.
    """
    try:
        import uvicorn
    except ImportError:
        log.warning("[dashboard] uvicorn not installed -- dashboard disabled")
        return

    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("" if host == "0.0.0.0" else host, port))
    except OSError:
        log.info("[dashboard] port %s already in use -- embedded monitor skipped", port)
        return
    finally:
        probe.close()

    api = _get_api()

    def _run() -> None:
        uvicorn.run(api.app, host=host, port=port, log_level="warning")

    thread = threading.Thread(target=_run, name="dashboard", daemon=True)
    thread.start()
    log.info("[dashboard] started at http://%s:%s", host, port)


# ---------------------------------------------------------------------------
# Module proxy: makes `last_poll_ts` writes propagate to src.dashboard.api
# ---------------------------------------------------------------------------
# When run.py does `_dashboard.last_poll_ts = ts`, the bridge's
# __setattr__ must also update src.dashboard.api.last_poll_ts so the
# HTTP endpoints in api.py (/health, /status) see the current timestamp.
#
# We install a module proxy that overrides __setattr__ to mirror writes of
# last_poll_ts and _db to src.dashboard.api.  __getattr__ is NOT overridden
# for these — reads come from the real __dict__ so that patch.object() works
# correctly (patch.object stores its value in __dict__ and expects reads to
# see it there).

import sys as _sys
import types as _types


class _BridgeModule(_types.ModuleType):
    """Module that mirrors last_poll_ts / _db writes to src.dashboard.api.

    - Reads come from the real __dict__ (normal Python attribute lookup).
    - Writes to last_poll_ts and _db also propagate to src.dashboard.api.
    - __delattr__ for last_poll_ts / _db resets to None (for patch.object cleanup).
    - app is forwarded from src.dashboard.api via __getattr__.
    """

    _MIRRORED = frozenset({"last_poll_ts", "_db", "weather_health"})

    def __setattr__(self, name: str, value) -> None:
        if name in self._MIRRORED:
            # Store locally so reads from __dict__ (and patch.object) work
            self.__dict__[name] = value
            # Mirror to src.dashboard.api
            try:
                api = _get_api()
                setattr(api, name, value)
            except Exception:
                pass  # import cycle or not-yet-loaded — ignore
            return
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        # patch.object calls delattr to restore when the original value was
        # not present as a real __dict__ entry.  For mirrored attributes we
        # reset to None rather than raising AttributeError.
        if name in self._MIRRORED:
            self.__dict__[name] = None
            try:
                setattr(_get_api(), name, None)
            except Exception:
                pass
            return
        super().__delattr__(name)

    def __getattr__(self, name: str):
        # Only called when normal __dict__ lookup fails.
        if name == "app":
            return _get_api().app
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Install the proxy and copy all current module attributes into it.
_bridge = _BridgeModule(__name__)
_current_vars = dict(vars(_sys.modules[__name__]))
for _k, _v in _current_vars.items():
    if _k not in ("_BridgeModule", "_bridge", "_current_vars", "_sys", "_types"):
        _bridge.__dict__[_k] = _v
_sys.modules[__name__] = _bridge

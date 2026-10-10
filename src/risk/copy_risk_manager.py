"""Copy-trading realized-P&L circuit breaker (issue #1139, story D1 of
epic #1138), plus its live-specific counterpart (issue #1175, epic I
#1160).

Gates *new* copy-signal execution -- never settlement of already-open
``copy_positions``/``copy_live_positions`` (that stays exclusively
``src/scripts/copy_settle.py``'s job; nothing in this module is imported
by, or imports, that script).

**Deliberately isolated from ``src/risk/manager.py``.** Per the #1100
isolation decision, copy-trading and the weather strategy never share
tables, config, or code -- this module does not import anything from
``src/risk/manager.py``. Its ``RiskManager.allow_trade`` is the *pattern*
this mirrors (a daily-loss-limit + drawdown-stop gate returning
``(bool, reason)``), not its storage: that one holds state in an
in-memory dataclass seeded once from the DB at construction time and
reset locally at midnight; ``allow_copy_signal`` (paper) below re-derives
its answer from the DB on every single call
(``Database.get_copy_realized_pnl_total_for_date`` /
``get_copy_realized_pnl_total``) -- no in-memory counters at all -- so the
answer is identical across ``copy_signal_loop.py`` process restarts and
across however many callers ask. ``allow_live_copy_signal`` below is
DB-backed the same way, but additionally persists a trip record once
tripped -- see its own docstring (issue #1348) for why "re-derive every
call" alone was not enough for the live breaker specifically.

Both limits are scoped to ``COPY_TRADING_CAPITAL_USD`` (a fixed module
constant, not live-editable -- see its own comment in ``src/config.py``),
never ``STARTING_CAPITAL_EUR``: same capital-pool isolation as everywhere
else in copy-trading.

**Live variant (:func:`allow_live_copy_signal`), one layer up.** Even when
paper's own breaker above hasn't tripped, live's own realized P&L (real
fills, real slippage) could independently blow through a daily-loss or
drawdown limit paper never sees -- this is a genuinely separate risk
control, not redundant with :func:`allow_copy_signal`. Same shape, same
daily-loss-then-drawdown precedence, but reads ``copy_live_positions``
(via ``Database.get_copy_live_realized_pnl_total_for_date`` /
``get_copy_live_realized_pnl_total``) and is scoped to
``COPY_LIVE_CAPITAL_USD``, never ``COPY_TRADING_CAPITAL_USD`` -- same
paper/live isolation as everywhere else in this epic set. The two
functions never share a DB call or a config key, so tripping one can
never trip (or mask) the other.

**Persisted trip state (issue #1348).** Unlike paper's breaker,
``allow_live_copy_signal`` does not purely re-derive its answer from PnL
on every call any more. Two production problems with the pure-recompute
design forced this: (1) the drawdown breaker, scoped to all-time (or
``COPY_LIVE_DRAWDOWN_SINCE``) cumulative P&L, is a one-way door once
tripped -- blocked new trades mean realized P&L can never recover, so it
never auto-clears; (2) the daily-loss breaker could flicker mid-day --a
later win on the same UTC day could push the day's running total back
above ``-daily_loss_limit``, un-tripping a breaker the operator expects to
stay tripped once a bad session trips it. The fix persists a trip record
(``{tripped_at, reason, trip_utc_date}``) in ``bot_config`` (key
``COPY_LIVE_BREAKER_TRIP_STATE``, JSON-encoded, same
``Database.get_config``/``set_config`` mechanism ``COPY_LIVE_TRADING_ENABLED``
and the wallet-balance-drift verdict already use) the moment either limit
trips. Every subsequent call on the SAME UTC day returns that persisted
``(False, reason)`` directly, WITHOUT re-reading PnL -- deterministic, no
flicker. A call on a LATER UTC day clears the stale record and falls
through to a fresh evaluation (may immediately re-trip -- that's a new
day's assessment, not a bug). This is still fully DB-backed, no
in-memory state at all -- the answer is identical across
``copy_signal_loop.py`` process restarts, same contract as before, just
with one more persisted fact. :func:`reset_live_circuit_breaker` gives the
operator a manual, immediate, day-boundary-independent clear path (wired
to a dashboard control), and :func:`get_live_circuit_breaker_status` is
the read-only accessor the dashboard uses to show current trip state.
Paper's :func:`allow_copy_signal` is explicitly OUT of scope for this --
it keeps the pure re-derive-every-call design unchanged.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.config import COPY_LIVE_CAPITAL_USD, COPY_TRADING_CAPITAL_USD

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from src.data.db import Database

REASON_DAILY_LOSS = "circuit_breaker_daily_loss"
REASON_DRAWDOWN = "circuit_breaker_drawdown"
REASON_LIVE_DAILY_LOSS = "live_circuit_breaker_daily_loss"
REASON_LIVE_DRAWDOWN = "live_circuit_breaker_drawdown"

# bot_config key for the live breaker's persisted trip record (issue #1348).
# JSON-encoded {"tripped_at": <iso>, "reason": <REASON_LIVE_*>,
# "trip_utc_date": <YYYY-MM-DD>}. Absent/empty/unparseable all mean
# "not currently tripped" -- see _read_live_breaker_trip.
_LIVE_BREAKER_STATE_KEY = "COPY_LIVE_BREAKER_TRIP_STATE"


def allow_copy_signal(db: "Database", live_config: dict) -> "tuple[bool, str]":
    """Determine whether new copy-signal execution is currently permitted.

    Checked once per ``copy_signal_loop.py`` cycle, before the per-wallet
    loop -- the same global answer applies to every signal detected in
    that cycle, exactly like the existing ``COPY_TRADING_ENABLED``
    kill-switch check it sits alongside.

    Args:
        db: ``Database`` handle -- both limits are computed fresh from it
            on every call (no in-memory state).
        live_config: the dict returned by ``src.config.get_live_config(db)``,
            supplying ``COPY_DAILY_LOSS_LIMIT_USD`` and
            ``COPY_DRAWDOWN_STOP_PCT``.

    Returns:
        ``(True, "")`` when neither limit is breached.
        ``(False, reason)`` when one is -- *reason* is one of
        ``REASON_DAILY_LOSS`` / ``REASON_DRAWDOWN``, meant to be written
        straight into ``copy_signals.skip_reason``.

    **Precedence when both limits are breached simultaneously:** the
    daily-loss check runs first and wins, mirroring
    ``RiskManager.allow_trade``'s own check order (daily loss, then
    drawdown). This is a deliberate, documented choice, not an accident
    of code order -- pick whichever one changes, this docstring must be
    updated to match.
    """
    daily_loss_limit = live_config["COPY_DAILY_LOSS_LIMIT_USD"]
    drawdown_stop_pct = live_config["COPY_DRAWDOWN_STOP_PCT"]

    today = datetime.now(timezone.utc).date().isoformat()
    daily = db.get_copy_realized_pnl_total_for_date(today)
    if daily["total_pnl_usd"] <= -daily_loss_limit:
        return False, REASON_DAILY_LOSS

    total = db.get_copy_realized_pnl_total()
    if COPY_TRADING_CAPITAL_USD > 0:
        drawdown = -total["total_pnl_usd"] / COPY_TRADING_CAPITAL_USD
        if drawdown >= drawdown_stop_pct:
            return False, REASON_DRAWDOWN

    return True, ""


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_live_breaker_trip(db: "Database") -> "dict | None":
    """Read the persisted live-breaker trip record, or ``None`` if there is
    no trip currently on file (absent row, empty string, unparseable JSON,
    or a shape missing an expected key).

    Deliberately never raises -- mirrors
    ``copy_live_settle.get_wallet_balance_drift_status``'s own "a corrupt or
    missing persisted blob degrades to the safe default, never a crash"
    rule, applied here to a trading-safety gate instead of a read-only
    dashboard fact, so the stakes for silently swallowing a parse failure
    are higher: logged at ``warning`` rather than left silent. Does NOT
    compare ``trip_utc_date`` against today -- that's the caller's job
    (:func:`allow_live_copy_signal` clears + re-evaluates on a stale trip;
    :func:`get_live_circuit_breaker_status` just reports "not tripped"
    without writing anything), since "stale" means a different action to
    each of them.
    """
    try:
        raw = db.get_config(_LIVE_BREAKER_STATE_KEY)
    except Exception as e:  # noqa: BLE001 — read failure must degrade, never crash the gate
        log.warning("[live-breaker] trip-state read failed: %s", e)
        return None
    if not raw:
        return None
    try:
        record = json.loads(raw)
        if not isinstance(record, dict):
            raise TypeError(f"expected dict, got {type(record).__name__}")
        if not all(k in record for k in ("tripped_at", "reason", "trip_utc_date")):
            raise KeyError("missing tripped_at/reason/trip_utc_date")
    except (ValueError, TypeError, KeyError) as e:
        log.warning("[live-breaker] trip-state row unparseable: %r (%s)", raw, e)
        return None
    return record


def _persist_live_breaker_trip(db: "Database", reason: str) -> None:
    """Write a new trip record the moment a limit is breached."""
    record = {"tripped_at": _now_utc_iso(), "reason": reason, "trip_utc_date": _today_utc()}
    try:
        db.set_config(_LIVE_BREAKER_STATE_KEY, json.dumps(record))
    except Exception as e:  # noqa: BLE001 — see _read_live_breaker_trip's own rationale
        log.warning("[live-breaker] failed to persist trip state: %s", e)


def _clear_live_breaker_trip(db: "Database") -> None:
    """Clear any persisted trip record (stale-day rollover or manual reset)."""
    try:
        db.set_config(_LIVE_BREAKER_STATE_KEY, "")
    except Exception as e:  # noqa: BLE001 — see _read_live_breaker_trip's own rationale
        log.warning("[live-breaker] failed to clear trip state: %s", e)


def reset_live_circuit_breaker(db: "Database") -> bool:
    """Manually clear a persisted live-breaker trip immediately, independent
    of the UTC day boundary (issue #1348 acceptance criteria) -- the
    operator's "I've reviewed the situation, resume trading now" path,
    wired to a dashboard control (``POST
    /api/copy-trading/live-breaker/reset`` in ``src/dashboard/api.py``).

    Returns whether a trip was actually cleared (``False`` when nothing was
    tripped -- a no-op, mirroring ``halt_live_copy_trading.py``'s own
    "already in the target state is a no-op" convention). The reset is
    always logged at ``warning`` with what was cleared and when -- "who" is
    just "operator" (single-operator system today, per the issue), but the
    fact and timestamp are always on record in the application log.
    """
    persisted = _read_live_breaker_trip(db)
    if persisted is None:
        return False
    _clear_live_breaker_trip(db)
    log.warning(
        "[live-breaker] manual reset by operator at %s -- was tripped since %s (reason=%s)",
        _now_utc_iso(), persisted.get("tripped_at"), persisted.get("reason"),
    )
    return True


def get_live_circuit_breaker_status(db: "Database") -> dict:
    """Read-only accessor for the live breaker's current trip state (issue
    #1348) -- exposed to the dashboard via ``GET
    /api/copy-trading/live-breaker`` (``src/dashboard/api.py``), mirroring
    ``copy_live_settle.get_wallet_balance_drift_status``'s own
    "module owns both the gate and a read-only getter" split.

    Returns ``{"tripped": bool, "reason": str | None, "tripped_at": str | None,
    "trip_utc_date": str | None}``. A trip persisted on a PRIOR UTC day is
    reported as not-tripped here too (matching
    :func:`allow_live_copy_signal`'s own clear-and-reevaluate rule) but
    WITHOUT writing anything -- a status read must stay read-only; clearing
    the stale row is :func:`allow_live_copy_signal`'s job, triggered by the
    next actual signal-loop cycle, not by a dashboard poll. Never raises:
    a missing DB, missing config row, or corrupt JSON blob all degrade to
    ``tripped=False``, never a 500.
    """
    empty = {"tripped": False, "reason": None, "tripped_at": None, "trip_utc_date": None}
    persisted = _read_live_breaker_trip(db)
    if persisted is None:
        return empty
    if persisted["trip_utc_date"] != _today_utc():
        return empty
    return {
        "tripped": True,
        "reason": persisted["reason"],
        "tripped_at": persisted["tripped_at"],
        "trip_utc_date": persisted["trip_utc_date"],
    }


def _drawdown_baseline(live_config: dict) -> "str | None":
    """Return the ISO-8601 baseline for the live drawdown, or None for all-time.

    An unparseable value falls back to None, which counts every live loss
    (the stricter rule), so a typo can never widen the baseline.
    """
    raw = live_config.get("COPY_LIVE_DRAWDOWN_SINCE") or ""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        log.warning("[live-breaker] COPY_LIVE_DRAWDOWN_SINCE=%r is not ISO-8601 -- using all-time", raw)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def allow_live_copy_signal(db: "Database", live_config: dict) -> "tuple[bool, str]":
    """Determine whether new LIVE copy-signal execution is currently
    permitted (issue #1175, epic I #1160).

    Mirrors :func:`allow_copy_signal` one layer up: same daily-loss-then-
    drawdown precedence, same table/config/capital-pool isolation --
    ``copy_live_positions`` (via
    ``Database.get_copy_live_realized_pnl_total_for_date`` /
    ``get_copy_live_realized_pnl_total``) and ``COPY_LIVE_CAPITAL_USD``,
    never ``copy_positions`` or ``COPY_TRADING_CAPITAL_USD``. This keeps
    live and paper fully independent in both directions: a losing streak
    on one can never trip (or mask) the other, since neither reads the
    other's table or config key.

    **Persisted trip, not a pure re-derive (issue #1348).** Unlike
    :func:`allow_copy_signal`, this function does NOT just recompute
    fresh PnL every call. It first checks for a persisted trip (see the
    module docstring's "Persisted trip state" section for the full
    rationale):

    - A trip persisted for TODAY (UTC) short-circuits straight to
      ``(False, reason)`` -- the PnL thresholds below are not even read.
      This is what makes the trip deterministic for the rest of the day,
      immune to a later win pulling the daily total back above the limit.
    - A trip persisted for a PRIOR UTC day is stale: it is cleared, and
      evaluation falls through to the fresh-PnL path below, exactly as if
      no trip had ever existed -- a new day's assessment, which may
      immediately re-trip if conditions are still bad.
    - With no persisted trip, PnL is evaluated fresh (unchanged logic from
      before #1348); the instant either limit breaches, a new trip record
      is persisted (:func:`_persist_live_breaker_trip`) before returning.

    Still fully DB-backed, no in-memory state: the persisted record itself
    lives in ``bot_config``, so the answer (tripped or not, and why) is
    identical across ``copy_signal_loop.py`` process restarts, same
    contract as always.

    Args:
        db: ``Database`` handle -- the persisted trip record and (when no
            trip is on file) both PnL limits are read fresh from it on
            every call; still no in-memory state.
        live_config: the dict returned by ``src.config.get_live_config(db)``,
            supplying ``COPY_LIVE_DAILY_LOSS_LIMIT_USD`` and
            ``COPY_LIVE_DRAWDOWN_STOP_PCT``.

    Returns:
        ``(True, "")`` when neither limit is breached (and no trip is
        persisted for today).
        ``(False, reason)`` when tripped -- either just now, or earlier
        today -- *reason* is one of ``REASON_LIVE_DAILY_LOSS`` /
        ``REASON_LIVE_DRAWDOWN``, meant to be threaded through
        ``copy_signal_loop.py``'s existing ``live_gate_reason`` mechanism
        (from #1167) straight into ``copy_live_positions.rejected_reason``.

    **Precedence when both limits are breached simultaneously:** the
    daily-loss check runs first and wins, exactly mirroring
    :func:`allow_copy_signal`'s own documented precedence. This is a
    deliberate, documented choice, not an accident of code order -- pick
    whichever one changes, this docstring must be updated to match.
    """
    today = _today_utc()
    persisted = _read_live_breaker_trip(db)
    if persisted is not None:
        if persisted["trip_utc_date"] == today:
            return False, persisted["reason"]
        # Stale trip from a prior UTC day -- clear it and fall through to
        # a fresh evaluation below (today's own assessment).
        _clear_live_breaker_trip(db)

    daily_loss_limit = live_config["COPY_LIVE_DAILY_LOSS_LIMIT_USD"]
    drawdown_stop_pct = live_config["COPY_LIVE_DRAWDOWN_STOP_PCT"]

    daily = db.get_copy_live_realized_pnl_total_for_date(today)
    if daily["total_pnl_usd"] <= -daily_loss_limit:
        _persist_live_breaker_trip(db, REASON_LIVE_DAILY_LOSS)
        return False, REASON_LIVE_DAILY_LOSS

    since = _drawdown_baseline(live_config)
    total = db.get_copy_live_realized_pnl_total(since=since)
    if COPY_LIVE_CAPITAL_USD > 0:
        drawdown = -total["total_pnl_usd"] / COPY_LIVE_CAPITAL_USD
        if drawdown >= drawdown_stop_pct:
            _persist_live_breaker_trip(db, REASON_LIVE_DRAWDOWN)
            return False, REASON_LIVE_DRAWDOWN

    return True, ""

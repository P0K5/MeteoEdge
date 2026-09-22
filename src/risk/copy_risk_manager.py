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
reset locally at midnight; this one re-derives its answer from the DB on
every single call (``Database.get_copy_realized_pnl_total_for_date`` /
``get_copy_realized_pnl_total``) -- no in-memory counters at all -- so the
answer is identical across ``copy_signal_loop.py`` process restarts and
across however many callers ask.

Both limits are scoped to ``COPY_TRADING_CAPITAL_USD`` (a fixed module
constant, not live-editable -- see its own comment in ``src/config.py``),
never ``STARTING_CAPITAL_EUR``: same capital-pool isolation as everywhere
else in copy-trading.

**Live variant (:func:`allow_live_copy_signal`), one layer up.** Even when
paper's own breaker above hasn't tripped, live's own realized P&L (real
fills, real slippage) could independently blow through a daily-loss or
drawdown limit paper never sees -- this is a genuinely separate risk
control, not redundant with :func:`allow_copy_signal`. Same shape, same
daily-loss-then-drawdown precedence, same "re-derive from DB every call"
contract, but reads ``copy_live_positions`` (via
``Database.get_copy_live_realized_pnl_total_for_date`` /
``get_copy_live_realized_pnl_total``) and is scoped to
``COPY_LIVE_CAPITAL_USD``, never ``COPY_TRADING_CAPITAL_USD`` -- same
paper/live isolation as everywhere else in this epic set. The two
functions never share a DB call or a config key, so tripping one can
never trip (or mask) the other.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.config import COPY_LIVE_CAPITAL_USD, COPY_TRADING_CAPITAL_USD

if TYPE_CHECKING:
    from src.data.db import Database

REASON_DAILY_LOSS = "circuit_breaker_daily_loss"
REASON_DRAWDOWN = "circuit_breaker_drawdown"
REASON_LIVE_DAILY_LOSS = "live_circuit_breaker_daily_loss"
REASON_LIVE_DRAWDOWN = "live_circuit_breaker_drawdown"


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


def allow_live_copy_signal(db: "Database", live_config: dict) -> "tuple[bool, str]":
    """Determine whether new LIVE copy-signal execution is currently
    permitted (issue #1175, epic I #1160).

    Mirrors :func:`allow_copy_signal` exactly, one layer up: checked once
    per ``copy_signal_loop.py`` cycle, same daily-loss-then-drawdown
    precedence, same "re-derive from DB every call, no in-memory state"
    contract. The only differences are *which* table/config/capital-pool
    it reads -- ``copy_live_positions`` (via
    ``Database.get_copy_live_realized_pnl_total_for_date`` /
    ``get_copy_live_realized_pnl_total``) and ``COPY_LIVE_CAPITAL_USD``,
    never ``copy_positions`` or ``COPY_TRADING_CAPITAL_USD``. This keeps
    live and paper fully independent in both directions: a losing streak
    on one can never trip (or mask) the other, since neither reads the
    other's table or config key.

    Args:
        db: ``Database`` handle -- both limits are computed fresh from it
            on every call (no in-memory state).
        live_config: the dict returned by ``src.config.get_live_config(db)``,
            supplying ``COPY_LIVE_DAILY_LOSS_LIMIT_USD`` and
            ``COPY_LIVE_DRAWDOWN_STOP_PCT``.

    Returns:
        ``(True, "")`` when neither limit is breached.
        ``(False, reason)`` when one is -- *reason* is one of
        ``REASON_LIVE_DAILY_LOSS`` / ``REASON_LIVE_DRAWDOWN``, meant to be
        threaded through ``copy_signal_loop.py``'s existing
        ``live_gate_reason`` mechanism (from #1167) straight into
        ``copy_live_positions.rejected_reason``.

    **Precedence when both limits are breached simultaneously:** the
    daily-loss check runs first and wins, exactly mirroring
    :func:`allow_copy_signal`'s own documented precedence. This is a
    deliberate, documented choice, not an accident of code order -- pick
    whichever one changes, this docstring must be updated to match.
    """
    daily_loss_limit = live_config["COPY_LIVE_DAILY_LOSS_LIMIT_USD"]
    drawdown_stop_pct = live_config["COPY_LIVE_DRAWDOWN_STOP_PCT"]

    today = datetime.now(timezone.utc).date().isoformat()
    daily = db.get_copy_live_realized_pnl_total_for_date(today)
    if daily["total_pnl_usd"] <= -daily_loss_limit:
        return False, REASON_LIVE_DAILY_LOSS

    total = db.get_copy_live_realized_pnl_total()
    if COPY_LIVE_CAPITAL_USD > 0:
        drawdown = -total["total_pnl_usd"] / COPY_LIVE_CAPITAL_USD
        if drawdown >= drawdown_stop_pct:
            return False, REASON_LIVE_DRAWDOWN

    return True, ""

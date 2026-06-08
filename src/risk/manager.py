"""Risk manager — gates every trade before execution.

All limits are read from src/config.py and can be overridden via environment
variables. State is held in memory and resets daily at midnight UTC; it is
not persisted across process restarts (acceptable for the paper-trading phase).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

from src.config import (
    RISK_DAILY_LOSS_LIMIT_EUR,
    RISK_DRAWDOWN_STOP_PCT,
    RISK_MAX_OPEN_POSITIONS,
    RISK_MIN_LIQUIDITY,
    STARTING_CAPITAL_EUR,
)

if TYPE_CHECKING:
    from src.data.db import Database


@dataclass
class RiskManager:
    """Gates trade execution against daily-loss, position, drawdown, and liquidity limits.

    Parameters are loaded from config.py defaults so that a plain
    ``RiskManager()`` always uses the values set via environment variables.

    Usage::

        rm = RiskManager()
        ok, reason = rm.allow_trade(capital=current_capital, liquidity_contracts=200)
        if not ok:
            print(f"Trade blocked: {reason}")
            return

        rm.open_position()
        # … execute trade …
        rm.close_position()
        rm.record_pnl(pnl_eur)
    """

    daily_loss_limit_eur: float = field(default_factory=lambda: RISK_DAILY_LOSS_LIMIT_EUR)
    max_open_positions: int = field(default_factory=lambda: RISK_MAX_OPEN_POSITIONS)
    drawdown_stop_pct: float = field(default_factory=lambda: RISK_DRAWDOWN_STOP_PCT)
    min_market_liquidity: int = field(default_factory=lambda: RISK_MIN_LIQUIDITY)
    starting_capital: float = field(default_factory=lambda: STARTING_CAPITAL_EUR)
    db: "Database | None" = field(default=None, repr=False)

    # Internal state — reset daily at midnight UTC
    _daily_pnl: float = field(default=0.0, init=False, repr=False)
    _open_positions: int = field(default=0, init=False, repr=False)
    _trade_day: date = field(
        default_factory=lambda: datetime.now(timezone.utc).date(),
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        """Seed _daily_pnl from DB if db is provided."""
        if self.db is not None:
            today = datetime.now(timezone.utc).date().isoformat()
            self._daily_pnl = self.db.get_daily_pnl(today)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _reset_if_new_day(self) -> None:
        """Reset daily PnL accumulator when the UTC calendar day has rolled over."""
        today = datetime.now(timezone.utc).date()
        if today != self._trade_day:
            self._daily_pnl = 0.0
            self._trade_day = today
            if self.db is not None:
                today_str = today.isoformat()
                self.db.upsert_daily_risk(today_str, pnl_delta=0.0, open_positions=self._open_positions)

    # ------------------------------------------------------------------
    # State mutators
    # ------------------------------------------------------------------

    def record_pnl(self, pnl_eur: float) -> None:
        """Accumulate realised PnL for the current trading day.

        Call this after every trade closes (whether profitable or not).
        """
        self._reset_if_new_day()
        self._daily_pnl += pnl_eur
        if self.db is not None:
            today = datetime.now(timezone.utc).date().isoformat()
            self.db.upsert_daily_risk(today, pnl_delta=pnl_eur, open_positions=self._open_positions)

    def open_position(self) -> None:
        """Increment the open-position counter when a trade is entered."""
        self._open_positions += 1
        if self.db is not None:
            today = datetime.now(timezone.utc).date().isoformat()
            self.db.upsert_daily_risk(today, pnl_delta=0.0, open_positions=self._open_positions)

    def close_position(self) -> None:
        """Decrement the open-position counter when a trade is exited."""
        self._open_positions = max(0, self._open_positions - 1)
        if self.db is not None:
            today = datetime.now(timezone.utc).date().isoformat()
            self.db.upsert_daily_risk(today, pnl_delta=0.0, open_positions=self._open_positions)

    # ------------------------------------------------------------------
    # Trade gate
    # ------------------------------------------------------------------

    def allow_trade(
        self,
        capital: float,
        liquidity_contracts: int = 9999,
    ) -> tuple[bool, str]:
        """Determine whether a new trade is permitted under current risk limits.

        Args:
            capital: Current portfolio value in EUR.
            liquidity_contracts: Combined yes_ask_size + no_ask_size for the
                target market. Defaults to a large value so callers that do not
                track liquidity are not accidentally blocked.

        Returns:
            ``(True, "")`` when all limits are clear.
            ``(False, reason)`` with a human-readable explanation when blocked.
        """
        self._reset_if_new_day()

        if self._daily_pnl <= -self.daily_loss_limit_eur:
            return False, f"daily loss limit hit (€{self._daily_pnl:.2f})"

        if self._open_positions >= self.max_open_positions:
            return False, f"max open positions ({self.max_open_positions}) reached"

        drawdown = (self.starting_capital - capital) / self.starting_capital
        if drawdown >= self.drawdown_stop_pct:
            return False, f"drawdown stop triggered ({drawdown:.1%})"

        if liquidity_contracts < self.min_market_liquidity:
            return False, f"insufficient liquidity ({liquidity_contracts} < {self.min_market_liquidity})"

        return True, ""

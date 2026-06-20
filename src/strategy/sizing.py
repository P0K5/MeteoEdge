"""Fractional Kelly position sizing.

Usage
-----
    from src.strategy.sizing import compute_position_size

    size_eur = compute_position_size(
        p_win=0.90,
        price_cents=28,   # ask price in cents
        fee_cents=1.0,
        bankroll=100.0,   # available USDC balance
        sizing_mode="kelly",  # or "flat"
    )

Kelly formula for binary NO contracts
--------------------------------------
For a NO buy at price q¢ with win probability p (model output):
    edge = p * 100 - q - fee          (expected profit in cents per contract)
    odds = 100 - q                    (profit per unit staked if win)
    f*   = edge / odds                (Kelly fraction)

Kelly fraction is then multiplied by KELLY_MULTIPLIER (0.25) and bankroll,
then clamped to [MIN_SIZE_EUR, MAX_SIZE_EUR].

Warning: Do not enable SIZING_MODE=kelly in production until model calibration
is complete (issue #70). Mid-range probabilities are known to be overconfident
per BACKTEST_SUMMARY.md; full Kelly on inflated p over-bets.
"""
from __future__ import annotations

import os

# Kelly multiplier -- quarter Kelly to stay conservative while model is uncalibrated.
KELLY_MULTIPLIER: float = float(os.getenv("KELLY_MULTIPLIER", "0.25"))
KELLY_CAP: float = float(os.getenv("KELLY_CAP", "0.25"))   # max fraction of bankroll
MIN_SIZE_EUR: float = float(os.getenv("MIN_SIZE_EUR", "1.0"))
MAX_SIZE_EUR: float = float(os.getenv("MAX_SIZE_EUR", "10.0"))  # 2x default flat size


def kelly_fraction(p_win: float, price_cents: float, fee_cents: float) -> float:
    """Return the raw (pre-multiplier) Kelly fraction for a binary contract.

    Returns 0.0 when edge is zero or negative (no bet).
    """
    edge = p_win * 100.0 - price_cents - fee_cents
    if edge <= 0:
        return 0.0
    odds = 100.0 - price_cents
    if odds <= 0:
        return 0.0
    return min(edge / odds, KELLY_CAP)


def compute_position_size(
    p_win: float,
    price_cents: float,
    fee_cents: float,
    bankroll: float,
    sizing_mode: str = "flat",
    flat_size: float | None = None,
) -> float:
    """Return position size in EUR.

    Args:
        p_win:       Model win probability (0-1).
        price_cents: Ask price in cents (e.g. 28 for 28c).
        fee_cents:   Estimated fee in cents.
        bankroll:    Available balance in EUR/USDC.
        sizing_mode: "flat" (default) or "kelly".
        flat_size:   Flat size override; if None, uses POSITION_SIZE_EUR from env.

    Returns:
        Position size in EUR. Returns 0.0 when Kelly edge is non-positive.
    """
    if flat_size is None:
        flat_size = float(os.getenv("POSITION_SIZE_EUR", "5.0"))

    if sizing_mode != "kelly":
        return flat_size

    f = kelly_fraction(p_win, price_cents, fee_cents)
    if f <= 0.0:
        return 0.0

    raw_size = bankroll * f * KELLY_MULTIPLIER
    return max(MIN_SIZE_EUR, min(MAX_SIZE_EUR, raw_size))

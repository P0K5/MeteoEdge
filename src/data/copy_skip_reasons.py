"""Copy-signal skip-reason vocabulary, classification and aggregation (#1314).

``copy_signals.skip_reason`` is a plain TEXT column written by
``copy_signal_loop._handle_buy_trade``; before this module nothing read it
back. This module is read-only: it never changes which trades are skipped.

Classes (the operator-facing distinction the issue asks for):

- ``STRUCTURAL`` -- the wallet trades something this engine cannot represent
  at all. More tuning of the wallet/strategy cannot fix it.
- ``ROUTINE`` -- a normal gate rejection (price floor, exposure caps, ...).
- ``LIFECYCLE`` -- expected bookkeeping around following a wallet / market
  state; not a quality signal about the wallet.
- ``BREAKER`` -- a circuit breaker is tripped; account-level, not wallet-level.
- ``UNKNOWN`` -- a value not in the vocabulary (new reason or a typo): surfaced
  so it cannot hide.

Comparison basis for "changed materially": the last 24h count versus the
average daily count over the 7 days before that (``BASELINE_DAYS``).
"""
from __future__ import annotations

STRUCTURAL = "structural"
ROUTINE = "routine"
LIFECYCLE = "lifecycle"
BREAKER = "breaker"
UNKNOWN = "unknown"

# reason -> (class, meaning). Single source for docs/OPERATIONS.md.
SKIP_REASONS: "dict[str, tuple[str, str]]" = {
    "non_binary_outcome_index": (
        STRUCTURAL,
        "trade is on a multi-outcome market (outcomeIndex 2+); the engine only models binary markets",
    ),
    "missing_outcome_index": (
        STRUCTURAL,
        "trade tape gave no usable outcome side; cannot be assigned a position side",
    ),
    "below_min_entry_price": (ROUTINE, "source price under COPY_MIN_ENTRY_PRICE"),
    "duplicate_market_exposure": (ROUTINE, "wallet already holds an open position in this market"),
    "opposite_side_above_parity": (ROUTINE, "opposite side is already held above parity"),
    "wallet_exposure_limit": (ROUTINE, "per-wallet exposure cap reached"),
    "total_exposure_limit": (ROUTINE, "total exposure cap reached"),
    "wallet_newly_followed": (LIFECYCLE, "first poll of a newly followed wallet; history is not copied"),
    "market_resolved": (LIFECYCLE, "market already resolved when the trade was seen"),
    "circuit_breaker_daily_loss": (BREAKER, "paper daily-loss breaker tripped"),
    "circuit_breaker_drawdown": (BREAKER, "paper drawdown breaker tripped"),
}

BASELINE_DAYS = 7
WINDOW_HOURS = 24
# A reason is flagged "SPIKE" when the window count is at least MIN_COUNT and
# at least SPIKE_RATIO x the baseline daily average (or the reason is new).
MIN_COUNT = 5
SPIKE_RATIO = 3.0


def classify(reason: str) -> str:
    if reason in SKIP_REASONS:
        return SKIP_REASONS[reason][0]
    if reason.startswith(("circuit_breaker", "live_circuit_breaker")):
        return BREAKER
    return UNKNOWN


def trend(window: int, baseline_total: int) -> str:
    """``NEW`` (no baseline), ``SPIKE``, or ``STEADY`` -- see module docstring."""
    if window < MIN_COUNT:
        return "STEADY"
    if baseline_total == 0:
        return "NEW"
    if window >= SPIKE_RATIO * (baseline_total / BASELINE_DAYS):
        return "SPIKE"
    return "STEADY"

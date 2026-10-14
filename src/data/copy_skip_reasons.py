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


# Reviewed allow-list (#1390): STRUCTURAL reasons that are known and accepted for
# a wallet, so the daily verdict does not WARN on them forever. Key is
# (wallet address prefix, reason); a prefix of "" applies to every wallet.
# value = (accepting issue/PR, rationale). Only STRUCTURAL reasons can be
# accepted; an accepted reason that is NEW or SPIKEs (see ``trend``) still warns.
# Adding an entry is a reviewed code change: cite the issue/PR that accepted it.
ACCEPTED_STRUCTURAL: "dict[tuple[str, str], tuple[str, str]]" = {
    ("0xbca08c1b", "non_binary_outcome_index"): (
        "#1390",
        "wallet persistently trades multi-outcome markets (~4-6/day); the engine models "
        "binary markets only, so its paper record knowingly excludes them",
    ),
}


def accepted_ref(address: str, reason: str) -> "str | None":
    """Issue/PR that accepted this structural ``reason`` for ``address``, else None."""
    if classify(reason) != STRUCTURAL:
        return None
    for (prefix, r), (ref, _why) in ACCEPTED_STRUCTURAL.items():
        if r == reason and address.startswith(prefix):
            return ref
    return None


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

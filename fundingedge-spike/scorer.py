"""Funding score + entry/exit logic. Stateless; same shape as production module."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from config import (
    ENTRY_THRESHOLD_BPS, EXIT_THRESHOLD_BPS, ENTRY_BASIS_CEILING_BPS,
    EMERGENCY_BASIS_BPS, PERSISTENCE_FRACTION_MIN, MIN_MINUTES_TO_NEXT_FUNDING,
)


@dataclass
class MarketState:
    symbol: str
    now_utc: datetime
    funding_rate: float           # realised, as decimal (not bps); 0.0003 = 3 bps = 0.03%/8h
    funding_time: datetime
    spot_bid: float
    spot_ask: float
    perp_bid: float
    perp_ask: float
    basis_bps: float
    persistence_fraction: float            # fraction of last 72h where rate >= +threshold
    negative_persistence_fraction: float = 0.0  # fraction where rate <= -threshold (observation only)


def rate_to_bps(rate: float) -> float:
    """Convert decimal (0.0003) to bps (3.0)."""
    return rate * 10_000


def should_enter(s: MarketState) -> tuple[bool, str]:
    """Return (decision, reason)."""
    rate_bps = rate_to_bps(s.funding_rate)
    if rate_bps < ENTRY_THRESHOLD_BPS:
        return False, f"rate {rate_bps:.2f} bps < entry threshold {ENTRY_THRESHOLD_BPS}"
    if s.persistence_fraction < PERSISTENCE_FRACTION_MIN:
        return False, f"persistence {s.persistence_fraction:.2f} < {PERSISTENCE_FRACTION_MIN}"
    if s.basis_bps > ENTRY_BASIS_CEILING_BPS:
        return False, f"basis {s.basis_bps:.2f} bps > ceiling {ENTRY_BASIS_CEILING_BPS}"
    minutes_to_funding = (s.funding_time - s.now_utc).total_seconds() / 60
    if minutes_to_funding < MIN_MINUTES_TO_NEXT_FUNDING:
        return False, f"only {minutes_to_funding:.1f} min to next funding"
    return True, "all entry rules passed"


def should_exit(s: MarketState, hedge_age_hours: float, negative_streak: int) -> tuple[bool, str]:
    """Decide whether to close an open virtual hedge."""
    rate_bps = rate_to_bps(s.funding_rate)
    if rate_bps < EXIT_THRESHOLD_BPS:
        return True, f"rate {rate_bps:.2f} bps < exit threshold"
    if negative_streak >= 2:
        return True, f"{negative_streak} consecutive negative-funding windows"
    if s.basis_bps > EMERGENCY_BASIS_BPS:
        return True, f"basis blow-out {s.basis_bps:.2f} bps"
    if hedge_age_hours > 14 * 24:
        return True, "max holding period reached"
    return False, "hold"


def compute_basis_bps(spot_mid: float, perp_mid: float) -> float:
    if spot_mid <= 0:
        return 0.0
    return (perp_mid - spot_mid) / spot_mid * 10_000


def persistence_fraction_from_history(history: list[dict], threshold_rate: float) -> float:
    """Fraction of historical funding payments at or above threshold.
    history items: {"fundingRate": "0.00012", "fundingTime": 1700000000000}."""
    if not history:
        return 0.0
    qualifying = sum(1 for h in history if float(h["fundingRate"]) >= threshold_rate)
    return qualifying / len(history)


def negative_persistence_fraction_from_history(history: list[dict], threshold_rate: float) -> float:
    """Observation-only mirror: fraction of settlements at or below -threshold.
    Logged so we can study negative-funding regimes against the V2 question of
    whether to add the inverse cash-and-carry trade. Not used in entry/exit."""
    if not history:
        return 0.0
    qualifying = sum(1 for h in history if float(h["fundingRate"]) <= -threshold_rate)
    return qualifying / len(history)

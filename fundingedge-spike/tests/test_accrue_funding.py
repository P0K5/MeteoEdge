"""Unit tests for accrue_funding — verifies exactly-once-per-8h accrual logic.

Uses a fake clock (fixed datetime values) so no monkeypatching of the datetime
module is needed. This is possible because spike.py passes clock through to
MarketState.now_utc rather than calling datetime.now() inside accrue_funding.
"""
import sys
import os

# Ensure the fundingedge-spike package root is on the path when running from
# the repo root (e.g. pytest fundingedge-spike/tests/...)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta, timezone

from scorer import MarketState
from spike import accrue_funding


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(
    now_utc: datetime,
    funding_time: datetime,
    funding_rate: float = 0.0003,   # 3 bps
    symbol: str = "BTCUSDT",
) -> MarketState:
    """Build a minimal MarketState for testing accrue_funding."""
    return MarketState(
        symbol=symbol,
        now_utc=now_utc,
        funding_rate=funding_rate,
        funding_time=funding_time,
        spot_bid=60_000.0,
        spot_ask=60_001.0,
        perp_bid=60_005.0,
        perp_ask=60_006.0,
        basis_bps=0.83,
        persistence_fraction=0.80,
    )


def _make_hedge(last_accrued_at=None, notional_usd: float = 500.0) -> dict:
    """Build a minimal hedge dict for testing."""
    return {
        "id": "test1234",
        "symbol": "BTCUSDT",
        "notional_usd": notional_usd,
        "opened_at": "2026-01-01T00:00:00+00:00",
        "spot_entry_price": 60_001.0,
        "perp_entry_price": 60_005.0,
        "entry_basis_bps": 0.83,
        "entry_funding_rate_bps": 3.0,
        "entry_persistence": 0.80,
        "accrued_funding_usd": 0.0,
        "funding_events_count": 0,
        "negative_streak": 0,
        "last_accrued_at": last_accrued_at,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAccrueFunding:
    """Tests for the accrue_funding function."""

    def test_accrues_once_when_settlement_due(self):
        """A hedge with last_accrued_at = 8h ago should accrue exactly one payment."""
        # The funding_time Binance gives us is the *next* settlement.
        # prev_settlement = funding_time - 8h.
        # We set last_accrued_at to 8h before prev_settlement (i.e. 16h before funding_time)
        # so that prev_settlement > last_accrued_at → accrual fires.

        funding_time = datetime(2026, 1, 2, 8, 0, 0, tzinfo=timezone.utc)   # next settlement
        prev_settlement = funding_time - timedelta(hours=8)                  # 2026-01-02 00:00 UTC
        last_accrued = prev_settlement - timedelta(hours=8)                  # 8h before prev_settlement

        now_utc = datetime(2026, 1, 2, 0, 30, 0, tzinfo=timezone.utc)       # 30 min after settlement

        state = _make_state(now_utc=now_utc, funding_time=funding_time, funding_rate=0.0003)
        hedge = _make_hedge(last_accrued_at=last_accrued.isoformat(), notional_usd=500.0)

        accrue_funding(hedge, state)

        expected_payment = 0.0003 * 500.0  # = 0.15 USD
        assert hedge["funding_events_count"] == 1, "Should fire exactly once"
        assert abs(hedge["accrued_funding_usd"] - expected_payment) < 1e-9, (
            f"Expected {expected_payment}, got {hedge['accrued_funding_usd']}"
        )
        assert hedge["last_accrued_at"] == prev_settlement.isoformat(), (
            "last_accrued_at should be updated to prev_settlement"
        )

    def test_no_double_accrual_same_state(self):
        """Calling accrue_funding twice with the same state must NOT double-accrue.

        This is the crash-resume idempotency guarantee: even if poll_once runs
        twice on the same funding boundary, the hedge accrues only once.
        """
        funding_time = datetime(2026, 1, 2, 8, 0, 0, tzinfo=timezone.utc)
        prev_settlement = funding_time - timedelta(hours=8)
        last_accrued = prev_settlement - timedelta(hours=8)

        now_utc = datetime(2026, 1, 2, 0, 30, 0, tzinfo=timezone.utc)

        state = _make_state(now_utc=now_utc, funding_time=funding_time, funding_rate=0.0003)
        hedge = _make_hedge(last_accrued_at=last_accrued.isoformat(), notional_usd=500.0)

        accrue_funding(hedge, state)   # first call — should accrue
        accrue_funding(hedge, state)   # second call — should be a no-op

        assert hedge["funding_events_count"] == 1, (
            "Should still be 1 after duplicate call — no double-accrual"
        )
        assert abs(hedge["accrued_funding_usd"] - 0.0003 * 500.0) < 1e-9

    def test_no_accrual_when_already_current(self):
        """If last_accrued_at == prev_settlement, no new accrual should fire."""
        funding_time = datetime(2026, 1, 2, 8, 0, 0, tzinfo=timezone.utc)
        prev_settlement = funding_time - timedelta(hours=8)

        now_utc = datetime(2026, 1, 2, 0, 30, 0, tzinfo=timezone.utc)

        state = _make_state(now_utc=now_utc, funding_time=funding_time, funding_rate=0.0003)
        # Hedge was already accrued at the most recent settlement
        hedge = _make_hedge(last_accrued_at=prev_settlement.isoformat(), notional_usd=500.0)

        accrue_funding(hedge, state)

        assert hedge["funding_events_count"] == 0, (
            "No accrual expected when already up to date"
        )
        assert hedge["accrued_funding_usd"] == 0.0

    def test_accrues_first_time_no_last_accrued(self):
        """A brand-new hedge (last_accrued_at=None) always accrues on first poll."""
        funding_time = datetime(2026, 1, 2, 8, 0, 0, tzinfo=timezone.utc)
        now_utc = datetime(2026, 1, 2, 0, 30, 0, tzinfo=timezone.utc)

        state = _make_state(now_utc=now_utc, funding_time=funding_time, funding_rate=0.0005)
        hedge = _make_hedge(last_accrued_at=None, notional_usd=1000.0)

        accrue_funding(hedge, state)

        expected = 0.0005 * 1000.0  # = 0.50 USD
        assert hedge["funding_events_count"] == 1
        assert abs(hedge["accrued_funding_usd"] - expected) < 1e-9

    def test_multiple_sequential_settlements(self):
        """Advancing funding_time by 8h each round should accrue once per advance."""
        notional = 500.0
        rate = 0.0003
        hedge = _make_hedge(last_accrued_at=None, notional_usd=notional)

        base_funding = datetime(2026, 1, 2, 8, 0, 0, tzinfo=timezone.utc)
        now_base = datetime(2026, 1, 2, 0, 30, 0, tzinfo=timezone.utc)

        # First settlement window
        state1 = _make_state(now_utc=now_base, funding_time=base_funding, funding_rate=rate)
        accrue_funding(hedge, state1)
        assert hedge["funding_events_count"] == 1

        # Next settlement window — funding_time advances by 8h
        state2 = _make_state(
            now_utc=now_base + timedelta(hours=8),
            funding_time=base_funding + timedelta(hours=8),
            funding_rate=rate,
        )
        accrue_funding(hedge, state2)
        assert hedge["funding_events_count"] == 2
        assert abs(hedge["accrued_funding_usd"] - 2 * rate * notional) < 1e-9

        # Calling again with the same state2 should NOT double-accrue
        accrue_funding(hedge, state2)
        assert hedge["funding_events_count"] == 2

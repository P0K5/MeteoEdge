"""Unit tests for src/strategy/fee.py."""
import pytest
from src.strategy.fee import estimate_fee_cents


class TestEstimateFeeCents:
    """Tests for the Polymarket taker fee approximation."""

    def test_fee_at_fifty_cents_is_max(self):
        """50¢ price gives the maximum fee (0.07 * 0.5 * 0.5 = 1.75¢)."""
        fee = estimate_fee_cents(50)
        assert abs(fee - 1.75) < 0.01

    def test_fee_at_one_cent_floors_to_one(self):
        """1¢ price gives minimum fee (floored at 1.0¢)."""
        fee = estimate_fee_cents(1)
        assert fee == pytest.approx(1.0)

    def test_fee_at_ninety_nine_cents_floors_to_one(self):
        """99¢ price gives minimum fee (floored at 1.0¢)."""
        fee = estimate_fee_cents(99)
        assert fee == pytest.approx(1.0, abs=0.1)

    def test_fee_is_symmetric(self):
        """Fee at price p equals fee at price (100-p) by symmetry."""
        assert abs(estimate_fee_cents(30) - estimate_fee_cents(70)) < 0.001

    def test_fee_always_at_least_one_cent(self):
        """Fee is always >= 1¢ for any valid price."""
        for price in range(1, 100):
            assert estimate_fee_cents(price) >= 1.0

    def test_fee_midpoint_greater_than_floor(self):
        """Fee at 50¢ must be greater than the 1¢ floor."""
        assert estimate_fee_cents(50) > 1.0

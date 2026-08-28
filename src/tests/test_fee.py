"""Unit tests for src/strategy/fee.py."""
import pytest
from src.strategy.fee import estimate_fee_cents


class TestEstimateFeeCents:
    """Tests for the Polymarket fee model (weather markets, 0.05% taker)."""

    def test_fee_at_fifty_cents_is_max(self):
        """50¢ price gives the maximum fee (0.05 * 0.5 * 0.5 * 100 = 1.25¢)."""
        fee = estimate_fee_cents(50)
        assert fee == pytest.approx(1.25, abs=1e-4)

    def test_fee_at_ninety_nine_cents(self):
        """99¢ price gives 0.05 * 0.99 * 0.01 * 100 = 0.0495¢ (not floored)."""
        fee = estimate_fee_cents(99)
        assert fee == pytest.approx(0.0495, abs=1e-4)

    def test_fee_at_one_cent(self):
        """1¢ price gives 0.05 * 0.01 * 0.99 * 100 = 0.0495¢ (not floored)."""
        fee = estimate_fee_cents(1)
        assert fee == pytest.approx(0.0495, abs=1e-4)

    def test_fee_is_symmetric(self):
        """Fee at price p equals fee at price (100-p) by symmetry."""
        assert abs(estimate_fee_cents(30) - estimate_fee_cents(70)) < 1e-6

    def test_fee_never_clamped_to_floor(self):
        """Regression test: the old 1.0¢ floor is removed."""
        # At 1¢ and 99¢, the old model would return 1.0¢
        # The new model returns 0.0495¢ (no floor)
        assert estimate_fee_cents(1) < 0.1
        assert estimate_fee_cents(99) < 0.1

    def test_fee_monotonically_decreasing_from_midpoint(self):
        """Fee decreases monotonically as price moves away from 50¢."""
        midpoint = estimate_fee_cents(50)
        # Test moving left from 50
        for price in range(49, 25, -1):
            assert estimate_fee_cents(price) < estimate_fee_cents(price + 1)
        # Test moving right from 50
        for price in range(51, 76):
            assert estimate_fee_cents(price) < estimate_fee_cents(price - 1)

    def test_maker_fee_is_zero(self):
        """Maker fees are always zero (0% maker fee)."""
        for price in range(1, 100):
            assert estimate_fee_cents(price, maker=True) == 0.0

    def test_maker_fee_explicit_true(self):
        """Maker=True returns exactly 0.0."""
        assert estimate_fee_cents(50, maker=True) == 0.0
        assert estimate_fee_cents(1, maker=True) == 0.0
        assert estimate_fee_cents(99, maker=True) == 0.0

    def test_maker_fee_explicit_false(self):
        """Maker=False (default) returns taker fee."""
        # Should be identical whether maker is explicit False or default
        default_50 = estimate_fee_cents(50)
        explicit_50 = estimate_fee_cents(50, maker=False)
        assert default_50 == explicit_50

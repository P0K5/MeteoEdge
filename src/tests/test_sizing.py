"""Tests for src/strategy/sizing.py"""
import pytest
from src.strategy.sizing import kelly_fraction, compute_position_size


class TestKellyFraction:
    def test_positive_edge(self):
        # p=0.90, price=28, fee=1 -> edge=61, odds=72 -> f=61/72~0.847, capped at 0.25
        f = kelly_fraction(0.90, 28.0, 1.0)
        assert f == pytest.approx(0.25)  # KELLY_CAP

    def test_zero_edge_returns_zero(self):
        # p=0.30, price=29, fee=1 -> edge=0
        f = kelly_fraction(0.30, 29.0, 1.0)
        assert f == 0.0

    def test_negative_edge_returns_zero(self):
        f = kelly_fraction(0.20, 29.0, 1.0)
        assert f == 0.0

    def test_fee_reduces_edge(self):
        # Without fee: edge=p*100-price. With fee: edge=p*100-price-fee
        f_no_fee = kelly_fraction(0.50, 45.0, 0.0)
        f_with_fee = kelly_fraction(0.50, 45.0, 10.0)
        assert f_no_fee > f_with_fee

    def test_cap_binds(self):
        # Very high p -> fraction would exceed cap
        f = kelly_fraction(0.99, 1.0, 0.0)
        assert f == pytest.approx(0.25)  # capped at KELLY_CAP


class TestComputePositionSize:
    def test_flat_mode_returns_flat_size(self):
        size = compute_position_size(0.90, 28.0, 1.0, bankroll=100.0, sizing_mode="flat", flat_size=5.0)
        assert size == 5.0

    def test_flat_mode_ignores_bankroll(self):
        size1 = compute_position_size(0.90, 28.0, 1.0, bankroll=10.0, sizing_mode="flat", flat_size=5.0)
        size2 = compute_position_size(0.90, 28.0, 1.0, bankroll=1000.0, sizing_mode="flat", flat_size=5.0)
        assert size1 == size2 == 5.0

    def test_kelly_zero_edge_returns_zero(self):
        size = compute_position_size(0.20, 29.0, 1.0, bankroll=100.0, sizing_mode="kelly", flat_size=5.0)
        assert size == 0.0

    def test_kelly_respects_min_clamp(self):
        # Small bankroll -> raw size below MIN_SIZE_EUR
        size = compute_position_size(0.60, 45.0, 1.0, bankroll=1.0, sizing_mode="kelly", flat_size=5.0)
        assert size >= 1.0  # MIN_SIZE_EUR

    def test_kelly_respects_max_clamp(self):
        # Large bankroll -> raw size above MAX_SIZE_EUR
        size = compute_position_size(0.90, 28.0, 1.0, bankroll=10000.0, sizing_mode="kelly", flat_size=5.0)
        assert size <= 10.0  # MAX_SIZE_EUR

    def test_kelly_scales_with_bankroll(self):
        size_small = compute_position_size(0.70, 40.0, 1.0, bankroll=10.0, sizing_mode="kelly", flat_size=5.0)
        size_large = compute_position_size(0.70, 40.0, 1.0, bankroll=100.0, sizing_mode="kelly", flat_size=5.0)
        # Larger bankroll -> larger or equal size (until max clamp)
        assert size_large >= size_small

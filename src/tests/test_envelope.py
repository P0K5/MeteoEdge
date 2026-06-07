"""Unit tests for src/model/envelope.py — pure math functions, no API calls.

Ported from archive/polymarket-spike/tests/test_envelope.py with imports
updated to use src.model.envelope (src.model.climb_rates provides climb rates).
"""
from datetime import datetime
from math import isclose

import pytest

from src.model.envelope import (
    Bracket,
    WeatherState,
    compute_envelope,
    p_normal_between,
    true_probability_yes,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_state(
    current_high_f: float = 80.0,
    latest_temp_f: float = 80.0,
    forecast_high_f: float | None = 81.0,
    hour: int = 14,
    station: str = "KNYC",
) -> WeatherState:
    """Build a WeatherState with sensible defaults for testing."""
    now = datetime(2026, 5, 15, hour, 30)
    return WeatherState(
        station=station,
        now_local=now,
        sunset_local=datetime(2026, 5, 15, 20, 15),
        current_high_f=current_high_f,
        current_high_time=now,
        latest_temp_f=latest_temp_f,
        latest_temp_time=now,
        forecast_high_f=forecast_high_f,
    )


def make_bracket(
    low_f: float,
    high_f: float,
    yes_ask_cents: int = 50,
    no_ask_cents: int = 52,
) -> Bracket:
    return Bracket(
        ticker="TEST-TICKER",
        low_f=low_f,
        high_f=high_f,
        yes_ask_cents=yes_ask_cents,
        yes_ask_size=100,
        no_ask_cents=no_ask_cents,
        no_ask_size=100,
    )


# ---------------------------------------------------------------------------
# p_normal_between
# ---------------------------------------------------------------------------

class TestPNormalBetween:
    def test_known_value_symmetric_interval(self):
        """P(80 <= X <= 82) where X~N(81, 2^2) ≈ 0.3829."""
        result = p_normal_between(80.0, 82.0, mean=81.0, stddev=2.0)
        assert isclose(result, 0.3829, abs_tol=0.001), f"Got {result}"

    def test_wide_interval_near_one(self):
        result = p_normal_between(-100.0, 100.0, mean=50.0, stddev=5.0)
        assert result > 0.9999

    def test_interval_far_from_mean_near_zero(self):
        result = p_normal_between(200.0, 210.0, mean=80.0, stddev=2.0)
        assert result < 1e-6

    def test_clamps_to_zero(self):
        result = p_normal_between(100.0, 90.0, mean=80.0, stddev=2.0)
        assert result == 0.0

    def test_clamps_to_one(self):
        result = p_normal_between(-1000.0, 1000.0, mean=0.0, stddev=1.0)
        assert result == 1.0

    def test_symmetric_around_mean(self):
        mean, d, stddev = 75.0, 3.0, 2.0
        left = p_normal_between(mean - d, mean, mean=mean, stddev=stddev)
        right = p_normal_between(mean, mean + d, mean=mean, stddev=stddev)
        assert isclose(left, right, abs_tol=1e-10)

    def test_point_interval_near_zero(self):
        result = p_normal_between(81.0, 81.0, mean=81.0, stddev=2.0)
        assert result == 0.0

    def test_known_value_one_sigma(self):
        """P(mean - sigma <= X <= mean + sigma) ≈ 0.6827."""
        mean, stddev = 80.0, 3.0
        result = p_normal_between(mean - stddev, mean + stddev, mean=mean, stddev=stddev)
        assert isclose(result, 0.6827, abs_tol=0.001), f"Got {result}"


# ---------------------------------------------------------------------------
# compute_envelope — climb_rates values (May): hour14=4.5, hour12=6, hour21=0, hour15=3.5
# ---------------------------------------------------------------------------

class TestComputeEnvelope:
    def test_min_high_equals_current_high(self):
        state = make_state(current_high_f=82.0, latest_temp_f=80.0, hour=14)
        min_high, _ = compute_envelope(state)
        assert min_high == 82.0

    def test_max_high_gte_current_high(self):
        state = make_state(current_high_f=82.0, latest_temp_f=80.0, hour=14)
        _, max_high = compute_envelope(state)
        assert max_high >= 82.0

    def test_max_high_uses_climb_from_latest_temp(self):
        """hour=14, climb=4.5: max = max(82, 80+4.5) = 84.5."""
        state = make_state(current_high_f=82.0, latest_temp_f=80.0, hour=14)
        min_high, max_high = compute_envelope(state)
        assert min_high == 82.0
        assert max_high == 84.5

    def test_max_high_when_latest_temp_above_high(self):
        """hour=12, climb=6: max = max(85, 85+6) = 91."""
        state = make_state(current_high_f=85.0, latest_temp_f=85.0, hour=12)
        _, max_high = compute_envelope(state)
        assert max_high == 91.0

    def test_no_rise_after_hour_20(self):
        """After 8pm, expected additional rise is 0."""
        state = make_state(current_high_f=88.0, latest_temp_f=88.0, hour=21)
        min_high, max_high = compute_envelope(state)
        assert min_high == 88.0
        assert max_high == 88.0

    def test_envelope_bounds_when_latest_temp_lower(self):
        """hour=15, climb=3.5: max = max(85, 79+3.5) = max(85, 82.5) = 85."""
        state = make_state(current_high_f=85.0, latest_temp_f=79.0, hour=15)
        min_high, max_high = compute_envelope(state)
        assert min_high == 85.0
        assert max_high == 85.0

    def test_minutes_to_settlement_optional(self):
        """compute_envelope must accept zero positional arguments beyond state."""
        state = make_state(current_high_f=80.0, latest_temp_f=80.0, hour=14)
        result = compute_envelope(state)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# true_probability_yes
# ---------------------------------------------------------------------------

class TestTrueProbabilityYes:
    def test_bracket_below_current_high_returns_zero(self):
        state = make_state(current_high_f=85.0, latest_temp_f=83.0, hour=15)
        bracket = make_bracket(low_f=78.0, high_f=84.0)
        assert true_probability_yes(bracket, state) == 0.0

    def test_bracket_above_envelope_returns_zero(self):
        state = make_state(current_high_f=85.0, latest_temp_f=85.0, hour=21)
        bracket = make_bracket(low_f=90.0, high_f=95.0)
        assert true_probability_yes(bracket, state) == 0.0

    def test_bracket_contains_full_envelope_returns_one(self):
        state = make_state(current_high_f=82.0, latest_temp_f=82.0, hour=21)
        bracket = make_bracket(low_f=70.0, high_f=90.0)
        assert true_probability_yes(bracket, state) == 1.0

    def test_exact_envelope_bracket_returns_one(self):
        state = make_state(current_high_f=82.0, latest_temp_f=82.0, hour=21)
        bracket = make_bracket(low_f=82.0, high_f=82.0)
        assert true_probability_yes(bracket, state) == 1.0

    def test_bayesian_case_uses_forecast(self):
        """Partial bracket in mid-day uncertainty window returns 0 < p < 1."""
        # hour=14, climb=4.5; max_env = max(80, 79+4.5) = 83.5
        # bracket [81, 85]: lo(81) > current_high(80) but lo < max_env(83.5)
        state = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=82.0)
        bracket = make_bracket(low_f=81.0, high_f=85.0)
        result = true_probability_yes(bracket, state)
        assert 0.0 < result < 1.0

    def test_no_forecast_falls_back_to_midpoint(self):
        """With no forecast, bracket containing full envelope returns 1.0."""
        state = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=None)
        bracket = make_bracket(low_f=80.0, high_f=84.0)
        result = true_probability_yes(bracket, state)
        assert result == 1.0

    def test_high_confidence_no_side(self):
        """Bracket far above max envelope returns 0.0."""
        state = make_state(current_high_f=79.0, latest_temp_f=78.0, hour=14, forecast_high_f=80.0)
        bracket = make_bracket(low_f=95.0, high_f=100.0)
        result = true_probability_yes(bracket, state)
        assert result == 0.0

    def test_probability_bounded_zero_to_one(self):
        state = make_state(current_high_f=82.0, latest_temp_f=81.0, hour=13, forecast_high_f=84.0)
        for lo, hi in [(70, 75), (80, 85), (85, 90), (100, 110)]:
            bracket = make_bracket(low_f=float(lo), high_f=float(hi))
            result = true_probability_yes(bracket, state)
            assert 0.0 <= result <= 1.0, f"Out of bounds for [{lo}, {hi}]: {result}"

    def test_minutes_to_settlement_optional(self):
        """true_probability_yes must work with just bracket and state."""
        state = make_state(current_high_f=80.0, latest_temp_f=80.0, hour=14, forecast_high_f=82.0)
        bracket = make_bracket(low_f=79.0, high_f=85.0)
        result = true_probability_yes(bracket, state)
        assert 0.0 <= result <= 1.0

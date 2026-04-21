"""Unit tests for envelope.py pure math functions.
No external API calls required — all tests use synthetic inputs.
"""
import sys
import os
import pytest
from datetime import datetime
from math import isclose

# Ensure meteoedge-spike is on the path so imports resolve without installation
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from envelope import (
    Bracket,
    WeatherState,
    p_normal_between,
    compute_envelope,
    true_probability_yes,
    expected_additional_rise,
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
    now = datetime(2024, 7, 15, hour, 30)
    return WeatherState(
        station=station,
        now_local=now,
        sunset_local=datetime(2024, 7, 15, 20, 15),
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
# p_normal_between tests
# ---------------------------------------------------------------------------

class TestPNormalBetween:
    def test_known_value_symmetric_interval(self):
        """P(80 <= X <= 82) where X~N(81, 2^2).
        By symmetry this equals 2*Phi(0.5) - 1 ≈ 0.3829.
        """
        result = p_normal_between(80.0, 82.0, mean=81.0, stddev=2.0)
        assert isclose(result, 0.3829, abs_tol=0.001), f"Got {result}"

    def test_wide_interval_near_one(self):
        """P(-100 <= X <= 100) where X~N(50, 5^2) should be essentially 1."""
        result = p_normal_between(-100.0, 100.0, mean=50.0, stddev=5.0)
        assert result > 0.9999

    def test_interval_far_from_mean_near_zero(self):
        """P(200 <= X <= 210) where X~N(80, 2^2) should be ~0."""
        result = p_normal_between(200.0, 210.0, mean=80.0, stddev=2.0)
        assert result < 1e-6

    def test_clamps_to_zero(self):
        """Result must never be negative."""
        result = p_normal_between(100.0, 90.0, mean=80.0, stddev=2.0)
        assert result == 0.0

    def test_clamps_to_one(self):
        """Result must never exceed 1."""
        result = p_normal_between(-1000.0, 1000.0, mean=0.0, stddev=1.0)
        assert result == 1.0

    def test_symmetric_around_mean(self):
        """P(mean-d <= X <= mean) == P(mean <= X <= mean+d) by symmetry."""
        mean, d, stddev = 75.0, 3.0, 2.0
        left = p_normal_between(mean - d, mean, mean=mean, stddev=stddev)
        right = p_normal_between(mean, mean + d, mean=mean, stddev=stddev)
        assert isclose(left, right, abs_tol=1e-10)

    def test_point_interval_near_zero(self):
        """P(X == 81) is 0 for a continuous distribution."""
        result = p_normal_between(81.0, 81.0, mean=81.0, stddev=2.0)
        # CDF(81) - CDF(81) = 0
        assert result == 0.0

    def test_known_value_one_sigma(self):
        """P(mean - sigma <= X <= mean + sigma) ≈ 0.6827."""
        mean, stddev = 80.0, 3.0
        result = p_normal_between(mean - stddev, mean + stddev, mean=mean, stddev=stddev)
        assert isclose(result, 0.6827, abs_tol=0.001), f"Got {result}"


# ---------------------------------------------------------------------------
# compute_envelope tests
# ---------------------------------------------------------------------------

class TestComputeEnvelope:
    def test_min_high_equals_current_high(self):
        """min_plausible_high is always the current recorded daily high."""
        state = make_state(current_high_f=82.0, latest_temp_f=80.0, hour=14)
        min_high, _ = compute_envelope(state)
        assert min_high == 82.0

    def test_max_high_gte_current_high(self):
        """max_plausible_high must be >= current_high_f (daily high can't go down)."""
        state = make_state(current_high_f=82.0, latest_temp_f=80.0, hour=14)
        _, max_high = compute_envelope(state)
        assert max_high >= 82.0

    def test_max_high_uses_climb_from_latest_temp(self):
        """At hour 14, DEFAULT_CLIMB_LOOKUP[14]=4. latest_temp=80 -> max = max(82, 80+4)=84."""
        state = make_state(current_high_f=82.0, latest_temp_f=80.0, hour=14)
        min_high, max_high = compute_envelope(state)
        assert min_high == 82.0
        assert max_high == 84.0

    def test_max_high_when_latest_temp_above_high(self):
        """latest_temp may equal current high (e.g. rising all day)."""
        # hour=12, climb=6: max = max(85, 85+6) = 91
        state = make_state(current_high_f=85.0, latest_temp_f=85.0, hour=12)
        _, max_high = compute_envelope(state)
        assert max_high == 91.0

    def test_no_rise_after_hour_20(self):
        """After 8pm, expected additional rise is 0; max_high == current_high if no more climb."""
        # latest_temp == current_high at hour 21; no further climb expected
        state = make_state(current_high_f=88.0, latest_temp_f=88.0, hour=21)
        min_high, max_high = compute_envelope(state)
        assert min_high == 88.0
        assert max_high == 88.0

    def test_envelope_bounds_when_latest_temp_lower(self):
        """If latest_temp is below current high, max = max(current_high, latest_temp + climb)."""
        # hour=15, climb=3: max = max(85, 79+3) = max(85, 82) = 85
        state = make_state(current_high_f=85.0, latest_temp_f=79.0, hour=15)
        min_high, max_high = compute_envelope(state)
        assert min_high == 85.0
        assert max_high == 85.0


# ---------------------------------------------------------------------------
# true_probability_yes tests
# ---------------------------------------------------------------------------

class TestTrueProbabilityYes:
    def test_bracket_below_current_high_returns_zero(self):
        """A bracket whose top is below the current daily high is impossible (returns 0.0)."""
        # current_high=85, bracket is [78, 84]
        state = make_state(current_high_f=85.0, latest_temp_f=83.0, hour=15)
        bracket = make_bracket(low_f=78.0, high_f=84.0)
        assert true_probability_yes(bracket, state) == 0.0

    def test_bracket_above_envelope_returns_zero(self):
        """A bracket above the max envelope is physically impossible (returns 0.0)."""
        # hour=21, no climb; current_high=85, latest=85 -> max_env=85
        # bracket [90, 95] is above envelope
        state = make_state(current_high_f=85.0, latest_temp_f=85.0, hour=21)
        bracket = make_bracket(low_f=90.0, high_f=95.0)
        assert true_probability_yes(bracket, state) == 0.0

    def test_bracket_contains_full_envelope_returns_one(self):
        """If the bracket fully contains [current_high, max_env], the outcome is certain (1.0)."""
        # hour=21, no climb; current_high=82, max_env=82
        # bracket [70, 90] fully contains envelope [82, 82]
        state = make_state(current_high_f=82.0, latest_temp_f=82.0, hour=21)
        bracket = make_bracket(low_f=70.0, high_f=90.0)
        assert true_probability_yes(bracket, state) == 1.0

    def test_exact_envelope_bracket_returns_one(self):
        """A bracket exactly matching [current_high, max_env] returns 1.0."""
        # hour=21; current_high=82 => min_env=82, max_env=82
        state = make_state(current_high_f=82.0, latest_temp_f=82.0, hour=21)
        bracket = make_bracket(low_f=82.0, high_f=82.0)
        assert true_probability_yes(bracket, state) == 1.0

    def test_bayesian_case_uses_forecast(self):
        """Partial bracket — result should be a p_normal_between value, 0 < p < 1."""
        # hour=21; current_high=80, max_env=80; forecast=80
        # bracket [78, 82] overlaps but doesn't fully contain
        # Bracket hi(82) >= current_high(80) and lo(78) <= current_high(80),
        # but hi(82) > max_env(80) — so lo <= current_high and hi >= max_env → returns 1.0
        # Use a mid-day scenario where there's still uncertainty
        state = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=82.0)
        # hour=14, climb=4; max_env = max(80, 79+4) = 83
        # bracket [81, 85]: lo(81) > current_high(80) but lo(81) <= max_env(83)
        # doesn't trigger either shortcut -> bayesian
        bracket = make_bracket(low_f=81.0, high_f=85.0)
        result = true_probability_yes(bracket, state)
        assert 0.0 < result < 1.0

    def test_no_forecast_falls_back_to_midpoint(self):
        """With no forecast, mean is (current_high + max_env) / 2."""
        state = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=None)
        # max_env = max(80, 79+4) = 83; midpoint mean = (80+83)/2 = 81.5
        bracket = make_bracket(low_f=80.0, high_f=83.0)
        result = true_probability_yes(bracket, state)
        # Should be near 1.0 since bracket fully contains envelope
        assert result == 1.0

    def test_high_confidence_no_side(self):
        """A bracket well above forecast should yield very low p_yes."""
        # forecast=80, stddev=2; bracket [95, 100] is 7.5+ sigma away
        state = make_state(current_high_f=79.0, latest_temp_f=78.0, hour=14, forecast_high_f=80.0)
        # max_env = max(79, 78+4) = 82; bracket [95,100] > max_env(82) -> returns 0.0
        bracket = make_bracket(low_f=95.0, high_f=100.0)
        result = true_probability_yes(bracket, state)
        assert result == 0.0

    def test_probability_bounded_zero_to_one(self):
        """Result is always in [0, 1]."""
        state = make_state(current_high_f=82.0, latest_temp_f=81.0, hour=13, forecast_high_f=84.0)
        for lo, hi in [(70, 75), (80, 85), (85, 90), (100, 110)]:
            bracket = make_bracket(low_f=float(lo), high_f=float(hi))
            result = true_probability_yes(bracket, state)
            assert 0.0 <= result <= 1.0, f"Out of bounds for [{lo}, {hi}]: {result}"


# ---------------------------------------------------------------------------
# parse_bracket_from_market tests (no API calls — pure parsing)
# ---------------------------------------------------------------------------

class TestParseBracketFromMarket:
    """Test the regex parsing in spike.parse_bracket_from_market."""

    def setup_method(self):
        # Import here so test file doesn't require spike's runtime deps at module level
        import importlib.util
        import sys as _sys
        spec_path = os.path.join(os.path.dirname(__file__), "..", "spike.py")
        spec = importlib.util.spec_from_file_location("spike_module", spec_path)
        mod = importlib.util.module_from_spec(spec)
        # We need the deps available; skip if not installed
        try:
            spec.loader.exec_module(mod)
            self.parse = mod.parse_bracket_from_market
        except ImportError:
            pytest.skip("spike.py runtime deps not installed")

    def _market(self, subtitle: str, ticker: str = "T-TEST") -> dict:
        return {"ticker": ticker, "subtitle": subtitle}

    def test_range_dash(self):
        b = self.parse(self._market("82-84°"))
        assert b is not None
        assert b.low_f == 82.0
        assert b.high_f == 84.0

    def test_range_to(self):
        b = self.parse(self._market("82 to 84 degrees"))
        assert b is not None
        assert b.low_f == 82.0
        assert b.high_f == 84.0

    def test_range_em_dash(self):
        b = self.parse(self._market("82–84°"))
        assert b is not None
        assert b.low_f == 82.0
        assert b.high_f == 84.0

    def test_gte_symbol(self):
        b = self.parse(self._market(">=85°"))
        assert b is not None
        assert b.low_f == 85.0
        assert b.high_f == 200.0

    def test_lte_symbol(self):
        b = self.parse(self._market("<=82°"))
        assert b is not None
        assert b.low_f == -50.0
        assert b.high_f == 82.0

    def test_unrecognized_returns_none(self):
        b = self.parse(self._market("something unexpected 999xyz"))
        assert b is None

    def test_missing_ticker_returns_none(self):
        b = self.parse({"subtitle": "82-84°"})
        assert b is None

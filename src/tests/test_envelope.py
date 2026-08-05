"""Unit tests for src/model/envelope.py — pure math functions, no API calls.

Ported from archive/polymarket-spike/tests/test_envelope.py with imports
updated to use src.model.envelope (src.model.climb_rates provides climb rates).
"""
import os
from datetime import date, datetime
from math import isclose

import pytest

from src.model.envelope import (
    Bracket,
    WeatherState,
    compute_envelope,
    ensemble_forecast,
    next_day_probability_yes,
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
    obs_bias_offset_f: float | None = None,
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
        obs_bias_offset_f=obs_bias_offset_f,
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

    def test_stddev_zero_mean_inside_bracket(self):
        """When stddev=0, distribution is point mass at mean.
        P(low <= mean <= high) = 1 when low <= mean <= high."""
        result = p_normal_between(79.0, 83.0, mean=81.0, stddev=0.0)
        assert result == 1.0

    def test_stddev_zero_mean_below_bracket(self):
        """When stddev=0 and mean < low, probability is 0."""
        result = p_normal_between(82.0, 84.0, mean=81.0, stddev=0.0)
        assert result == 0.0

    def test_stddev_zero_mean_above_bracket(self):
        """When stddev=0 and mean > high, probability is 0."""
        result = p_normal_between(78.0, 80.0, mean=81.0, stddev=0.0)
        assert result == 0.0

    def test_stddev_zero_mean_at_low_edge(self):
        """When stddev=0 and mean equals low edge (inclusive), probability is 1."""
        result = p_normal_between(81.0, 83.0, mean=81.0, stddev=0.0)
        assert result == 1.0

    def test_stddev_zero_mean_at_high_edge(self):
        """When stddev=0 and mean equals high edge (exclusive), probability is 0."""
        result = p_normal_between(79.0, 81.0, mean=81.0, stddev=0.0)
        assert result == 0.0

    def test_stddev_zero_point_mass(self):
        """When stddev=0 and the bracket is [81, 81) (empty range), probability is 0."""
        result = p_normal_between(81.0, 81.0, mean=81.0, stddev=0.0)
        assert result == 0.0


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
        """hour=14, KNYC falls back to DEFAULT_CLIMB_LOOKUP: climb=4.0: max = max(82, 80+4.0) = 84.0."""
        state = make_state(current_high_f=82.0, latest_temp_f=80.0, hour=14)
        min_high, max_high = compute_envelope(state)
        assert min_high == 82.0
        assert max_high == 84.0

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
# compute_envelope — per-station coverage for previously-fallback international
# stations (issue #571). Before this fix, EGLC and ZGGG had no CLIMB_LOOKUP
# entry and fell back to the flat, month-agnostic, US-continental-shaped
# _DEFAULT_CLIMB_LOOKUP. They now resolve to per-station (synthetic
# climatological or DB-derived) values that vary by month/hour and diverge
# sharply from the old flat default.
# ---------------------------------------------------------------------------

class TestPreviouslyFallbackInternationalStations:
    def test_eglc_january_midnight_uses_per_station_table_not_flat_default(self):
        """EGLC (London City), January hour=0: per-station climb is 3.6F.

        The old flat _DEFAULT_CLIMB_LOOKUP[0] was 25.0F (a US-continental
        summer diurnal range applied year-round). London's actual winter
        diurnal range is far smaller, so the per-station value must differ
        from the flat default by a wide margin.
        """
        from src.data.climb_lookup import CLIMB_LOOKUP
        from src.model.climb_rates import _DEFAULT_CLIMB_LOOKUP

        station_climb = CLIMB_LOOKUP["EGLC"][1][0]
        assert station_climb == pytest.approx(3.6)

        flat_default = _DEFAULT_CLIMB_LOOKUP[0]
        assert flat_default == 25.0
        assert abs(station_climb - flat_default) > 10.0, (
            "per-station EGLC value should diverge sharply from the flat "
            "US-shaped default"
        )

        state = WeatherState(
            station="EGLC",
            now_local=datetime(2026, 1, 15, 0, 30),
            sunset_local=datetime(2026, 1, 15, 16, 0),
            current_high_f=40.0,
            current_high_time=datetime(2026, 1, 15, 0, 30),
            latest_temp_f=40.0,
            latest_temp_time=datetime(2026, 1, 15, 0, 30),
            forecast_high_f=None,
        )
        min_high, max_high = compute_envelope(state)
        assert min_high == 40.0
        # max_high must be sourced from the per-station table (40 + 3.6),
        # not the flat default (40 + 25.0 = 65.0).
        assert max_high == pytest.approx(43.6)
        assert max_high != pytest.approx(40.0 + flat_default)

    def test_zggg_january_midnight_uses_per_station_table_not_flat_default(self):
        """ZGGG (Guangzhou), January hour=0: per-station climb is 7.0F.

        ZGGG is slated for live NO promotion (#557), which is blocked on
        this station having real per-station coverage instead of the flat
        US default.
        """
        from src.data.climb_lookup import CLIMB_LOOKUP
        from src.model.climb_rates import _DEFAULT_CLIMB_LOOKUP

        station_climb = CLIMB_LOOKUP["ZGGG"][1][0]
        assert station_climb == pytest.approx(7.0)

        flat_default = _DEFAULT_CLIMB_LOOKUP[0]
        assert abs(station_climb - flat_default) > 10.0, (
            "per-station ZGGG value should diverge sharply from the flat "
            "US-shaped default"
        )

        state = WeatherState(
            station="ZGGG",
            now_local=datetime(2026, 1, 15, 0, 30),
            sunset_local=datetime(2026, 1, 15, 18, 0),
            current_high_f=55.0,
            current_high_time=datetime(2026, 1, 15, 0, 30),
            latest_temp_f=55.0,
            latest_temp_time=datetime(2026, 1, 15, 0, 30),
            forecast_high_f=None,
        )
        min_high, max_high = compute_envelope(state)
        assert min_high == 55.0
        assert max_high == pytest.approx(62.0)
        assert max_high != pytest.approx(55.0 + flat_default)


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

    def test_degenerate_bracket_at_high_returns_zero(self):
        """Markets resolve [lo, hi): a zero-width bracket is empty, and a high
        AT the top edge belongs to the bracket above (issue #652). The old
        assertion (== 1.0) encoded the inclusive-top-edge bug."""
        state = make_state(current_high_f=82.0, latest_temp_f=82.0, hour=21)
        bracket = make_bracket(low_f=82.0, high_f=82.0)
        assert true_probability_yes(bracket, state) == 0.0

    def test_bracket_starting_at_high_returns_one_when_no_rise_left(self):
        """The non-degenerate version of the old intent: [high, high+2) with
        the envelope fully inside is certain — lo is inclusive."""
        state = make_state(current_high_f=82.0, latest_temp_f=82.0, hour=21)
        bracket = make_bracket(low_f=82.0, high_f=84.0)
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


# ---------------------------------------------------------------------------
# obs_bias_offset_f — bias correction in true_probability_yes
# ---------------------------------------------------------------------------

class TestObsBiasCorrection:
    """The bracket here is [82, 84), inside the envelope, not the [83, 87) it
    used to be.

    That fixture had ``max_env == 83.0`` exactly, so [83, 87) lay entirely on
    and above the ceiling. It only produced a non-zero probability because the
    old code's ``lo > max_env`` guard was a strict inequality: a bracket
    starting at 83.001 returned 0.0 while one starting at 83.0 was integrated
    in full, above the ceiling. #920's conditional clips to the envelope
    instead, so the boundary no longer behaves differently from its own
    neighbourhood -- and the deleted mass was never legitimate.

    The property under test is unchanged: shifting the mean via
    ``obs_bias_offset_f`` moves probability toward or away from a bracket on
    the upper side of it. It just needs a bracket the model can actually reach.
    """

    def _states(self, offset=None):
        return make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14,
                          forecast_high_f=82.0, obs_bias_offset_f=offset)

    def test_positive_offset_increases_probability_above_mean(self):
        """A positive obs_bias_offset_f shifts forecast_mean up, raising p for high brackets."""
        bracket = make_bracket(low_f=82.0, high_f=84.0)
        p_base = true_probability_yes(bracket, self._states())
        p_offset = true_probability_yes(bracket, self._states(3.0))
        assert p_offset > p_base, (
            f"Positive bias offset should raise p for bracket above mean; "
            f"got base={p_base:.4f}, offset={p_offset:.4f}"
        )

    def test_negative_offset_decreases_probability_above_mean(self):
        """A negative obs_bias_offset_f shifts forecast_mean down, lowering p for high brackets."""
        bracket = make_bracket(low_f=82.0, high_f=84.0)
        p_base = true_probability_yes(bracket, self._states())
        p_offset = true_probability_yes(bracket, self._states(-3.0))
        assert p_offset < p_base, (
            f"Negative bias offset should lower p for bracket above mean; "
            f"got base={p_base:.4f}, offset={p_offset:.4f}"
        )

    def test_a_bracket_entirely_above_the_ceiling_is_zero(self):
        """The behaviour change that moved the brackets above, pinned so it
        cannot silently revert: mass above ``max_env`` is not assignable, and
        that must not depend on whether the bracket's edge lands exactly on it."""
        state = self._states()
        assert true_probability_yes(make_bracket(low_f=83.0, high_f=87.0), state) == 0.0
        assert true_probability_yes(make_bracket(low_f=83.001, high_f=87.0), state) == 0.0

    def test_none_offset_is_identical_to_baseline(self):
        """obs_bias_offset_f=None must produce identical result to no offset field at all."""
        state_no_field = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=82.0)
        state_none_offset = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=82.0,
                                       obs_bias_offset_f=None)
        bracket = make_bracket(low_f=83.0, high_f=87.0)
        p_no_field = true_probability_yes(bracket, state_no_field)
        p_none_offset = true_probability_yes(bracket, state_none_offset)
        assert p_no_field == p_none_offset, (
            f"None offset must equal no-offset baseline; "
            f"got no_field={p_no_field:.6f}, none={p_none_offset:.6f}"
        )

    def test_large_offset_clamps_to_envelope_bounds(self):
        """A huge positive offset must clamp at max_env — result bounded [0, 1], not NaN."""
        from math import isnan
        state = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=82.0)
        state.obs_bias_offset_f = 999.0
        bracket = make_bracket(low_f=83.0, high_f=87.0)
        result = true_probability_yes(bracket, state)
        assert not isnan(result), "Result must not be NaN with large offset"
        assert 0.0 <= result <= 1.0, f"Result out of bounds: {result}"


# ---------------------------------------------------------------------------
# deb_mu_f — DEB-weighted forecast mean in true_probability_yes
# ---------------------------------------------------------------------------

class TestDebMuF:
    def test_deb_mu_f_used_when_enabled(self, monkeypatch):
        """WeatherState with deb_mu_f=90.0, DEB_ENABLED=true -> forecast_mean equals deb_mu_f."""
        monkeypatch.setenv("DEB_ENABLED", "true")
        # hour=14, climb=4.5; max_env = max(80, 79+4.5) = 83.5
        # deb_mu_f=90 will be clamped to max_env=83.5, ensemble would give ~81.2
        # Use a bracket well within the clamped mean region so p differs meaningfully
        state_deb = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=81.0)
        state_deb.secondary_forecast_f = 81.0
        state_deb.deb_mu_f = 90.0   # high DEB value → after clamping, mean = max_env = 83.5

        state_no_deb = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=81.0)
        state_no_deb.secondary_forecast_f = 81.0
        # deb_mu_f is None (default) — must use ensemble_forecast

        bracket = make_bracket(low_f=81.0, high_f=85.0)

        p_deb = true_probability_yes(bracket, state_deb)
        p_ensemble = true_probability_yes(bracket, state_no_deb)

        # deb_mu_f path should produce a different (higher here) probability than ensemble path
        assert p_deb != p_ensemble, (
            f"DEB path must differ from ensemble path; got deb={p_deb:.4f}, ensemble={p_ensemble:.4f}"
        )

    def test_deb_mu_f_ignored_when_disabled(self, monkeypatch):
        """DEB_ENABLED=false (default) -> result equals baseline even with deb_mu_f set."""
        monkeypatch.setenv("DEB_ENABLED", "false")
        state_with_deb = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=81.0)
        state_with_deb.secondary_forecast_f = 81.0
        state_with_deb.deb_mu_f = 90.0

        state_no_deb = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=81.0)
        state_no_deb.secondary_forecast_f = 81.0
        # deb_mu_f is None (default)

        bracket = make_bracket(low_f=81.0, high_f=85.0)

        p_with_deb_field = true_probability_yes(bracket, state_with_deb)
        p_baseline = true_probability_yes(bracket, state_no_deb)

        assert p_with_deb_field == p_baseline, (
            f"DEB_ENABLED=false must leave result identical to baseline; "
            f"got deb_field={p_with_deb_field:.6f}, baseline={p_baseline:.6f}"
        )

    def test_deb_mu_f_none_falls_back(self, monkeypatch):
        """deb_mu_f=None with DEB_ENABLED=true -> falls back to ensemble_forecast() unchanged."""
        monkeypatch.setenv("DEB_ENABLED", "true")
        state = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=81.0)
        state.secondary_forecast_f = 81.0
        state.deb_mu_f = None   # explicitly None — must fall through to ensemble_forecast()

        bracket = make_bracket(low_f=81.0, high_f=85.0)
        result = true_probability_yes(bracket, state)

        # Compute expected value via ensemble_forecast directly to confirm it matches
        ensemble_mean = ensemble_forecast(81.0, 81.0)   # 81.0 (both equal)
        assert result is not None
        assert 0.0 <= result <= 1.0


# ---------------------------------------------------------------------------
# DEB_ENABLED — resolved flag passed by the caller (from live config)
# ---------------------------------------------------------------------------

class TestDebMuFLiveConfig:
    """Tests for the deb_enabled parameter (resolved from live config by the scanner)."""

    def test_deb_enabled_true_from_caller(self):
        """deb_enabled=True (live-config value) -> deb_mu_f is used."""
        state_deb = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=81.0)
        state_deb.secondary_forecast_f = 81.0
        state_deb.deb_mu_f = 90.0

        state_no_deb = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=81.0)
        state_no_deb.secondary_forecast_f = 81.0

        bracket = make_bracket(low_f=81.0, high_f=85.0)

        p_deb = true_probability_yes(bracket, state_deb, deb_enabled=True)
        p_ensemble = true_probability_yes(bracket, state_no_deb, deb_enabled=True)

        # Probabilities should differ because DEB path uses deb_mu_f
        assert p_deb != p_ensemble, (
            f"DEB path must differ from ensemble path when deb_enabled=True; "
            f"got deb={p_deb:.4f}, ensemble={p_ensemble:.4f}"
        )

    def test_deb_disabled_from_caller(self):
        """deb_enabled=False (live-config value) -> deb_mu_f is ignored."""
        state_with_deb = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=81.0)
        state_with_deb.secondary_forecast_f = 81.0
        state_with_deb.deb_mu_f = 90.0

        state_no_deb = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=81.0)
        state_no_deb.secondary_forecast_f = 81.0

        bracket = make_bracket(low_f=81.0, high_f=85.0)

        p_with_deb_field = true_probability_yes(bracket, state_with_deb, deb_enabled=False)
        p_baseline = true_probability_yes(bracket, state_no_deb, deb_enabled=False)

        assert p_with_deb_field == p_baseline, (
            f"deb_enabled=False must leave result identical to baseline; "
            f"got deb_field={p_with_deb_field:.6f}, baseline={p_baseline:.6f}"
        )

    def test_caller_value_takes_precedence_over_env(self, monkeypatch):
        """Caller-passed deb_enabled=True (live config) overrides env var false."""
        monkeypatch.setenv("DEB_ENABLED", "false")

        state_deb = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=81.0)
        state_deb.secondary_forecast_f = 81.0
        state_deb.deb_mu_f = 90.0

        state_no_deb = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=81.0)
        state_no_deb.secondary_forecast_f = 81.0

        bracket = make_bracket(low_f=81.0, high_f=85.0)

        p_deb = true_probability_yes(bracket, state_deb, deb_enabled=True)
        p_ensemble = true_probability_yes(bracket, state_no_deb, deb_enabled=True)

        # Caller value (True) should be used, not env var (false), so probabilities differ
        assert p_deb != p_ensemble, (
            f"Caller deb_enabled=True must override env var false; "
            f"got deb={p_deb:.4f}, ensemble={p_ensemble:.4f}"
        )

    def test_env_var_fallback_when_deb_enabled_none(self, monkeypatch):
        """deb_enabled=None -> env var is used (backward compatibility)."""
        monkeypatch.setenv("DEB_ENABLED", "true")
        state_deb = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=81.0)
        state_deb.secondary_forecast_f = 81.0
        state_deb.deb_mu_f = 90.0

        state_no_deb = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=81.0)
        state_no_deb.secondary_forecast_f = 81.0

        bracket = make_bracket(low_f=81.0, high_f=85.0)

        p_deb = true_probability_yes(bracket, state_deb, deb_enabled=None)
        p_ensemble = true_probability_yes(bracket, state_no_deb, deb_enabled=None)

        # Env var says true, so the DEB path must fire and differ from baseline
        assert p_deb != p_ensemble, (
            f"deb_enabled=None with env DEB_ENABLED=true must use deb_mu_f; "
            f"got deb={p_deb:.4f}, ensemble={p_ensemble:.4f}"
        )

    def test_corrected_mu_f_precedence_over_deb(self):
        """corrected_mu_f takes precedence even when deb_enabled=True."""
        state = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=81.0)
        state.secondary_forecast_f = 81.0
        state.deb_mu_f = 90.0
        state.corrected_mu_f = 75.0  # Lower value

        state_corrected_only = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=81.0)
        state_corrected_only.secondary_forecast_f = 81.0
        state_corrected_only.corrected_mu_f = 75.0  # same corrected_mu_f, no deb_mu_f

        bracket = make_bracket(low_f=81.0, high_f=85.0)
        p_both = true_probability_yes(bracket, state, deb_enabled=True)
        p_corrected_only = true_probability_yes(bracket, state_corrected_only, deb_enabled=True)

        # corrected_mu_f (75.0) must be used instead of deb_mu_f (90.0):
        # result with both set equals result with corrected_mu_f alone
        assert p_both == p_corrected_only, (
            f"corrected_mu_f must win over deb_mu_f; "
            f"got both={p_both:.6f}, corrected_only={p_corrected_only:.6f}"
        )
        assert 0.0 <= p_both <= 1.0


class TestExclusiveTopEdge:
    """Issue #652 Bug A: markets resolve [lo, hi) — a running high AT the top
    edge belongs to the bracket above and can never come back down. 119 of the
    calibration report's 'certain YES' markets had current_high == bracket_high
    exactly and resolved NO 96-100% of the time."""

    def test_current_high_exactly_at_top_edge_is_zero(self):
        # RCSS 2026-06-18 replica: bracket [93.2, 95.0], running high 95.0,
        # evening (no further rise). Old code returned 1.0; market resolved NO.
        state = make_state(current_high_f=95.0, latest_temp_f=88.0,
                           forecast_high_f=None, hour=20, station="RCSS")
        bracket = make_bracket(low_f=93.2, high_f=95.0)
        assert true_probability_yes(bracket, state, minutes_to_settlement=16.0) == 0.0

    def test_current_high_above_top_edge_is_zero(self):
        state = make_state(current_high_f=96.0, latest_temp_f=90.0,
                           forecast_high_f=None, hour=20)
        bracket = make_bracket(low_f=93.2, high_f=95.0)
        assert true_probability_yes(bracket, state) == 0.0

    def test_current_high_at_bottom_edge_still_in_bracket(self):
        """lo is inclusive: high == lo means YES is currently winning."""
        state = make_state(current_high_f=95.0, latest_temp_f=88.0,
                           forecast_high_f=None, hour=20, station="RCSS")
        bracket = make_bracket(low_f=95.0, high_f=96.8)
        p = true_probability_yes(bracket, state, minutes_to_settlement=16.0)
        assert p > 0.5  # currently-winning bracket, little rise left

    def test_current_high_strictly_inside_still_certain(self):
        """Past peak, high strictly inside the bracket → certainty unchanged."""
        state = make_state(current_high_f=94.0, latest_temp_f=88.0,
                           forecast_high_f=None, hour=20, station="RCSS")
        bracket = make_bracket(low_f=93.2, high_f=95.0)
        # max_env == current_high when no further rise is expected
        from unittest.mock import patch
        with patch("src.model.envelope.expected_additional_rise", return_value=0.0):
            assert true_probability_yes(bracket, state) == 1.0


class TestSigmaClimbFloor:
    """Issue #652 Bug B: the effective stddev is floored at a fraction of the
    climb still to come, so early-day evaluations cannot claim near-certainty
    about a mostly-unrealized daily high."""

    def test_morning_open_low_bracket_not_certain(self):
        # KORD 2026-07-07 11:06 UTC replica: 'high <= 73' bracket, current 66,
        # obs-anchored mu ~66, NWS forecast 84, ~18F of climb still to come.
        # Old code: p ~ 0.9998 with sigma=2. Market resolved NO.
        from unittest.mock import patch
        state = make_state(current_high_f=66.02, latest_temp_f=66.02,
                           forecast_high_f=84.0, hour=6, station="KORD")
        state.corrected_mu_f = 66.0  # intraday correction anchored to morning obs
        bracket = make_bracket(low_f=-50.0, high_f=73.0)
        with patch("src.model.envelope.expected_additional_rise", return_value=18.0):
            p = true_probability_yes(bracket, state, minutes_to_settlement=54.0)
        assert p < 0.95, f"morning certainty must be suppressed, got {p:.4f}"

    def test_past_peak_behavior_unchanged(self):
        """No remaining climb → floor inactive → identical to fixed stddev."""
        from unittest.mock import patch
        state = make_state(current_high_f=80.0, latest_temp_f=78.0,
                           forecast_high_f=80.5, hour=19)
        bracket = make_bracket(low_f=79.0, high_f=81.0)
        with patch("src.model.envelope.expected_additional_rise", return_value=0.0):
            p_default = true_probability_yes(bracket, state, sigma_climb_fraction=0.5)
            p_zero_frac = true_probability_yes(bracket, state, sigma_climb_fraction=0.0)
        assert p_default == p_zero_frac

    def test_fraction_zero_restores_old_behavior(self):
        from unittest.mock import patch
        state = make_state(current_high_f=66.0, latest_temp_f=66.0,
                           forecast_high_f=None, hour=6, station="KORD")
        state.corrected_mu_f = 66.0
        bracket = make_bracket(low_f=-50.0, high_f=73.0)
        with patch("src.model.envelope.expected_additional_rise", return_value=18.0):
            p_old = true_probability_yes(bracket, state, sigma_climb_fraction=0.0)
            p_new = true_probability_yes(bracket, state, sigma_climb_fraction=0.5)
        assert p_old > 0.99      # the old deluded certainty
        assert p_new < p_old     # the floor widens uncertainty


# ---------------------------------------------------------------------------
# ensemble_sigma_f / USE_ENSEMBLE_SIGMA — issue #448
# ---------------------------------------------------------------------------

class TestEnsembleSigma:
    """WeatherState.ensemble_sigma_f replaces the fixed forecast_stddev when
    USE_ENSEMBLE_SIGMA resolves True and the field is set; otherwise the
    legacy fixed-sigma (FORECAST_STDDEV_F-equivalent) path is used unchanged
    (issue #448, epic #70 Phase 2 / #445)."""

    def _state_and_bracket(self, ensemble_sigma_f: "float | None" = None):
        # Mirrors TestDebMuF's setup: hour=14, climb=4.5 (mocked away below),
        # a bracket comfortably inside the envelope so the stddev actually
        # matters (not swallowed by the p=0/p=1 early exits).
        state = make_state(current_high_f=80.0, latest_temp_f=79.0, hour=14, forecast_high_f=81.0)
        state.secondary_forecast_f = 81.0
        state.ensemble_sigma_f = ensemble_sigma_f
        bracket = make_bracket(low_f=81.0, high_f=85.0)
        return state, bracket

    def test_ensemble_sigma_used_when_flag_true_and_field_set(self):
        """use_ensemble_sigma=True + ensemble_sigma_f set -> differs from the
        fixed-stddev baseline (a much wider sigma changes p_yes)."""
        state, bracket = self._state_and_bracket(ensemble_sigma_f=8.0)
        state_baseline, bracket_baseline = self._state_and_bracket(ensemble_sigma_f=None)

        p_ensemble = true_probability_yes(bracket, state, use_ensemble_sigma=True)
        p_baseline = true_probability_yes(bracket_baseline, state_baseline, use_ensemble_sigma=True)

        assert p_ensemble != p_baseline, (
            f"ensemble_sigma_f=8.0 must widen/shift the distribution vs the "
            f"fixed-stddev baseline; got ensemble={p_ensemble:.4f}, baseline={p_baseline:.4f}"
        )

    def test_legacy_fallback_when_ensemble_sigma_f_none(self):
        """use_ensemble_sigma=True but ensemble_sigma_f=None (e.g. GEFS
        unavailable) -> identical to the legacy fixed-sigma result."""
        state, bracket = self._state_and_bracket(ensemble_sigma_f=None)

        p_flag_on = true_probability_yes(bracket, state, use_ensemble_sigma=True)
        p_flag_off = true_probability_yes(bracket, state, use_ensemble_sigma=False)

        assert p_flag_on == p_flag_off, (
            f"With ensemble_sigma_f=None, the flag must have no effect; "
            f"got flag_on={p_flag_on:.6f}, flag_off={p_flag_off:.6f}"
        )

    def test_flag_false_ignores_ensemble_sigma_f_even_when_set(self):
        """use_ensemble_sigma=False -> ensemble_sigma_f is ignored, identical
        to a state with no ensemble_sigma_f at all."""
        state_with_sigma, bracket_a = self._state_and_bracket(ensemble_sigma_f=8.0)
        state_without_sigma, bracket_b = self._state_and_bracket(ensemble_sigma_f=None)

        p_with = true_probability_yes(bracket_a, state_with_sigma, use_ensemble_sigma=False)
        p_without = true_probability_yes(bracket_b, state_without_sigma, use_ensemble_sigma=False)

        assert p_with == p_without, (
            f"use_ensemble_sigma=False must ignore ensemble_sigma_f entirely; "
            f"got with_field={p_with:.6f}, without_field={p_without:.6f}"
        )

    def test_default_off_matches_legacy_behavior(self):
        """Default (use_ensemble_sigma unset, USE_ENSEMBLE_SIGMA env unset)
        -> ensemble_sigma_f must not change behaviour (strict default-off
        rollout, issue #448)."""
        state_with_sigma, bracket_a = self._state_and_bracket(ensemble_sigma_f=8.0)
        state_without_sigma, bracket_b = self._state_and_bracket(ensemble_sigma_f=None)

        p_with = true_probability_yes(bracket_a, state_with_sigma)
        p_without = true_probability_yes(bracket_b, state_without_sigma)

        assert p_with == p_without, (
            f"Default rollout state must be behaviour-preserving; "
            f"got with_field={p_with:.6f}, without_field={p_without:.6f}"
        )

    def test_caller_value_takes_precedence_over_env(self, monkeypatch):
        """Caller-passed use_ensemble_sigma=True (live config) overrides env var false."""
        monkeypatch.setenv("USE_ENSEMBLE_SIGMA", "false")
        state, bracket = self._state_and_bracket(ensemble_sigma_f=8.0)
        state_baseline, bracket_baseline = self._state_and_bracket(ensemble_sigma_f=None)

        p_ensemble = true_probability_yes(bracket, state, use_ensemble_sigma=True)
        p_baseline = true_probability_yes(bracket_baseline, state_baseline, use_ensemble_sigma=True)

        assert p_ensemble != p_baseline, (
            "Caller use_ensemble_sigma=True must override env var false"
        )

    def test_env_var_fallback_when_use_ensemble_sigma_none(self, monkeypatch):
        """use_ensemble_sigma=None -> USE_ENSEMBLE_SIGMA env var is used
        (backward compatibility, mirrors DEB_ENABLED's resolution)."""
        monkeypatch.setenv("USE_ENSEMBLE_SIGMA", "true")
        state, bracket = self._state_and_bracket(ensemble_sigma_f=8.0)
        state_baseline, bracket_baseline = self._state_and_bracket(ensemble_sigma_f=None)

        p_ensemble = true_probability_yes(bracket, state, use_ensemble_sigma=None)
        p_baseline = true_probability_yes(bracket_baseline, state_baseline, use_ensemble_sigma=None)

        assert p_ensemble != p_baseline, (
            "use_ensemble_sigma=None with env USE_ENSEMBLE_SIGMA=true must use ensemble_sigma_f"
        )

    def test_env_var_false_by_default(self, monkeypatch):
        """use_ensemble_sigma=None with no env var set -> defaults to off."""
        monkeypatch.delenv("USE_ENSEMBLE_SIGMA", raising=False)
        state, bracket = self._state_and_bracket(ensemble_sigma_f=8.0)
        state_baseline, bracket_baseline = self._state_and_bracket(ensemble_sigma_f=None)

        p_ensemble = true_probability_yes(bracket, state, use_ensemble_sigma=None)
        p_baseline = true_probability_yes(bracket_baseline, state_baseline, use_ensemble_sigma=None)

        assert p_ensemble == p_baseline, (
            "use_ensemble_sigma=None with no env var set must default to off"
        )

    def test_climb_floor_still_applies_on_top_of_ensemble_sigma(self):
        """The remaining-climb sigma floor (#652) still wins over a narrow
        ensemble_sigma_f -- ensemble sigma only replaces the *base*
        forecast_stddev, not the effective_stddev floor logic."""
        from unittest.mock import patch
        state = make_state(current_high_f=66.0, latest_temp_f=66.0,
                           forecast_high_f=None, hour=6, station="KORD")
        state.corrected_mu_f = 66.0
        state.ensemble_sigma_f = 0.5  # much narrower than the fixed 2.0F default
        bracket = make_bracket(low_f=-50.0, high_f=73.0)
        with patch("src.model.envelope.expected_additional_rise", return_value=18.0):
            p_narrow_sigma = true_probability_yes(
                bracket, state, use_ensemble_sigma=True, sigma_climb_fraction=0.5,
            )
        # Despite ensemble_sigma_f=0.5, the climb floor (0.5 * 18F = 9F) still
        # dominates, so the morning "certain YES" delusion remains suppressed.
        assert p_narrow_sigma < 0.95, (
            f"climb floor must still suppress early-day certainty even with a "
            f"narrow ensemble_sigma_f, got {p_narrow_sigma:.4f}"
        )

    def test_sub_floor_ensemble_sigma_is_floored_at_sigma_floor_f(self):
        """Issue #887: 63% of GEFS sigma_f rows sit below SIGMA_FLOOR_F (1.0F).
        With no remaining climb to fall back on (sigma_climb_fraction floor
        inactive), a raw sub-floor ensemble_sigma_f must still be floored at
        SIGMA_FLOOR_F before it reaches p_normal_between -- never fed through
        raw, which would reproduce the overconfidence M0 (#820) removed."""
        from unittest.mock import patch
        from src.model.ensemble_sigma import SIGMA_FLOOR_F

        state, bracket = self._state_and_bracket(ensemble_sigma_f=0.3)
        with patch("src.model.envelope.expected_additional_rise", return_value=0.0):
            p_sub_floor = true_probability_yes(
                bracket, state, use_ensemble_sigma=True, sigma_climb_fraction=0.5,
            )

        state_at_floor, bracket_at_floor = self._state_and_bracket(
            ensemble_sigma_f=SIGMA_FLOOR_F
        )
        with patch("src.model.envelope.expected_additional_rise", return_value=0.0):
            p_at_floor = true_probability_yes(
                bracket_at_floor, state_at_floor, use_ensemble_sigma=True,
                sigma_climb_fraction=0.5,
            )

        assert p_sub_floor == p_at_floor, (
            f"ensemble_sigma_f=0.3 (below SIGMA_FLOOR_F={SIGMA_FLOOR_F}) must "
            f"be clamped to the same result as ensemble_sigma_f=SIGMA_FLOOR_F; "
            f"got sub_floor={p_sub_floor:.6f}, at_floor={p_at_floor:.6f}"
        )

    def test_sub_floor_ensemble_sigma_not_more_confident_than_floor(self):
        """A near-zero ensemble_sigma_f must not push p_yes closer to a rail
        than the floored value would (issue #887 regression guard). Bracket
        edge sits just below the forecast mean so a tiny sigma collapses the
        CDF toward the rail -- exactly the M0 (#820) failure mode this floor
        prevents."""
        from unittest.mock import patch
        from src.model.ensemble_sigma import SIGMA_FLOOR_F

        state, _ = self._state_and_bracket(ensemble_sigma_f=0.05)
        bracket = make_bracket(low_f=80.95, high_f=85.0)

        with patch("src.model.envelope.expected_additional_rise", return_value=0.0):
            p_floored = true_probability_yes(
                bracket, state, use_ensemble_sigma=True, sigma_climb_fraction=0.0,
            )
            # What serving would produce if the floor were NOT applied here:
            # the same raw 0.05F sigma fed straight into the pipeline.
            p_unfloored_would_be = true_probability_yes(
                bracket, state, use_ensemble_sigma=False, forecast_stddev=0.05,
                sigma_climb_fraction=0.0,
            )
            # Sanity anchor: the floored path must match SIGMA_FLOOR_F fed
            # in directly.
            p_floor_direct = true_probability_yes(
                bracket, state, use_ensemble_sigma=False,
                forecast_stddev=SIGMA_FLOOR_F, sigma_climb_fraction=0.0,
            )

        assert p_floored != p_unfloored_would_be, (
            f"SIGMA_FLOOR_F must actually change the outcome for a sub-floor "
            f"ensemble_sigma_f; got floored={p_floored:.6f}, "
            f"unfloored_would_be={p_unfloored_would_be:.6f}"
        )
        assert isclose(p_floored, p_floor_direct, abs_tol=1e-9), (
            f"floored ensemble path must match feeding SIGMA_FLOOR_F directly; "
            f"got {p_floored:.6f} vs {p_floor_direct:.6f}"
        )
        # The unfloored 0.05F sigma is far more "certain" (closer to the p=1
        # rail) than the floored 1.0F sigma -- the overconfidence pattern #887
        # exists to prevent.
        assert p_unfloored_would_be > p_floored


# ---------------------------------------------------------------------------
# next_day_probability_yes (issue #687)
# ---------------------------------------------------------------------------

class TestNextDayProbabilityYes:
    """Unit tests for the forecast-only next-day probability path.

    Distinct from true_probability_yes: no observed-high floor, no max_env
    climb ceiling, no time_to_settlement_boost -- plain Gaussian bracket
    integration against N(mu, sigma) only.
    """

    def test_matches_p_normal_between_directly(self):
        """next_day_probability_yes is a thin pass-through to p_normal_between --
        no envelope/climb/floor logic applied."""
        bracket = make_bracket(low_f=80.0, high_f=82.0)
        result = next_day_probability_yes(bracket, mu=81.0, sigma=2.0)
        expected = p_normal_between(80.0, 82.0, mean=81.0, stddev=2.0)
        assert isclose(result, expected, abs_tol=1e-12)

    def test_no_observed_high_floor(self):
        """A bracket entirely below mu is NOT forced to 0 the way the same-day
        floor (hi <= current_high_f) would -- there's no 'already observed'
        running high for a next-day market."""
        bracket = make_bracket(low_f=60.0, high_f=65.0)
        # mu=81 is far above the bracket, so probability mass is genuinely
        # small here -- but it comes from the Gaussian tail, not a hard floor.
        result = next_day_probability_yes(bracket, mu=81.0, sigma=5.0)
        assert 0.0 <= result < 0.01

    def test_no_max_env_ceiling_wider_tails_than_same_day(self):
        """Without max_env truncation, a bracket well above mu still gets
        some probability mass from the upper tail (same-day's ceiling would
        truncate this away once max_env is exceeded)."""
        bracket = make_bracket(low_f=95.0, high_f=97.0)
        result = next_day_probability_yes(bracket, mu=81.0, sigma=8.0)
        assert result > 0.0

    def test_symmetric_around_mu(self):
        """No floor/ceiling asymmetry: brackets equidistant from mu on either
        side get equal probability (same-day's floor/ceiling would break this
        symmetry near the current running high)."""
        bracket_below = make_bracket(low_f=76.0, high_f=79.0)
        bracket_above = make_bracket(low_f=83.0, high_f=86.0)
        p_below = next_day_probability_yes(bracket_below, mu=81.0, sigma=4.0)
        p_above = next_day_probability_yes(bracket_above, mu=81.0, sigma=4.0)
        assert isclose(p_below, p_above, abs_tol=1e-9)


# ---------------------------------------------------------------------------
# Day-mismatch guard (issue #820)
# ---------------------------------------------------------------------------

class TestDayMismatchGuard:
    """Certainty shortcuts must not fire when the WeatherState's local day
    differs from the settlement day (evening false-certainty entries).
    """

    def test_certainty_shortcuts_bypassed_when_days_differ(self):
        """Evening window: state is from yesterday (after peak), market settles
        today. Old code: hi(75) <= current_high_f(88) → returns 0.0. New code:
        settlement_date mismatch → skips shortcuts → uses forecast Gaussian.
        """
        from datetime import date, timedelta
        yesterday = datetime(2026, 5, 15, 20, 30)  # 8:30pm, past peak
        state = make_state(
            current_high_f=88.0, latest_temp_f=86.0,
            forecast_high_f=85.0, hour=20,
        )
        state = WeatherState(
            station=state.station,
            now_local=yesterday,  # state is from yesterday
            sunset_local=yesterday.replace(hour=20, minute=15),
            current_high_f=88.0,
            current_high_time=yesterday,
            latest_temp_f=86.0,
            latest_temp_time=yesterday,
            forecast_high_f=85.0,
        )
        # Settlement is today (state's now_local is yesterday)
        today = yesterday + timedelta(days=1)
        settlement = today.date()

        # A bracket well below yesterday's 88°F high would normally fire
        # the certainty shortcut (hi=75 <= current_high=88 → 0.0).
        bracket = make_bracket(low_f=72.0, high_f=75.0)

        # Without settlement_date: old behavior returns 0.0
        p_without = true_probability_yes(bracket, state, forecast_stddev=3.0)
        assert p_without == 0.0

        # With settlement_date that differs: shortcuts bypassed → Gaussian
        p_with = true_probability_yes(
            bracket, state, forecast_stddev=3.0,
            settlement_date=settlement,
        )
        # Should get a non-zero, non-certain probability from the Gaussian
        # tail: P(72 <= X <= 75 | X~N(85, 3)) is small but > 0
        assert 0.0 < p_with < 0.02, f"expected small positive, got {p_with}"

    def test_days_match_behavior_unchanged(self):
        """When local day == settlement day, shortcuts fire normally."""
        state = make_state(
            current_high_f=88.0, latest_temp_f=86.0,
            forecast_high_f=85.0, hour=20,
        )
        today = state.now_local.date()
        bracket = make_bracket(low_f=72.0, high_f=75.0)

        # With matching settlement_date: shortcuts still fire
        p = true_probability_yes(
            bracket, state, forecast_stddev=3.0,
            settlement_date=today,
        )
        assert p == 0.0  # hi(75) <= current_high(88)

    def test_forecast_mean_not_clamped_to_wrong_day_envelope(self):
        """When days differ, forecast_mean must not be clamped to yesterday's
        envelope (e.g. clamping today's 85°F forecast to yesterday's 88°F
        realized high would inflate the forecast)."""
        from datetime import date, timedelta
        yesterday = datetime(2026, 5, 15, 20, 30)
        state = WeatherState(
            station="KNYC",
            now_local=yesterday,
            sunset_local=yesterday.replace(hour=20, minute=15),
            current_high_f=88.0,  # yesterday's high
            current_high_time=yesterday,
            latest_temp_f=86.0,
            latest_temp_time=yesterday,
            forecast_high_f=85.0,  # today's forecast, lower than yesterday's high
        )
        today = (yesterday + timedelta(days=1)).date()

        # With day mismatch: forecast_mean=85 should NOT be clamped to 88
        bracket = make_bracket(low_f=80.0, high_f=86.0)
        p = true_probability_yes(
            bracket, state, forecast_stddev=3.0,
            settlement_date=today,
        )
        # If clamped to 88, bracket [80,86] with mu=88 would be mostly below mu
        # → low probability. Without clamping, mu=85 centers on the bracket →
        # higher probability. Check it's > what the clamped version would give.
        assert p > 0.15, f"expected unclamped forecast to give higher p, got {p}"

    def test_day_mismatch_no_false_one(self):
        """A bracket spanning the observation envelope must NOT return 1.0
        when days differ (the lo <= current_high_f AND hi >= max_env shortcut)."""
        from datetime import date, timedelta
        yesterday = datetime(2026, 5, 15, 20, 30)
        state = WeatherState(
            station="KNYC",
            now_local=yesterday,
            sunset_local=yesterday.replace(hour=20, minute=15),
            current_high_f=88.0,
            current_high_time=yesterday,
            latest_temp_f=86.0,
            latest_temp_time=yesterday,
            forecast_high_f=85.0,
        )
        today = (yesterday + timedelta(days=1)).date()

        # Bracket spans yesterday's envelope: [87, 89] covers [88, 88]
        # Old code: lo(87) <= 88 AND hi(89) >= 88 → returns 1.0
        bracket = make_bracket(low_f=87.0, high_f=89.0)

        # Without settlement_date: certainty shortcut fires → 1.0
        p_without = true_probability_yes(bracket, state, forecast_stddev=3.0)
        assert p_without == 1.0

        # With day mismatch: shortcuts bypassed → Gaussian, NOT 1.0
        p_with = true_probability_yes(
            bracket, state, forecast_stddev=3.0,
            settlement_date=today,
        )
        assert 0.0 < p_with < 1.0, \
            f"expected non-certain probability, got {p_with}"

    def test_no_settlement_date_skips_all_shortcuts(self):
        """Regression test for #916: covers the position_tracker scenario where
        a position was opened for one calendar day but the state reflects a
        different local day (evening window after UTC rollover).  When
        settlement_date is passed and differs from the state's local day, ALL
        three certainty shortcuts must be bypassed and the function must return
        a computed (non-certainty) probability.

        This mirrors the fix in _log_open_position_snapshots(), which now
        threads the position's settlement date through to true_probability_yes.
        """
        from datetime import date, timedelta
        yesterday = datetime(2026, 7, 30, 20, 30)  # 8:30pm, past peak
        state = WeatherState(
            station="KORD",
            now_local=yesterday,
            sunset_local=yesterday.replace(hour=20, minute=15),
            current_high_f=92.0,
            current_high_time=yesterday,
            latest_temp_f=90.0,
            latest_temp_time=yesterday,
            forecast_high_f=88.0,
        )
        # Position is for "today" but state is from yesterday (UTC rolled over)
        today = (yesterday + timedelta(days=1)).date()

        # Case 1: bracket below yesterday's high → shortcut would return 0.0
        bracket_low = make_bracket(low_f=80.0, high_f=83.0)
        p = true_probability_yes(
            bracket_low, state, forecast_stddev=3.0,
            settlement_date=today,
        )
        assert 0.0 < p < 1.0, \
            f"Shortcut hi<=current_high fired: got {p}, expected non-certain"

        # Case 2: bracket spans envelope → shortcut would return 1.0
        bracket_span = make_bracket(low_f=91.0, high_f=93.0)
        p = true_probability_yes(
            bracket_span, state, forecast_stddev=3.0,
            settlement_date=today,
        )
        assert 0.0 < p < 1.0, \
            f"Shortcut span->1.0 fired: got {p}, expected non-certain"

        # Case 3: bracket above max_env → shortcut would return 0.0
        bracket_above = make_bracket(low_f=95.0, high_f=98.0)
        p = true_probability_yes(
            bracket_above, state, forecast_stddev=3.0,
            settlement_date=today,
        )
        assert 0.0 < p < 1.0, \
            f"Shortcut lo>max_env fired: got {p}, expected non-certain"

    def test_sigma_zero_mean_inside_bracket(self):
        """When sigma=0, next_day_probability_yes returns point-mass probability.
        P(low <= mu < high) = 1 when low <= mu < high."""
        bracket = make_bracket(low_f=80.0, high_f=82.0)
        result = next_day_probability_yes(bracket, mu=81.0, sigma=0.0)
        assert result == 1.0

    def test_sigma_zero_mean_at_low_edge(self):
        """When sigma=0 and mean equals low edge (inclusive), probability is 1."""
        bracket = make_bracket(low_f=81.0, high_f=83.0)
        result = next_day_probability_yes(bracket, mu=81.0, sigma=0.0)
        assert result == 1.0

    def test_sigma_zero_mean_at_high_edge(self):
        """When sigma=0 and mean equals high edge (exclusive), probability is 0."""
        bracket = make_bracket(low_f=79.0, high_f=81.0)
        result = next_day_probability_yes(bracket, mu=81.0, sigma=0.0)
        assert result == 0.0

    def test_sigma_zero_mean_below_bracket(self):
        """When sigma=0 and mean < low, probability is 0."""
        bracket = make_bracket(low_f=82.0, high_f=84.0)
        result = next_day_probability_yes(bracket, mu=81.0, sigma=0.0)
        assert result == 0.0

    def test_sigma_zero_mean_above_bracket(self):
        """When sigma=0 and mean > high, probability is 0."""
        bracket = make_bracket(low_f=78.0, high_f=80.0)
        result = next_day_probability_yes(bracket, mu=81.0, sigma=0.0)
        assert result == 0.0

    def test_sigma_negative_mean_inside_bracket(self):
        """When sigma<0, next_day_probability_yes returns point-mass probability.
        Negative sigma is treated like zero (degenerate case)."""
        bracket = make_bracket(low_f=80.0, high_f=82.0)
        result = next_day_probability_yes(bracket, mu=81.0, sigma=-1.0)
        assert result == 1.0

    def test_sigma_negative_mean_at_high_edge(self):
        """When sigma<0 and mean equals high edge (exclusive), probability is 0."""
        bracket = make_bracket(low_f=79.0, high_f=81.0)
        result = next_day_probability_yes(bracket, mu=81.0, sigma=-1.0)
        assert result == 0.0


# ---------------------------------------------------------------------------
# Mass conservation through the SERVING path (issue #920)
# ---------------------------------------------------------------------------

class TestServedLadderMassConservation:
    """A full ladder must sum to ~1.0 through ``true_probability_yes`` itself.

    ``TestBracketLadderMassConservation`` in test_scanner.py already asserts
    this invariant -- but against ``p_normal_between`` directly, one layer
    below where probabilities are actually served. That is exactly why it
    stayed green while production ladders summed to 0.80: the parser geometry
    it checks was correct after #917, and the loss happened afterwards, in the
    truncation shortcuts that ``true_probability_yes`` applies on top.

    So this asserts the same invariant where it was being violated. The
    truncated regimes are the point -- an untruncated ladder conserved mass
    even before the fix.
    """

    # Real 2°F US ladder, gap-free, with both open-ended tails.
    _EDGES = [-50.0] + [float(x) for x in range(56, 94, 2)] + [200.0]

    def _ladder_sum(self, state, **kw):
        return sum(
            true_probability_yes(make_bracket(low_f=lo, high_f=hi), state, **kw)
            for lo, hi in zip(self._EDGES, self._EDGES[1:])
        )

    def test_untruncated_ladder_conserves(self):
        state = make_state(current_high_f=60.0, latest_temp_f=60.0,
                           forecast_high_f=80.0, hour=9)
        assert self._ladder_sum(state) == pytest.approx(1.0, abs=0.02)

    def test_bottom_truncated_ladder_conserves(self):
        """Brackets below an observed running high are zeroed -- the surviving
        mass must be renormalised, not simply left short."""
        state = make_state(current_high_f=78.0, latest_temp_f=78.0,
                           forecast_high_f=84.0, hour=13)
        assert self._ladder_sum(state) == pytest.approx(1.0, abs=0.02)

    def test_top_truncated_ladder_conserves(self):
        """The expensive one: mass above ``max_env`` was being discarded, which
        measurement put at ~15% against ~3% for the bottom cut."""
        state = make_state(current_high_f=60.0, latest_temp_f=76.0,
                           forecast_high_f=78.0, hour=16)
        assert self._ladder_sum(state) == pytest.approx(1.0, abs=0.02)

    def test_post_peak_ladder_conserves(self):
        """Both cuts at once -- the RKSI 0.288 case. After the peak
        ``expected_additional_rise`` -> 0, ``max_env`` collapses onto
        ``current_high``, and both shortcuts fire together."""
        state = make_state(current_high_f=86.0, latest_temp_f=82.0,
                           forecast_high_f=80.0, hour=19)
        assert self._ladder_sum(state) == pytest.approx(1.0, abs=0.02)

    def test_conserves_when_the_forecast_is_badly_wrong(self):
        """Forecast far below an already-observed high: the conditional is
        taken over a region the forecast gives almost no mass, which is where a
        naive renormalisation divides by ~zero."""
        state = make_state(current_high_f=95.0, latest_temp_f=95.0,
                           forecast_high_f=70.0, hour=15)
        assert self._ladder_sum(state) == pytest.approx(1.0, abs=0.02)

    def test_day_mismatch_branch_rules_nothing_out(self):
        """#820's wrong-day branch must not condition on the observations at all.

        Conserving mass is *not* the discriminating assertion here -- a
        conditioned ladder also sums to 1.0, so that alone would pass even if
        this branch started conditioning on the wrong day's high. What must
        hold is that a bracket *below* the (wrong-day) running high still
        carries probability: tomorrow's high is not constrained by today's, and
        zeroing it is the exact false certainty #820 was opened to remove.
        """
        state = make_state(current_high_f=86.0, latest_temp_f=86.0,
                           forecast_high_f=70.0, hour=19)
        below_the_wrong_days_high = make_bracket(low_f=68.0, high_f=70.0)
        tomorrow = date(2026, 5, 16)

        assert true_probability_yes(
            below_the_wrong_days_high, state, settlement_date=tomorrow) > 0.0
        # Same bracket, same day -> correctly impossible, because then the high
        # really has been observed at 86.
        assert true_probability_yes(
            below_the_wrong_days_high, state, settlement_date=date(2026, 5, 15)) == 0.0
        assert self._ladder_sum(state, settlement_date=tomorrow) == pytest.approx(
            1.0, abs=0.02)

    def test_the_three_legacy_shortcuts_survive_at_their_boundaries(self):
        """The conditional replaces them; it must not change them."""
        state = make_state(current_high_f=80.0, latest_temp_f=80.0,
                           forecast_high_f=84.0, hour=13)
        env_hi = max(compute_envelope(state)[1], 84.0)
        # entirely below the running high -> impossible
        assert true_probability_yes(make_bracket(low_f=70.0, high_f=80.0), state) == 0.0
        # entirely above the ceiling -> impossible
        assert true_probability_yes(
            make_bracket(low_f=env_hi + 1.0, high_f=env_hi + 5.0), state) == 0.0
        # spans the whole surviving interval -> certain
        assert true_probability_yes(
            make_bracket(low_f=80.0, high_f=env_hi), state) == pytest.approx(1.0)

"""Unit tests for src/model/envelope_low.py — pure math functions, no API calls.

Mirrors the structure of src/tests/test_envelope.py.
All tests that invoke expected_additional_drop patch it at the module level
(src.model.envelope_low.expected_additional_drop) to keep tests hermetic and
independent of the cooling_rates module (#454).

cooling_rates (#454) may not be implemented yet.  We install a sys.modules
stub before importing envelope_low so that collection succeeds regardless.
"""
import glob
import inspect
import os
import sys
from datetime import datetime
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub out cooling_rates before importing envelope_low — #454 may not exist yet
# ---------------------------------------------------------------------------
if "src.model.cooling_rates" not in sys.modules:
    _stub = ModuleType("src.model.cooling_rates")
    _stub.expected_additional_drop = MagicMock(return_value=0.0)  # type: ignore[attr-defined]
    sys.modules["src.model.cooling_rates"] = _stub

import src.model.envelope_low as envelope_low_module  # noqa: E402
from src.model.envelope import Bracket  # noqa: E402
from src.model.envelope_low import (  # noqa: E402
    WeatherStateLow,
    compute_envelope_low,
    true_probability_low_in_bracket,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_state_low(
    current_low_f: float = 64.0,
    latest_temp_f: float = 66.0,
    forecast_low_f: float | None = 62.0,
    hour: int = 2,
    station: str = "KORD",
) -> WeatherStateLow:
    """Build a WeatherStateLow with sensible defaults for testing."""
    now = datetime(2026, 7, 2, hour, 0)
    return WeatherStateLow(
        station=station,
        now_local=now,
        sunrise_local=datetime(2026, 7, 2, 5, 45),
        current_low_f=current_low_f,
        current_low_time=now,
        latest_temp_f=latest_temp_f,
        latest_temp_time=now,
        forecast_low_f=forecast_low_f,
    )


def make_bracket(
    low_f: float,
    high_f: float,
    yes_ask_cents: int = 50,
    no_ask_cents: int = 52,
) -> Bracket:
    return Bracket(
        ticker="TEST-LOW-TICKER",
        low_f=low_f,
        high_f=high_f,
        yes_ask_cents=yes_ask_cents,
        yes_ask_size=100,
        no_ask_cents=no_ask_cents,
        no_ask_size=100,
    )


# ---------------------------------------------------------------------------
# compute_envelope_low
# ---------------------------------------------------------------------------

class TestComputeEnvelopeLow:
    def test_compute_envelope_low_basic(self):
        """current_low_f=64, drop=3.0 → (61.0, 64.0)."""
        state = make_state_low(current_low_f=64.0)
        with patch(
            "src.model.envelope_low.expected_additional_drop", return_value=3.0
        ):
            result = compute_envelope_low(state)
        assert result == (61.0, 64.0), f"Expected (61.0, 64.0), got {result}"

    def test_compute_envelope_low_zero_drop(self):
        """With zero additional drop, min and max both equal current_low_f."""
        state = make_state_low(current_low_f=55.0)
        with patch(
            "src.model.envelope_low.expected_additional_drop", return_value=0.0
        ):
            min_low, max_low = compute_envelope_low(state)
        assert min_low == 55.0
        assert max_low == 55.0


# ---------------------------------------------------------------------------
# true_probability_low_in_bracket
# ---------------------------------------------------------------------------

class TestTrueProbabilityLowInBracket:
    def test_true_probability_low_basic(self):
        """current_low_f=64, drop=3.0, forecast=62, bracket=[62,64] → 0 < p < 1.

        Envelope: [61, 64].  Bracket [62, 64] is a partial overlap (lo=62 > min_env=61)
        so full-containment does not trigger; result must be strictly between 0 and 1.
        """
        state = make_state_low(current_low_f=64.0, forecast_low_f=62.0)
        bracket = make_bracket(low_f=62.0, high_f=64.0)
        with patch(
            "src.model.envelope_low.expected_additional_drop", return_value=3.0
        ):
            result = true_probability_low_in_bracket(bracket, state)
        assert 0.0 < result < 1.0, f"Expected value in (0,1), got {result}"

    def test_running_low_excludes_bracket(self):
        """bracket low_f > current_low_f → 0.0 (running low exclusion)."""
        # current_low_f=64; bracket lo=68 > 64 → impossible, low is locked at or below 64
        state = make_state_low(current_low_f=64.0)
        bracket = make_bracket(low_f=68.0, high_f=72.0)
        with patch(
            "src.model.envelope_low.expected_additional_drop", return_value=3.0
        ):
            result = true_probability_low_in_bracket(bracket, state)
        assert result == 0.0, (
            f"bracket lo=68 > current_low_f=64: running-low exclusion should give 0.0, got {result}"
        )

    def test_boundary_running_low_excludes(self):
        """bracket low_f=65.1 > current_low_f=65.0 → 0.0 (boundary case)."""
        state = make_state_low(current_low_f=65.0)
        bracket = make_bracket(low_f=65.1, high_f=70.0)
        with patch(
            "src.model.envelope_low.expected_additional_drop", return_value=3.0
        ):
            result = true_probability_low_in_bracket(bracket, state)
        assert result == 0.0, (
            f"bracket lo=65.1 > current_low_f=65.0: should return 0.0, got {result}"
        )

    def test_bracket_floor_above_envelope_ceiling(self):
        """bracket low_f > current_low_f (max_env) → 0.0."""
        # current_low_f=64; max_env=64 (current_low_f is the ceiling).
        # bracket low=70 > max_env=64 → 0.0
        state = make_state_low(current_low_f=64.0, forecast_low_f=None)
        bracket = make_bracket(low_f=70.0, high_f=75.0)
        with patch(
            "src.model.envelope_low.expected_additional_drop", return_value=3.0
        ):
            result = true_probability_low_in_bracket(bracket, state)
        assert result == 0.0, (
            f"bracket floor above envelope ceiling should give 0.0, got {result}"
        )

    def test_full_containment(self):
        """Bracket spanning entire envelope [min_env, max_env] → 1.0."""
        # current_low_f=64, drop=3 → min_env=61, max_env=64.
        # bracket [55, 70] fully contains [61, 64].
        state = make_state_low(current_low_f=64.0, forecast_low_f=62.0)
        bracket = make_bracket(low_f=55.0, high_f=70.0)
        with patch(
            "src.model.envelope_low.expected_additional_drop", return_value=3.0
        ):
            result = true_probability_low_in_bracket(bracket, state)
        assert result == 1.0, f"Full containment should give 1.0, got {result}"

    def test_probability_bounded_zero_to_one(self):
        """All bracket positions should produce probabilities in [0, 1]."""
        state = make_state_low(current_low_f=64.0, forecast_low_f=62.0)
        with patch(
            "src.model.envelope_low.expected_additional_drop", return_value=3.0
        ):
            for lo, hi in [(50, 55), (58, 63), (60, 65), (65, 70), (70, 75)]:
                bracket = make_bracket(low_f=float(lo), high_f=float(hi))
                result = true_probability_low_in_bracket(bracket, state)
                assert 0.0 <= result <= 1.0, (
                    f"Out of bounds for [{lo}, {hi}]: {result}"
                )

    def test_no_forecast_falls_back_to_midpoint(self):
        """With forecast_low_f=None, function must still return a valid probability."""
        state = make_state_low(current_low_f=64.0, forecast_low_f=None)
        bracket = make_bracket(low_f=60.0, high_f=65.0)
        with patch(
            "src.model.envelope_low.expected_additional_drop", return_value=3.0
        ):
            result = true_probability_low_in_bracket(bracket, state)
        assert 0.0 <= result <= 1.0, f"No-forecast path out of bounds: {result}"


# ---------------------------------------------------------------------------
# Module docstring semantics
# ---------------------------------------------------------------------------

class TestDayWindowSemanticsDocumented:
    def test_day_window_semantics_documented(self):
        """Module docstring must mention 'sunset' and 'sunrise' for the window spec."""
        doc = inspect.getdoc(envelope_low_module)
        assert doc is not None, "Module must have a docstring"
        assert "sunset" in doc, "Docstring must mention 'sunset' (window start)"
        assert "sunrise" in doc, "Docstring must mention 'sunrise' (window end)"


# ---------------------------------------------------------------------------
# Shadow-first guardrail
# ---------------------------------------------------------------------------

class TestNoLiveTradingPathImport:
    def test_no_cooling_rates_import_from_live_paths(self):
        """No file in src/strategy/ or src/trading/ should import from envelope_low."""
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        live_dirs = [
            os.path.join(base, "strategy"),
            os.path.join(base, "trading"),
        ]
        violations = []
        for live_dir in live_dirs:
            if not os.path.isdir(live_dir):
                continue
            for py_file in glob.glob(os.path.join(live_dir, "**", "*.py"), recursive=True):
                with open(py_file) as f:
                    content = f.read()
                if "envelope_low" in content:
                    violations.append(py_file)
        assert violations == [], (
            f"Shadow-first violation: envelope_low imported from live paths: {violations}"
        )

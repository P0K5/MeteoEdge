"""Weather envelope model: computes plausible daily high range and YES probability.

Promoted from src/improved_envelope.py. fetch_secondary_forecast has moved to
src/data/open_meteo.py. Climb rates are now sourced from src/model/climb_rates.py.
"""
from dataclasses import dataclass
from datetime import datetime
from math import erf, sqrt

from src.model.climb_rates import expected_additional_rise


@dataclass
class WeatherState:
    station: str
    now_local: datetime
    sunset_local: datetime
    current_high_f: float
    current_high_time: datetime
    latest_temp_f: float
    latest_temp_time: datetime
    forecast_high_f: float | None
    secondary_forecast_f: float | None = None


@dataclass
class Bracket:
    ticker: str
    low_f: float
    high_f: float
    yes_ask_cents: int
    yes_ask_size: int
    no_ask_cents: int
    no_ask_size: int
    yes_token_id: str | None = None
    no_token_id: str | None = None


def p_normal_between(low: float, high: float, mean: float, stddev: float) -> float:
    """P(low <= X <= high) for X ~ N(mean, stddev^2)."""
    def cdf(x):
        return 0.5 * (1 + erf((x - mean) / (stddev * sqrt(2))))
    return max(0.0, min(1.0, cdf(high) - cdf(low)))


def ensemble_forecast(primary: float | None, secondary: float | None) -> float | None:
    """Combine multiple forecast sources."""
    if primary and secondary:
        return (primary * 0.6 + secondary * 0.4)  # Weight NWS heavier (proven accuracy)
    return primary or secondary


def time_to_settlement_boost(p: float, minutes_left: float) -> float:
    """Boost confidence as settlement approaches and actual temp is nearly determined."""
    if minutes_left < 60:
        # Final hour: compress toward extremes
        # If model says 70%, boost to 75% (more confident at end)
        return min(1.0, max(0.0, p + (p - 0.5) * 0.2 * (1 - minutes_left / 60)))
    return p


def compute_envelope(state: WeatherState, minutes_to_settlement: float = 9999.0) -> tuple[float, float]:
    """Return (min_plausible_high, max_plausible_high) for the rest of the day."""
    min_high = state.current_high_f
    additional = expected_additional_rise(state.now_local)
    max_high = max(
        state.current_high_f,
        state.latest_temp_f + additional,
    )
    return min_high, max_high


def true_probability_yes(bracket: Bracket, state: WeatherState,
                         minutes_to_settlement: float = 9999.0,
                         forecast_stddev: float = 2.0) -> float:
    """Compute P(daily high falls in this bracket).

    Enhanced: uses ensemble forecast and time-to-settlement boost.
    """
    lo, hi = bracket.low_f, bracket.high_f
    min_env, max_env = compute_envelope(state, minutes_to_settlement)

    if hi < state.current_high_f:
        return 0.0
    if lo > max_env:
        return 0.0
    if lo <= state.current_high_f and hi >= max_env:
        return 1.0

    # Use ensemble forecast
    forecast_mean = ensemble_forecast(state.forecast_high_f, state.secondary_forecast_f)
    if forecast_mean is None:
        forecast_mean = (state.current_high_f + max_env) / 2

    forecast_mean = max(min_env, min(max_env, forecast_mean))

    # Base probability
    p = p_normal_between(lo, hi, forecast_mean, forecast_stddev)

    # Boost confidence near settlement
    p = time_to_settlement_boost(p, minutes_to_settlement)

    return p

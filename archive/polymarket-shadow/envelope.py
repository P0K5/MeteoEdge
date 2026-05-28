"""
Unit-aware envelope and edge calculations.

The model is identical to the live spike's envelope.py — same physical
envelope, same Gaussian-around-forecast probability calc — but every
function now carries an explicit `unit` ("F" or "C") so non-US stations
can be modeled in Celsius natively. METAR temps are converted into the
station's native unit before being passed in (see shadow.py).
"""
from dataclasses import dataclass, field
from datetime import datetime
from math import erf, sqrt

from config import DEFAULT_CLIMB_LOOKUP_F, FORECAST_STDDEV_F, FORECAST_STDDEV_C


@dataclass
class WeatherState:
    station: str
    unit: str                     # "F" or "C"
    now_local: datetime
    sunset_local: datetime
    current_high: float           # in station unit
    current_high_time: datetime
    latest_temp: float            # in station unit
    latest_temp_time: datetime
    forecast_high: float | None   # in station unit


@dataclass
class Bracket:
    ticker: str
    unit: str                     # "F" or "C"
    low: float                    # inclusive lower bound, station unit
    high: float                   # inclusive upper bound, station unit
    yes_ask_cents: int
    yes_ask_size: int
    no_ask_cents: int
    no_ask_size: int
    yes_token_id: str | None = field(default=None)
    no_token_id: str | None = field(default=None)


def p_normal_between(low: float, high: float, mean: float, stddev: float) -> float:
    def cdf(x): return 0.5 * (1 + erf((x - mean) / (stddev * sqrt(2))))
    return max(0.0, min(1.0, cdf(high) - cdf(low)))


def expected_additional_rise(unit: str, now_local: datetime) -> float:
    """Returns expected p95 additional rise from `now` to end-of-day, in `unit`."""
    hour = now_local.hour
    if hour >= 20:
        return 0.0
    rise_f = DEFAULT_CLIMB_LOOKUP_F.get(hour, 0.0)
    return rise_f if unit == "F" else rise_f / 1.8


def compute_envelope(state: WeatherState) -> tuple[float, float]:
    """Return (min_plausible_high, max_plausible_high) for the rest of the day."""
    min_high = state.current_high
    additional = expected_additional_rise(state.unit, state.now_local)
    max_high = max(state.current_high, state.latest_temp + additional)
    return min_high, max_high


def true_probability_yes(bracket: Bracket, state: WeatherState) -> float:
    """P(daily high falls in this bracket). Returns [0, 1]."""
    if bracket.unit != state.unit:
        raise ValueError(
            f"unit mismatch: bracket={bracket.unit} state={state.unit} "
            f"for {state.station}"
        )
    lo, hi = bracket.low, bracket.high
    min_env, max_env = compute_envelope(state)

    if hi < state.current_high:
        return 0.0
    if lo > max_env:
        return 0.0
    if lo <= state.current_high and hi >= max_env:
        return 1.0

    forecast_mean = state.forecast_high if state.forecast_high is not None else (
        (state.current_high + max_env) / 2
    )
    forecast_mean = max(min_env, min(max_env, forecast_mean))
    stddev = FORECAST_STDDEV_F if state.unit == "F" else FORECAST_STDDEV_C
    return p_normal_between(lo, hi, forecast_mean, stddev)

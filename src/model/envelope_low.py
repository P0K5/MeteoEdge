"""Daily-low predictor: computes plausible overnight low range and probability.

Observation window for daily-low markets:
  START:  previous calendar day's sunset (approximately 18:00–22:00 local)
  END:    settlement day's sunrise (approximately 05:00–09:00 local)
  SETTLEMENT: typically 12:00 UTC on the settlement day

The 'current_low_f' in WeatherStateLow is the minimum temperature observed
within this sunset-to-sunrise window, NOT the calendar-day minimum.

Example (Chicago, KORD, July):
  Window start: 2026-07-01 20:30 CDT (sunset)
  Window end:   2026-07-02 05:45 CDT (sunrise)
  Settlement:   2026-07-02 07:00 CDT (12:00 UTC)
  At 02:00 CDT: current_low_f = 64 F observed so far.
  expected_additional_drop(02:00 CDT, 'KORD') = 3 F → could drop to 61 F.
  For bracket [60, 65]: compute P(low in [60,65]) using N(forecast_low, stddev^2).
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime

from src.model.envelope import Bracket, p_normal_between, ensemble_forecast, time_to_settlement_boost
from src.model.cooling_rates import expected_additional_drop


@dataclass
class WeatherStateLow:
    station: str
    now_local: datetime
    sunrise_local: datetime        # next sunrise — the settlement horizon
    current_low_f: float           # running minimum in this observation window
    current_low_time: datetime
    latest_temp_f: float
    latest_temp_time: datetime
    forecast_low_f: float | None
    secondary_forecast_low_f: float | None = None


def compute_envelope_low(state: WeatherStateLow) -> tuple[float, float]:
    """Return (min_plausible_low, max_plausible_low) for the rest of the night.

    max_plausible_low = current_low_f (floor already set by observation)
    min_plausible_low = current_low_f - expected_additional_drop
    """
    additional = expected_additional_drop(state.now_local, station=state.station)
    min_low = state.current_low_f - additional
    max_low = state.current_low_f
    return min_low, max_low


def true_probability_low_in_bracket(
    bracket: Bracket,
    state: WeatherStateLow,
    minutes_to_settlement: float = 9999.0,
    forecast_stddev: float = 2.0,
) -> float:
    """Compute P(daily low falls in [bracket.low_f, bracket.high_f]).

    Analogous to true_probability_yes in envelope.py but for the low side.
    Running-low exclusion: if bracket.high_f < state.current_low_f, return 0.0 —
    the running low is already above the bracket ceiling, so the daily low
    (which is locked at or below current_low_f) cannot fall in this bracket.
    """
    lo, hi = bracket.low_f, bracket.high_f
    min_env, max_env = compute_envelope_low(state)

    forecast_mean = ensemble_forecast(state.forecast_low_f, state.secondary_forecast_low_f)

    # Expand envelope downward if forecast is below temperature-progression floor
    if forecast_mean is not None and forecast_mean < min_env:
        min_env = forecast_mean

    # Running-low exclusion (per spec): daily low is already at or below current_low_f;
    # if bracket.high_f is above current_low_f (hi > current_low_f), it's in range.
    # if bracket.high_f < current_low_f, bracket is entirely below what's been observed — 0.0
    if hi < state.current_low_f:
        return 0.0

    # Bracket floor above the envelope ceiling → 0.0
    if lo > max_env:
        return 0.0

    # Full containment
    if lo <= min_env and hi >= max_env:
        return 1.0

    if forecast_mean is None:
        forecast_mean = (min_env + max_env) / 2

    forecast_mean = max(min_env, min(max_env, forecast_mean))

    p = p_normal_between(lo, hi, forecast_mean, forecast_stddev)
    p = time_to_settlement_boost(p, minutes_to_settlement)
    return p

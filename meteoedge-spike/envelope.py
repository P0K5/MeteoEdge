from dataclasses import dataclass
from datetime import datetime
from config import DEFAULT_CLIMB_LOOKUP, FORECAST_STDDEV_F

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

@dataclass
class Bracket:
    ticker: str
    low_f: float
    high_f: float
    yes_ask_cents: int
    yes_ask_size: int
    no_ask_cents: int
    no_ask_size: int

def compute_envelope(state: WeatherState) -> tuple[float, float]:
    return (0.0, 0.0)

def true_probability_yes(bracket: Bracket, state: WeatherState) -> float:
    return 0.0

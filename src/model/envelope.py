"""Weather envelope model: computes plausible daily high range and YES probability.

Promoted from src/improved_envelope.py. fetch_secondary_forecast has moved to
src/data/open_meteo.py. Climb rates are now sourced from src/model/climb_rates.py.
"""
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from math import erf, sqrt

from src.model.climb_rates import expected_additional_rise

log = logging.getLogger(__name__)

# Module-level flag to ensure DEB_ENABLED status is logged only once per process
_deb_enabled_logged = False


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
    obs_bias_offset_f: float | None = None   # intraday obs bias vs model hourly temp
    deb_mu_f: float | None = None            # DEB-weighted forecast high; used when DEB_ENABLED=true
    corrected_mu_f: float | None = None      # intraday-corrected forecast; highest-priority when set
    # The decayed intraday delta alone (corrected_mu_f - deb_mu_f at the time
    # the intraday correction fired), kept separately so the EMOS serving path
    # can layer the nowcast signal ON TOP of its calibrated mean instead of
    # consuming the fully-corrected mu it was never trained on (#658).
    intraday_delta_f: float | None = None
    # Per-station GEFS ensemble-spread sigma (°F), from src/model/ensemble_sigma.py
    # via src/scripts/capture_forecasts.py. None when GEFS is unavailable for the
    # station or a caller hasn't wired the producer yet. Consumed by
    # true_probability_yes (and the EMOS-shadow serving path) instead of the
    # fixed FORECAST_STDDEV_F only when USE_ENSEMBLE_SIGMA is enabled (#448).
    ensemble_sigma_f: float | None = None
    # Per-model forecast-highs (°F) for the expanded FORECAST_STACK regimes
    # (hrrr_nbm / intl_ecmwf_icon). Populated by src.weather.builder from the
    # already-captured model_forecast_log row for that model (lowest
    # lead_hours, same source src.model.ensemble_distribution reads) ONLY
    # when the active FORECAST_STACK needs it -- see src.config.
    # MODEL_STATE_ATTRS for the model->attribute mapping. None on the
    # baseline stack (the default) and for stations/regions the model
    # doesn't cover. Consumed by src.model.emos_mode.emos_serving_mu to keep
    # EMOS train/serve parity when a non-baseline stack is active (#666, #760).
    hrrr_forecast_f: float | None = None
    nbm_forecast_f: float | None = None
    ecmwf_forecast_f: float | None = None
    icon_forecast_f: float | None = None


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


def next_day_probability_yes(bracket: Bracket, mu: float, sigma: float) -> float:
    """P(next-day daily high falls in this bracket) -- forecast-only path (issue #687).

    This is a distinct probability path from ``true_probability_yes``, not a
    same-day call with a doctored ``WeatherState``. Next-day markets are
    evaluated before today's observation window for that settlement date has
    even started, so none of the same-day concepts apply:

    - No observed-high floor (``min_high = current_high_f``) -- there is no
      "already observed" running high for a day that hasn't started yet.
    - No ``max_env`` climb ceiling -- dropping the floor without also
      dropping the ceiling would still leave an upper truncation that has no
      meaning before the day's climb has begun. Removing both means
      next-day probability mass is wider and more symmetric around ``mu``
      than an equivalent same-day evaluation -- intentional, per the #687
      design doc, not an oversight.
    - No ``time_to_settlement_boost`` -- that assumes close observation of a
      controlled process approaching settlement, which is also a same-day
      property.

    Plain Gaussian bracket integration via ``p_normal_between``.

    Args:
        bracket: Bracket to evaluate.
        mu: Forecast mean daily high for tomorrow's date. Callers must derive
            this from the forecast stack (or a lead-appropriate DEB/EMOS
            mean) -- never from today's observations.
        sigma: Forecast stddev for tomorrow's date. When a caller applies an
            EMOS calibration transform to mu, sigma must come from that same
            calibration row's transform (or neither should be calibrated) --
            see the round-2 #687 review's calibration consistency rule. This
            function does not enforce that; it is the caller's contract.
    """
    if sigma <= 0:
        raise ValueError(f"next_day_probability_yes: sigma must be positive, got {sigma!r}")
    return p_normal_between(bracket.low_f, bracket.high_f, mu, sigma)


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
    additional = expected_additional_rise(state.now_local, station=state.station)
    max_high = max(
        state.current_high_f,
        state.latest_temp_f + additional,
    )
    return min_high, max_high


def true_probability_yes(bracket: Bracket, state: WeatherState,
                         minutes_to_settlement: float = 9999.0,
                         forecast_stddev: float = 2.0,
                         deb_enabled: "bool | None" = None,
                         sigma_climb_fraction: float = 0.5,
                         use_ensemble_sigma: "bool | None" = None) -> float:
    """Compute P(daily high falls in this bracket).

    Enhanced: uses ensemble forecast and time-to-settlement boost.

    Args:
        bracket: Bracket to evaluate
        state: WeatherState with forecasts and observations
        minutes_to_settlement: Time until market resolves (default 9999 = far in future)
        forecast_stddev: Forecast uncertainty (default 2.0 degrees F). Overridden by
            state.ensemble_sigma_f when use_ensemble_sigma resolves True and the
            field is set (issue #448) -- see use_ensemble_sigma below.
        deb_enabled: Resolved DEB_ENABLED flag. Callers with DB access should pass
            the value from get_live_config (read once per scan cycle, not per bracket).
            When None, falls back to the DEB_ENABLED env var (backward compatibility).
        sigma_climb_fraction: The effective stddev is floored at this fraction of
            the climb still to come (max_env - current_high), so early-day
            evaluations cannot claim near-certainty about a high that is mostly
            unrealized (issue #652). Callers with DB access pass the live
            ENVELOPE_SIGMA_CLIMB_FRACTION config value.
        use_ensemble_sigma: Resolved USE_ENSEMBLE_SIGMA flag (issue #448). Callers
            with DB access should pass the value from get_live_config, read once
            per scan cycle. When None, falls back to the USE_ENSEMBLE_SIGMA env
            var (backward compatibility). When the resolved flag is True AND
            state.ensemble_sigma_f is not None, forecast_stddev is replaced by
            state.ensemble_sigma_f before the climb-fraction floor is applied.
            When False, or when ensemble_sigma_f is None (e.g. GEFS unavailable),
            behaviour is unchanged -- the legacy fixed-sigma (forecast_stddev)
            path is used.
    """
    global _deb_enabled_logged

    lo, hi = bracket.low_f, bracket.high_f
    min_env, max_env = compute_envelope(state, minutes_to_settlement)

    # Resolve DEB_ENABLED: caller-provided (from live config) wins; env var is the fallback
    if deb_enabled is not None:
        deb_source = "caller"
    else:
        deb_enabled = os.getenv("DEB_ENABLED", "false").lower() == "true"
        deb_source = "env var"

    # Log DEB_ENABLED status once at first evaluation
    if not _deb_enabled_logged:
        log.info("DEB_ENABLED=%s (source: %s)", deb_enabled, deb_source)
        _deb_enabled_logged = True

    # Resolve USE_ENSEMBLE_SIGMA: caller-provided (from live config) wins; env
    # var is the fallback (same precedence pattern as DEB_ENABLED above).
    if use_ensemble_sigma is None:
        use_ensemble_sigma = os.getenv("USE_ENSEMBLE_SIGMA", "false").lower() == "true"
    if use_ensemble_sigma and state.ensemble_sigma_f is not None:
        forecast_stddev = state.ensemble_sigma_f

    # Priority: corrected_mu_f (intraday) > deb_mu_f (DEB-enabled) > ensemble fallback.
    # Compute before early exits so a high forecast can expand max_env.
    if state.corrected_mu_f is not None:
        forecast_mean = state.corrected_mu_f
    elif state.deb_mu_f is not None and deb_enabled:
        forecast_mean = state.deb_mu_f
    else:
        forecast_mean = ensemble_forecast(state.forecast_high_f, state.secondary_forecast_f)

    # A forecast above the temperature-progression ceiling expands the envelope.
    if forecast_mean is not None and forecast_mean > max_env:
        max_env = forecast_mean

    # Markets resolve [lo, hi): a running high AT the top edge already belongs
    # to the bracket above and, being a running max, can never come back down.
    # The old `hi < current_high` let boundary-exact highs (every integer-°C
    # high on C-bucket stations) fall through to the certainty shortcut below,
    # booking p=1.0 on brackets that resolved NO 96-100% of the time (#652).
    if hi <= state.current_high_f:
        return 0.0
    if lo > max_env:
        return 0.0
    if lo <= state.current_high_f and hi >= max_env:
        return 1.0

    if forecast_mean is None:
        forecast_mean = (state.current_high_f + max_env) / 2

    forecast_mean = max(min_env, min(max_env, forecast_mean))
    if state.obs_bias_offset_f is not None:
        forecast_mean = max(min_env, min(max_env, forecast_mean + state.obs_bias_offset_f))

    # Uncertainty about the day's high can never be tighter than a fraction of
    # the climb still to come: with a fixed 2°F stddev, 6am evaluations claimed
    # near-certainty on "X or below" brackets while the climb table still
    # allowed a 20°F+ rise — those calls resolved wrong ~95% of the time (#652).
    # Past the peak (max_env ≈ current_high) the floor vanishes and behavior
    # is unchanged.
    remaining_rise = max(0.0, max_env - state.current_high_f)
    effective_stddev = max(forecast_stddev, sigma_climb_fraction * remaining_rise)

    # Base probability
    p = p_normal_between(lo, hi, forecast_mean, effective_stddev)

    # Boost confidence near settlement
    p = time_to_settlement_boost(p, minutes_to_settlement)

    return p

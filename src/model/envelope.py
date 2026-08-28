"""Weather envelope model: computes plausible daily high range and YES probability.

Promoted from src/improved_envelope.py. fetch_secondary_forecast has moved to
src/data/open_meteo.py. Climb rates are now sourced from src/model/climb_rates.py.
"""
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from math import erf, sqrt

from src.model.climb_rates import expected_additional_rise
from src.model.ensemble_sigma import SIGMA_FLOOR_F

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
    # station or a caller hasn't wired the producer yet (#885). Consumed by
    # true_probability_yes (and the EMOS-shadow serving path) instead of the
    # fixed FORECAST_STDDEV_F only when USE_ENSEMBLE_SIGMA is enabled (#448).
    #
    # Contract (#887): this must carry the SAME raw, unfloored quantity EMOS
    # trains on (src.model.ensemble_sigma.raw_member_sigma /
    # model_forecast_log.sigma_f) -- never SIGMA_FLOOR_F-floored or
    # regression-calibrated here on WeatherState. #885's wiring must NOT
    # source this from compute_ensemble_sigma() (that applies the floor +
    # calibration meant for a caller with no other transform downstream,
    # which floors twice for EMOS-served cities and breaks train/serve
    # parity). Each of this field's two consumers is responsible for its
    # own floor/calibration: true_probability_yes's direct-substitution
    # branch floors at SIGMA_FLOOR_F itself (see its use_ensemble_sigma
    # docstring); resolve_sigma_raw -> apply_emos deliberately does not,
    # because apply_emos's (c, d) transform IS the calibration step for
    # that path.
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
    # `*_ask_cents` are clamped to the tradeable integer-cent range
    # `max(1, min(99, round(price * 100)))` -- see `_safe_price`/
    # `parse_bracket_from_market` in src/strategy/scanner.py. This clamp
    # destroys any true price below 0.5c or above 99.5c, which matters
    # because Polymarket's own tick size tightens to $0.001 in exactly that
    # region (price > 0.96 or < 0.04) -- 74% of our brackets sit there
    # (issue #1076). The clamped fields are retained because every existing
    # consumer (gates, sizing, fee estimation) is built around integer cents
    # and changing that is out of scope here.
    yes_ask_cents: int
    yes_ask_size: int
    no_ask_cents: int
    no_ask_size: int
    yes_token_id: str | None = None
    no_token_id: str | None = None
    # Unclamped float price ([0, 1], not cents) as quoted by the venue,
    # preserved alongside the clamped integer above so sub-penny prices
    # survive into `bracket_evals`/`scan_decisions` instead of being
    # silently rounded away (issue #1076). Callers analysing true price
    # (e.g. the rail question, dutch-book detection) should prefer these
    # over `*_ask_cents`; every gate/trading code path is unchanged and
    # keeps reading the clamped cents fields. Mirrors `_safe_price`'s own
    # 0.5 fallback when the venue price is missing/unparseable -- #1029
    # (not this issue) will decide whether that fallback should instead
    # skip the bracket.
    yes_price_raw: float | None = None
    no_price_raw: float | None = None


def p_normal_between(low: float, high: float, mean: float, stddev: float) -> float:
    """P(low <= X < high) for X ~ N(mean, stddev^2).

    Bracket convention: [low, high) — lower bound inclusive, upper bound exclusive.
    This prevents double-counting at bracket boundaries (see issues #861, #881).

    When stddev <= 0, treats the distribution as a point mass at the mean:
    - Returns 1 if low <= mean < high, else 0.
    """
    # Handle point mass case (stddev <= 0)
    if stddev <= 0:
        return 1.0 if (low <= mean < high) else 0.0

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


#: Below this, the surviving interval has collapsed and the conditional is not
#: computable in floating point. Handled explicitly rather than clamped, so a
#: degenerate day never silently produces a near-arbitrary ratio.
_MASS_EPS = 1e-12


def conditional_bracket_probability(lo: float, hi: float, current_high: float,
                                    max_env: float, mean: float,
                                    stddev: float) -> float:
    """P(daily high in ``[lo, hi)`` | it lies in ``[current_high, max_env]``).

    The day's high is known to sit inside that interval: it cannot fall below
    a running maximum already observed, and it cannot exceed the temperature-
    progression ceiling. Reporting the *unconditional* normal probability for
    each bracket therefore leaks every unit of mass outside the interval --
    which is what issue #920 measured, and why production ladders summed to
    0.80 rather than 1.0.

    Conditioning fixes it by construction: clip the bracket to the surviving
    interval, then divide by that interval's own mass. The ladder sums to 1.0
    for any bracket set that partitions the interval, with no ladder-level pass
    and no cross-bracket state -- which matters, because the scanner evaluates
    one bracket at a time and cannot see the others.

    This subsumes the three certainty shortcuts it replaces, rather than
    sitting alongside them:

    * ``hi <= current_high`` -> the clipped interval is empty -> 0.0
    * ``lo > max_env``       -> the clipped interval is empty -> 0.0
    * bracket spans the whole interval -> numerator == denominator -> 1.0

    So the old behaviour is preserved exactly at those three boundaries, and
    the previously-unnormalised middle is what changes.

    **The top clip is the expensive one.** Brackets below an observed high hold
    almost no mass anyway -- a running maximum cannot decrease -- so the bottom
    clip costs ~3%. Brackets above ``max_env`` hold real mass, because the day
    can still warm, so the top clip costs ~15% (measured 2026-08-05 across
    5,829 production ladders). #920 as filed covered only the bottom.
    """
    # Collapse is checked FIRST, and must be: past the peak the interval is a
    # single point, so every bracket's clipped width is zero and the empty-clip
    # branch below would return 0.0 for all of them -- a ladder summing to 0
    # instead of 1. The degenerate day is the one where mass conservation is
    # most obviously required, not least.
    if max_env <= current_high:
        return 1.0 if lo <= current_high < hi else 0.0

    lo_eff = max(lo, current_high)
    hi_eff = min(hi, max_env)
    if hi_eff <= lo_eff:
        return 0.0

    surviving = p_normal_between(current_high, max_env, mean, stddev)
    if surviving <= _MASS_EPS:
        # Numerically collapsed rather than geometrically: the interval has
        # width but the forecast puts effectively no mass in it (a badly wrong
        # forecast against a high already observed). Same resolution -- pin to
        # the bracket containing the observed high rather than divide by ~zero.
        return 1.0 if lo <= current_high < hi else 0.0

    return p_normal_between(lo_eff, hi_eff, mean, stddev) / surviving


def true_probability_yes(bracket: Bracket, state: WeatherState,
                         minutes_to_settlement: float = 9999.0,
                         forecast_stddev: float = 2.0,
                         deb_enabled: "bool | None" = None,
                         sigma_climb_fraction: float = 0.5,
                         use_ensemble_sigma: "bool | None" = None,
                         settlement_date: "date | None" = None) -> float:
    """Compute P(daily high falls in this bracket).

    Enhanced: uses ensemble forecast and time-to-settlement boost.

    Args:
        bracket: Bracket to evaluate
        state: WeatherState with forecasts and observations
        minutes_to_settlement: Time until market resolves (default 9999 = far in future)
        forecast_stddev: Forecast uncertainty (default 2.0 degrees F). Overridden by
            state.ensemble_sigma_f when use_ensemble_sigma resolves True and the
            field is set (issue #448) -- see use_ensemble_sigma below. The
            SIGMA_FLOOR_F floor is applied to that override (issue #887): this
            direct-substitution path has no EMOS transform downstream of it, so
            it is the one ensemble_sigma_f consumer that must floor for itself
            (see the #887 note on use_ensemble_sigma below).
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
            ``max(state.ensemble_sigma_f, SIGMA_FLOOR_F)`` before the
            climb-fraction floor is applied. When False, or when
            ensemble_sigma_f is None (e.g. GEFS unavailable), behaviour is
            unchanged -- the legacy fixed-sigma (forecast_stddev) path is used.

            Issue #887 (train-raw / serve-calibrated decision): 63% of GEFS
            sigma_f rows sit below SIGMA_FLOOR_F (1.0F) -- the classic
            under-dispersive ensemble signature. EMOS training deliberately
            keeps consuming that RAW, unfloored sigma_f (src.model.
            emos_calibration.fetch_training_data / #555's capture-vs-
            consumption split) so its own (c, d) regression learns the real
            spread-vs-error relationship instead of one starved by a
            pre-applied floor. Whatever populates state.ensemble_sigma_f
            (#885) should carry that SAME raw quantity, so
            src.model.emos_mode.resolve_sigma_raw -> apply_emos keeps
            train/serve parity for EMOS-served cities (apply_emos's own
            c/d transform is the "calibrated" half of that path -- do not
            floor ensemble_sigma_f before it reaches apply_emos). BUT this
            function's direct substitution above has no such transform: it
            is what scanner.py's "legacy" branch (mode != emos_primary) --
            still every station's SOLE serving path per #886, zero cities
            promoted to emos_primary -- feeds straight into
            p_normal_between. Without a floor here, a genuine 0.2-0.9F raw
            ensemble spread would reproduce exactly the overconfidence M0
            (#820) removed. Hence the floor is applied HERE, at this one
            consumption site, and nowhere else -- the same "floor only at
            consumption, per-consumer" principle #555 established for
            capture vs. compute_ensemble_sigma(), extended to cover the
            second, non-EMOS consumer #555 didn't have to consider.
        settlement_date: The market's settlement date (issue #820). When provided
            and the WeatherState's local day differs from the settlement date,
            observation-based certainty shortcuts are skipped -- current_high_f
            and max_env are observations from the wrong local day and would
            produce false 0.0/1.0 certainty in the evening window (e.g.
            19:00-22:00 CDT when UTC has already rolled over but the station is
            still in the previous local day). Behaviour is unchanged when None
            or when the dates match.
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
        # #887: floor the raw ensemble spread HERE, at this direct-
        # substitution consumption site only -- see the use_ensemble_sigma
        # docstring above for why this must not move upstream onto
        # state.ensemble_sigma_f itself (that would break EMOS train/serve
        # parity for resolve_sigma_raw/apply_emos, the other consumer of
        # the same field).
        forecast_stddev = max(state.ensemble_sigma_f, SIGMA_FLOOR_F)

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

    # When the WeatherState's local day differs from the settlement day,
    # current_high_f and max_env are observations from the wrong day --
    # the certainty shortcuts would produce false 0.0/1.0 (issue #820:
    # evening false-certainty entries). Skip them and use forecast-only
    # evaluation instead.
    _day_mismatch = (settlement_date is not None
                     and state.now_local.date() != settlement_date)

    # The three certainty shortcuts that used to sit here -- return 0.0 when
    # `hi <= current_high`, 0.0 when `lo > max_env`, 1.0 when the bracket spans
    # the whole envelope -- are now special cases of
    # conditional_bracket_probability() below, which reproduces all three at
    # their boundaries and additionally normalises the middle (#920).
    #
    # They had to move: the conditional needs forecast_mean and
    # effective_stddev, which are computed further down. Returning before those
    # exist is what made normalisation impossible in the first place.
    #
    # Markets resolve [lo, hi): a running high AT the top edge already belongs
    # to the bracket above and, being a running max, can never come back down.
    # The old `hi < current_high` let boundary-exact highs (every integer-°C
    # high on C-bucket stations) fall through to the certainty shortcut,
    # booking p=1.0 on brackets that resolved NO 96-100% of the time (#652) --
    # the clip preserves that fix, since `hi <= current_high` still empties the
    # interval.

    if forecast_mean is None:
        forecast_mean = (state.current_high_f + max_env) / 2

    # Clamp forecast_mean to the observation-based envelope only when
    # observations are from the correct local day. On a day mismatch the
    # envelope bounds are from the wrong day and would distort the
    # forecast (e.g. clamping today's 85°F forecast to yesterday's 88°F
    # realized high -- issue #820).
    if not _day_mismatch:
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

    if _day_mismatch:
        # Wrong-day observations (#820): current_high_f and max_env describe a
        # different day, so conditioning on them would be conditioning on the
        # wrong evidence. Forecast-only, unconditioned -- the ladder is not
        # expected to sum to 1.0 in this branch, and that is correct: no
        # observation has ruled anything out.
        p = p_normal_between(lo, hi, forecast_mean, effective_stddev)
    else:
        p = conditional_bracket_probability(
            lo, hi, state.current_high_f, max_env,
            forecast_mean, effective_stddev,
        )

    # Boost confidence near settlement.
    #
    # NOTE (#920 follow-up): this is a per-bracket nonlinear transform --
    # `p + (p-0.5)*0.2*(1-t/60)` -- so inside the final hour it perturbs the
    # conservation the conditional above establishes. Most ladder brackets sit
    # below 0.5, where it pulls them down, so a final-hour ladder sums slightly
    # under 1.0. Deliberately left alone here: landing two probability changes
    # in one window is what #920 itself warns against, and the conditional
    # already sharpens naturally as [current_high, max_env] narrows through the
    # day, which is what this boost was approximating. Filed separately.
    p = time_to_settlement_boost(p, minutes_to_settlement)

    return p

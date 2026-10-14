"""Regression test: emos_primary serving must not double-count obs_bias_offset_f.

Issue: scanner.py's emos_primary branch injects EMOS's calibrated mean via
``dataclasses.replace(state, corrected_mu_f=_mu_final)`` without clearing
``state.obs_bias_offset_f``. ``true_probability_yes`` (src/model/envelope.py)
adds ``obs_bias_offset_f`` on top of ``forecast_mean`` unconditionally,
regardless of where ``forecast_mean`` came from. builder.py already clears
that field to None whenever ITS OWN intraday correction succeeds, for
exactly this double-counting reason (issue #657) -- but that guard never
fires when builder's correction legitimately returns None (no deb_mu_f, no
fresh observation, a stale one) while emos_serving_mu() still produces a
valid mean independently. Left unguarded, an emos_primary city would get
EMOS's already bias-corrected mean plus the stale legacy nowcast nudge
stacked on top -- the same error class #657 fixed for the legacy path.

This test asserts the fix: scanner.py must clear obs_bias_offset_f at the
same point it overwrites corrected_mu_f for emos_primary.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.model.envelope import Bracket, WeatherState
from src.strategy.scanner import scan_markets

_FROZEN_NOW = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)


def _make_weather_state(obs_bias_offset_f) -> WeatherState:
    """A WeatherState as builder.py would leave it when its OWN intraday
    correction (compute_correction) returned None -- deb_mu_f/corrected_mu_f
    unset, but obs_bias_offset_f still populated as the fallback nowcast
    nudge (builder.py's #657 guard only clears it inside the branch where
    compute_correction succeeds)."""
    return WeatherState(
        station="KORD",
        now_local=_FROZEN_NOW,
        sunset_local=_FROZEN_NOW,
        current_high_f=70.0,
        current_high_time=_FROZEN_NOW,
        latest_temp_f=68.0,
        latest_temp_time=_FROZEN_NOW,
        forecast_high_f=82.0,
        obs_bias_offset_f=obs_bias_offset_f,
    )


def _make_bracket() -> Bracket:
    return Bracket(
        ticker="0xEMOS_PRIMARY_TEST",
        low_f=81.0,
        high_f=83.0,
        yes_ask_cents=5,
        yes_ask_size=500,
        no_ask_cents=97,
        no_ask_size=500,
    )


def _make_market() -> dict:
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:00Z")
    return {
        "question": "Will the highest temperature in Chicago be 81-83°F on test date?",
        "groupItemTitle": "81-83°F",
        "conditionId": "0xCONDITION",
        "endDate": today_str,
        "yesTokenId": "0xYES",
        "noTokenId": "0xNO",
    }


def test_obs_bias_offset_f_cleared_before_true_probability_yes_for_emos_primary():
    """The state object scanner.py hands to true_probability_yes for an
    emos_primary city must have obs_bias_offset_f=None, even when the
    WeatherState it started from carried a non-None value."""
    from src.strategy import scanner as _scanner_mod

    weather = {"KORD": _make_weather_state(obs_bias_offset_f=3.5)}
    bracket = _make_bracket()
    market = _make_market()
    db = MagicMock()
    db.get_config.return_value = None
    db.has_open_live_position.return_value = False

    seen_states = []

    def _capture_true_probability_yes(bracket, state, *args, **kwargs):
        seen_states.append(state)
        return 0.5  # arbitrary; not under test here

    with (
        patch.object(_scanner_mod, "ENABLE_CLOB_ENRICHMENT", False),
        patch.object(_scanner_mod, "parse_bracket_from_market", return_value=bracket),
        patch("src.strategy.scanner.get_orderbook", return_value={"asks": [], "bids": []}),
        patch("src.strategy.scanner.check_taf_disruption", return_value=False),
        patch("src.strategy.scanner.get_city_mode", return_value="emos_primary"),
        patch("src.strategy.scanner._check_ready_for_promotion", return_value=True),
        patch("src.strategy.scanner.emos_serving_mu", return_value=(79.0, 2.0)),
        patch("src.strategy.scanner.true_probability_yes", side_effect=_capture_true_probability_yes),
        patch("src.strategy.scanner.estimate_fee_cents", return_value=1.0),
        patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0),
        patch("src.strategy.scanner.get_live_config", return_value={}),
    ):
        scan_markets(weather, [market], db=db)

    assert seen_states, "true_probability_yes was never called -- test setup is wrong"
    for state in seen_states:
        assert state.corrected_mu_f == 79.0, (
            "EMOS's calibrated mean should have been injected into corrected_mu_f"
        )
        assert state.obs_bias_offset_f is None, (
            "obs_bias_offset_f must be cleared when corrected_mu_f is overwritten "
            "for emos_primary -- otherwise true_probability_yes adds the stale "
            "legacy nowcast nudge on top of EMOS's already bias-corrected mean "
            "(issue #657 recurrence)"
        )

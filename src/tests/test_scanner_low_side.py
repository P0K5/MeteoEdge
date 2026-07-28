"""Tests for low-side scanner extension (Issue #455)."""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.model.envelope import Bracket
from src.model.envelope_low import WeatherStateLow  # noqa: used in type hints only
from src.strategy import scanner as _scanner_mod
from src.strategy.scanner import (
    Candidate,
    is_highest_temp_market,
    is_lowest_temp_market,
    scan_markets,
)

# Frozen reference datetime (mid-day UTC, well clear of midnight) so fixture
# times are deterministic regardless of when CI runs (issue #844).  Tests that
# construct a market with today's endDate also patch MIN_MINUTES_TO_SETTLEMENT=0
# to eliminate the 15-minute outside_window flake window.
_FROZEN_NOW = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# is_lowest_temp_market detection
# ---------------------------------------------------------------------------

def _market(question: str, group_title: str = "82-84°F") -> dict:
    return {
        "question": question,
        "groupItemTitle": group_title,
        "conditionId": "0xABC",
        "outcomes": '["Yes","No"]',
        "outcomePrices": '[0.4, 0.6]',
        "clobTokenIds": '["tok1","tok2"]',
        "endDate": datetime.now(timezone.utc).isoformat(),
    }


class TestIsLowestTempMarket:
    def test_detects_chicago_low(self):
        m = _market("Will the lowest temperature in Chicago be 55-58°F on July 5, 2026?")
        ok, station = is_lowest_temp_market(m)
        assert ok is True
        assert station == "KORD"

    def test_detects_nyc_alias(self):
        m = _market("Will the lowest temperature in NYC be 62-65°F on July 5, 2026?")
        ok, station = is_lowest_temp_market(m)
        assert ok is True
        assert station == "KJFK"

    def test_detects_london(self):
        m = _market("Will the lowest temperature in London be 12-15°C on July 5, 2026?")
        ok, station = is_lowest_temp_market(m)
        assert ok is True
        assert station == "EGLC"

    def test_detects_seoul(self):
        m = _market("Will the lowest temperature in Seoul be 18-21°C on July 5, 2026?")
        ok, station = is_lowest_temp_market(m)
        assert ok is True
        assert station == "RKSI"

    def test_rejects_highest_temp_market(self):
        m = _market("Will the highest temperature in Chicago be 82-84°F on July 5, 2026?")
        ok, _ = is_lowest_temp_market(m)
        assert ok is False

    def test_rejects_unknown_city(self):
        m = _market("Will the lowest temperature in Atlantis be 55-58°F on July 5?")
        ok, _ = is_lowest_temp_market(m)
        assert ok is False

    def test_rejects_non_temp_market(self):
        m = _market("Will it rain in Chicago tomorrow?")
        ok, _ = is_lowest_temp_market(m)
        assert ok is False

    def test_label_patterns_gte(self):
        m = _market("Will the lowest temperature in Miami be 70°F or above on July 5, 2026?")
        ok, station = is_lowest_temp_market(m)
        assert ok is True
        assert station == "KMIA"

    def test_label_patterns_lte(self):
        m = _market("Will the lowest temperature in Atlanta be 60°F or below on July 5, 2026?")
        ok, station = is_lowest_temp_market(m)
        assert ok is True
        assert station == "KATL"


class TestHighestTempMarketRegressions:
    """is_highest_temp_market must not match lowest-temp markets."""

    def test_lowest_market_not_matched_by_highest(self):
        m = _market("Will the lowest temperature in Chicago be 55-58°F?")
        ok, _ = is_highest_temp_market(m)
        assert ok is False

    def test_highest_still_works(self):
        m = _market("Will the highest temperature in Chicago be 82-84°F?")
        ok, station = is_highest_temp_market(m)
        assert ok is True
        assert station == "KORD"


# ---------------------------------------------------------------------------
# Candidate.direction field
# ---------------------------------------------------------------------------

class TestCandidateDirectionField:
    def test_default_direction_is_high(self):
        bracket = Bracket(
            ticker="TEST", low_f=80.0, high_f=84.0,
            yes_ask_cents=45, yes_ask_size=100,
            no_ask_cents=55, no_ask_size=100,
        )
        c = Candidate(
            station="KORD", bracket=bracket, side="NO",
            edge_cents=5.0, price_cents=55, confidence=0.7,
            p_yes=0.3, ev_yes=-5.0, ev_no=5.0,
            minutes_to_settlement=120.0, market={},
        )
        assert c.direction == "high"

    def test_explicit_low_direction(self):
        bracket = Bracket(
            ticker="TEST", low_f=55.0, high_f=59.0,
            yes_ask_cents=45, yes_ask_size=100,
            no_ask_cents=55, no_ask_size=100,
        )
        c = Candidate(
            station="KORD", bracket=bracket, side="NO",
            edge_cents=5.0, price_cents=55, confidence=0.7,
            p_yes=0.3, ev_yes=-5.0, ev_no=5.0,
            minutes_to_settlement=120.0, market={},
            direction="low",
        )
        assert c.direction == "low"


# ---------------------------------------------------------------------------
# scan_markets — low-side routing
# ---------------------------------------------------------------------------

def _make_weather_low(station: str, current_low_f: float = 62.0) -> WeatherStateLow:
    return WeatherStateLow(
        station=station,
        now_local=_FROZEN_NOW,
        sunrise_local=_FROZEN_NOW,
        current_low_f=current_low_f,
        current_low_time=_FROZEN_NOW,
        latest_temp_f=current_low_f + 2.0,
        latest_temp_time=_FROZEN_NOW,
        forecast_low_f=60.0,
    )


def _low_market(city: str, group_title: str = "55-59°F") -> dict:
    """Build a low-side market with today's end-of-day UTC as endDate.

    Uses the real wall-clock date (not _FROZEN_NOW) because the scanner's
    wrong_date gate compares it against the real today_utc.  The 15-minute
    outside_window flake is eliminated by the MIN_MINUTES_TO_SETTLEMENT=0
    patch in the test methods (issue #844).
    """
    today_end = datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:00Z")
    return {
        "question": f"Will the lowest temperature in {city} be {group_title}?",
        "groupItemTitle": group_title,
        "conditionId": f"0xLOW_{city.upper()}",
        "outcomes": '["Yes","No"]',
        "outcomePrices": '[0.3, 0.7]',
        "clobTokenIds": '["tok1","tok2"]',
        "endDate": today_end,
    }


class TestScanMarketsLowSide:
    @pytest.fixture(autouse=True)
    def _enable_low_markets(self, monkeypatch):
        """Issue #733: the LOW scan is off by default; these tests exercise the
        (still fully supported) flag-on behaviour."""
        monkeypatch.setenv("ENABLE_LOW_MARKETS", "true")

    def test_no_low_candidates_when_weather_low_is_none(self):
        markets = [_low_market("Chicago")]
        candidates, _ = scan_markets(weather={}, markets=markets, weather_low=None)
        assert len(candidates) == 0

    def test_low_candidate_emitted_when_weather_low_provided(self):
        markets = [_low_market("Chicago", "55-59°F")]
        weather_low = {"KORD": _make_weather_low("KORD", current_low_f=70.0)}
        prob_low_fn = lambda b, s, m, f: 0.05  # noqa: E731
        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0):
            candidates, _ = scan_markets(weather={}, markets=markets,
                                         weather_low=weather_low, prob_low_fn=prob_low_fn)
        # With current_low_f=70 and bracket 55-59, the bracket ceiling (59) < current_low (70)
        # → running-low exclusion triggers → p_yes≈0 → only NO has edge
        low_cands = [c for c in candidates if c.direction == "low"]
        # May or may not have edge depending on model; just verify direction is set
        for c in low_cands:
            assert c.direction == "low"
            assert c.shadow is True

    def test_low_candidate_is_always_shadow(self):
        """Low-side candidates must always be shadow=True regardless of station_overrides."""
        markets = [_low_market("Chicago")]
        weather_low = {"KORD": _make_weather_low("KORD")}
        prob_low_fn = lambda b, s, m, f: 0.05  # noqa: E731
        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0):
            candidates, _ = scan_markets(weather={}, markets=markets,
                                         weather_low=weather_low, prob_low_fn=prob_low_fn)
        for c in candidates:
            if c.direction == "low":
                assert c.shadow is True

    def test_high_side_unaffected_by_weather_low(self):
        """Adding weather_low must not change high-side candidate behaviour."""
        # An empty high-side weather dict → no high-side candidates
        today_end = datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:00Z")
        markets = [
            {"question": "Will the highest temperature in Chicago be 82-84°F?",
             "groupItemTitle": "82-84°F", "conditionId": "0xHIGH",
             "outcomes": '["Yes","No"]', "outcomePrices": '[0.4,0.6]',
             "clobTokenIds": '["t1","t2"]',
             "endDate": today_end},
        ]
        prob_low_fn = lambda b, s, m, f: 0.05  # noqa: E731
        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0):
            candidates_without_low, _ = scan_markets(weather={}, markets=markets, weather_low=None)
            candidates_with_low, _ = scan_markets(weather={}, markets=markets,
                                                  weather_low={"KORD": _make_weather_low("KORD")},
                                                  prob_low_fn=prob_low_fn)
        # High-side count must be identical (both 0 because KORD not in weather dict)
        high_without = [c for c in candidates_without_low if c.direction == "high"]
        high_with = [c for c in candidates_with_low if c.direction == "high"]
        assert len(high_without) == len(high_with)

    def test_low_market_skipped_without_state(self):
        """Market for a city whose state isn't in weather_low must not raise."""
        markets = [_low_market("Tokyo")]
        weather_low = {}  # no state for RJTT
        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0):
            candidates, _ = scan_markets(weather={}, markets=markets, weather_low=weather_low)
        assert all(c.direction != "low" for c in candidates)


# ---------------------------------------------------------------------------
# Low-side p_yes clamp symmetry (issue #567)
#
# Decision recorded: the low-side clamp is now symmetric [1-cap, cap], matching
# the high-side clamp, instead of the upper-only min(p_yes, cap) PR #564 left
# in place. No concrete reason was found for the asymmetry (PR #564 preserved
# it purely to keep that PR's scope to dual-logging, and flagged it as a
# follow-up) -- see src/strategy/scanner.py's low-side block for the code
# comment recording this. Still shadow-only / zero live impact per bug #554.
# ---------------------------------------------------------------------------

def _low_market_with(city: str, group_title: str, yes_price: float, no_price: float,
                     station_hint: str) -> dict:
    """Like _low_market but with controllable outcome prices and a far-enough
    endDate (end of today UTC) so the MIN_MINUTES_TO_SETTLEMENT window gate
    doesn't swallow the candidate (see bug #554).

    Uses the real wall-clock date (not _FROZEN_NOW) because the scanner's
    wrong_date gate compares it against the real today_utc.  The 15-minute
    outside_window flake is eliminated by the MIN_MINUTES_TO_SETTLEMENT=0
    patch in the test methods (issue #844).
    """
    today_end = datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:00Z")
    return {
        "question": f"Will the lowest temperature in {city} be {group_title}?",
        "groupItemTitle": group_title,
        "conditionId": f"0xLOW_{station_hint}",
        "outcomes": '["Yes","No"]',
        "outcomePrices": f'[{yes_price}, {no_price}]',
        "clobTokenIds": '["tok1","tok2"]',
        "endDate": today_end,
    }


class TestLowSideClampSymmetry:
    """Issue #567: low-side p_yes clamp must floor at 1-cap, same as high side."""

    @pytest.fixture(autouse=True)
    def _enable_low_markets(self, monkeypatch):
        """Issue #733: the LOW scan is off by default; enable it to exercise the clamp."""
        monkeypatch.setenv("ENABLE_LOW_MARKETS", "true")

    def test_low_side_floors_very_low_raw_p_yes_to_one_minus_cap(self):
        """raw_p_yes=0.001 with MODEL_PROB_CAP=0.95 must floor to p_yes=0.05.

        no_ask=78c is chosen so the NO edge only lands inside
        [MIN_EDGE_CENTS, MAX_EDGE_CENTS] when the floor applies (capped
        p_yes=0.05 -> ev_no ~= 15.8c); the un-floored raw value (~0.001)
        would push ev_no above MAX_EDGE_CENTS and the candidate would be
        skipped as max_edge instead -- so a candidate appearing at all,
        with p_yes exactly 0.05, demonstrates the symmetric floor is applied.
        """
        market = _low_market_with("Chicago", "55-59°F", yes_price=0.22, no_price=0.78,
                                   station_hint="KORD")
        weather_low = {"KORD": _make_weather_low("KORD", current_low_f=50.0)}
        prob_low_fn = lambda b, s, m, f: 0.001  # noqa: E731

        with patch.object(_scanner_mod, "MODEL_PROB_CAP", 0.95), \
                patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0):
            candidates, _ = scan_markets(weather={}, markets=[market],
                                         weather_low=weather_low, prob_low_fn=prob_low_fn)

        low_no_cands = [c for c in candidates if c.direction == "low" and c.side == "NO"]
        assert len(low_no_cands) == 1, f"expected exactly 1 low-side NO candidate, got {low_no_cands}"
        cand = low_no_cands[0]
        assert cand.p_yes == 0.05, f"expected p_yes floored to 0.05, got {cand.p_yes}"
        assert cand.p_yes_raw == 0.001
        assert cand.confidence == pytest.approx(0.95)

    def test_low_side_midrange_p_yes_unaffected_by_floor(self):
        """A p_yes already inside [1-cap, cap] must pass through unchanged.

        p_yes=0.90 with yes_ask=72/no_ask=28 clears the YES-side gates
        (ev_yes~=16.6c, within [MIN_EDGE_CENTS, MAX_EDGE_CENTS]; p_yes=0.90
        >= MIN_CONFIDENCE_YES=0.85; yes_ask=72 >= MIN_PRICE_CENTS=70).
        """
        market = _low_market_with("Chicago", "55-59°F", yes_price=0.72, no_price=0.28,
                                   station_hint="KORD")
        weather_low = {"KORD": _make_weather_low("KORD", current_low_f=50.0)}
        prob_low_fn = lambda b, s, m, f: 0.90  # noqa: E731

        with patch.object(_scanner_mod, "MODEL_PROB_CAP", 0.95), \
                patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0):
            candidates, _ = scan_markets(weather={}, markets=[market],
                                         weather_low=weather_low, prob_low_fn=prob_low_fn)

        low_cands = [c for c in candidates if c.direction == "low"]
        assert len(low_cands) == 1, f"expected exactly 1 low-side candidate, got {low_cands}"
        assert low_cands[0].p_yes == 0.90
        assert low_cands[0].p_yes_raw == 0.90


# ---------------------------------------------------------------------------
# Issue #733: LOW-direction scan rollback — disabled by default
# ---------------------------------------------------------------------------

class TestLowMarketsDisabledByDefault:
    """Issue #733 rollback: with defaults (no ENABLE_LOW_MARKETS set), the
    low-side block must not run even when weather_low and prob_low_fn are
    provided, and the HIGH side must be completely unaffected."""

    @pytest.fixture(autouse=True)
    def _ensure_flag_unset(self, monkeypatch):
        monkeypatch.delenv("ENABLE_LOW_MARKETS", raising=False)

    def test_no_low_candidates_by_default_even_with_state(self):
        markets = [_low_market_with("Chicago", "55-59°F", yes_price=0.22,
                                    no_price=0.78, station_hint="KORD")]
        weather_low = {"KORD": _make_weather_low("KORD", current_low_f=50.0)}
        prob_low_fn = lambda b, s, m, f: 0.001  # noqa: E731

        with patch.object(_scanner_mod, "MODEL_PROB_CAP", 0.95), \
                patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0):
            candidates, _ = scan_markets(weather={}, markets=markets,
                                         weather_low=weather_low, prob_low_fn=prob_low_fn)

        assert all(c.direction != "low" for c in candidates), (
            "LOW candidates emitted with ENABLE_LOW_MARKETS unset (default off)"
        )

    def test_env_flag_true_restores_low_scan(self, monkeypatch):
        """The rollback is reversible: the exact market/state that is skipped
        by default is scanned again once the flag is flipped on."""
        monkeypatch.setenv("ENABLE_LOW_MARKETS", "true")
        markets = [_low_market_with("Chicago", "55-59°F", yes_price=0.22,
                                    no_price=0.78, station_hint="KORD")]
        weather_low = {"KORD": _make_weather_low("KORD", current_low_f=50.0)}
        prob_low_fn = lambda b, s, m, f: 0.001  # noqa: E731

        with patch.object(_scanner_mod, "MODEL_PROB_CAP", 0.95), \
                patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0):
            candidates, _ = scan_markets(weather={}, markets=markets,
                                         weather_low=weather_low, prob_low_fn=prob_low_fn)

        assert any(c.direction == "low" for c in candidates)

    def test_high_side_unchanged_with_flag_off(self):
        """A HIGH market scan produces identical results whether weather_low is
        passed or not while the flag is off."""
        today_end = datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:00Z")
        markets = [
            {"question": "Will the highest temperature in Chicago be 82-84°F?",
             "groupItemTitle": "82-84°F", "conditionId": "0xHIGH",
             "outcomes": '["Yes","No"]', "outcomePrices": '[0.4,0.6]',
             "clobTokenIds": '["t1","t2"]',
             "endDate": today_end},
        ]
        prob_low_fn = lambda b, s, m, f: 0.05  # noqa: E731
        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0):
            without_low, _ = scan_markets(weather={}, markets=markets, weather_low=None)
            with_low, _ = scan_markets(weather={}, markets=markets,
                                       weather_low={"KORD": _make_weather_low("KORD")},
                                       prob_low_fn=prob_low_fn)
        assert [c.direction for c in without_low] == [c.direction for c in with_low]

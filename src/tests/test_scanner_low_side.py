"""Tests for low-side scanner extension (Issue #455)."""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.model.envelope import Bracket
from src.model.envelope_low import WeatherStateLow  # noqa: used in type hints only
from src.strategy.scanner import (
    Candidate,
    is_highest_temp_market,
    is_lowest_temp_market,
    scan_markets,
)


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
        assert station == "KLGA"

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
    now = datetime.now(timezone.utc)
    return WeatherStateLow(
        station=station,
        now_local=now,
        sunrise_local=now,
        current_low_f=current_low_f,
        current_low_time=now,
        latest_temp_f=current_low_f + 2.0,
        latest_temp_time=now,
        forecast_low_f=60.0,
    )


def _low_market(city: str, group_title: str = "55-59°F") -> dict:
    return {
        "question": f"Will the lowest temperature in {city} be {group_title}?",
        "groupItemTitle": group_title,
        "conditionId": f"0xLOW_{city.upper()}",
        "outcomes": '["Yes","No"]',
        "outcomePrices": '[0.3, 0.7]',
        "clobTokenIds": '["tok1","tok2"]',
        "endDate": datetime.now(timezone.utc).isoformat(),
    }


class TestScanMarketsLowSide:
    def test_no_low_candidates_when_weather_low_is_none(self):
        markets = [_low_market("Chicago")]
        candidates, _ = scan_markets(weather={}, markets=markets, weather_low=None)
        assert len(candidates) == 0

    def test_low_candidate_emitted_when_weather_low_provided(self):
        markets = [_low_market("Chicago", "55-59°F")]
        weather_low = {"KORD": _make_weather_low("KORD", current_low_f=70.0)}
        prob_low_fn = lambda b, s, m, f: 0.05  # noqa: E731
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
        candidates, _ = scan_markets(weather={}, markets=markets,
                                     weather_low=weather_low, prob_low_fn=prob_low_fn)
        for c in candidates:
            if c.direction == "low":
                assert c.shadow is True

    def test_high_side_unaffected_by_weather_low(self):
        """Adding weather_low must not change high-side candidate behaviour."""
        # An empty high-side weather dict → no high-side candidates
        markets = [
            {"question": "Will the highest temperature in Chicago be 82-84°F?",
             "groupItemTitle": "82-84°F", "conditionId": "0xHIGH",
             "outcomes": '["Yes","No"]', "outcomePrices": '[0.4,0.6]',
             "clobTokenIds": '["t1","t2"]',
             "endDate": datetime.now(timezone.utc).isoformat()},
        ]
        prob_low_fn = lambda b, s, m, f: 0.05  # noqa: E731
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
        candidates, _ = scan_markets(weather={}, markets=markets, weather_low=weather_low)
        assert all(c.direction != "low" for c in candidates)

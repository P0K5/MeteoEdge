"""Unit tests for Polymarket bracket analysis (issue #512, B2).

Covers:
- test_bracket_analysis_normal: all fields present, edge = model_prob - polymarket_prob
- test_bracket_analysis_missing_polymarket: bracket with no Polymarket market
- test_bracket_analysis_missing_model: no ensemble data -> model_prob and edge are None
"""
from __future__ import annotations

import datetime
from unittest.mock import patch, MagicMock

import pytest

from src.model.bracket_analysis import (
    get_bracket_analysis,
    _model_prob_for_bracket,
    _market_yes_prob,
    _market_settlement_date,
)


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------

class TestModelProbForBracket:
    def test_returns_correct_fraction(self):
        # 5 members at 82, 5 at 83 out of 20 total -> 50%
        dist = {80: 5, 81: 5, 82: 5, 83: 5}
        result = _model_prob_for_bracket(dist, 82.0, 84.0)
        assert result == pytest.approx(50.0)

    def test_empty_distribution_returns_none(self):
        assert _model_prob_for_bracket({}, 80.0, 84.0) is None

    def test_zero_total_returns_none(self):
        assert _model_prob_for_bracket({80: 0}, 80.0, 84.0) is None

    def test_bracket_exclusive_high(self):
        # high boundary is exclusive: floor=84 should NOT count for [80, 84)
        dist = {80: 1, 84: 1}
        result = _model_prob_for_bracket(dist, 80.0, 84.0)
        assert result == pytest.approx(50.0)  # only the 80 bin counts

    def test_all_members_in_bracket(self):
        dist = {72: 10, 73: 10}
        result = _model_prob_for_bracket(dist, 72.0, 74.0)
        assert result == pytest.approx(100.0)

    def test_no_members_in_bracket_returns_zero(self):
        dist = {70: 10, 71: 10}
        result = _model_prob_for_bracket(dist, 80.0, 84.0)
        assert result == pytest.approx(0.0)


class TestMarketYesProb:
    def test_json_string_prices(self):
        market = {"outcomePrices": '["0.65", "0.35"]', "outcomes": '["Yes", "No"]'}
        assert _market_yes_prob(market) == pytest.approx(65.0)

    def test_list_prices(self):
        market = {"outcomePrices": [0.40, 0.60], "outcomes": ["Yes", "No"]}
        assert _market_yes_prob(market) == pytest.approx(40.0)

    def test_no_outcome_prices_returns_none(self):
        assert _market_yes_prob({}) is None

    def test_bad_prices_returns_none(self):
        market = {"outcomePrices": "not-json"}
        assert _market_yes_prob(market) is None


class TestMarketSettlementDate:
    def test_parses_enddate(self):
        market = {"endDate": "2026-06-30T18:00:00Z"}
        assert _market_settlement_date(market) == datetime.date(2026, 6, 30)

    def test_missing_date_returns_none(self):
        assert _market_settlement_date({}) is None


# ---------------------------------------------------------------------------
# Integration-level tests (all external calls mocked)
# ---------------------------------------------------------------------------

def _make_market(
    station_city: str,
    date_str: str,
    group_title: str,
    yes_price: str = "0.55",
) -> dict:
    """Build a minimal synthetic market dict."""
    return {
        "conditionId": f"0xABC-{group_title}",
        "question": f"Will the highest temperature in {station_city} be {group_title} on {date_str}?",
        "groupItemTitle": group_title,
        "endDate": f"{date_str}T18:00:00Z",
        "outcomePrices": f'["{yes_price}", "{1 - float(yes_price):.2f}"]',
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["tok-yes", "tok-no"]',
    }


_ENSEMBLE_NORMAL = {
    "ensemble_mean": 83.0,
    "bias_corrected": 83.5,
    "member_count": 20,
    "range": (80.0, 86.0),
    "distribution": {80: 2, 81: 3, 82: 5, 83: 5, 84: 3, 85: 2},
    "active_stack_models": ["gefs"],
}


class TestGetBracketAnalysisNormal:
    """Normal case: Polymarket has markets, ensemble has data."""

    def test_all_fields_present_and_edge_correct(self):
        date = datetime.date(2026, 6, 30)
        # Single market for bracket 82-84°F, yes_price=0.55 (55%)
        # Model: bins 82 + 83 = 5+5=10 out of 20 total = 50%
        # Edge = 50 - 55 = -5 pp
        markets = [
            _make_market("Chicago", "2026-06-30", "82-84°F", yes_price="0.55"),
        ]

        with (
            patch("src.model.bracket_analysis.get_weather_markets", return_value=markets),
            patch("src.model.bracket_analysis.get_ensemble_distribution",
                  return_value=_ENSEMBLE_NORMAL),
            patch("src.model.bracket_analysis.is_highest_temp_market",
                  return_value=(True, "KORD")),
        ):
            result = get_bracket_analysis("KORD", date)

        assert len(result) == 1
        row = result[0]
        assert row["bracket_low"] == 82.0
        assert row["bracket_high"] == 84.0
        assert row["polymarket_prob"] == pytest.approx(55.0)
        assert row["model_prob"] == pytest.approx(50.0)
        assert row["edge"] == pytest.approx(row["model_prob"] - row["polymarket_prob"])
        assert row["range"] == "82–84°F"

    def test_brackets_sorted_ascending_by_low(self):
        date = datetime.date(2026, 6, 30)
        markets = [
            _make_market("Chicago", "2026-06-30", "84-86°F", yes_price="0.30"),
            _make_market("Chicago", "2026-06-30", "80-82°F", yes_price="0.20"),
            _make_market("Chicago", "2026-06-30", "82-84°F", yes_price="0.50"),
        ]

        with (
            patch("src.model.bracket_analysis.get_weather_markets", return_value=markets),
            patch("src.model.bracket_analysis.get_ensemble_distribution",
                  return_value=_ENSEMBLE_NORMAL),
            patch("src.model.bracket_analysis.is_highest_temp_market",
                  return_value=(True, "KORD")),
        ):
            result = get_bracket_analysis("KORD", date)

        lows = [r["bracket_low"] for r in result]
        assert lows == sorted(lows)


class TestGetBracketAnalysisMissingPolymarket:
    """A model bracket with no corresponding Polymarket market.

    The acceptance criteria say: if Polymarket has no market for a bracket
    -> polymarket_prob=None, edge=None.

    Practically this manifests when the model predicts significant probability
    for a temperature range that Polymarket doesn't offer a market for.
    We surface this by including the bracket only when Polymarket has a market
    (we don't invent brackets the market doesn't offer), but we can test the
    missing-polymarket scenario by returning an empty markets list.
    """

    def test_empty_markets_returns_empty_list(self):
        """If Polymarket returns no matching markets, result is empty."""
        date = datetime.date(2026, 6, 30)
        with (
            patch("src.model.bracket_analysis.get_weather_markets", return_value=[]),
            patch("src.model.bracket_analysis.get_ensemble_distribution",
                  return_value=_ENSEMBLE_NORMAL),
        ):
            result = get_bracket_analysis("KORD", date)
        assert result == []

    def test_wrong_station_markets_filtered_out(self):
        """Markets for a different station must not appear in the result."""
        date = datetime.date(2026, 6, 30)
        markets = [
            _make_market("Miami", "2026-06-30", "88-90°F", yes_price="0.60"),
        ]

        def _is_highest(market):
            q = market.get("question", "").lower()
            if "miami" in q:
                return (True, "KMIA")
            return (False, None)

        with (
            patch("src.model.bracket_analysis.get_weather_markets", return_value=markets),
            patch("src.model.bracket_analysis.get_ensemble_distribution",
                  return_value=_ENSEMBLE_NORMAL),
            patch("src.model.bracket_analysis.is_highest_temp_market",
                  side_effect=_is_highest),
        ):
            result = get_bracket_analysis("KORD", date)  # asking for KORD
        assert result == []

    def test_market_for_wrong_date_filtered_out(self):
        """Market for a different date must not appear in the result."""
        date = datetime.date(2026, 6, 30)
        markets = [
            _make_market("Chicago", "2026-07-01", "82-84°F", yes_price="0.55"),
        ]
        with (
            patch("src.model.bracket_analysis.get_weather_markets", return_value=markets),
            patch("src.model.bracket_analysis.get_ensemble_distribution",
                  return_value=_ENSEMBLE_NORMAL),
            patch("src.model.bracket_analysis.is_highest_temp_market",
                  return_value=(True, "KORD")),
        ):
            result = get_bracket_analysis("KORD", date)
        assert result == []


class TestGetBracketAnalysisMissingModel:
    """No ensemble data -> model_prob and edge are None for all brackets."""

    def test_none_ensemble_gives_none_model_and_edge(self):
        date = datetime.date(2026, 6, 30)
        markets = [
            _make_market("Chicago", "2026-06-30", "82-84°F", yes_price="0.55"),
        ]
        with (
            patch("src.model.bracket_analysis.get_weather_markets", return_value=markets),
            patch("src.model.bracket_analysis.get_ensemble_distribution",
                  return_value=None),
            patch("src.model.bracket_analysis.is_highest_temp_market",
                  return_value=(True, "KORD")),
        ):
            result = get_bracket_analysis("KORD", date)

        assert len(result) == 1
        row = result[0]
        assert row["polymarket_prob"] == pytest.approx(55.0)
        assert row["model_prob"] is None
        assert row["edge"] is None

    def test_empty_distribution_gives_none_model_and_edge(self):
        """Ensemble returns but distribution is empty -> model_prob=None, edge=None."""
        date = datetime.date(2026, 6, 30)
        markets = [
            _make_market("Chicago", "2026-06-30", "82-84°F", yes_price="0.40"),
        ]
        empty_ensemble = {
            "ensemble_mean": None,
            "bias_corrected": None,
            "member_count": 0,
            "range": None,
            "distribution": {},
            "active_stack_models": [],
        }
        with (
            patch("src.model.bracket_analysis.get_weather_markets", return_value=markets),
            patch("src.model.bracket_analysis.get_ensemble_distribution",
                  return_value=empty_ensemble),
            patch("src.model.bracket_analysis.is_highest_temp_market",
                  return_value=(True, "KORD")),
        ):
            result = get_bracket_analysis("KORD", date)

        assert len(result) == 1
        assert result[0]["model_prob"] is None
        assert result[0]["edge"] is None

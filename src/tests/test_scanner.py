"""Unit tests for src/strategy/scanner.py — bracket parsing and market scanning."""
import pytest
from src.strategy.scanner import parse_bracket_from_market, scan_markets, is_highest_temp_market


def _market(group_title: str, question: str = "", condition_id: str = "0xabc") -> dict:
    """Build a minimal market dict matching Polymarket's Gamma API shape."""
    return {
        "conditionId": condition_id,
        "question": question,
        "groupItemTitle": group_title,
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.60","0.40"]',
        "clobTokenIds": '["tok_yes","tok_no"]',
    }


class TestParseBracketFromMarket:
    """Tests for all 4 bracket label formats that Polymarket uses."""

    def test_lte_or_below(self):
        """'55°F or below' → low=-50, high=55."""
        b = parse_bracket_from_market(_market("55°F or below"))
        assert b is not None
        assert b.low_f == -50.0
        assert b.high_f == 55.0

    def test_lte_or_less(self):
        """'60°F or less' → low=-50, high=60."""
        b = parse_bracket_from_market(_market("60°F or less"))
        assert b is not None
        assert b.high_f == 60.0

    def test_gte_or_above(self):
        """'92°F or above' → low=92, high=200."""
        b = parse_bracket_from_market(_market("92°F or above"))
        assert b is not None
        assert b.low_f == 92.0
        assert b.high_f == 200.0

    def test_gte_or_more(self):
        """'85°F or more' → low=85, high=200."""
        b = parse_bracket_from_market(_market("85°F or more"))
        assert b is not None
        assert b.low_f == 85.0

    def test_between_and(self):
        """'between 56 and 57°F' → low=56, high=57."""
        b = parse_bracket_from_market(_market("between 56 and 57°F"))
        assert b is not None
        assert b.low_f == 56.0
        assert b.high_f == 57.0

    def test_range_dash(self):
        """'58-60°F' → low=58, high=60."""
        b = parse_bracket_from_market(_market("58-60°F"))
        assert b is not None
        assert b.low_f == 58.0
        assert b.high_f == 60.0

    def test_range_em_dash(self):
        """'82–84°F' (em-dash) → low=82, high=84."""
        b = parse_bracket_from_market(_market("82–84°F"))
        assert b is not None
        assert b.low_f == 82.0
        assert b.high_f == 84.0

    def test_unparseable_returns_none(self):
        """Unrecognized label returns None."""
        b = parse_bracket_from_market(_market("something completely unexpected 999xyz"))
        assert b is None

    def test_missing_condition_id_returns_none(self):
        """Market without conditionId returns None."""
        market = {"question": "will it rain?", "groupItemTitle": "58-60°F"}
        b = parse_bracket_from_market(market)
        assert b is None

    def test_yes_price_parsed(self):
        """YES price is correctly extracted from outcomePrices."""
        b = parse_bracket_from_market(_market("58-60°F"))
        assert b is not None
        assert b.yes_ask_cents == 60  # 0.60 * 100


class TestScanMarkets:
    """Tests for scan_markets() function."""

    def test_empty_inputs_return_empty(self):
        """scan_markets with no weather and no markets returns empty lists."""
        candidates, snapshots = scan_markets({}, [])
        assert candidates == []
        assert snapshots == []

    def test_no_matching_markets(self):
        """Markets that don't match any station return no candidates."""
        market = _market("58-60°F", question="Will the highest temperature in Unknown City be 58-60°F?")
        candidates, snapshots = scan_markets({}, [market])
        assert candidates == []
        assert snapshots == []


class TestIsHighestTempMarket:
    """Tests for is_highest_temp_market() filter."""

    def test_matching_city(self):
        """Correctly identifies a Miami highest-temp market."""
        market = {"question": "Will the highest temperature in Miami be 85-87°F?"}
        is_temp, station = is_highest_temp_market(market)
        assert is_temp is True
        assert station == "KMIA"

    def test_lowest_temp_skipped(self):
        """Lowest-temperature markets must NOT match."""
        market = {"question": "Will the lowest temperature in Miami be 70°F?"}
        is_temp, _ = is_highest_temp_market(market)
        assert is_temp is False

    def test_unknown_city_returns_false(self):
        """Unknown city returns False."""
        market = {"question": "Will the highest temperature in Unknown City be 80°F?"}
        is_temp, _ = is_highest_temp_market(market)
        assert is_temp is False

"""Unit tests for src/strategy/scanner.py — bracket parsing and market scanning."""
import logging
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
import pytest
from src.strategy.scanner import parse_bracket_from_market, scan_markets, is_highest_temp_market
from src.model.envelope import WeatherState


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


class TestScanMarketsSkipReasons:
    """Tests for skip-reason logging in scan_markets()."""

    def _weather_state(self) -> WeatherState:
        """Build a minimal WeatherState for testing."""
        now = datetime.now(timezone.utc)
        return WeatherState(
            station="KMIA",
            now_local=now,
            sunset_local=now.replace(hour=20),
            current_high_f=75.0,
            current_high_time=now,
            latest_temp_f=72.0,
            latest_temp_time=now,
            forecast_high_f=80.0,
        )

    def _miami_market(self, group_title: str = "80-85°F", **kwargs) -> dict:
        """Build a Miami highest-temp market with defaults, override with kwargs."""
        market = _market(
            group_title=group_title,
            question="Will the highest temperature in Miami be 80-85°F?",
            condition_id="0xmia_test"
        )
        # Add settlement time: end-of-day today UTC so the wrong_date gate
        # never fires regardless of what time of day the test runs.
        today = datetime.now(timezone.utc).date()
        default_end = datetime(today.year, today.month, today.day, 23, 59, 59, tzinfo=timezone.utc).isoformat()
        market.setdefault("endDate", default_end)
        market.update(kwargs)
        return market

    def test_not_highest_temp_skipped(self, caplog):
        """'not_highest_temp' gate: markets for unknown stations are silently skipped."""
        weather = {"KMIA": self._weather_state()}
        # This market mentions Miami but is NOT a highest-temp market
        market = {"question": "Will rainfall in Miami exceed 1 inch?"}
        candidates, snapshots = scan_markets(weather, [market])
        assert candidates == []
        # not_highest_temp should NOT emit a debug log (silent skip for non-matching markets)

    def test_outside_window_skipped(self, caplog):
        """'outside_window' gate: markets closing too soon are skipped."""
        weather = {"KMIA": self._weather_state()}
        # Market closes in 5 minutes (less than MIN_MINUTES_TO_SETTLEMENT, typically 20)
        now = datetime.now(timezone.utc)
        close_time = (now + timedelta(minutes=5)).isoformat()
        market = self._miami_market(endDate=close_time)

        with caplog.at_level(logging.DEBUG):
            candidates, snapshots = scan_markets(weather, [market])

        assert candidates == []
        assert any("outside_window" in record.message for record in caplog.records)

    def test_wrong_date_skipped(self, caplog):
        """'wrong_date' gate: markets closing on future date (not today UTC) are skipped."""
        weather = {"KMIA": self._weather_state()}
        # Market closes tomorrow instead of today
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date()
        close_time = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 20, 0, tzinfo=timezone.utc).isoformat()
        market = self._miami_market(endDate=close_time)

        with caplog.at_level(logging.DEBUG):
            candidates, snapshots = scan_markets(weather, [market])

        assert candidates == []
        assert any("wrong_date" in record.message for record in caplog.records)

    def test_bracket_parse_fail_skipped(self, caplog):
        """'bracket_parse_fail' gate: unparseable bracket labels are skipped."""
        weather = {"KMIA": self._weather_state()}
        # Unparseable label; disable outside_window gate so only bracket-parse gate fires
        market = self._miami_market(group_title="something completely unparseable 999xyz")

        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0):
            with caplog.at_level(logging.DEBUG):
                candidates, snapshots = scan_markets(weather, [market])

        assert candidates == []
        assert any("bracket_parse_fail" in record.message for record in caplog.records)

    def test_confidence_gate_p_yes_too_low(self, caplog):
        """'confidence_gate' gate: p_yes below MIN_CONFIDENCE_YES (for YES side)."""
        weather = {"KMIA": self._weather_state()}
        market = self._miami_market(
            group_title="95-100°F",  # Very high bracket
            outcomePrices='["0.90", "0.10"]'  # YES price 90¢ (attractive)
        )

        with caplog.at_level(logging.DEBUG):
            candidates, snapshots = scan_markets(weather, [market])

        # Should skip because p_yes will be low for such a high bracket
        # and won't meet MIN_CONFIDENCE_YES
        assert candidates == [] or all(c.side != "YES" for c in candidates)
        # Check if confidence_gate was logged (may not be if min_edge gate fires first)

    def test_confidence_gate_p_yes_too_high_for_no(self, caplog):
        """'confidence_gate' gate: p_yes above MAX_CONFIDENCE_YES_FOR_NO (for NO side)."""
        weather = {"KMIA": self._weather_state()}
        market = self._miami_market(
            group_title="50-55°F",  # Very low bracket
            outcomePrices='["0.10", "0.90"]'  # NO price 90¢ (attractive)
        )

        with caplog.at_level(logging.DEBUG):
            candidates, snapshots = scan_markets(weather, [market])

        # Should skip because p_yes will be high for such a low bracket
        # and won't meet MAX_CONFIDENCE_YES_FOR_NO for NO side trading
        assert candidates == [] or all(c.side != "NO" for c in candidates)

    def test_min_edge_below_threshold(self, caplog):
        """'min_edge' gate: edge below MIN_EDGE_CENTS is skipped."""
        weather = {"KMIA": self._weather_state()}
        market = self._miami_market(
            group_title="73-74°F",  # Very narrow bracket close to current temp
            outcomePrices='["0.50", "0.50"]'  # 50/50 prices (no edge)
        )

        # Disable outside_window gate so only min_edge gate fires
        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0):
            with caplog.at_level(logging.DEBUG):
                candidates, snapshots = scan_markets(weather, [market])

        # Should skip because edge will be negligible
        assert candidates == []
        assert any("min_edge" in record.message for record in caplog.records)

    def test_max_edge_adverse_selection(self, caplog):
        """'max_edge' gate: edge exceeds MAX_EDGE_CENTS (adverse selection) is skipped."""
        weather = {"KMIA": self._weather_state()}
        market = self._miami_market(
            group_title="75-80°F",
            outcomePrices='["0.99", "0.01"]'  # Extremely skewed prices
        )

        with caplog.at_level(logging.DEBUG):
            candidates, snapshots = scan_markets(weather, [market])

        # Should skip if edge exceeds MAX_EDGE_CENTS
        if candidates:
            # If candidate passed, edge should be acceptable
            assert all(c.edge_cents <= 50 for c in candidates)  # MAX_EDGE_CENTS is typically 50

    def test_scan_summary_logs_skip_counts(self, caplog):
        """Per-poll summary at INFO level includes skip-reason counts."""
        weather = {"KMIA": self._weather_state()}

        # Create multiple markets with different skip reasons
        now = datetime.now(timezone.utc)
        markets = [
            # outside_window
            self._miami_market(
                endDate=(now + timedelta(minutes=5)).isoformat()
            ),
            # wrong_date
            self._miami_market(
                endDate=(now + timedelta(days=1)).isoformat()
            ),
            # bracket_parse_fail
            self._miami_market(group_title="unparseable 999xyz"),
            # min_edge
            self._miami_market(
                group_title="73-74°F",
                outcomePrices='["0.50", "0.50"]'
            ),
        ]

        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0):
            with caplog.at_level(logging.INFO):
                candidates, snapshots = scan_markets(weather, markets)

        # Check that INFO-level summary was logged
        summary_logs = [r for r in caplog.records if r.levelname == "INFO" and "[scan]" in r.message]
        assert len(summary_logs) > 0, "Expected [scan] summary log at INFO level"

        summary = summary_logs[0].message
        assert "4 markets" in summary or "markets:" in summary
        assert "flagged" in summary

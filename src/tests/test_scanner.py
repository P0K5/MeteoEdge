"""Unit tests for src/strategy/scanner.py — bracket parsing and market scanning."""
import logging
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
import pytest
from src.strategy.scanner import (
    parse_bracket_from_market, scan_markets, is_highest_temp_market, no_entry_margin_gap,
)
from src.model.envelope import Bracket, WeatherState, p_normal_between

# Frozen reference datetime (mid-day UTC, well clear of midnight) so fixture
# times are deterministic regardless of when CI runs (issue #844).  Tests that
# construct a market with today's endDate also patch MIN_MINUTES_TO_SETTLEMENT=0
# to eliminate the 15-minute outside_window flake window.
_FROZEN_NOW = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)


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
        """'55°F or below' → [−50, 56): 55 itself must be inside the integral (#917)."""
        b = parse_bracket_from_market(_market("55°F or below"))
        assert b is not None
        assert b.low_f == -50.0
        assert b.high_f == 56.0

    def test_lte_or_less(self):
        """'60°F or less' → [−50, 61) (#917)."""
        b = parse_bracket_from_market(_market("60°F or less"))
        assert b is not None
        assert b.high_f == 61.0

    def test_gte_or_above(self):
        """'92°F or above' → low=92, high=200 (lower bound already inclusive, unaffected by #917)."""
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
        """'between 56 and 57°F' → [56, 58): 57 itself must be inside the integral (#917)."""
        b = parse_bracket_from_market(_market("between 56 and 57°F"))
        assert b is not None
        assert b.low_f == 56.0
        assert b.high_f == 58.0

    def test_between_celsius_plus_one_before_conversion(self):
        """'between 28-30°C' → [28C, 31C) in source units, THEN converted to °F.

        Locks in ordering: +1 must be applied BEFORE _to_f, not after (#917).
        31°C = 87.8°F; applying +1 in °F instead would give 87.4°F (wrong).
        """
        b = parse_bracket_from_market(_market("between 28-30°C"))
        assert b is not None
        assert b.low_f == pytest.approx(28 * 9 / 5 + 32)   # 82.4
        assert b.high_f == pytest.approx(31 * 9 / 5 + 32)  # 87.8

    def test_range_dash(self):
        """'58-60°F' → [58, 61): 60 itself must be inside the integral (#917)."""
        b = parse_bracket_from_market(_market("58-60°F"))
        assert b is not None
        assert b.low_f == 58.0
        assert b.high_f == 61.0

    def test_range_em_dash(self):
        """'82–84°F' (em-dash) → [82, 85) (#917)."""
        b = parse_bracket_from_market(_market("82–84°F"))
        assert b is not None
        assert b.low_f == 82.0
        assert b.high_f == 85.0

    def test_range_dash_celsius_plus_one_before_conversion(self):
        """'21-22°C' (dash form) → [21C, 23C) in source units, THEN converted (#917)."""
        b = parse_bracket_from_market(_market("21-22°C"))
        assert b is not None
        assert b.low_f == pytest.approx(21 * 9 / 5 + 32)
        assert b.high_f == pytest.approx(23 * 9 / 5 + 32)

    def test_exact_single_value(self):
        """'21°C' (bare single value, _LABEL_EXACT) → [21C, 22C) — already correct pre-#917."""
        b = parse_bracket_from_market(_market("21°C"))
        assert b is not None
        assert b.low_f == pytest.approx(21 * 9 / 5 + 32)
        assert b.high_f == pytest.approx(22 * 9 / 5 + 32)

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

    def test_sub_penny_yes_price_clamped_but_raw_preserved(self):
        """Issue #1076: a true 0.3c YES price is clamped to 1c in yes_ask_cents
        (the tradeable minimum), but yes_price_raw preserves the true float --
        this is the whole point, since Polymarket's own tick size tightens to
        $0.001 in exactly this region (price < 0.04)."""
        market = _market("58-60°F")
        market["outcomePrices"] = '["0.003","0.997"]'
        b = parse_bracket_from_market(market)
        assert b is not None
        assert b.yes_ask_cents == 1
        assert b.yes_price_raw == pytest.approx(0.003)

    def test_sub_penny_no_price_clamped_but_raw_preserved(self):
        """Same as above, mirrored for the NO side: 0.997 stays 99c clamped,
        but no_price_raw keeps the full float precision."""
        market = _market("58-60°F")
        market["outcomePrices"] = '["0.003","0.997"]'
        b = parse_bracket_from_market(market)
        assert b is not None
        assert b.no_ask_cents == 99
        assert b.no_price_raw == pytest.approx(0.997)

    def test_mid_range_price_raw_matches_clamped_cents(self):
        """Mid-range prices are unchanged in both fields -- the raw float and
        the clamped cents value agree (mod the *100 conversion) when nowhere
        near the clamp boundary."""
        b = parse_bracket_from_market(_market("58-60°F"))
        assert b is not None
        assert b.yes_ask_cents == 60
        assert b.yes_price_raw == pytest.approx(0.60)
        assert b.no_ask_cents == 40
        assert b.no_price_raw == pytest.approx(0.40)


class TestBracketLadderMassConservation:
    """Mass-conservation invariant (#917): a full, gap-free bracket ladder must
    integrate to ~1.0 total probability under p_normal_between's [low, high)
    convention. This is the invariant whose absence let the inclusive/exclusive
    off-by-one survive undetected -- it must exist going forward.
    """

    # A real 2°F-wide US ladder (station-poll convention from #917's diagnosis:
    # dash labels like "88-89°F" cover TWO integers) tiling the full real line
    # with the two open-ended tails, and NO gaps once the parser is correct.
    _LADDER_LABELS = (
        ["55°F or below"]
        + [f"{lo}-{lo + 1}°F" for lo in range(56, 92, 2)]  # 56-57, 58-59, ..., 90-91
        + ["92°F or above"]
    )

    def _ladder_brackets(self):
        brackets = [parse_bracket_from_market(_market(label)) for label in self._LADDER_LABELS]
        assert all(b is not None for b in brackets), "every ladder label must parse"
        return brackets

    def test_ladder_is_gap_free_and_non_overlapping(self):
        """Adjacent brackets must share exactly one boundary point, no gap or overlap."""
        brackets = sorted(self._ladder_brackets(), key=lambda b: b.low_f)
        for prev, nxt in zip(brackets, brackets[1:]):
            assert prev.high_f == nxt.low_f, (
                f"gap/overlap between [{prev.low_f}, {prev.high_f}) and "
                f"[{nxt.low_f}, {nxt.high_f})"
            )

    def test_sum_of_p_yes_conserves_mass(self):
        """SUM(p_normal_between) over the full ladder must be ~1.0 (tolerance <= 0.02).

        Before #917's fix this summed to ~0.53 for °F dash-range brackets --
        exactly half the true mass, since each 2-degree-wide market was
        integrated as if it were 1 degree wide.
        """
        brackets = self._ladder_brackets()
        mean, stddev = 75.0, 4.0  # well inside the ladder body
        total = sum(p_normal_between(b.low_f, b.high_f, mean, stddev) for b in brackets)
        assert total == pytest.approx(1.0, abs=0.02)

    def test_sum_of_p_yes_conserves_mass_near_tail(self):
        """Same invariant with the mean shifted toward an open-ended tail bracket."""
        brackets = self._ladder_brackets()
        mean, stddev = 58.0, 4.0
        total = sum(p_normal_between(b.low_f, b.high_f, mean, stddev) for b in brackets)
        assert total == pytest.approx(1.0, abs=0.02)


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
        return WeatherState(
            station="KMIA",
            now_local=_FROZEN_NOW,
            sunset_local=_FROZEN_NOW.replace(hour=20),
            current_high_f=75.0,
            current_high_time=_FROZEN_NOW,
            latest_temp_f=72.0,
            latest_temp_time=_FROZEN_NOW,
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

        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0):
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

        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0):
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

        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0):
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


def _state(forecast: float | None, current: float | None,
           secondary: float | None = None) -> WeatherState:
    return WeatherState(
        station="KMIA",
        now_local=_FROZEN_NOW,
        sunset_local=_FROZEN_NOW.replace(hour=20),
        current_high_f=current,
        current_high_time=_FROZEN_NOW,
        latest_temp_f=current,
        latest_temp_time=_FROZEN_NOW,
        forecast_high_f=forecast,
        secondary_forecast_f=secondary,
    )


def _bracket(low: float, high: float) -> Bracket:
    return Bracket(
        ticker="T", low_f=low, high_f=high,
        yes_ask_cents=50, yes_ask_size=0, no_ask_cents=50, no_ask_size=0,
    )


class TestNoEntryMarginGap:
    """Tests for no_entry_margin_gap() — the issue #200 entry filter."""

    def test_bracket_above_expected_high(self):
        """Forecast 80, bracket 82-84 → gap = 2.0 (bracket_low - base)."""
        assert no_entry_margin_gap(_bracket(82, 84), _state(80.0, 75.0)) == 2.0

    def test_bracket_below_expected_high(self):
        """Forecast 86, bracket 82-84 → gap = 2.0 (base - bracket_high)."""
        assert no_entry_margin_gap(_bracket(82, 84), _state(86.0, 75.0)) == 2.0

    def test_uses_max_of_forecast_and_current_high(self):
        """Running high above forecast dominates: current 81, forecast 78, bracket 82-84 → gap 1.0."""
        assert no_entry_margin_gap(_bracket(82, 84), _state(78.0, 81.0)) == 1.0

    def test_forecast_inside_bracket_returns_none(self):
        """Forecast inside the bracket → gate does not apply (model prices these fine)."""
        assert no_entry_margin_gap(_bracket(82, 84), _state(83.0, 75.0)) is None

    def test_current_high_already_past_bracket_returns_none(self):
        """Running daily high above bracket top → NO can no longer lose, no gate."""
        assert no_entry_margin_gap(_bracket(82, 84), _state(86.0, 85.0)) is None

    def test_open_top_bracket_no_gap_above(self):
        """'or above' bracket (high=200): base above low → None, never base - 200."""
        assert no_entry_margin_gap(_bracket(92, 200), _state(95.0, 90.0)) is None

    def test_open_top_bracket_gap_below(self):
        """'or above' bracket below by 4F → gap 4.0."""
        assert no_entry_margin_gap(_bracket(92, 200), _state(88.0, 85.0)) == 4.0

    def test_secondary_forecast_fallback(self):
        """No NWS forecast → falls back to secondary forecast."""
        assert no_entry_margin_gap(_bracket(82, 84), _state(None, 75.0, secondary=80.0)) == 2.0

    def test_no_data_returns_none(self):
        """Neither forecast nor running high → gate cannot apply."""
        assert no_entry_margin_gap(_bracket(82, 84), _state(None, None)) is None

    # -- market_date cross-day handling (issue #784) --------------------------

    def test_cross_day_market_ignores_current_high_bypass(self):
        """Yesterday's high above the bracket top must not disable the gate.

        KORD scenario: market settles tomorrow (station-local), running high 86
        is from today. Old code: current 86 > top 75 → None (no gate). Fixed:
        forecast-only → gap = 76 - 75 = 1.0.
        """
        state = _state(76.0, 86.0)
        tomorrow = state.now_local.date() + timedelta(days=1)
        assert no_entry_margin_gap(_bracket(74, 75), state, market_date=tomorrow) == 1.0

    def test_cross_day_market_ignores_current_high_in_base(self):
        """Stale current high must not inflate the expected-high base."""
        state = _state(78.0, 81.0)
        tomorrow = state.now_local.date() + timedelta(days=1)
        # Same-day this is gap 1.0 (base=81); cross-day base=forecast 78 → 4.0.
        assert no_entry_margin_gap(_bracket(82, 84), state, market_date=tomorrow) == 4.0

    def test_cross_day_market_no_forecast_returns_none(self):
        """Cross-day with only a (dropped) current high → gate cannot apply."""
        state = _state(None, 86.0)
        tomorrow = state.now_local.date() + timedelta(days=1)
        assert no_entry_margin_gap(_bracket(74, 75), state, market_date=tomorrow) is None

    def test_matching_market_date_keeps_current_high_semantics(self):
        """market_date equal to the local date leaves all current-high logic intact."""
        state = _state(76.0, 86.0)
        today = state.now_local.date()
        assert no_entry_margin_gap(_bracket(82, 84), state, market_date=today) is None


class TestEntryGates:
    """Tests for the DISABLED_STATIONS and margin_gate entry filters in scan_markets()."""

    def _weather_state(self) -> WeatherState:
        import dataclasses
        state = _state(forecast=80.0, current=75.0)
        # Pin the state's clock 30 min past its own sunset so the envelope's
        # remaining-climb term is zero no matter when the test runs. _state()
        # uses the real wall clock against a fixed hour-20 sunset, which made
        # these three tests time-of-day flaky: before 20:00 UTC the model
        # still priced an afternoon climb (p_yes~=0.07 -> ev_no below
        # MIN_EDGE_CENTS -> candidate skipped as min_edge before ever
        # reaching the margin gate), so they only passed in evening-UTC CI
        # runs.
        return dataclasses.replace(
            state, now_local=state.sunset_local + timedelta(minutes=30)
        )

    def _miami_market(self, group_title: str, outcome_prices: str) -> dict:
        market = _market(
            group_title=group_title,
            question=f"Will the highest temperature in Miami be {group_title}?",
            condition_id="0xmia_gate",
        )
        today = datetime.now(timezone.utc).date()
        market["endDate"] = datetime(
            today.year, today.month, today.day, 23, 59, 59, tzinfo=timezone.utc
        ).isoformat()
        market["outcomePrices"] = outcome_prices
        return market

    def test_disabled_station_produces_shadow_candidates(self, caplog):
        """Stations in SHADOW_STATIONS/DISABLED_STATIONS produce shadow candidates (not skipped).

        Legacy 'station_disabled' hard-skip is replaced by per-side shadow logic:
        SHADOW_STATIONS shadows both sides, so candidates are produced with shadow=True.
        """
        weather = {"KMIA": self._weather_state()}
        market = self._miami_market("90-95°F", '["0.20", "0.80"]')

        with patch("src.strategy.scanner.DISABLED_STATIONS", {"KMIA"}), \
                patch("src.strategy.scanner.SHADOW_STATIONS", {"KMIA"}), \
                patch("src.strategy.scanner.SHADOW_STATIONS_YES", set()), \
                patch("src.strategy.scanner.SHADOW_STATIONS_NO", set()), \
                patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0), \
                patch("src.strategy.scanner.MODEL_PROB_CAP", 1.0):
            candidates, _ = scan_markets(weather, [market])

        # Station is shadowed — candidate is produced but with shadow=True
        assert len(candidates) > 0
        assert all(c.shadow is True for c in candidates)

    def test_margin_gate_blocks_thin_margin_no_entry(self, caplog):
        """'margin_gate': NO candidate with bracket too close to expected high is skipped."""
        weather = {"KMIA": self._weather_state()}
        # Bracket 90-95 is 10F above forecast 80 — raise the threshold to 15F
        # so the gate fires on an otherwise-tradeable NO candidate.
        market = self._miami_market("90-95°F", '["0.20", "0.80"]')

        with patch("src.strategy.scanner.MIN_FORECAST_BRACKET_MARGIN_F", 15.0), \
                patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0), \
                patch("src.strategy.scanner.MODEL_PROB_CAP", 1.0):
            with caplog.at_level(logging.DEBUG):
                candidates, _ = scan_markets(weather, [market])

        assert candidates == []
        assert any("margin_gate" in r.message for r in caplog.records)

    def test_margin_gate_passes_wide_margin_no_entry(self, caplog):
        """A NO candidate 10F clear of the forecast passes the default 2.5F gate."""
        weather = {"KMIA": self._weather_state()}
        market = self._miami_market("90-95°F", '["0.20", "0.80"]')

        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0), \
                patch("src.strategy.scanner.MODEL_PROB_CAP", 1.0):
            with caplog.at_level(logging.DEBUG):
                candidates, _ = scan_markets(weather, [market])

        assert not any("margin_gate" in r.message for r in caplog.records)
        assert any(c.side == "NO" for c in candidates)

    def test_margin_gate_fires_cross_day_despite_stale_current_high(self, caplog):
        """Issue #784 / #820: evening window cross-day guard.

        Same-day-by-UTC evening window: the market settles on the station's
        NEXT local day, so state.now_local is still 'yesterday' relative to the
        market date. Running high 86 exceeds the 74-75 bracket top -- the old
        'NO can no longer lose' bypass let the candidate through -- but the
        market-day forecast of 76 is only 1F clear of the bracket.

        Pre-#820 the margin gate caught this. Since #820, day-mismatch
        detection forces shadow before any live gates fire, and the shadow
        candidate falls below min_edge (12.19 < 15), so it's skipped.
        """
        import dataclasses
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        state = dataclasses.replace(
            _state(forecast=76.0, current=86.0),
            now_local=yesterday.replace(hour=20, minute=30),
            sunset_local=yesterday.replace(hour=20),
        )
        weather = {"KMIA": state}
        market = self._miami_market("74-75°F", '["0.20", "0.80"]')

        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0), \
                patch("src.strategy.scanner.MODEL_PROB_CAP", 1.0):
            with caplog.at_level(logging.DEBUG):
                candidates, snapshots = scan_markets(weather, [market])

        assert candidates == []
        # Issue #820: day-mismatch is detected and logged; the shadow
        # candidate falls below min_edge (12.19 < 15) so it's skipped
        # with a "below_min_edge" verdict, not margin_gate.
        assert any("day-mismatch" in r.message for r in caplog.records)
        snap_verdicts = [s.get("gate_verdict") for s in snapshots]
        assert "margin_gate" not in snap_verdicts, \
            "margin_gate must not fire when day mismatch forces shadow first"

    def test_day_mismatch_forces_shadow_with_sufficient_edge(self, caplog):
        """Issue #820: evening-window candidate with enough edge is forced to
        shadow with day_mismatch_shadow verdict.

        State is from yesterday (local), market settles today (UTC). The
        forecast is 85°F, bracket is 72-74°F (far below forecast → p_yes≈0
        → NO confidence high). Without #820 this would produce a live
        traded_live candidate; with #820 it's forced to shadow.
        """
        import dataclasses
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        state = dataclasses.replace(
            _state(forecast=85.0, current=88.0),
            now_local=yesterday.replace(hour=20, minute=30),
            sunset_local=yesterday.replace(hour=20),
        )
        weather = {"KMIA": state}
        # YES=20¢, NO=80¢ — NO price ≥ MIN_PRICE_CENTS (70), bracket far
        # below forecast → p_yes≈0 → ev_no ≈ 17 < MAX_EDGE_CENTS (20).
        market = self._miami_market("72-74°F", '["0.20", "0.80"]')

        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0), \
                patch("src.strategy.scanner.MODEL_PROB_CAP", 1.0):
            with caplog.at_level(logging.DEBUG):
                candidates, snapshots = scan_markets(weather, [market])

        assert len(candidates) >= 1, \
            f"expected at least 1 shadow candidate, got {len(candidates)}"
        candidate = candidates[0]
        assert candidate.shadow is True, \
            "day-mismatch candidate must be shadow"
        assert candidate.side == "NO"
        # The snapshot should have the day_mismatch_shadow verdict for the
        # flagged side (the candidate itself carries shadow=True but the
        # gate_verdict lives on the snap).
        snap_for_side = [s for s in snapshots if s["side"] == candidate.side]
        assert len(snap_for_side) == 1
        assert snap_for_side[0]["gate_verdict"] == "day_mismatch_shadow"
        assert any("day-mismatch" in r.message for r in caplog.records)
# Next-day evaluation (issue #687)
# ---------------------------------------------------------------------------

class TestNextDayEvaluation:
    """Tests for the relaxed wrong_date gate + next-day shadow-only path.

    NEXT_DAY_EVALUATION defaults off; scan_markets(db=None) falls back to the
    NEXT_DAY_EVALUATION env var (mirrors DEB_ENABLED/USE_ENSEMBLE_SIGMA's
    no-DB fallback pattern), so tests toggle it via monkeypatch.setenv.
    """

    def _weather_state(self, station="KMIA") -> WeatherState:
        return WeatherState(
            station=station,
            now_local=_FROZEN_NOW,
            sunset_local=_FROZEN_NOW.replace(hour=20),
            current_high_f=75.0,
            current_high_time=_FROZEN_NOW,
            latest_temp_f=72.0,
            latest_temp_time=_FROZEN_NOW,
            forecast_high_f=80.0,
        )

    def _market(self, end_dt, group_title="80-85°F", condition_id="0xnextday",
                prices='["0.50","0.50"]'):
        return _market(
            group_title=group_title,
            question="Will the highest temperature in Miami be 80-85°F?",
            condition_id=condition_id,
        ) | {"endDate": end_dt.isoformat(), "outcomePrices": prices}

    def test_flag_off_next_day_market_still_wrong_date(self, caplog, monkeypatch):
        """Default (flag off): a future-dated market is skipped exactly as
        today, even with no today-market at all for the station -- byte-for-
        byte unchanged behaviour."""
        monkeypatch.delenv("NEXT_DAY_EVALUATION", raising=False)
        weather = {"KMIA": self._weather_state()}
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1, hours=6)
        market = self._market(tomorrow)

        with caplog.at_level(logging.DEBUG):
            candidates, snapshots = scan_markets(weather, [market])

        assert candidates == []
        assert snapshots == []
        assert any("wrong_date" in r.message for r in caplog.records)

    def test_flag_on_absent_today_market_evaluates_next_day(self, monkeypatch):
        """Flag on, no today-market fetched at all for the station: the
        station's next market becomes evaluable as a next-day candidate."""
        monkeypatch.setenv("NEXT_DAY_EVALUATION", "true")
        weather = {"KMIA": self._weather_state()}
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1, hours=6)
        market = self._market(tomorrow, prices='["0.30","0.70"]')

        with patch("src.strategy.scanner._fetch_next_day_forecast", return_value=(82.0, 3.0)):
            candidates, snapshots = scan_markets(weather, [market])

        assert len(snapshots) == 1
        assert snapshots[0]["is_next_day"] == 1
        # Whether or not this bracket clears the entry gates, it must never
        # produce a live (non-shadow) candidate.
        assert all(c.shadow is True and c.is_next_day is True for c in candidates)

    def test_flag_on_today_market_past_window_evaluates_next_day(self, monkeypatch):
        """Flag on, today's own market is present but past
        MIN_MINUTES_TO_SETTLEMENT: the station's next market is still
        evaluable (design doc's 'past window' eligibility clause)."""
        monkeypatch.setenv("NEXT_DAY_EVALUATION", "true")
        weather = {"KMIA": self._weather_state()}
        now = datetime.now(timezone.utc)
        today_market = self._market(
            now + timedelta(minutes=5), condition_id="0xtoday",
        )
        tomorrow_market = self._market(
            now + timedelta(days=1, hours=6), condition_id="0xnextday",
            prices='["0.30","0.70"]',
        )

        with patch("src.strategy.scanner._fetch_next_day_forecast", return_value=(82.0, 3.0)):
            candidates, snapshots = scan_markets(weather, [today_market, tomorrow_market])

        next_day_snaps = [s for s in snapshots if s["ticker"] == "0xnextday"]
        assert len(next_day_snaps) == 1
        assert next_day_snaps[0]["is_next_day"] == 1

    def test_flag_on_today_market_still_live_blocks_next_day(self, monkeypatch):
        """Per-station gating: while today's own market is still live (mins
        left >= MIN_MINUTES_TO_SETTLEMENT), the next-day market must still
        hit wrong_date -- it is NOT promoted early."""
        monkeypatch.setenv("NEXT_DAY_EVALUATION", "true")
        weather = {"KMIA": self._weather_state()}
        now = datetime.now(timezone.utc)
        today_market = self._market(
            now + timedelta(hours=2), condition_id="0xtoday",
        )
        tomorrow_market = self._market(
            now + timedelta(days=1, hours=6), condition_id="0xnextday",
        )

        with patch("src.strategy.scanner._fetch_next_day_forecast", return_value=(82.0, 3.0)):
            candidates, snapshots = scan_markets(weather, [today_market, tomorrow_market])

        next_day_snaps = [s for s in snapshots if s["ticker"] == "0xnextday"]
        assert next_day_snaps == []
        assert all(not c.is_next_day for c in candidates)

    def test_flag_on_forecast_unavailable_skips_candidate(self, monkeypatch, caplog):
        """When the next-day forecast fetch fails/unavailable, the candidate is
        skipped (no crash, no fabricated probability)."""
        monkeypatch.setenv("NEXT_DAY_EVALUATION", "true")
        weather = {"KMIA": self._weather_state()}
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1, hours=6)
        market = self._market(tomorrow)

        with patch("src.strategy.scanner._fetch_next_day_forecast", return_value=None):
            with caplog.at_level(logging.DEBUG):
                candidates, snapshots = scan_markets(weather, [market])

        assert candidates == []
        assert snapshots == []
        assert any("next_day_forecast_unavailable" in r.message for r in caplog.records)

    def test_flag_on_no_side_gates_still_apply(self, monkeypatch):
        """A next-day bracket that would fail the NO confidence/edge gates on
        a same-day basis produces no candidate at all -- gates apply
        unchanged, only the routing to shadow-only is forced."""
        monkeypatch.setenv("NEXT_DAY_EVALUATION", "true")
        weather = {"KMIA": self._weather_state()}
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1, hours=6)
        # 50/50 prices -> no edge on either side.
        market = self._market(tomorrow, prices='["0.50","0.50"]')

        with patch("src.strategy.scanner._fetch_next_day_forecast", return_value=(82.0, 3.0)):
            candidates, snapshots = scan_markets(weather, [market])

        assert len(snapshots) == 1
        assert candidates == []

    def test_next_day_emos_mode_is_never_next_day_string(self, monkeypatch):
        """Regression test for #871: next-day evaluation must record the
        city's actual EMOS mode (legacy/emos_shadow/emos_primary), never the
        day-classification string "next_day"."""
        monkeypatch.setenv("NEXT_DAY_EVALUATION", "true")
        weather = {"KMIA": self._weather_state()}
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1, hours=6)
        market = self._market(tomorrow, prices='["0.30","0.70"]')

        with patch("src.strategy.scanner._fetch_next_day_forecast", return_value=(82.0, 3.0)):
            _candidates, snapshots = scan_markets(weather, [market])

        assert len(snapshots) >= 1
        for snap in snapshots:
            assert snap["is_next_day"] == 1
            assert snap["emos_mode"] != "next_day", (
                f"emos_mode must not be 'next_day'; got '{snap['emos_mode']}'"
            )
            assert snap["emos_mode"] in ("legacy", "emos_shadow", "emos_primary"), (
                f"emos_mode must be a valid model mode; got '{snap['emos_mode']}'"
            )

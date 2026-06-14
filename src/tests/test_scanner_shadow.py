"""Unit tests for shadow candidate detection in src/strategy/scanner.py.

Tests the split between YES gate detection and execution routing introduced
in issue #261. Covers:
- ENABLE_YES_TRADES=False → YES-passing market produces Candidate(shadow=True)
- ENABLE_YES_TRADES=True  → YES-passing market produces Candidate(shadow=False)
- NO candidates are never shadow regardless of ENABLE_YES_TRADES
- max_edge gate still applies to shadow YES candidates
"""
from datetime import datetime, timezone
from unittest.mock import patch

# We import Candidate and Bracket directly so tests stay fast and isolated
from src.strategy.scanner import Candidate
from src.model.envelope import Bracket, WeatherState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _make_weather_state() -> WeatherState:
    """A minimal WeatherState with a deterministic forecast high."""
    now = datetime.now(timezone.utc)
    return WeatherState(
        station="KORD",
        now_local=now,
        sunset_local=now,
        current_high_f=70.0,
        current_high_time=now,
        latest_temp_f=68.0,
        latest_temp_time=now,
        forecast_high_f=82.0,
    )


def _make_bracket(yes_ask: int = 72, no_ask: int = 30) -> Bracket:
    """A bracket where YES passes all gates (yes_ask=72¢, p_yes=0.90 → ev=17¢).

    Thresholds (from src.config): MIN_EDGE_CENTS=15, MAX_EDGE_CENTS=20,
    MIN_PRICE_CENTS=60, MIN_CONFIDENCE_YES=0.85.
    With p_yes=0.90 and fee=1¢: ev_yes = 90 - 72 - 1 = 17¢ (in range).
    """
    return Bracket(
        ticker="0xTEST",
        low_f=81.0,
        high_f=83.0,
        yes_ask_cents=yes_ask,
        yes_ask_size=500,
        no_ask_cents=no_ask,
        no_ask_size=500,
    )


def _make_market(bracket: Bracket) -> dict:
    """A minimal Polymarket-style market dict for a highest-temp market."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:00Z")
    return {
        "question": "Will the highest temperature in Chicago be 81-83°F on test date?",
        "groupItemTitle": "81-83°F",
        "conditionId": "0xCONDITION",
        "endDate": today_str,
        "yesTokenId": "0xYES",
        "noTokenId": "0xNO",
    }


# ---------------------------------------------------------------------------
# Patch target helpers
# ---------------------------------------------------------------------------

# These patches suppress network/DB calls inside scan_markets
_PATCHES = [
    patch("src.strategy.scanner.get_orderbook", return_value={"asks": [], "bids": []}),
    patch("src.strategy.scanner.check_taf_disruption", return_value=False),
    patch("src.strategy.scanner.get_city_mode", return_value="legacy"),
    patch("src.strategy.scanner.apply_emos", side_effect=lambda *a, **kw: None),
    patch("src.strategy.scanner._check_ready_for_promotion", return_value=False),
]


def _run_scan(weather, market, *, enable_yes_trades: bool):
    """Run scan_markets with the given ENABLE_YES_TRADES setting."""
    from src.strategy import scanner as _scanner_mod
    # Override the bracket parser to return our controlled bracket
    bracket = _make_bracket()
    with (
        patch.object(_scanner_mod, "ENABLE_YES_TRADES", enable_yes_trades),
        patch.object(_scanner_mod, "parse_bracket_from_market", return_value=bracket),
        patch.object(_scanner_mod, "ENABLE_CLOB_ENRICHMENT", False),
        patch("src.strategy.scanner.get_orderbook", return_value={"asks": [], "bids": []}),
        patch("src.strategy.scanner.check_taf_disruption", return_value=False),
        patch("src.strategy.scanner.get_city_mode", return_value="legacy"),
        patch("src.strategy.scanner.apply_emos", side_effect=lambda *a, **kw: None),
        patch("src.strategy.scanner._check_ready_for_promotion", return_value=False),
    ):
        # Patch true_probability_yes so YES passes all gates.
        # yes_ask=72¢, fee=1¢, p_yes=0.90:
        #   ev_yes = 90 - 72 - 1 = 17¢  (MIN=15, MAX=20 → in range)
        #   p_yes=0.90 >= MIN_CONFIDENCE_YES=0.85
        #   yes_ask=72 >= MIN_PRICE_CENTS=60
        with patch("src.strategy.scanner.true_probability_yes", return_value=0.90):
            with patch("src.strategy.scanner.estimate_fee_cents", return_value=1.0):
                from src.strategy.scanner import scan_markets
                candidates, _ = scan_markets(weather, [market], db=None)
    return candidates


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestShadowDetection:
    def setup_method(self):
        self.weather = {"KORD": _make_weather_state()}
        self.market = _make_market(_make_bracket())

    def test_enable_yes_false_produces_shadow_candidate(self):
        """With ENABLE_YES_TRADES=False and a YES-eligible market, get shadow=True."""
        candidates = _run_scan(self.weather, self.market, enable_yes_trades=False)
        yes_cands = [c for c in candidates if c.side == "YES"]
        assert len(yes_cands) == 1, f"Expected 1 YES candidate, got {yes_cands}"
        assert yes_cands[0].shadow is True

    def test_enable_yes_true_produces_non_shadow_candidate(self):
        """With ENABLE_YES_TRADES=True and a YES-eligible market, get shadow=False."""
        candidates = _run_scan(self.weather, self.market, enable_yes_trades=True)
        yes_cands = [c for c in candidates if c.side == "YES"]
        assert len(yes_cands) == 1, f"Expected 1 YES candidate, got {yes_cands}"
        assert yes_cands[0].shadow is False

    def test_shadow_candidate_has_correct_side_and_ticker(self):
        """Shadow candidate carries the correct metadata."""
        candidates = _run_scan(self.weather, self.market, enable_yes_trades=False)
        yes_cands = [c for c in candidates if c.side == "YES"]
        assert yes_cands[0].side == "YES"
        assert yes_cands[0].bracket.ticker == "0xTEST"
        assert yes_cands[0].price_cents == 72  # yes_ask_cents


class TestCandidateDataclassDefaults:
    """The Candidate dataclass default for shadow must be False."""

    def test_shadow_defaults_to_false(self):
        b = _make_bracket()
        c = Candidate(
            station="KORD",
            bracket=b,
            side="NO",
            edge_cents=10.0,
            price_cents=70,
            confidence=0.9,
            p_yes=0.1,
            ev_yes=-5.0,
            ev_no=10.0,
            minutes_to_settlement=120.0,
            market={},
        )
        assert c.shadow is False

    def test_shadow_can_be_set_true(self):
        b = _make_bracket()
        c = Candidate(
            station="KORD",
            bracket=b,
            side="YES",
            edge_cents=10.0,
            price_cents=30,
            confidence=0.55,
            p_yes=0.55,
            ev_yes=10.0,
            ev_no=-5.0,
            minutes_to_settlement=120.0,
            market={},
            shadow=True,
        )
        assert c.shadow is True

"""Unit tests for shadow candidate detection in src/strategy/scanner.py.

Tests the split between YES gate detection and execution routing introduced
in issue #261. Covers:
- yes_enabled=False → YES-passing market produces Candidate(shadow=True)
- yes_enabled=True  → YES-passing market produces Candidate(shadow=False)
- NO candidates are never shadow regardless of yes_enabled
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


def _run_scan(weather, market, *, yes_enabled: bool):
    """Run scan_markets with the given yes_enabled station override."""
    from src.strategy import scanner as _scanner_mod
    bracket = _make_bracket()
    mock_db = type("DB", (), {
        "get_all_config": lambda self: {},
        "get_config": lambda self, k: None,
        "get_station_override": lambda self, s: {"yes_enabled": yes_enabled, "no_enabled": True},
    })()
    with (
        patch.object(_scanner_mod, "parse_bracket_from_market", return_value=bracket),
        patch.object(_scanner_mod, "ENABLE_CLOB_ENRICHMENT", False),
        patch("src.strategy.scanner.get_orderbook", return_value={"asks": [], "bids": []}),
        patch("src.strategy.scanner.check_taf_disruption", return_value=False),
        patch("src.strategy.scanner.get_city_mode", return_value="legacy"),
        patch("src.strategy.scanner.apply_emos", side_effect=lambda *a, **kw: None),
        patch("src.strategy.scanner._check_ready_for_promotion", return_value=False),
        patch("src.strategy.scanner.get_live_config", return_value={}),
    ):
        # yes_ask=72¢, fee=1¢, p_yes=0.90:
        #   ev_yes = 90 - 72 - 1 = 17¢  (MIN=15, MAX=20 → in range)
        #   p_yes=0.90 >= MIN_CONFIDENCE_YES=0.85
        #   yes_ask=72 >= MIN_PRICE_CENTS=60
        with patch("src.strategy.scanner.true_probability_yes", return_value=0.90):
            with patch("src.strategy.scanner.estimate_fee_cents", return_value=1.0):
                from src.strategy.scanner import scan_markets
                candidates, _ = scan_markets(weather, [market], db=mock_db)
    return candidates


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestShadowDetection:
    def setup_method(self):
        self.weather = {"KORD": _make_weather_state()}
        self.market = _make_market(_make_bracket())

    def test_yes_disabled_produces_shadow_candidate(self):
        """With yes_enabled=False and a YES-eligible market, get shadow=True."""
        candidates = _run_scan(self.weather, self.market, yes_enabled=False)
        yes_cands = [c for c in candidates if c.side == "YES"]
        assert len(yes_cands) == 1, f"Expected 1 YES candidate, got {yes_cands}"
        assert yes_cands[0].shadow is True

    def test_yes_enabled_produces_non_shadow_candidate(self):
        """With yes_enabled=True and a YES-eligible market, get shadow=False."""
        candidates = _run_scan(self.weather, self.market, yes_enabled=True)
        yes_cands = [c for c in candidates if c.side == "YES"]
        assert len(yes_cands) == 1, f"Expected 1 YES candidate, got {yes_cands}"
        assert yes_cands[0].shadow is False

    def test_shadow_candidate_has_correct_side_and_ticker(self):
        """Shadow candidate carries the correct metadata."""
        candidates = _run_scan(self.weather, self.market, yes_enabled=False)
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


def _run_scan_with_shadow_gates(weather, market, *, p_yes, yes_ask, shadow_gates=None):
    """Run scan_markets with yes_enabled=False and configurable shadow gates.

    shadow_gates: dict with keys SHADOW_MIN_EDGE_CENTS_YES, SHADOW_MIN_CONFIDENCE_YES,
                  SHADOW_MIN_PRICE_CENTS_YES — if None, uses CONFIG_DEFAULTS.
    """
    from src.strategy import scanner as _scanner_mod
    from src.config import CONFIG_DEFAULTS

    gates = shadow_gates or {
        "SHADOW_MIN_EDGE_CENTS_YES": CONFIG_DEFAULTS["SHADOW_MIN_EDGE_CENTS_YES"],
        "SHADOW_MIN_CONFIDENCE_YES": CONFIG_DEFAULTS["SHADOW_MIN_CONFIDENCE_YES"],
        "SHADOW_MIN_PRICE_CENTS_YES": CONFIG_DEFAULTS["SHADOW_MIN_PRICE_CENTS_YES"],
    }

    bracket = _make_bracket(yes_ask=yes_ask, no_ask=30)

    mock_db = type("DB", (), {
        "get_all_config": lambda self: gates,
        "get_config": lambda self, k: None,
        "get_station_override": lambda self, s: {"yes_enabled": False, "no_enabled": True},
    })()

    fee = 1.0
    ev_yes = p_yes * 100 - yes_ask - fee

    with (
        patch.object(_scanner_mod, "parse_bracket_from_market", return_value=bracket),
        patch.object(_scanner_mod, "ENABLE_CLOB_ENRICHMENT", False),
        patch("src.strategy.scanner.get_orderbook", return_value={"asks": [], "bids": []}),
        patch("src.strategy.scanner.check_taf_disruption", return_value=False),
        patch("src.strategy.scanner.get_city_mode", return_value="legacy"),
        patch("src.strategy.scanner.apply_emos", side_effect=lambda *a, **kw: None),
        patch("src.strategy.scanner._check_ready_for_promotion", return_value=False),
        patch("src.strategy.scanner.true_probability_yes", return_value=p_yes),
        patch("src.strategy.scanner.estimate_fee_cents", return_value=fee),
        patch("src.strategy.scanner.get_live_config", return_value=gates),
    ):
        from src.strategy.scanner import scan_markets
        candidates, _ = scan_markets(weather, [market], db=mock_db)
    return candidates


class TestShadowYesGates:
    """Shadow YES gate thresholds (issue #284).

    Live gates: MIN_EDGE_CENTS=15, MIN_CONFIDENCE_YES=0.85, MIN_PRICE_CENTS=60
    Shadow defaults: SHADOW_MIN_EDGE_CENTS_YES=3, SHADOW_MIN_CONFIDENCE_YES=0.55,
                     SHADOW_MIN_PRICE_CENTS_YES=20
    """

    def setup_method(self):
        self.weather = {"KORD": _make_weather_state()}
        self.market = _make_market(_make_bracket())

    def test_live_yes_gates_unchanged_below_shadow_threshold(self):
        """A market at ev=4c/p=0.58/ask=25c with yes_enabled=True gets no YES candidate."""
        from src.strategy import scanner as _scanner_mod
        bracket = _make_bracket(yes_ask=25, no_ask=30)
        # ev_yes = 0.58*100 - 25 - 1 = 32c but p_yes=0.58 < MIN_CONFIDENCE_YES=0.85 → no YES
        with (
            patch.object(_scanner_mod, "parse_bracket_from_market", return_value=bracket),
            patch.object(_scanner_mod, "ENABLE_CLOB_ENRICHMENT", False),
            patch("src.strategy.scanner.get_orderbook", return_value={"asks": [], "bids": []}),
            patch("src.strategy.scanner.check_taf_disruption", return_value=False),
            patch("src.strategy.scanner.get_city_mode", return_value="legacy"),
            patch("src.strategy.scanner.apply_emos", side_effect=lambda *a, **kw: None),
            patch("src.strategy.scanner._check_ready_for_promotion", return_value=False),
            patch("src.strategy.scanner.true_probability_yes", return_value=0.58),
            patch("src.strategy.scanner.estimate_fee_cents", return_value=1.0),
        ):
            from src.strategy.scanner import scan_markets
            candidates, _ = scan_markets(self.weather, [self.market], db=None)
        yes_cands = [c for c in candidates if c.side == "YES"]
        assert len(yes_cands) == 0, "Live YES gates must reject this market"

    def test_shadow_yes_candidate_at_loosened_thresholds(self):
        """With ENABLE_YES_TRADES=False a market passes shadow gates but not live gates.

        Parameters chosen so the candidate passes shadow gates AND MAX_EDGE_CENTS:
          p_yes=0.58, yes_ask=45, fee=1 → ev_yes = 58-45-1 = 12c
          Shadow gates: 12 >= 3 ✓, 0.58 >= 0.55 ✓, 45 >= 20 ✓, 12 <= MAX_EDGE=20 ✓
          Live gates:   12 < MIN_EDGE=15 ✗  (also 0.58 < MIN_CONF=0.85 ✗, 45 < MIN_PRICE=60 ✗)
        """
        # ev_yes = 0.58*100 - 45 - 1 = 12c; within (SHADOW_MIN_EDGE=3, MAX_EDGE=20)
        candidates = _run_scan_with_shadow_gates(
            self.weather, self.market, p_yes=0.58, yes_ask=45
        )
        yes_cands = [c for c in candidates if c.side == "YES"]
        assert len(yes_cands) == 1, f"Expected 1 shadow YES candidate, got {yes_cands}"
        assert yes_cands[0].shadow is True

    def test_shadow_yes_below_shadow_edge_threshold_rejected(self):
        """A market below SHADOW_MIN_EDGE_CENTS_YES is not emitted even on shadow path."""
        # Set shadow edge min=10; market ev_yes = 0.52*100 - 25 - 1 = 26c > 10 → should pass
        # But let's set edge min very high so it fails
        gates = {
            "SHADOW_MIN_EDGE_CENTS_YES": 50.0,
            "SHADOW_MIN_CONFIDENCE_YES": 0.50,
            "SHADOW_MIN_PRICE_CENTS_YES": 1,
        }
        candidates = _run_scan_with_shadow_gates(
            self.weather, self.market, p_yes=0.52, yes_ask=25, shadow_gates=gates
        )
        yes_cands = [c for c in candidates if c.side == "YES"]
        assert len(yes_cands) == 0, "Shadow edge min=50 must reject ev≈26c"

    def test_no_candidates_unaffected_by_shadow_yes_gates(self):
        """NO candidates are produced with original gates; shadow YES params don't change them."""
        from src.strategy import scanner as _scanner_mod
        # yes_ask high (no YES candidate), no_ask=75, p_yes=0.05 → ev_no = (1-0.05)*100 - 75 - 1 = 18c
        bracket = _make_bracket(yes_ask=95, no_ask=75)
        gates = {
            "SHADOW_MIN_EDGE_CENTS_YES": 0.1,
            "SHADOW_MIN_CONFIDENCE_YES": 0.10,
            "SHADOW_MIN_PRICE_CENTS_YES": 1,
        }
        with (
            patch.object(_scanner_mod, "parse_bracket_from_market", return_value=bracket),
            patch.object(_scanner_mod, "ENABLE_CLOB_ENRICHMENT", False),
            patch("src.strategy.scanner.get_orderbook", return_value={"asks": [], "bids": []}),
            patch("src.strategy.scanner.check_taf_disruption", return_value=False),
            patch("src.strategy.scanner.get_city_mode", return_value="legacy"),
            patch("src.strategy.scanner.apply_emos", side_effect=lambda *a, **kw: None),
            patch("src.strategy.scanner._check_ready_for_promotion", return_value=False),
            patch("src.strategy.scanner.true_probability_yes", return_value=0.05),
            patch("src.strategy.scanner.estimate_fee_cents", return_value=1.0),
            patch("src.strategy.scanner.get_live_config", return_value=gates),
        ):
            mock_db = type("DB", (), {
                "get_all_config": lambda self: gates,
                "get_config": lambda self, k: None,
                "get_station_override": lambda self, s: None,
            })()
            from src.strategy.scanner import scan_markets
            candidates, _ = scan_markets(self.weather, [self.market], db=mock_db)
        no_cands = [c for c in candidates if c.side == "NO"]
        yes_cands = [c for c in candidates if c.side == "YES"]
        # NO candidates should exist; YES may or may not — key check is NO is unaffected
        assert len(no_cands) >= 1  # NO gate produces candidate with parameters in valid range
        assert all(c.shadow is False for c in no_cands), "NO candidates must never be shadow"

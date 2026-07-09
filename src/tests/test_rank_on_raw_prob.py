"""Unit tests for RANK_ON_RAW_PROB (issue #551, stage 1).

Covers:
- p_yes_raw / ev_yes_raw / ev_no_raw are populated on every Candidate and equal
  the capped values when MODEL_PROB_CAP did not fire.
- Default (flag off): candidate order out of scan_markets() is unchanged from
  today's scan order -- entry gates and edges are bit-identical.
- Flag on: candidate order is re-sorted by the raw-probability-derived edge on
  the flagged side, while the entry gate decisions (which candidates appear at
  all) and their capped edge_cents are unaffected.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.model.envelope import Bracket, WeatherState
from src.strategy.scanner import scan_markets


def _make_weather_state() -> WeatherState:
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


def _make_bracket(ticker: str, yes_ask: int = 5, no_ask: int = 79) -> Bracket:
    return Bracket(
        ticker=ticker,
        low_f=81.0,
        high_f=83.0,
        yes_ask_cents=yes_ask,
        yes_ask_size=500,
        no_ask_cents=no_ask,
        no_ask_size=500,
    )


def _make_market(ticker: str) -> dict:
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:00Z")
    return {
        "question": "Will the highest temperature in Chicago be 81-83°F on test date?",
        "groupItemTitle": "81-83°F",
        "conditionId": ticker,
        "endDate": today_str,
        "yesTokenId": "0xYES",
        "noTokenId": "0xNO",
    }


def _run_scan_two(rank_on_raw_prob: bool):
    """Run scan_markets with two NO candidates (TICK-A, TICK-B).

    Both pass NO entry gates on the CAPPED p_yes=0.05 (identical for both, one
    at the natural boundary, one pulled up from a lower raw value by the
    symmetric MODEL_PROB_CAP clamp). Their RAW p_yes differ (0.05 vs 0.0), so
    ev_no_raw differs (15c vs 20c) even though the capped edge is identical
    (15c) for both -- this is what lets the ranking test tell raw-based
    ordering apart from capped-based ordering.
    """
    from src.strategy import scanner as _scanner_mod

    bracket_a = _make_bracket("TICK-A")
    bracket_b = _make_bracket("TICK-B")
    market_a = _make_market("TICK-A")
    market_b = _make_market("TICK-B")
    weather = {"KORD": _make_weather_state()}

    mock_db = MagicMock()
    mock_db.get_station_override.return_value = None

    with (
        patch.object(_scanner_mod, "MODEL_PROB_CAP", 0.95),
        patch.object(_scanner_mod, "ENABLE_CLOB_ENRICHMENT", False),
        patch.object(_scanner_mod, "parse_bracket_from_market", side_effect=[bracket_a, bracket_b]),
        patch("src.strategy.scanner.get_orderbook", return_value={"asks": [], "bids": []}),
        patch("src.strategy.scanner.check_taf_disruption", return_value=False),
        patch("src.strategy.scanner.get_city_mode", return_value="legacy"),
        patch("src.strategy.scanner.emos_serving_mu", side_effect=lambda *a, **kw: None),
        patch("src.strategy.scanner._check_ready_for_promotion", return_value=False),
        patch("src.strategy.scanner.compute_residual_stats", return_value=None),
        patch("src.strategy.scanner.true_probability_yes", side_effect=[0.05, 0.0]),
        patch("src.strategy.scanner.estimate_fee_cents", return_value=1.0),
        patch("src.strategy.scanner.get_live_config", return_value={"RANK_ON_RAW_PROB": rank_on_raw_prob}),
    ):
        candidates, _ = scan_markets(weather, [market_a, market_b], db=mock_db)
    return candidates


class TestCandidateRawFieldsPopulated:
    """p_yes_raw / ev_*_raw are always populated by scan_markets()."""

    def test_raw_fields_populated_on_every_candidate(self):
        candidates = _run_scan_two(rank_on_raw_prob=False)
        assert len(candidates) == 2
        for c in candidates:
            assert c.p_yes_raw is not None
            assert c.ev_yes_raw is not None
            assert c.ev_no_raw is not None

    def test_raw_equals_capped_when_no_clamp_fired(self):
        """TICK-A: raw_p_yes=0.05 == capped p_yes=0.05 (no clamp effect)."""
        candidates = _run_scan_two(rank_on_raw_prob=False)
        cand_a = next(c for c in candidates if c.bracket.ticker == "TICK-A")
        assert cand_a.p_yes_raw == 0.05
        assert cand_a.p_yes == 0.05
        assert cand_a.p_yes_raw == cand_a.p_yes
        assert abs(cand_a.ev_no_raw - cand_a.ev_no) < 1e-9

    def test_raw_differs_from_capped_when_clamp_fired(self):
        """TICK-B: raw_p_yes=0.0 clamped up to capped p_yes=0.05."""
        candidates = _run_scan_two(rank_on_raw_prob=False)
        cand_b = next(c for c in candidates if c.bracket.ticker == "TICK-B")
        assert cand_b.p_yes_raw == 0.0
        assert cand_b.p_yes == 0.05
        assert cand_b.ev_no_raw != cand_b.ev_no
        # capped ev_no = 15c, raw ev_no = 20c (see _run_scan_two docstring)
        assert abs(cand_b.ev_no - 15.0) < 0.01
        assert abs(cand_b.ev_no_raw - 20.0) < 0.01


class TestRankOnRawProbDefaultOff:
    """Regression: flag off reproduces today's behaviour exactly."""

    def test_flag_off_preserves_scan_order(self):
        candidates = _run_scan_two(rank_on_raw_prob=False)
        tickers = [c.bracket.ticker for c in candidates]
        assert tickers == ["TICK-A", "TICK-B"], (
            f"Expected scan order preserved (no sort applied) when flag is off, got {tickers}"
        )

    def test_flag_off_candidate_set_and_edges_unchanged(self):
        candidates = _run_scan_two(rank_on_raw_prob=False)
        assert len(candidates) == 2
        assert all(c.side == "NO" for c in candidates)
        for c in candidates:
            assert abs(c.edge_cents - 15.0) < 0.01  # capped ev_no for both


class TestRankOnRawProbEnabled:
    """Flag on: ordering follows the raw-probability-derived edge; gates unchanged."""

    def test_flag_on_reorders_by_raw_edge_descending(self):
        candidates = _run_scan_two(rank_on_raw_prob=True)
        tickers = [c.bracket.ticker for c in candidates]
        # TICK-B has the higher raw edge (20c > 15c) so it should rank first,
        # reversing the scan order used when the flag is off.
        assert tickers == ["TICK-B", "TICK-A"], (
            f"Expected raw-edge ranking to prefer TICK-B first, got {tickers}"
        )

    def test_flag_on_does_not_change_candidate_set_or_capped_edges(self):
        """Same two candidates, same capped edge_cents -- only order differs."""
        off_candidates = _run_scan_two(rank_on_raw_prob=False)
        on_candidates = _run_scan_two(rank_on_raw_prob=True)

        off_by_ticker = {c.bracket.ticker: round(c.edge_cents, 2) for c in off_candidates}
        on_by_ticker = {c.bracket.ticker: round(c.edge_cents, 2) for c in on_candidates}
        assert off_by_ticker == on_by_ticker

        off_sides = {c.bracket.ticker: c.side for c in off_candidates}
        on_sides = {c.bracket.ticker: c.side for c in on_candidates}
        assert off_sides == on_sides

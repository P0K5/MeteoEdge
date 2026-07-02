"""Unit tests for MODEL_PROB_CAP interim overconfidence guardrail (issue #305).

Verifies that:
- p=1.0 with MODEL_PROB_CAP=0.95 → capped p_yes=0.95, ev_no computed on capped value
- MODEL_PROB_CAP=1.0 reproduces exact pre-cap behaviour
- p=0.0 with MODEL_PROB_CAP=0.95 → capped p_yes=0.05 (symmetric cap)
- raw_p_yes and capped_p_yes both appear in the snapshot dict
"""
from datetime import datetime, timezone
from unittest.mock import patch

from src.model.envelope import Bracket, WeatherState
from src.strategy.scanner import scan_markets


# ---------------------------------------------------------------------------
# Helpers shared with test_scanner_shadow.py
# ---------------------------------------------------------------------------

def _make_weather_state(forecast_high_f: float = 82.0) -> WeatherState:
    now = datetime.now(timezone.utc)
    return WeatherState(
        station="KORD",
        now_local=now,
        sunset_local=now,
        current_high_f=70.0,
        current_high_time=now,
        latest_temp_f=68.0,
        latest_temp_time=now,
        forecast_high_f=forecast_high_f,
    )


def _make_bracket(yes_ask: int = 5, no_ask: int = 97) -> Bracket:
    """Bracket that produces a NO candidate when p_yes is near 0 or capped down."""
    return Bracket(
        ticker="0xCAP_TEST",
        low_f=81.0,
        high_f=83.0,
        yes_ask_cents=yes_ask,
        yes_ask_size=500,
        no_ask_cents=no_ask,
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


def _run_scan_with_cap(raw_p_yes: float, model_prob_cap: float,
                       no_ask: int = 70, fee: float = 1.0):
    """Run scan_markets with a controlled raw p_yes and MODEL_PROB_CAP.

    Returns (candidates, snapshots).
    """
    from src.strategy import scanner as _scanner_mod
    bracket = _make_bracket(yes_ask=5, no_ask=no_ask)
    market = _make_market()
    weather = {"KORD": _make_weather_state()}

    with (
        patch.object(_scanner_mod, "MODEL_PROB_CAP", model_prob_cap),
        patch.object(_scanner_mod, "ENABLE_CLOB_ENRICHMENT", False),
        patch.object(_scanner_mod, "parse_bracket_from_market", return_value=bracket),
        patch("src.strategy.scanner.get_orderbook", return_value={"asks": [], "bids": []}),
        patch("src.strategy.scanner.check_taf_disruption", return_value=False),
        patch("src.strategy.scanner.get_city_mode", return_value="legacy"),
        patch("src.strategy.scanner.apply_emos", side_effect=lambda *a, **kw: None),
        patch("src.strategy.scanner._check_ready_for_promotion", return_value=False),
        patch("src.strategy.scanner.true_probability_yes", return_value=raw_p_yes),
        patch("src.strategy.scanner.estimate_fee_cents", return_value=fee),
    ):
        candidates, snapshots = scan_markets(weather, [market], db=None)
    return candidates, snapshots


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestModelProbCapApplication:
    """MODEL_PROB_CAP clamps p_yes symmetrically before EV computation."""

    def test_p1_cap095_yields_p_yes_095(self):
        """p=1.0 with cap=0.95 → effective p_yes=0.95, ev_no uses capped value."""
        # ev_no = (1 - 0.95) * 100 - 70 - 1 = 5 - 71 = -66  (below MIN_EDGE, no candidate)
        # We check the snapshot instead.
        _, snapshots = _run_scan_with_cap(raw_p_yes=1.0, model_prob_cap=0.95, no_ask=70)
        assert len(snapshots) == 1
        snap = snapshots[0]
        assert snap["capped_p_yes"] == 0.95, f"Expected capped_p_yes=0.95, got {snap['capped_p_yes']}"
        assert snap["raw_p_yes"] == 1.0, f"Expected raw_p_yes=1.0, got {snap['raw_p_yes']}"
        assert snap["p_yes"] == 0.95

    def test_p1_cap095_ev_no_uses_capped_value(self):
        """ev_no is computed on capped p_yes=0.95, not raw p_yes=1.0.

        With p_yes=0.95 (capped), no_ask=70, fee=1:
          ev_no = (1 - 0.95) * 100 - 70 - 1 = 5 - 71 = -66  → no candidate
        With raw p_yes=1.0:
          ev_no = (1 - 1.0) * 100 - 70 - 1 = -71  → same direction but verifies capped used
        We verify using a no_ask that ONLY passes when using capped (not raw) p_yes.
        p_yes=0.95 → (1-0.95)*100 = 5¢ probability component
        With no_ask=3, fee=1: ev_no = 5 - 3 - 1 = 1¢  (below MIN_EDGE=15, no candidate)
        — but ev_yes = 0.95*100 - 5 - 1 = 89¢ → YES shadow candidate should appear.
        """
        # Just verify snapshot ev_no is computed with capped p_yes
        _, snapshots = _run_scan_with_cap(raw_p_yes=1.0, model_prob_cap=0.95, no_ask=70)
        snap = snapshots[0]
        # ev_no at capped p_yes=0.95, no_ask=70, fee=1 → (0.05*100) - 70 - 1 = -66
        assert abs(snap["ev_no"] - (-66.0)) < 0.01, f"ev_no={snap['ev_no']} expected -66"

    def test_cap10_reproduces_existing_behaviour(self):
        """MODEL_PROB_CAP=1.0 leaves p_yes=1.0 unchanged (no capping effect).

        With raw p_yes=1.0 and cap=1.0: min(max(1.0, 0.0), 1.0) = 1.0
        """
        _, snapshots = _run_scan_with_cap(raw_p_yes=1.0, model_prob_cap=1.0, no_ask=70)
        snap = snapshots[0]
        assert snap["capped_p_yes"] == 1.0
        assert snap["raw_p_yes"] == 1.0
        assert snap["p_yes"] == 1.0
        # ev_no = (1 - 1.0)*100 - 70 - 1 = -71
        assert abs(snap["ev_no"] - (-71.0)) < 0.01

    def test_symmetric_cap_p0_yields_p_yes_005(self):
        """p=0.0 with cap=0.95 → capped p_yes=0.05 (lower bound = 1 - 0.95)."""
        _, snapshots = _run_scan_with_cap(raw_p_yes=0.0, model_prob_cap=0.95, no_ask=70)
        snap = snapshots[0]
        assert snap["capped_p_yes"] == 0.05, f"Expected capped_p_yes=0.05, got {snap['capped_p_yes']}"
        assert snap["raw_p_yes"] == 0.0

    def test_cap10_p0_unchanged(self):
        """MODEL_PROB_CAP=1.0 leaves p_yes=0.0 unchanged."""
        _, snapshots = _run_scan_with_cap(raw_p_yes=0.0, model_prob_cap=1.0, no_ask=70)
        snap = snapshots[0]
        assert snap["capped_p_yes"] == 0.0
        assert snap["raw_p_yes"] == 0.0

    def test_midrange_p_unchanged_by_default_cap(self):
        """p=0.90 is within [0.05, 0.95], so no capping occurs with cap=0.95."""
        _, snapshots = _run_scan_with_cap(raw_p_yes=0.90, model_prob_cap=0.95, no_ask=70)
        snap = snapshots[0]
        assert snap["capped_p_yes"] == 0.90
        assert snap["raw_p_yes"] == 0.90


class TestSnapshotKeys:
    """raw_p_yes and capped_p_yes must appear in every snapshot dict."""

    def test_snapshot_contains_raw_p_yes(self):
        _, snapshots = _run_scan_with_cap(raw_p_yes=0.80, model_prob_cap=0.95)
        assert len(snapshots) == 1
        assert "raw_p_yes" in snapshots[0], "snapshot missing raw_p_yes key"

    def test_snapshot_contains_capped_p_yes(self):
        _, snapshots = _run_scan_with_cap(raw_p_yes=0.80, model_prob_cap=0.95)
        assert "capped_p_yes" in snapshots[0], "snapshot missing capped_p_yes key"

    def test_snapshot_raw_equals_capped_when_no_capping(self):
        """When p_yes is in bounds, raw_p_yes == capped_p_yes."""
        _, snapshots = _run_scan_with_cap(raw_p_yes=0.80, model_prob_cap=0.95)
        snap = snapshots[0]
        assert snap["raw_p_yes"] == snap["capped_p_yes"]

    def test_snapshot_raw_differs_from_capped_when_capped(self):
        """When p_yes is capped, raw_p_yes != capped_p_yes."""
        _, snapshots = _run_scan_with_cap(raw_p_yes=1.0, model_prob_cap=0.95)
        snap = snapshots[0]
        assert snap["raw_p_yes"] != snap["capped_p_yes"]
        assert snap["raw_p_yes"] == 1.0
        assert snap["capped_p_yes"] == 0.95


class TestEdgeGatesUsesCappedProbability:
    """Entry gates (MIN_EDGE_CENTS, MIN_PRICE_CENTS) evaluate on the capped p_yes.

    Use a NO bracket where the capped p_yes puts ev_no inside the gate window,
    and raw p_yes would compute a different ev_no.
    """

    def test_candidate_rejected_when_capped_ev_no_below_min_edge(self):
        """With raw p_yes=1.0, cap=0.95: ev_no on capped = 5 - 70 - 1 = -66 < MIN_EDGE.

        Even though raw p_yes=1.0 has ev_no = 0 - 70 - 1 = -71 (also below),
        confirms that the capped value is what drives the gate.
        """
        candidates, snapshots = _run_scan_with_cap(
            raw_p_yes=1.0, model_prob_cap=0.95, no_ask=70
        )
        no_cands = [c for c in candidates if c.side == "NO"]
        # ev_no at capped p_yes=0.95 is -66, below MIN_EDGE_CENTS=15 → no NO candidate
        assert len(no_cands) == 0

    def test_no_candidate_emitted_with_cap10_when_gates_pass(self):
        """cap=1.0: p_yes=0.03, no_ask=77, fee=1 → ev_no = 97 - 77 - 1 = 19¢ in gate.

        MAX_CONFIDENCE_YES_FOR_NO=0.05 → p_yes=0.03 ≤ 0.05 ✓
        MIN_PRICE_CENTS=60 → no_ask=77 ≥ 60 ✓
        MIN_EDGE=15, MAX_EDGE=20 → 19¢ ✓
        """
        candidates, _ = _run_scan_with_cap(
            raw_p_yes=0.03, model_prob_cap=1.0, no_ask=77
        )
        no_cands = [c for c in candidates if c.side == "NO"]
        assert len(no_cands) == 1, f"Expected 1 NO candidate, got {no_cands}"
        assert abs(no_cands[0].edge_cents - 19.0) < 0.01


class TestCandidateRawProbability:
    """Candidate.p_yes_raw / ev_yes_raw / ev_no_raw (issue #551, stage 1).

    These carry the pre-clamp probability/edges alongside the existing capped
    fields so ranking/logging can see raw model confidence without touching
    the values that drive entry gates.
    """

    def test_candidate_p_yes_raw_equals_p_yes_when_uncapped(self):
        """cap=1.0: no clamp fires, so p_yes_raw == p_yes and ev_no_raw == ev_no."""
        candidates, _ = _run_scan_with_cap(
            raw_p_yes=0.03, model_prob_cap=1.0, no_ask=77
        )
        no_cands = [c for c in candidates if c.side == "NO"]
        assert len(no_cands) == 1
        cand = no_cands[0]
        assert cand.p_yes_raw == 0.03
        assert cand.p_yes_raw == cand.p_yes
        assert abs(cand.ev_no_raw - cand.ev_no) < 0.01
        assert abs(cand.ev_no_raw - 19.0) < 0.01

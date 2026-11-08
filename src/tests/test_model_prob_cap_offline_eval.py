"""Tests for src/scripts/model_prob_cap_offline_eval.py (issue #1490).

Covers: the #1507-T0 upper-bound window filter, #820-artifact/missing-value
row hygiene, the per-poll cap replay (reusing ``evaluate_entry``'s clamp and
the margin-gate reconstruction against a seeded read-only DB), per-cap
first-qualifying-poll selection (including a cap-driven flip), the
gate-verdict-flip counter, and the pre-registered decision rule's mechanical
items (1)/(2).
"""
import json

import pytest

from src.data.db import Database
from src.scripts.emos_shadow_reconstruction import ReadOnlyDatabase
from src.scripts.per_lead_emos_offline_eval import (
    ArmTrade,
    GateConfig,
    OfflineEvalCache,
    attach_outcomes,
    summarize_trades,
)
from src.scripts.model_prob_cap_offline_eval import (
    CapPollCandidate,
    FlipCounts,
    _evaluate_poll_for_cap,
    compute_flip_counts,
    count_excluded_rows,
    load_candidate_groups,
    passes_mechanical_checks,
    select_first_qualifying_trades_for_cap,
)

STATION = "KORD"
CITY = "Chicago"


def _default_cfg(**overrides) -> GateConfig:
    base = dict(
        min_edge_cents=15.0, max_edge_cents=20.0, min_price_cents=70.0,
        max_conf_yes_for_no=0.05, min_conf_yes=0.85, model_prob_cap=0.95,
        min_margin_f=2.5, deb_enabled=False, sigma_climb_fraction=0.5,
        use_ensemble_sigma=False,
    )
    base.update(overrides)
    return GateConfig(**base)


# ---------------------------------------------------------------------------
# load_candidate_groups -- the #1507-T0 upper-bound filter
# ---------------------------------------------------------------------------

class TestLoadCandidateGroups:
    def _write_jsonl(self, path, rows):
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def test_drops_polls_at_or_after_until_and_empty_groups(self, tmp_path):
        base = tmp_path / "bracket_evals.jsonl"
        self._write_jsonl(base, [
            {"poll_ts": "2026-11-08T10:00:00+00:00", "station": STATION, "ticker": "t1",
             "settlement_date": "2026-11-08", "bracket_low": 70, "bracket_high": 75,
             "yes_ask": 0.3, "no_ask": 0.7, "p_yes_raw": 0.3, "minutes_to_settlement": 600,
             "direction": "high", "is_next_day": 0},
            # This poll is at/after the cutoff -- should be dropped.
            {"poll_ts": "2026-11-08T16:00:00+00:00", "station": STATION, "ticker": "t2",
             "settlement_date": "2026-11-08", "bracket_low": 70, "bracket_high": 75,
             "yes_ask": 0.3, "no_ask": 0.7, "p_yes_raw": 0.3, "minutes_to_settlement": 300,
             "direction": "high", "is_next_day": 0},
        ])
        groups = load_candidate_groups(base, since="2000-01-01", until="2026-11-08T15:33:31+00:00")
        assert set(groups.keys()) == {(STATION, "t1", "2026-11-08")}

    def test_until_none_keeps_everything(self, tmp_path):
        base = tmp_path / "bracket_evals.jsonl"
        self._write_jsonl(base, [
            {"poll_ts": "2026-11-08T16:00:00+00:00", "station": STATION, "ticker": "t2",
             "settlement_date": "2026-11-08", "bracket_low": 70, "bracket_high": 75,
             "yes_ask": 0.3, "no_ask": 0.7, "p_yes_raw": 0.3, "minutes_to_settlement": 300,
             "direction": "high", "is_next_day": 0},
        ])
        groups = load_candidate_groups(base, since="2000-01-01", until=None)
        assert set(groups.keys()) == {(STATION, "t2", "2026-11-08")}


# ---------------------------------------------------------------------------
# count_excluded_rows -- missing p_yes_raw exclusion + zero-value disclosure
# (NOT exclusion -- see module docstring point 3, corrected after the first
# local run found the naive #820-style exclusion dropped ~60% of all polls)
# ---------------------------------------------------------------------------

class TestCountExcludedRows:
    def test_counts_missing_and_discloses_zero_without_excluding(self):
        groups = {
            ("KORD", "t1", "2026-09-20"): [
                {"p_yes_raw": 0.3},
                {"p_yes_raw": None},
                {"p_yes_raw": 0.0},
            ],
        }
        counts = count_excluded_rows(groups)
        assert counts == {"total_polls": 3, "missing_p_yes_raw": 1, "p_yes_raw_zero_not_excluded": 1}


# ---------------------------------------------------------------------------
# _evaluate_poll_for_cap / select_first_qualifying_trades_for_cap -- integration
# ---------------------------------------------------------------------------

def _seed_margin_db(path) -> None:
    """NWS forecast rows (for the margin gate's own input) + one observation,
    mirroring per_lead_emos_offline_eval's ``_seed_poll_db`` fixture (no
    intraday_corrections / open_meteo rows needed here -- this module never
    recomputes mu/sigma, only the margin gate, which reads the raw NWS
    forecast and the running daily high)."""
    db = Database(str(path))
    date = "2026-09-20"
    for lead in (6, 24):
        db.upsert_forecast_log_v2(station=STATION, model="nws", date=date,
                                   forecast_high_f=80.0, lead_hours=lead,
                                   issued_at=f"{date}T00:00:00+00:00")
    db.insert_observation(ts=f"{date}T12:00:00+00:00", station=STATION,
                           temp_f=78.0, temp_native=78.0, unit="F", source="metar")
    db._conn.commit()
    db._conn.close()


class TestEvaluatePollForCap:
    def test_tighter_cap_blocks_what_a_looser_cap_allows(self, tmp_path):
        """A raw p_yes near zero stays below max_conf_yes_for_no either way,
        but a tighter cap (0.95 -> floor 0.05) lowers the NO-side edge
        (ev_no = (1 - p_yes)*100 - no_ask - fee) relative to a looser cap
        (1.0 -> floor 0.0), which can push ev_no outside [min_edge, max_edge].
        This is the gate-verdict-flip mechanism issue #1490 asks to quantify.
        """
        db_path = tmp_path / "db.sqlite"
        _seed_margin_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            cache = OfflineEvalCache()
            poll = CapPollCandidate(
                station=STATION, ticker="0xabc", end_date="2026-09-20",
                ts="2026-09-20T13:00:00+00:00",
                bracket_low=120.0, bracket_high=125.0,
                yes_ask=2, no_ask=78, minutes_to_settlement=300.0,
                # Near-zero (not exactly 0.0, which gets its own dedicated
                # test -- test_zero_p_yes_raw_is_evaluated_normally_not_excluded --
                # since both clamp to the same 0.05 under cap=0.95 anyway).
                p_yes_raw=0.001,
            )
            cfg_tight = _default_cfg(model_prob_cap=0.95, min_edge_cents=15.0, max_edge_cents=20.0)
            cfg_loose = _default_cfg(model_prob_cap=1.0, min_edge_cents=15.0, max_edge_cents=20.0)

            result_tight = _evaluate_poll_for_cap(ro, poll, cfg_tight, cache)
            result_loose = _evaluate_poll_for_cap(ro, poll, cfg_loose, cache)
        finally:
            ro._conn.close()

        # cap=0.95 -> p_yes clamps to 0.05 -> ev_no = 95 - 78 - fee ~ inside [15, 20].
        assert result_tight is not None
        assert result_tight[2] == pytest.approx(0.05)
        # cap=1.0 -> p_yes stays 0.0 -> ev_no = 100 - 78 - fee, pushed past max_edge_cents=20 -> blocked.
        assert result_loose is None

    def test_missing_p_yes_raw_returns_none(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_margin_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            cache = OfflineEvalCache()
            poll = CapPollCandidate(
                station=STATION, ticker="0xabc", end_date="2026-09-20",
                ts="2026-09-20T13:00:00+00:00",
                bracket_low=120.0, bracket_high=125.0,
                yes_ask=2, no_ask=78, minutes_to_settlement=300.0,
                p_yes_raw=None,
            )
            result = _evaluate_poll_for_cap(ro, poll, _default_cfg(), cache)
        finally:
            ro._conn.close()
        assert result is None

    def test_zero_p_yes_raw_is_evaluated_normally_not_excluded(self, tmp_path):
        """p_yes_raw == 0.0 is envelope.py's ordinary observation-based
        certainty shortcut, not an artifact to drop -- the real scanner.py
        gate clamps and trades on it with no special-casing (module
        docstring point 3), so this harness must not exclude it either."""
        db_path = tmp_path / "db.sqlite"
        _seed_margin_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            cache = OfflineEvalCache()
            poll = CapPollCandidate(
                station=STATION, ticker="0xabc", end_date="2026-09-20",
                ts="2026-09-20T13:00:00+00:00",
                bracket_low=120.0, bracket_high=125.0,
                yes_ask=2, no_ask=78, minutes_to_settlement=300.0,
                p_yes_raw=0.0,
            )
            result = _evaluate_poll_for_cap(ro, poll, _default_cfg(model_prob_cap=0.95), cache)
        finally:
            ro._conn.close()
        # cap=0.95 clamps 0.0 -> 0.05 -> NO qualifies (same math as the
        # tighter-cap case in test_tighter_cap_blocks_what_a_looser_cap_allows).
        assert result is not None
        side, price, p_yes, _ev = result
        assert side == "NO"
        assert p_yes == pytest.approx(0.05)


class TestSelectFirstQualifyingTradesForCap:
    def test_different_caps_can_select_different_outcomes(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_margin_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            groups = {
                (STATION, "0xabc", "2026-09-20"): [
                    {"ts": "2026-09-20T13:00:00+00:00", "bracket_low": 120.0,
                     "bracket_high": 125.0, "yes_ask": 2, "no_ask": 78,
                     "minutes_to_settlement": 300.0, "p_yes_raw": 0.001},
                ],
            }
            cfg_tight = _default_cfg(model_prob_cap=0.95, min_edge_cents=15.0, max_edge_cents=20.0)
            cfg_loose = _default_cfg(model_prob_cap=1.0, min_edge_cents=15.0, max_edge_cents=20.0)
            cache = OfflineEvalCache()
            trades_tight = select_first_qualifying_trades_for_cap(ro, groups, cfg_tight, cache)
            trades_loose = select_first_qualifying_trades_for_cap(ro, groups, cfg_loose, cache)
        finally:
            ro._conn.close()
        assert len(trades_tight) == 1 and trades_tight[0].side == "NO"
        assert len(trades_loose) == 0


# ---------------------------------------------------------------------------
# compute_flip_counts
# ---------------------------------------------------------------------------

def _trade(key, side, price=76.0):
    station, ticker, end_date = key
    return ArmTrade(station=station, ticker=ticker, end_date=end_date, side=side,
                     price_cents=price, p_yes=0.1, poll_ts="x", bracket_low=1, bracket_high=2)


class TestComputeFlipCounts:
    def test_counts_new_qualify_stopped_and_side_switch(self):
        k1, k2, k3, k4 = (
            ("KORD", "t1", "2026-09-20"), ("KORD", "t2", "2026-09-20"),
            ("KORD", "t3", "2026-09-20"), ("KORD", "t4", "2026-09-20"),
        )
        all_keys = [k1, k2, k3, k4]
        baseline = [_trade(k2, "NO"), _trade(k3, "YES")]
        candidate = [_trade(k1, "NO"), _trade(k3, "NO")]
        # k1: baseline None -> candidate NO -> new_qualify
        # k2: baseline NO -> candidate None -> stopped_qualifying
        # k3: baseline YES -> candidate NO -> side_switch
        # k4: both None -> no flip
        counts = compute_flip_counts(all_keys, baseline, candidate)
        assert counts.total_candidates == 4
        assert counts.new_qualify == 1
        assert counts.stopped_qualifying == 1
        assert counts.side_switch == 1
        assert counts.total_flips == 3
        assert counts.flip_fraction == pytest.approx(0.75)

    def test_no_flips_when_arms_agree(self):
        k1 = ("KORD", "t1", "2026-09-20")
        baseline = [_trade(k1, "NO")]
        candidate = [_trade(k1, "NO")]
        counts = compute_flip_counts([k1], baseline, candidate)
        assert counts.total_flips == 0
        assert counts.flip_fraction == 0.0


class TestFlipCountsEdgeCases:
    def test_zero_candidates_flip_fraction_is_zero(self):
        counts = FlipCounts(total_candidates=0, new_qualify=0, stopped_qualifying=0, side_switch=0)
        assert counts.flip_fraction == 0.0


# ---------------------------------------------------------------------------
# passes_mechanical_checks -- pre-registered decision rule, items (1)/(2)
# ---------------------------------------------------------------------------

class _Summary:
    def __init__(self, total_pnl, no_pnl):
        self.total_pnl = total_pnl
        self.no_pnl = no_pnl


class TestPassesMechanicalChecks:
    def test_passes_when_pnl_higher_and_no_side_not_lower(self):
        baseline = _Summary(total_pnl=0.0, no_pnl=0.0)
        candidate = _Summary(total_pnl=1.0, no_pnl=0.0)
        assert passes_mechanical_checks(baseline, candidate) is True

    def test_fails_when_total_pnl_not_higher(self):
        baseline = _Summary(total_pnl=1.0, no_pnl=0.0)
        candidate = _Summary(total_pnl=1.0, no_pnl=0.0)
        assert passes_mechanical_checks(baseline, candidate) is False

    def test_fails_when_no_side_pnl_lower_even_if_total_higher(self):
        baseline = _Summary(total_pnl=0.0, no_pnl=1.0)
        candidate = _Summary(total_pnl=2.0, no_pnl=0.5)
        assert passes_mechanical_checks(baseline, candidate) is False

    def test_passes_when_no_side_pnl_exactly_equal(self):
        baseline = _Summary(total_pnl=0.0, no_pnl=1.0)
        candidate = _Summary(total_pnl=1.0, no_pnl=1.0)
        assert passes_mechanical_checks(baseline, candidate) is True


# ---------------------------------------------------------------------------
# End-to-end-ish: select -> attach_outcomes -> summarize, for one cap
# ---------------------------------------------------------------------------

class TestSummarizeAfterSelection:
    def test_no_trade_resolves_and_summarizes(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_margin_db(db_path)
        db = Database(str(db_path))
        db.insert_observation(ts="2026-09-20T18:00:00+00:00", station=STATION,
                               temp_f=82.0, temp_native=82.0, unit="F", source="metar")
        db._conn.commit()
        db._conn.close()

        ro = ReadOnlyDatabase(db_path)
        try:
            groups = {
                (STATION, "0xabc", "2026-09-20"): [
                    {"ts": "2026-09-20T13:00:00+00:00", "bracket_low": 120.0,
                     "bracket_high": 125.0, "yes_ask": 2, "no_ask": 78,
                     "minutes_to_settlement": 300.0, "p_yes_raw": 0.001},
                ],
            }
            cfg = _default_cfg(model_prob_cap=0.95, min_edge_cents=15.0, max_edge_cents=20.0)
            trades = select_first_qualifying_trades_for_cap(ro, groups, cfg, OfflineEvalCache())
        finally:
            ro._conn.close()

        outcomes = attach_outcomes(trades, db_path, use_gamma=False, allow_network=False)
        summary = summarize_trades(trades, outcomes)
        # Observed high (82) is well below the bracket [120, 125) -> NO wins.
        assert summary.n_trades == 1
        assert summary.no_trades == 1
        assert summary.no_pnl == pytest.approx(0.22)

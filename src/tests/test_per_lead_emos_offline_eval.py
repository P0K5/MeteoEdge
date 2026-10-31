"""Tests for src/scripts/per_lead_emos_offline_eval.py (issue #1453, Phase A).

Covers: the dated-triple train/eval split, pooling-blended fitting, the
entry-gate replay (YES/NO precedence, MODEL_PROB_CAP clamp, margin/max-edge
gates), per-poll reconstruction/first-qualifying-poll selection against a
seeded read-only DB, pnl convention, outcome resolution, the paired bootstrap,
and the pre-registered decision rule.
"""
import json
from datetime import datetime

import pytest

from src.config import FORECAST_STDDEV_F
from src.data.db import Database
from src.model.emos_calibration import fit_emos
from src.scripts.emos_shadow_reconstruction import ReadOnlyDatabase
from src.scripts.per_lead_emos_offline_eval import (
    ArmTrade,
    CityCoefficients,
    GateConfig,
    OfflineEvalCache,
    PollCandidate,
    _evaluate_poll,
    _fetch_dated_triples,
    _fit_with_pooling,
    attach_outcomes,
    decide,
    evaluate_entry,
    load_same_day_polls,
    paired_bootstrap_ci,
    select_first_qualifying_trades,
    summarize_trades,
    trade_pnl,
)

STATION = "KORD"
CITY = "Chicago"
REGIME = frozenset({"nws", "open_meteo"})


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
# evaluate_entry -- pure gate replay
# ---------------------------------------------------------------------------

class TestEvaluateEntry:
    def test_yes_side_passes(self):
        cfg = _default_cfg()
        # ev_yes = 90 - 74 - fee(~26c) ~ 15.9, inside [min_edge=15, max_edge=20];
        # p_yes=0.90 >= min_conf_yes=0.85; yes_ask=74 >= min_price=70.
        result = evaluate_entry(
            p_yes_raw=0.90, yes_ask_cents=74, no_ask_cents=26,
            margin_gap=None, cfg=cfg,
        )
        assert result is not None
        side, price, p_yes, ev = result
        assert side == "YES"
        assert price == 74

    def test_no_side_passes_when_no_edge_sufficient(self):
        cfg = _default_cfg()
        result = evaluate_entry(
            p_yes_raw=0.02, yes_ask_cents=5, no_ask_cents=78,
            margin_gap=10.0, cfg=cfg,
        )
        assert result is not None
        side, price, p_yes, ev = result
        assert side == "NO"
        assert price == 78

    def test_no_side_blocked_by_margin_gate(self):
        cfg = _default_cfg()
        result = evaluate_entry(
            p_yes_raw=0.02, yes_ask_cents=5, no_ask_cents=78,
            margin_gap=1.0,  # below min_margin_f=2.5
            cfg=cfg,
        )
        assert result is None

    def test_no_side_blocked_by_max_edge(self):
        cfg = _default_cfg(max_edge_cents=5.0)
        result = evaluate_entry(
            p_yes_raw=0.01, yes_ask_cents=2, no_ask_cents=90,
            margin_gap=10.0, cfg=cfg,
        )
        assert result is None

    def test_no_side_blocked_by_min_price(self):
        cfg = _default_cfg(min_price_cents=80.0)
        result = evaluate_entry(
            p_yes_raw=0.02, yes_ask_cents=5, no_ask_cents=78,
            margin_gap=10.0, cfg=cfg,
        )
        assert result is None

    def test_no_side_blocked_by_confidence_gate(self):
        cfg = _default_cfg(max_conf_yes_for_no=0.05)
        result = evaluate_entry(
            p_yes_raw=0.06, yes_ask_cents=10, no_ask_cents=78,
            margin_gap=10.0, cfg=cfg,
        )
        assert result is None

    def test_model_prob_cap_clamps_extreme_p_yes(self):
        cfg = _default_cfg(model_prob_cap=0.95, max_conf_yes_for_no=0.05)
        # p_yes_raw=0.0 clamps to 0.05 -- exactly at the NO confidence
        # threshold (<=), so the NO branch is still reachable. no_ask=75
        # keeps ev_no inside [min_edge, max_edge].
        result = evaluate_entry(
            p_yes_raw=0.0, yes_ask_cents=1, no_ask_cents=75,
            margin_gap=10.0, cfg=cfg,
        )
        assert result is not None
        assert result[2] == pytest.approx(0.05)

    def test_neither_side_qualifies_returns_none(self):
        cfg = _default_cfg()
        result = evaluate_entry(
            p_yes_raw=0.5, yes_ask_cents=50, no_ask_cents=50,
            margin_gap=None, cfg=cfg,
        )
        assert result is None


# ---------------------------------------------------------------------------
# trade_pnl -- settle.py convention
# ---------------------------------------------------------------------------

class TestTradePnl:
    def test_no_win(self):
        assert trade_pnl("NO", 76, yes_won=False) == pytest.approx(0.24)

    def test_no_loss(self):
        assert trade_pnl("NO", 76, yes_won=True) == pytest.approx(-0.76)

    def test_yes_win(self):
        assert trade_pnl("YES", 30, yes_won=True) == pytest.approx(0.70)

    def test_yes_loss(self):
        assert trade_pnl("YES", 30, yes_won=False) == pytest.approx(-0.30)


# ---------------------------------------------------------------------------
# paired_bootstrap_ci
# ---------------------------------------------------------------------------

class TestPairedBootstrapCi:
    def test_empty_pairs_returns_zero(self):
        assert paired_bootstrap_ci([]) == (0.0, 0.0, 0.0)

    def test_all_zero_diff_ci_is_degenerate_at_zero(self):
        pairs = [(0.5, 0.5)] * 20
        mean_diff, lo, hi = paired_bootstrap_ci(pairs, n_resamples=200)
        assert mean_diff == pytest.approx(0.0)
        assert lo == pytest.approx(0.0)
        assert hi == pytest.approx(0.0)

    def test_consistent_positive_diff_ci_excludes_zero(self):
        # nearest_bin always wins by +1.0 over 24h_only -- CI should sit
        # entirely above zero.
        pairs = [(0.0, 1.0)] * 30
        mean_diff, lo, hi = paired_bootstrap_ci(pairs, n_resamples=500)
        assert mean_diff == pytest.approx(1.0)
        assert lo > 0

    def test_deterministic_given_seed(self):
        pairs = [(0.1, 0.4), (0.2, -0.1), (0.0, 0.3), (-0.2, 0.1)]
        a = paired_bootstrap_ci(pairs, n_resamples=100, seed=42)
        b = paired_bootstrap_ci(pairs, n_resamples=100, seed=42)
        assert a == b


# ---------------------------------------------------------------------------
# decide() -- pre-registered rule
# ---------------------------------------------------------------------------

class _Summary:
    def __init__(self, total_pnl, no_pnl):
        self.total_pnl = total_pnl
        self.no_pnl = no_pnl


class TestDecide:
    def test_go_when_all_three_conditions_hold(self):
        s24 = _Summary(total_pnl=0.0, no_pnl=0.0)
        sbin = _Summary(total_pnl=5.0, no_pnl=1.0)
        assert decide(s24, sbin, (2.5, 1.0, 4.0)) == "GO"

    def test_no_go_when_total_pnl_not_higher(self):
        s24 = _Summary(total_pnl=5.0, no_pnl=1.0)
        sbin = _Summary(total_pnl=4.0, no_pnl=1.0)
        assert decide(s24, sbin, (-1.0, -2.0, -0.5)) == "NO-GO"

    def test_inconclusive_when_pnl_higher_but_ci_includes_zero(self):
        s24 = _Summary(total_pnl=0.0, no_pnl=0.0)
        sbin = _Summary(total_pnl=1.0, no_pnl=0.5)
        assert decide(s24, sbin, (0.2, -0.3, 0.7)) == "INCONCLUSIVE"

    def test_inconclusive_when_no_side_pnl_lower(self):
        s24 = _Summary(total_pnl=0.0, no_pnl=1.0)
        sbin = _Summary(total_pnl=1.0, no_pnl=0.5)  # NO pnl lower
        assert decide(s24, sbin, (1.0, 0.5, 1.5)) == "INCONCLUSIVE"


# ---------------------------------------------------------------------------
# summarize_trades
# ---------------------------------------------------------------------------

class TestSummarizeTrades:
    def test_splits_no_and_yes(self):
        trades = [
            ArmTrade(station="KORD", ticker="t1", end_date="2026-09-20", side="NO",
                     price_cents=76, p_yes=0.02, poll_ts="x", bracket_low=80, bracket_high=85),
            ArmTrade(station="KORD", ticker="t2", end_date="2026-09-20", side="YES",
                     price_cents=30, p_yes=0.9, poll_ts="x", bracket_low=80, bracket_high=85),
        ]
        outcomes = {("KORD", "t1", "2026-09-20"): False, ("KORD", "t2", "2026-09-20"): True}
        s = summarize_trades(trades, outcomes)
        assert s.n_trades == 2
        assert s.n_wins == 2
        assert s.no_trades == 1 and s.no_pnl == pytest.approx(0.24)
        assert s.yes_trades == 1 and s.yes_pnl == pytest.approx(0.70)

    def test_unresolved_trade_is_skipped(self):
        trades = [
            ArmTrade(station="KORD", ticker="t1", end_date="2026-09-20", side="NO",
                     price_cents=76, p_yes=0.02, poll_ts="x", bracket_low=80, bracket_high=85),
        ]
        s = summarize_trades(trades, outcomes={})
        assert s.n_trades == 0
        assert s.total_pnl == 0.0


# ---------------------------------------------------------------------------
# load_same_day_polls
# ---------------------------------------------------------------------------

class TestLoadSameDayPolls:
    def _write_jsonl(self, path, rows):
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def test_excludes_next_day_and_low_direction_and_orders_by_ts(self, tmp_path):
        base = tmp_path / "bracket_evals.jsonl"
        self._write_jsonl(base, [
            {"poll_ts": "2026-09-16T10:00:00+00:00", "station": "KORD", "ticker": "t1",
             "settlement_date": "2026-09-16", "bracket_low": 70, "bracket_high": 75,
             "yes_ask": 0.3, "no_ask": 0.7, "p_yes_raw": 0.3, "minutes_to_settlement": 600,
             "direction": "high", "is_next_day": 0},
            {"poll_ts": "2026-09-16T08:00:00+00:00", "station": "KORD", "ticker": "t1",
             "settlement_date": "2026-09-16", "bracket_low": 70, "bracket_high": 75,
             "yes_ask": 0.35, "no_ask": 0.65, "p_yes_raw": 0.35, "minutes_to_settlement": 720,
             "direction": "high", "is_next_day": 0},
            # next-day -- excluded
            {"poll_ts": "2026-09-16T09:00:00+00:00", "station": "KORD", "ticker": "t2",
             "settlement_date": "2026-09-17", "bracket_low": 70, "bracket_high": 75,
             "yes_ask": 0.3, "no_ask": 0.7, "p_yes_raw": 0.3, "minutes_to_settlement": 1200,
             "direction": "high", "is_next_day": 1},
            # low direction -- excluded
            {"poll_ts": "2026-09-16T09:00:00+00:00", "station": "KORD", "ticker": "t3",
             "settlement_date": "2026-09-16", "bracket_low": 70, "bracket_high": 75,
             "yes_ask": 0.3, "no_ask": 0.7, "p_yes_raw": 0.3, "minutes_to_settlement": 600,
             "direction": "low", "is_next_day": 0},
        ])
        groups = load_same_day_polls(base, since="2026-09-01")
        assert set(groups.keys()) == {("KORD", "t1", "2026-09-16")}
        polls = groups[("KORD", "t1", "2026-09-16")]
        assert len(polls) == 2
        # Sorted ascending by ts: 08:00 before 10:00.
        assert polls[0]["ts"] == "2026-09-16T08:00:00+00:00"
        assert polls[1]["ts"] == "2026-09-16T10:00:00+00:00"

    def test_since_filter_drops_earlier_polls(self, tmp_path):
        base = tmp_path / "bracket_evals.jsonl"
        self._write_jsonl(base, [
            {"poll_ts": "2026-09-10T10:00:00+00:00", "station": "KORD", "ticker": "t1",
             "settlement_date": "2026-09-10", "bracket_low": 70, "bracket_high": 75,
             "yes_ask": 0.3, "no_ask": 0.7, "p_yes_raw": 0.3, "minutes_to_settlement": 600,
             "direction": "high", "is_next_day": 0},
        ])
        groups = load_same_day_polls(base, since="2026-09-15")
        assert groups == {}


# ---------------------------------------------------------------------------
# _fetch_dated_triples -- train/eval time split
# ---------------------------------------------------------------------------

def _seed_forecast_and_obs(db: Database) -> None:
    """20 training days (before cutoff) + 5 eval days (on/after cutoff),
    each with nws+open_meteo rows at lead 24 and an observed daily high."""
    for i in range(20):
        date = f"2026-08-{i + 1:02d}"
        db.upsert_forecast_log_v2(station=STATION, model="nws", date=date,
                                   forecast_high_f=70.0 + i, lead_hours=24,
                                   issued_at=f"{date}T00:00:00+00:00")
        db.upsert_forecast_log_v2(station=STATION, model="open_meteo", date=date,
                                   forecast_high_f=72.0 + i, lead_hours=24,
                                   issued_at=f"{date}T00:00:00+00:00")
        db.insert_observation(ts=f"{date}T18:00:00+00:00", station=STATION,
                               temp_f=75.0 + i, temp_native=75.0 + i, unit="F", source="metar")
    for i in range(5):
        date = f"2026-09-{i + 15:02d}"
        db.upsert_forecast_log_v2(station=STATION, model="nws", date=date,
                                   forecast_high_f=80.0 + i, lead_hours=24,
                                   issued_at=f"{date}T00:00:00+00:00")
        db.upsert_forecast_log_v2(station=STATION, model="open_meteo", date=date,
                                   forecast_high_f=82.0 + i, lead_hours=24,
                                   issued_at=f"{date}T00:00:00+00:00")
        db.insert_observation(ts=f"{date}T18:00:00+00:00", station=STATION,
                               temp_f=85.0 + i, temp_native=85.0 + i, unit="F", source="metar")


class TestFetchDatedTriples:
    def test_train_eval_split_is_disjoint_and_covers_everything(self, tmp_path):
        db = Database(str(tmp_path / "db.sqlite"))
        _seed_forecast_and_obs(db)
        train = _fetch_dated_triples(db, STATION, 24, REGIME, "fixed", date_hi="2026-09-15")
        evalset = _fetch_dated_triples(db, STATION, 24, REGIME, "fixed", date_lo="2026-09-15")
        assert len(train) == 20
        assert len(evalset) == 5
        train_dates = {d for d, *_ in train}
        eval_dates = {d for d, *_ in evalset}
        assert train_dates.isdisjoint(eval_dates)
        assert all(d < "2026-09-15" for d in train_dates)
        assert all(d >= "2026-09-15" for d in eval_dates)

    def test_sigma_fixed_uses_forecast_stddev_f(self, tmp_path):
        db = Database(str(tmp_path / "db.sqlite"))
        _seed_forecast_and_obs(db)
        train = _fetch_dated_triples(db, STATION, 24, REGIME, "fixed", date_hi="2026-09-15")
        for _d, _mu, sigma, _y in train:
            assert sigma == pytest.approx(FORECAST_STDDEV_F)


# ---------------------------------------------------------------------------
# _fit_with_pooling
# ---------------------------------------------------------------------------

class TestFitWithPooling:
    def test_full_weight_city_matches_plain_fit_emos(self, tmp_path):
        """>= FULL_WEIGHT_SAMPLES own triples -> identical to fit_emos on the
        city's own triples alone (no pooling contribution)."""
        db = Database(str(tmp_path / "db.sqlite"))
        for i in range(65):
            date = f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}"
            db.upsert_forecast_log_v2(station=STATION, model="nws", date=date,
                                       forecast_high_f=70.0 + (i % 10), lead_hours=24,
                                       issued_at=f"{date}T00:00:00+00:00")
            db.upsert_forecast_log_v2(station=STATION, model="open_meteo", date=date,
                                       forecast_high_f=72.0 + (i % 10), lead_hours=24,
                                       issued_at=f"{date}T00:00:00+00:00")
            db.insert_observation(ts=f"{date}T18:00:00+00:00", station=STATION,
                                   temp_f=75.0 + (i % 10), temp_native=75.0 + (i % 10),
                                   unit="F", source="metar")
        coeff = _fit_with_pooling(db, CITY, 24, REGIME, "fixed", "2027-01-01", pooled_cache={})
        own = _fetch_dated_triples(db, STATION, 24, REGIME, "fixed", date_hi="2027-01-01")
        expected = fit_emos([(mu, sigma, y) for _d, mu, sigma, y in own])
        assert coeff == pytest.approx(expected)

    def test_insufficient_samples_without_pooling_group_returns_none(self, tmp_path):
        db = Database(str(tmp_path / "db.sqlite"))
        # Only 2 own triples -- below POOLED_MIN_CITY_SAMPLES=5.
        for i in range(2):
            date = f"2026-08-{i + 1:02d}"
            db.upsert_forecast_log_v2(station=STATION, model="nws", date=date,
                                       forecast_high_f=70.0, lead_hours=24,
                                       issued_at=f"{date}T00:00:00+00:00")
            db.upsert_forecast_log_v2(station=STATION, model="open_meteo", date=date,
                                       forecast_high_f=72.0, lead_hours=24,
                                       issued_at=f"{date}T00:00:00+00:00")
            db.insert_observation(ts=f"{date}T18:00:00+00:00", station=STATION,
                                   temp_f=75.0, temp_native=75.0, unit="F", source="metar")
        coeff = _fit_with_pooling(db, CITY, 24, REGIME, "fixed", "2027-01-01", pooled_cache={})
        assert coeff is None

    def test_no_data_returns_none(self, tmp_path):
        db = Database(str(tmp_path / "db.sqlite"))
        coeff = _fit_with_pooling(db, CITY, 24, REGIME, "fixed", "2027-01-01", pooled_cache={})
        assert coeff is None


# ---------------------------------------------------------------------------
# _evaluate_poll / select_first_qualifying_trades -- integration
# ---------------------------------------------------------------------------

def _seed_poll_db(path) -> None:
    db = Database(str(path))
    date = "2026-09-20"
    for lead in (6, 24):
        db.upsert_forecast_log_v2(station=STATION, model="nws", date=date,
                                   forecast_high_f=80.0, lead_hours=lead,
                                   issued_at=f"{date}T00:00:00+00:00")
        db.upsert_forecast_log_v2(station=STATION, model="open_meteo", date=date,
                                   forecast_high_f=82.0, lead_hours=lead,
                                   issued_at=f"{date}T00:00:00+00:00")
    db.insert_observation(ts=f"{date}T12:00:00+00:00", station=STATION,
                           temp_f=78.0, temp_native=78.0, unit="F", source="metar")
    db.upsert_intraday_correction(
        city=CITY, station=STATION, source="metar", date=date,
        obs_time=f"{date}T12:00:00+00:00",
        obs_temp_f=78.0, model_temp_f=81.0, delta_f=-3.0,
        corrected_mu_f=78.0, decay_factor=0.3,
        basis_weights='{"open_meteo": 1.0}',
    )
    db._conn.commit()
    db._conn.close()


class TestEvaluatePoll:
    def test_no_side_trade_fires_with_strongly_no_favouring_coefficients(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_poll_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            # Coefficients that push mu_cal far from the bracket -> near-zero
            # p_yes -> NO side should qualify at a generous price/edge.
            coeffs = CityCoefficients(
                city=CITY, coeff_24h_only=(0.0, 1.0, 0.5, 1.0),
                by_lead={6: (0.0, 1.0, 0.5, 1.0), 24: (0.0, 1.0, 0.5, 1.0)},
            )
            cfg = _default_cfg()
            poll = PollCandidate(
                station=STATION, ticker="0xabc", end_date="2026-09-20",
                ts="2026-09-20T13:00:00+00:00",
                bracket_low=120.0, bracket_high=125.0,  # far above forecast -> NO favoured
                yes_ask=2, no_ask=75, minutes_to_settlement=300.0,
            )
            cache = OfflineEvalCache()
            result = _evaluate_poll(ro, poll, coeffs, "nearest_bin", cfg, cache=cache)
        finally:
            ro._conn.close()
        assert result is not None
        side, price, p_yes, ev = result
        assert side == "NO"
        assert price == 75

    def test_missing_coefficients_for_variant_returns_none(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_poll_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            coeffs = CityCoefficients(city=CITY, coeff_24h_only=None, by_lead={})
            cfg = _default_cfg()
            poll = PollCandidate(
                station=STATION, ticker="0xabc", end_date="2026-09-20",
                ts="2026-09-20T13:00:00+00:00",
                bracket_low=120.0, bracket_high=125.0,
                yes_ask=2, no_ask=92, minutes_to_settlement=300.0,
            )
            result = _evaluate_poll(ro, poll, coeffs, "24h_only", cfg, cache=OfflineEvalCache())
        finally:
            ro._conn.close()
        assert result is None

    def test_select_first_qualifying_trades_picks_earliest_poll(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        _seed_poll_db(db_path)
        ro = ReadOnlyDatabase(db_path)
        try:
            coeffs_by_city = {
                CITY: CityCoefficients(
                    city=CITY, coeff_24h_only=(0.0, 1.0, 0.5, 1.0),
                    by_lead={6: (0.0, 1.0, 0.5, 1.0), 24: (0.0, 1.0, 0.5, 1.0)},
                )
            }
            cfg = _default_cfg()
            groups = {
                (STATION, "0xabc", "2026-09-20"): [
                    {"ts": "2026-09-20T13:00:00+00:00", "bracket_low": 120.0,
                     "bracket_high": 125.0, "yes_ask": 2, "no_ask": 75,
                     "minutes_to_settlement": 300.0},
                    {"ts": "2026-09-20T14:00:00+00:00", "bracket_low": 120.0,
                     "bracket_high": 125.0, "yes_ask": 2, "no_ask": 75,
                     "minutes_to_settlement": 240.0},
                ]
            }
            trades = select_first_qualifying_trades(
                ro, groups, coeffs_by_city, "nearest_bin", cfg, cache=OfflineEvalCache(),
            )
        finally:
            ro._conn.close()
        assert len(trades) == 1
        assert trades[0].poll_ts == "2026-09-20T13:00:00+00:00"
        assert trades[0].side == "NO"


# ---------------------------------------------------------------------------
# attach_outcomes
# ---------------------------------------------------------------------------

class TestAttachOutcomes:
    def test_resolves_from_observed_daily_high_without_network(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        db = Database(str(db_path))
        db.insert_observation(ts="2026-09-20T18:00:00+00:00", station=STATION,
                               temp_f=82.0, temp_native=82.0, unit="F", source="metar")
        db._conn.commit()
        db._conn.close()

        trades = [
            ArmTrade(station=STATION, ticker="0xabc", end_date="2026-09-20", side="NO",
                     price_cents=92, p_yes=0.02, poll_ts="x",
                     bracket_low=120.0, bracket_high=125.0),
        ]
        outcomes = attach_outcomes(trades, db_path, use_gamma=False, allow_network=False)
        # Observed high (82) is well below the bracket [120, 125) -> YES did
        # not win -> NO trade wins.
        assert outcomes[(STATION, "0xabc", "2026-09-20")] is False

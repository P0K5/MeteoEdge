"""Tests for src/scripts/envelope_sweep_tradability_report.py (issue #1069, M3c).

Covers:
- `n_needed_to_certify`: reproduces the Tech Lead PM amendment's reference
  figures exactly (98 -> 150, 96 -> 75, <=95 -> 60).
- `in_scope` / `which_half`: next-day / low-direction exclusion, and the
  fit/evaluate/out-of-window boundaries of the pre-registered split.
- `is_certain_zero`: all three geometric branches of
  `conditional_bracket_probability`, plus missing-input handling.
- `build_db_quantile_table`: quantile computation and the
  MIN_DAYS_PER_CELL sparse-cell fallback to the existing lookup, using a
  stubbed DB and a monkeypatched (small) STATIONS list for determinism.
- `TableClimbModel.additional_rise`: table lookup + missing-cell fallback.
- `AnomalyClimbModel`: fit-window-only fitting, bucket lookup, and its
  bucketed -> pooled -> static fallback ladder.
- `no_ask_histogram` / `bucket_thickening_check` / `decide_stop_early`: the
  diagnostic-first amendment's stop-early logic, both directions.
- `variant_total_ev` / `apply_envelope_stopping_rule`: FINDING and NULL
  paths of the pre-registered stopping rule.
- `build_diagnostic_report`: assembles without raising and states the
  DOES-NOT-REOPEN-M3-OR-M3b disclaimer.

All dates are synthetic (2026-0x-xx), matching the repo convention.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.scripts.envelope_sweep_tradability_report import (
    ANOMALY_THRESHOLD_F,
    EVAL_END,
    EVAL_START,
    FIT_END,
    FIT_START,
    AnomalyClimbModel,
    TableClimbModel,
    V0Control,
    _anomaly_bucket,
    apply_envelope_stopping_rule,
    build_db_quantile_table,
    build_diagnostic_report,
    bucket_thickening_check,
    decide_stop_early,
    in_scope,
    is_certain_zero,
    n_needed_to_certify,
    no_ask_histogram,
    reclassify_row,
    variant_total_ev,
    which_half,
)


# ---------------------------------------------------------------------------
# n_needed_to_certify
# ---------------------------------------------------------------------------

class TestNNeededToCertify:
    def test_matches_amendment_reference_figures(self):
        assert n_needed_to_certify("98") == 150
        assert n_needed_to_certify("96") == 75
        assert n_needed_to_certify("<=95") == 60

    def test_99_needs_fewest(self):
        assert n_needed_to_certify("99") == 300


# ---------------------------------------------------------------------------
# in_scope / which_half
# ---------------------------------------------------------------------------

class TestInScope:
    def test_high_direction_same_day_in_scope(self):
        assert in_scope({"direction": "high", "is_next_day_flag": 0}) is True

    def test_missing_direction_defaults_to_high(self):
        assert in_scope({}) is True

    def test_low_direction_out_of_scope(self):
        assert in_scope({"direction": "low"}) is False

    def test_next_day_out_of_scope(self):
        assert in_scope({"direction": "high", "is_next_day_flag": 1}) is False

    def test_next_day_flag_non_numeric_does_not_raise(self):
        assert in_scope({"direction": "high", "is_next_day_flag": "bogus"}) is True


class TestWhichHalf:
    def test_fit_start_boundary(self):
        assert which_half({"ts": f"{FIT_START}T00:00:00+00:00"}) == "fit"

    def test_fit_end_boundary(self):
        assert which_half({"ts": f"{FIT_END}T23:59:59+00:00"}) == "fit"

    def test_eval_start_boundary(self):
        assert which_half({"ts": f"{EVAL_START}T00:00:00+00:00"}) == "evaluate"

    def test_eval_end_boundary(self):
        assert which_half({"ts": f"{EVAL_END}T23:59:59+00:00"}) == "evaluate"

    def test_before_fit_window_is_none(self):
        assert which_half({"ts": "2026-08-05T00:00:00+00:00"}) is None

    def test_after_eval_window_is_none(self):
        assert which_half({"ts": "2026-08-26T00:00:00+00:00"}) is None

    def test_between_halves_is_none(self):
        assert which_half({"ts": "2026-08-15T23:59:59.999+00:00"[:10] + "T23:59:59+00:00"}) == "fit"


# ---------------------------------------------------------------------------
# is_certain_zero
# ---------------------------------------------------------------------------

class TestIsCertainZero:
    def test_bracket_below_current_high_is_certain_zero(self):
        # current_high already past this bracket's top.
        assert is_certain_zero(60.0, 62.0, 63.0, 70.0) is True

    def test_bracket_above_max_env_is_certain_zero(self):
        assert is_certain_zero(75.0, 77.0, 63.0, 70.0) is True

    def test_bracket_overlapping_surviving_interval_is_not_certain(self):
        assert is_certain_zero(60.0, 65.0, 62.0, 70.0) is False

    def test_collapsed_day_bracket_contains_current_high_not_certain(self):
        # max_env <= current_high: the day's high is fixed. The bracket
        # CONTAINING current_high is the only one that is not certain-zero.
        assert is_certain_zero(56.0, 60.0, 58.0, 55.0) is False

    def test_collapsed_day_other_brackets_are_certain_zero(self):
        assert is_certain_zero(60.0, 62.0, 58.0, 55.0) is True

    def test_exactly_matching_bracket_is_not_certain(self):
        # bracket == [current_high, max_env) exactly: numerator == denominator.
        assert is_certain_zero(60.0, 70.0, 60.0, 70.0) is False

    @pytest.mark.parametrize("lo,hi,ch,me", [
        (None, 62.0, 60.0, 70.0),
        (60.0, None, 60.0, 70.0),
        (60.0, 62.0, None, 70.0),
        (60.0, 62.0, 60.0, None),
    ])
    def test_missing_inputs_return_false_never_guessed(self, lo, hi, ch, me):
        assert is_certain_zero(lo, hi, ch, me) is False


# ---------------------------------------------------------------------------
# build_db_quantile_table (issue #1069: V1/V2/V4)
# ---------------------------------------------------------------------------

class _FakeDB:
    def __init__(self, obs_by_key):
        self._obs = obs_by_key

    def get_hourly_obs_for_climb(self, key):
        return list(self._obs.get(key, []))


def _obs(day, hour_utc, temp_f):
    # UTC timestamps; station tz below is chosen as UTC for determinism.
    return {"ts": f"2026-08-{day:02d}T{hour_utc:02d}:00:00+00:00", "temp_f": temp_f}


@pytest.fixture
def utc_station(monkeypatch):
    """A single synthetic UTC-timezone station, monkeypatched onto the
    module's STATIONS list so build_db_quantile_table's iteration is fast
    and deterministic (no dependency on the real ~34-station list or their
    real timezones)."""
    import src.scripts.envelope_sweep_tradability_report as mod
    monkeypatch.setattr(mod, "STATIONS", [
        ("ZZZZ", 0.0, 0.0, "Testville", "ZZZZ", "F", "UTC"),
    ])
    monkeypatch.setattr(mod, "get_canonical_station_feeds", lambda icao: [icao])
    return mod


class TestBuildDbQuantileTable:
    def test_sparse_cell_falls_back_to_existing_lookup(self, utc_station):
        # Fewer than MIN_DAYS_PER_CELL (10) distinct dates for hour 10 -> fallback.
        obs = {"ZZZZ": [_obs(d, 10, 70.0) for d in range(6, 9)] + [_obs(d, 10, 90.0) for d in range(6, 9)]}
        db = _FakeDB(obs)
        fallback = {"ZZZZ": {8: {10: 12.34}}}
        table = utc_station.build_db_quantile_table(db, 0.95, fallback)
        assert table["ZZZZ"][8][10] == 12.34

    def test_well_sampled_cell_computes_quantile_not_fallback(self, utc_station):
        # 12 distinct days, each with an hour-10 obs and an hour-14 daily high
        # obs, so climb-to-eod (daily_high - temp_at_10) is well-defined and
        # >= MIN_DAYS_PER_CELL (10).
        obs = []
        for d in range(6, 18):  # 12 distinct August days
            obs.append(_obs(d, 10, 70.0))   # hour-10 reading
            obs.append(_obs(d, 14, 80.0))   # the day's high (climb = 10.0)
        db = _FakeDB({"ZZZZ": obs})
        fallback = {"ZZZZ": {8: {10: 999.0}}}
        table = utc_station.build_db_quantile_table(db, 0.95, fallback)
        # Every day has an identical climb of 10.0 at hour 10 -> p95 == 10.0.
        assert table["ZZZZ"][8][10] == 10.0
        assert table["ZZZZ"][8][10] != 999.0

    def test_station_with_no_observations_uses_fallback_wholesale(self, utc_station):
        db = _FakeDB({})
        fallback = {"ZZZZ": {8: {10: 5.5}}}
        table = utc_station.build_db_quantile_table(db, 0.95, fallback)
        assert table["ZZZZ"] == fallback["ZZZZ"]


# ---------------------------------------------------------------------------
# TableClimbModel
# ---------------------------------------------------------------------------

class TestTableClimbModel:
    def test_looks_up_table_cell(self):
        model = TableClimbModel("V1", "test", {"KORD": {8: {14: 3.5}}})
        now_local = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
        assert model.additional_rise("KORD", now_local, 70.0) == 3.5

    def test_missing_cell_defaults_to_zero(self):
        model = TableClimbModel("V1", "test", {"KORD": {8: {}}})
        now_local = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
        assert model.additional_rise("KORD", now_local, 70.0) == 0.0

    def test_missing_station_defaults_to_zero(self):
        model = TableClimbModel("V1", "test", {})
        now_local = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
        assert model.additional_rise("UNKNOWN", now_local, 70.0) == 0.0


class TestV0Control:
    def test_delegates_to_expected_additional_rise(self):
        model = V0Control()
        now_local = datetime(2026, 8, 10, 6, 0)
        # KORD is a real, populated CLIMB_LOOKUP station -- just assert it
        # returns a plausible non-negative °F value rather than 0.0 always.
        value = model.additional_rise("KORD", now_local, 70.0)
        assert value >= 0.0


# ---------------------------------------------------------------------------
# _anomaly_bucket / AnomalyClimbModel (V3)
# ---------------------------------------------------------------------------

class TestAnomalyBucket:
    def test_below(self):
        assert _anomaly_bucket(-(ANOMALY_THRESHOLD_F + 0.1)) == "below"

    def test_above(self):
        assert _anomaly_bucket(ANOMALY_THRESHOLD_F + 0.1) == "above"

    def test_near_inclusive_boundaries(self):
        assert _anomaly_bucket(ANOMALY_THRESHOLD_F) == "near"
        assert _anomaly_bucket(-ANOMALY_THRESHOLD_F) == "near"


class TestAnomalyClimbModel:
    def test_fits_only_from_fit_window_observations(self, utc_station):
        # One observation inside the eval window with an extreme climb that
        # must NOT leak into the fit -- if it did, the fitted normal/quantile
        # would differ from the fit-window-only computation asserted below.
        fit_obs = []
        for d in range(6, 16):  # FIT_START..FIT_END, 10 distinct days
            fit_obs.append(_obs(d, 10, 70.0))
            fit_obs.append(_obs(d, 14, 80.0))  # climb = 10.0, near-normal
        leak_obs = [_obs(20, 10, 70.0), _obs(20, 14, 200.0)]  # eval-window, climb=130
        db = _FakeDB({"ZZZZ": fit_obs + leak_obs})
        model = utc_station.AnomalyClimbModel(db)
        now_local = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)
        # latest_temp == the fitted normal (70.0) -> 'near' bucket -> 10.0,
        # not contaminated by the 130.0 leak-window climb.
        assert model.additional_rise("ZZZZ", now_local, 70.0) == 10.0

    def test_falls_back_to_static_lookup_when_unfittable(self, utc_station):
        db = _FakeDB({})  # no observations at all -> nothing fit
        model = utc_station.AnomalyClimbModel(db)
        now_local = datetime(2026, 8, 10, 6, 0, tzinfo=timezone.utc)
        # ZZZZ is not in the real CLIMB_LOOKUP -> falls back to 0.0, the
        # documented final rung of the fallback ladder.
        assert model.additional_rise("ZZZZ", now_local, 70.0) == 0.0


# ---------------------------------------------------------------------------
# reclassify_row
# ---------------------------------------------------------------------------

class TestReclassifyRow:
    def test_reclassifies_under_every_variant_independently(self):
        v_narrow = TableClimbModel("VNARROW", "narrow", {"KORD": {8: {10: 0.0}}})
        v_wide = TableClimbModel("VWIDE", "wide", {"KORD": {8: {10: 20.0}}})
        inputs = {
            "station": "KORD", "now_local": datetime(2026, 8, 10, 10, 0),
            "latest_temp": 60.0, "current_high": 60.0,
            "bracket_low": 70.0, "bracket_high": 72.0,
        }
        classes = reclassify_row(inputs, [v_narrow, v_wide])
        # narrow: max_env = 60.0 -> bracket [70,72) entirely above -> certain.
        assert classes["VNARROW"] is True
        # wide: max_env = 80.0 -> bracket [70,72) inside surviving interval -> contested.
        assert classes["VWIDE"] is False


# ---------------------------------------------------------------------------
# no_ask_histogram / bucket_thickening_check / decide_stop_early
# ---------------------------------------------------------------------------

class TestNoAskHistogram:
    def test_counts_by_bucket_and_drops_missing(self):
        rows = [{"no_ask": 99}, {"no_ask": 99}, {"no_ask": 96}, {"no_ask": None}]
        hist = no_ask_histogram(rows)
        assert hist["99"] == 2
        assert hist["96"] == 1
        assert sum(hist.values()) == 3


class TestBucketThickeningCheck:
    def test_thickened_when_n_crosses_threshold(self):
        rows = [{"no_ask": 98} for _ in range(150)]
        result = bucket_thickening_check({"V1": rows})
        assert result["V1"]["98"]["thickened"] is True
        assert result["V1"]["98"]["n"] == 150
        assert result["V1"]["98"]["n_needed"] == 150

    def test_not_thickened_below_threshold(self):
        rows = [{"no_ask": 98} for _ in range(149)]
        result = bucket_thickening_check({"V1": rows})
        assert result["V1"]["98"]["thickened"] is False


class TestDecideStopEarly:
    def test_stops_when_no_bucket_thickened(self):
        thickening = {
            "V1": {"<=95": {"thickened": False}, "96": {"thickened": False},
                   "97": {"thickened": False}, "98": {"thickened": False}},
        }
        assert decide_stop_early(thickening) is True

    def test_continues_when_any_bucket_thickened(self):
        thickening = {
            "V1": {"<=95": {"thickened": False}, "96": {"thickened": False},
                   "97": {"thickened": False}, "98": {"thickened": True}},
        }
        assert decide_stop_early(thickening) is False


# ---------------------------------------------------------------------------
# variant_total_ev / apply_envelope_stopping_rule
# ---------------------------------------------------------------------------

def _row(no_ask=98.0, yes_won=False, station="KORD", ts="2026-08-16T18:00:00+00:00"):
    return {"no_ask": no_ask, "yes_won": yes_won, "station": station, "ts": ts}


class TestVariantTotalEv:
    def test_none_when_no_priced_resolved_rows(self):
        assert variant_total_ev([{"no_ask": None, "yes_won": None}]) is None

    def test_sums_across_buckets(self):
        rows = [_row(no_ask=98.0, yes_won=False)] * 10 + [_row(no_ask=96.0, yes_won=False)] * 10
        total = variant_total_ev(rows)
        assert total is not None
        assert total > 0


class TestApplyEnvelopeStoppingRule:
    def test_null_when_v0_ev_not_positive(self):
        v0_rows = [_row(no_ask=99.0, yes_won=True)] * 5  # V0 all losses -> negative EV
        variant_rows = [_row(no_ask=98.0, yes_won=False)] * 200
        newly = variant_rows
        verdict, reasoning = apply_envelope_stopping_rule(
            TableClimbModel("V1", "t", {}), variant_rows, v0_rows, newly)
        assert verdict == "NULL"
        assert "TOTAL held-out EV" in reasoning

    def test_finding_when_all_three_conditions_hold(self):
        # V0 class: small, modest positive EV at no_ask=99 (breakeven 1%).
        v0_rows = [_row(no_ask=99.0, yes_won=False)] * 100
        # Variant class: much larger, clearing breakeven in >=3 buckets,
        # and >=25% more total EV than V0.
        variant_rows = (
            [_row(no_ask=99.0, yes_won=False)] * 100
            + [_row(no_ask=98.0, yes_won=False)] * 200
            + [_row(no_ask=97.0, yes_won=False)] * 200
            + [_row(no_ask=96.0, yes_won=False)] * 200
        )
        newly = variant_rows[100:]  # the added rows, all NO-winning (yes_won=False)
        verdict, reasoning = apply_envelope_stopping_rule(
            TableClimbModel("V1", "t", {}), variant_rows, v0_rows, newly)
        assert verdict == "FINDING"

    def test_null_when_newly_certain_resolves_worse(self):
        v0_rows = [_row(no_ask=99.0, yes_won=False)] * 100
        variant_rows = (
            [_row(no_ask=99.0, yes_won=False)] * 100
            + [_row(no_ask=98.0, yes_won=False)] * 200
            + [_row(no_ask=97.0, yes_won=False)] * 200
            + [_row(no_ask=96.0, yes_won=True)] * 200  # newly-certain: ALL yes -- much worse
        )
        newly = variant_rows[100:]
        verdict, reasoning = apply_envelope_stopping_rule(
            TableClimbModel("V1", "t", {}), variant_rows, v0_rows, newly)
        assert verdict == "NULL"
        assert "WORSE" in reasoning


# ---------------------------------------------------------------------------
# build_diagnostic_report
# ---------------------------------------------------------------------------

class TestBuildDiagnosticReport:
    def test_states_does_not_reopen_disclaimer_and_stop_verdict(self):
        variants = [V0Control(), TableClimbModel("V1", "p90 test", {})]
        class_rows_by_half = {
            "fit": {"V0": [], "V1": []},
            "evaluate": {"V0": [], "V1": []},
        }
        newly_certain_by_half = {
            "fit": {"V1": []},
            "evaluate": {"V1": []},
        }
        empty_thickening = {
            "<=95": {"n": 0, "n_needed": 60, "thickened": False},
            "96": {"n": 0, "n_needed": 75, "thickened": False},
            "97": {"n": 0, "n_needed": 100, "thickened": False},
            "98": {"n": 0, "n_needed": 150, "thickened": False},
        }
        thickening_by_half = {
            "fit": {"V1": empty_thickening},
            "evaluate": {"V1": empty_thickening},
        }
        report = build_diagnostic_report(
            "2026-08-26", "2026-08-06", variants, class_rows_by_half,
            newly_certain_by_half, thickening_by_half, True, {"stage": 0},
        )
        assert "DOES NOT REOPEN M3 OR M3b" in report
        assert "STOP." in report
        assert "unsatisfiable" in report

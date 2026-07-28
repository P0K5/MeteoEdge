"""Tests for src/scripts/post_fix_model_health.py (issue #869).

This report is a LEADING INDICATOR, not a skill test — it scores the
prediction side only, so it works with no settled outcomes at all. The tests
therefore focus on the three measurements and on the report making a
pass/fail readable against the pre-fix baselines:

- rail_concentration: the headline. Share of p_yes_raw at <=0.02 / >=0.95,
  the buckets where the pre-fix model kept 62.6% of its mass.
- zero_artifact_rate: #820's exact-0.0 certainty shortcut, broken out by
  is_next_day and station-local hour (its diagnosed home is the evening) --
  reference context only, not a pass/fail (see interior_zero_violations).
- interior_zero_violations: the M0 structural invariant. A finite envelope
  produces exact-zero brackets only as a contiguous run at the top/bottom of a
  station-day's sorted ladder; a zero with non-zero brackets on both sides is a
  gap no envelope can produce. Replaced two earlier, wrong versions -- a raw
  rate compared against a mismatched-population baseline, then a "zero before
  the settlement day" heuristic that conflated the bug with ordinary envelope
  truncation and pooled genuinely pre-fix rows. This version needs neither.
- emos_d_coefficients / sigma_f_coverage: #799's identifiability question,
  grouped by sigma_source so constant-sigma and ensemble-sigma fits are never
  averaged together.
- Self-gating: no bracket_evals data -> no report written, ever; a missing
  database degrades check 3 rather than killing the report.

All dates are synthetic (2026-0x-xx), matching the repo convention of never
anchoring test fixtures to a real "today".
"""
from __future__ import annotations

import json
import sqlite3

from src.scripts.post_fix_model_health import (
    BASELINE_RAIL_SHARE,
    D_IDENTIFIABLE_THRESHOLD,
    build_report,
    emos_d_coefficients,
    interior_zero_violations,
    ladder_size,
    load_bracket_eval_rows,
    rail_concentration,
    rail_concentration_by_mode,
    run_report,
    sigma_f_coverage,
    structural_high_rail_ceiling,
    zero_artifact_rate,
)


def _eval_row(**overrides) -> dict:
    base = {
        "station": "KORD",
        "ticker": "0xabc001",
        "bracket_low": 70.0,
        "bracket_high": 72.0,
        "poll_ts": "2026-02-01T18:00:00+00:00",
        "yes_ask": 20.0,
        "no_ask": 82.0,
        "p_yes": 0.2,
        "p_yes_raw": 0.2,
        "emos_mode": "emos_shadow",
        "is_next_day": 0,
        "minutes_to_settlement": 300.0,
        "execution_mode": "paper",
        "settlement_date": "2026-02-01",
    }
    base.update(overrides)
    return base


def _write_evals(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _write_db(path, emos_rows=None, forecast_rows=None):
    """emos_rows: (city, sigma_source, c, d). forecast_rows: (model, sigma_f)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE emos_calibration (city TEXT, model_mode TEXT, forecast_source TEXT, "
        "sigma_source TEXT, lead_hours INTEGER, a REAL, b REAL, c REAL, d REAL, "
        "crps_score REAL, ready_for_promotion INTEGER, trained_at TEXT)"
    )
    for city, sigma_source, c, d in (emos_rows or []):
        con.execute(
            "INSERT INTO emos_calibration (city, model_mode, forecast_source, sigma_source, "
            "lead_hours, a, b, c, d, crps_score, ready_for_promotion, trained_at) "
            "VALUES (?, 'emos_shadow', 'open_meteo', ?, 24, 0.1, 1.0, ?, ?, 2.7, 0, "
            "'2026-02-01T00:00:00Z')",
            (city, sigma_source, c, d),
        )
    con.execute(
        "CREATE TABLE model_forecast_log (id INTEGER PRIMARY KEY, station TEXT, model TEXT, "
        "date TEXT, forecast_high_f REAL, logged_at TEXT, lead_hours INTEGER, "
        "issued_at TEXT, sigma_f REAL)"
    )
    for model, sigma_f in (forecast_rows or []):
        con.execute(
            "INSERT INTO model_forecast_log (station, model, date, forecast_high_f, "
            "logged_at, sigma_f) VALUES ('KORD', ?, '2026-02-01', 75.0, "
            "'2026-02-01T00:00:00Z', ?)",
            (model, sigma_f),
        )
    con.commit()
    con.close()


class TestRailConcentration:
    def test_counts_both_rails(self):
        rows = [
            _eval_row(p_yes_raw=0.0), _eval_row(p_yes_raw=0.01), _eval_row(p_yes_raw=0.02),
            _eval_row(p_yes_raw=0.99), _eval_row(p_yes_raw=1.0),
            _eval_row(p_yes_raw=0.45),
        ]
        out = rail_concentration(rows)
        assert out["n"] == 6
        assert out["n_low_rail"] == 3      # 0.0, 0.01, 0.02 (inclusive)
        assert out["n_high_rail"] == 2     # 0.99, 1.0
        assert out["rail_share"] == 5 / 6

    def test_p_yes_raw_of_exactly_one_is_not_dropped(self):
        """A p_yes_raw of exactly 1.0 sits on the top bucket's closed edge --
        a half-open scan would silently lose the model's most confident calls."""
        out = rail_concentration([_eval_row(p_yes_raw=1.0)])
        assert out["n_high_rail"] == 1
        assert out["histogram"][-1]["n"] == 1

    def test_missing_p_yes_raw_is_excluded_not_counted_as_zero(self):
        out = rail_concentration([_eval_row(p_yes_raw=None), _eval_row(p_yes_raw=0.5)])
        assert out["n"] == 1
        assert out["rail_share"] == 0.0

    def test_no_usable_rows_returns_none_shares_not_zero(self):
        """Zero would read as 'no rail concentration', which is a finding.
        Absent data is not a finding."""
        out = rail_concentration([])
        assert out["n"] == 0
        assert out["rail_share"] is None

    def test_split_by_emos_mode(self):
        rows = [
            _eval_row(emos_mode="emos_primary", p_yes_raw=0.0),
            _eval_row(emos_mode="emos_primary", p_yes_raw=0.0),
            _eval_row(emos_mode="legacy", p_yes_raw=0.5),
        ]
        by_mode = rail_concentration_by_mode(rows)
        assert by_mode["emos_primary"]["rail_share"] == 1.0
        assert by_mode["legacy"]["rail_share"] == 0.0


class TestZeroArtifactRate:
    def test_only_exact_zero_counts(self):
        """0.001 is a (bad) forecast; 0.0 is the #820 certainty shortcut."""
        rows = [_eval_row(p_yes_raw=0.0), _eval_row(p_yes_raw=0.001),
                _eval_row(p_yes_raw=0.5)]
        out = zero_artifact_rate(rows)
        assert out["n_zero"] == 1
        assert out["rate"] == 1 / 3

    def test_split_by_next_day(self):
        rows = [
            _eval_row(is_next_day=1, p_yes_raw=0.0),
            _eval_row(is_next_day=1, p_yes_raw=0.3),
            _eval_row(is_next_day=0, p_yes_raw=0.4),
        ]
        out = zero_artifact_rate(rows)
        assert out["by_next_day"]["next_day"]["rate"] == 0.5
        assert out["by_next_day"]["same_day"]["rate"] == 0.0

    def test_local_hour_uses_station_timezone_not_utc(self):
        """#820's artifact lives in the station's EVENING. 2026-02-01T01:00Z is
        19:00 the previous day in Chicago -- exactly the window -- and bucketing
        it under UTC hour 1 would hide the residue the check exists to find."""
        out = zero_artifact_rate(
            [_eval_row(station="KORD", poll_ts="2026-02-01T01:00:00+00:00", p_yes_raw=0.0)]
        )
        assert 19 in out["by_local_hour"]
        assert out["by_local_hour"][19]["n_zero"] == 1

    def test_unknown_station_timezone_is_skipped_not_crashed(self):
        out = zero_artifact_rate([_eval_row(station="ZZZZ", p_yes_raw=0.0)])
        assert out["n_zero"] == 1
        assert out["by_local_hour"] == {}


class TestSigmaIdentifiability:
    def test_d_grouped_by_sigma_source(self, tmp_path):
        """Constant-sigma and ensemble-sigma fits are different populations
        (#848) and must never be averaged into one verdict."""
        db = tmp_path / "meteoedge.db"
        _write_db(db, emos_rows=[
            ("chicago", "constant", 1.0, 0.001),
            ("london", "constant", 1.0, 0.0009),
            ("chicago", "ensemble", 1.0, 0.42),
        ])
        out = emos_d_coefficients(db)
        assert out["available"] is True
        assert out["by_sigma_source"]["constant"]["n_identifiable"] == 0
        assert out["by_sigma_source"]["ensemble"]["n_identifiable"] == 1

    def test_threshold_is_an_order_of_magnitude_above_the_baseline(self, tmp_path):
        """A d that merely wobbled off 0.001 must not read as 'identifiable'."""
        db = tmp_path / "meteoedge.db"
        _write_db(db, emos_rows=[("chicago", "ensemble", 1.0, D_IDENTIFIABLE_THRESHOLD)])
        out = emos_d_coefficients(db)
        assert out["by_sigma_source"]["ensemble"]["n_identifiable"] == 0

    def test_missing_db_reports_unavailable_not_empty(self, tmp_path):
        out = emos_d_coefficients(tmp_path / "nope.db")
        assert out["available"] is False

    def test_sigma_f_coverage_by_model(self, tmp_path):
        db = tmp_path / "meteoedge.db"
        _write_db(db, forecast_rows=[
            ("open_meteo", 1.5), ("open_meteo", None), ("gefs", 2.0),
        ])
        out = sigma_f_coverage(db)
        assert out["by_model"]["open_meteo"]["coverage"] == 0.5
        assert out["by_model"]["gefs"]["coverage"] == 1.0


class TestLoadAndFilter:
    def test_since_filters_on_settlement_date(self, tmp_path):
        path = tmp_path / "logs" / "bracket_evals.jsonl"
        _write_evals(path, [
            _eval_row(settlement_date="2026-02-01"),
            _eval_row(settlement_date="2026-02-05"),
        ])
        assert len(load_bracket_eval_rows(path)) == 2
        assert len(load_bracket_eval_rows(path, since="2026-02-03")) == 1


class TestEndToEnd:
    def test_no_data_writes_no_report(self, tmp_path):
        rc = run_report(
            bracket_evals_base=tmp_path / "logs" / "bracket_evals.jsonl",
            db_path=tmp_path / "data" / "meteoedge.db",
            out_dir=tmp_path / "backtest_results",
        )
        assert rc == 0
        assert not (tmp_path / "backtest_results").exists()

    def test_missing_db_degrades_check_3_but_still_writes(self, tmp_path):
        """Checks 1 and 2 read only the JSONL log, so the headline finding must
        survive a missing database."""
        path = tmp_path / "logs" / "bracket_evals.jsonl"
        _write_evals(path, [_eval_row(p_yes_raw=0.0), _eval_row(p_yes_raw=0.5)])
        out_dir = tmp_path / "backtest_results"
        rc = run_report(
            bracket_evals_base=path, db_path=tmp_path / "nope.db",
            out_dir=out_dir, run_date="2026-02-15",
        )
        assert rc == 0
        text = (out_dir / "post_fix_model_health_2026-02-15.md").read_text()
        assert "## 1. Sharpness" in text
        assert "`emos_calibration` unavailable" in text

    def test_report_marks_pass1_figures_as_not_comparable(self, tmp_path):
        """The defect this replaced: Pass-1's gate-selected baselines were
        presented as a direct comparison against full-ladder numbers, and the
        2026-07-28 run duly read as a catastrophic regression that was mostly
        population. They may still appear -- as labelled context only."""
        path = tmp_path / "logs" / "bracket_evals.jsonl"
        _write_evals(path, [
            _eval_row(ticker="0x1", p_yes_raw=0.0), _eval_row(ticker="0x2", p_yes_raw=1.0),
            _eval_row(ticker="0x3", p_yes_raw=0.45), _eval_row(ticker="0x4", p_yes_raw=0.55),
        ])
        db = tmp_path / "data" / "meteoedge.db"
        _write_db(db, emos_rows=[("chicago", "ensemble", 1.0, 0.42)],
                  forecast_rows=[("gefs", 2.0)])
        out_dir = tmp_path / "backtest_results"
        rc = run_report(bracket_evals_base=path, db_path=db, out_dir=out_dir,
                        run_date="2026-02-15")
        assert rc == 0
        text = (out_dir / "post_fix_model_health_2026-02-15.md").read_text()

        assert "NOT A SKILL TEST" in text
        assert "not directly comparable" in text.lower()
        assert "different population -- not a comparison" in text
        assert "structural ceiling" in text
        assert "Middle mass" in text
        assert "## What a pass and a fail look like" in text
        assert "| ensemble |" in text

    def test_no_usable_p_yes_raw_writes_no_report(self, tmp_path):
        path = tmp_path / "logs" / "bracket_evals.jsonl"
        _write_evals(path, [_eval_row(p_yes_raw=None)])
        rc = run_report(
            bracket_evals_base=path, db_path=tmp_path / "nope.db",
            out_dir=tmp_path / "backtest_results",
        )
        assert rc == 0
        assert not (tmp_path / "backtest_results").exists()


class TestBuildReportDirectly:
    def test_baseline_constant_is_used_not_hardcoded_in_prose(self):
        """Guards against the baseline drifting between the constant and the
        text a reader actually sees."""
        rails = rail_concentration([_eval_row(p_yes_raw=0.0)])
        report = build_report(
            rails, {}, zero_artifact_rate([_eval_row(p_yes_raw=0.0)]),
            {"available": False, "by_sigma_source": {}},
            {"available": False, "by_model": {}}, "2026-02-15", None,
        )
        assert f"{BASELINE_RAIL_SHARE:.1%}" in report


class TestMiddleMassAndCeiling:
    """The two population-robust measures that replaced the invalid
    cross-population baseline comparison."""

    def test_middle_mass_counts_the_range_the_pre_fix_model_never_used(self):
        rows = [
            _eval_row(p_yes_raw=0.0),    # rail
            _eval_row(p_yes_raw=0.30),   # middle
            _eval_row(p_yes_raw=0.60),   # middle
            _eval_row(p_yes_raw=1.0),    # rail
        ]
        assert rail_concentration(rows)["middle_share"] == 0.5

    def test_middle_mass_excludes_the_rails_at_its_own_edges(self):
        rows = [_eval_row(p_yes_raw=0.049), _eval_row(p_yes_raw=0.95)]
        assert rail_concentration(rows)["middle_share"] == 0.0

    def test_ladder_size_counts_distinct_tickers_per_station_day(self):
        """Rows are per POLL -- a bracket polled 20 times is still one bracket."""
        rows = (
            [_eval_row(ticker="0x1") for _ in range(20)]
            + [_eval_row(ticker="0x2") for _ in range(20)]
            + [_eval_row(ticker="0x3")]
        )
        assert ladder_size(rows) == 3

    def test_structural_ceiling_is_one_over_ladder_size(self):
        """At most one bracket on a station-day can contain the daily high, so a
        maximally overconfident model tops out at 1/N near-certain YES calls."""
        rows = [_eval_row(ticker=f"0x{i}") for i in range(11)]
        assert structural_high_rail_ceiling(rows) == 1 / 11

    def test_ceiling_is_none_when_ladder_is_unknowable(self):
        assert structural_high_rail_ceiling([]) is None

    def test_report_states_high_rail_as_a_fraction_of_the_ceiling(self):
        """0.4% against a 9.1% ceiling is the number that carries the verdict --
        composition explains 29.2% -> ~9%, not ~9% -> 0.4%."""
        rows = [_eval_row(ticker=f"0x{i}", p_yes_raw=0.30) for i in range(11)]
        rows[0]["p_yes_raw"] = 1.0     # 1 of 11 at the high rail == the ceiling
        rails = rail_concentration(rows)
        report = build_report(
            rails, {}, zero_artifact_rate(rows),
            {"available": False, "by_sigma_source": {}},
            {"available": False, "by_model": {}}, "2026-02-15", None,
            inv=interior_zero_violations(rows), rows_for_ceiling=rows,
        )
        assert "structural ceiling (1 / 11-bracket ladder)" in report
        assert "**100%** of the ceiling" in report


def _ladder(zeros_at, n=5, station="KORD", settlement_date="2026-02-01",
            poll_ts="2026-02-01T18:00:00+00:00"):
    """n brackets at consecutive 2-degree steps, ticker-per-bracket, all in one
    poll snapshot. zeros_at: set of bracket indices (0-based, sorted by
    bracket_low) that price at exactly 0.0; every other bracket gets 0.3."""
    rows = []
    for i in range(n):
        rows.append(_eval_row(
            ticker=f"0x{i}", station=station, settlement_date=settlement_date,
            poll_ts=poll_ts, bracket_low=60.0 + 2 * i, bracket_high=62.0 + 2 * i,
            p_yes_raw=(0.0 if i in zeros_at else 0.3),
        ))
    return rows


class TestInteriorZeroViolations:
    """The M0 structural invariant that replaced two earlier, wrong versions
    (see the function's own docstring for the full account): a finite envelope
    produces exact zeros only as a contiguous run at the top/bottom of a
    station-day's sorted ladder. A zero with non-zero brackets on both sides is
    a gap no envelope can produce -- exactly #820's failure shape."""

    def test_zero_at_the_bottom_tail_is_legitimate(self):
        """Brackets entirely below min_env -- ordinary envelope truncation."""
        rows = _ladder(zeros_at={0, 1})
        out = interior_zero_violations(rows)
        assert out["n_violations"] == 0
        assert out["n_ladders_checked"] == 1

    def test_zero_at_the_top_tail_is_legitimate(self):
        rows = _ladder(zeros_at={3, 4})
        assert interior_zero_violations(rows)["n_violations"] == 0

    def test_zeros_at_both_tails_are_legitimate(self):
        """The common envelope shape: narrow band of live brackets in the
        middle, zeros tapering off both above and below."""
        rows = _ladder(zeros_at={0, 4}, n=5)
        assert interior_zero_violations(rows)["n_violations"] == 0

    def test_zero_sandwiched_between_nonzero_brackets_is_a_violation(self):
        """The #820 shape: a gap where no finite envelope can produce one."""
        rows = _ladder(zeros_at={2}, n=5)   # nonzero, nonzero, ZERO, nonzero, nonzero
        out = interior_zero_violations(rows)
        assert out["n_violations"] == 1
        assert out["violations"][0]["bracket_low"] == 64.0   # index 2 -> 60+2*2

    def test_multiple_interior_gaps_all_counted(self):
        rows = _ladder(zeros_at={1, 3}, n=5)  # nonzero, ZERO, nonzero, ZERO, nonzero
        assert interior_zero_violations(rows)["n_violations"] == 2

    def test_all_zero_ladder_is_not_a_violation(self):
        """A full-ladder miss is a plausibly-real forecast failure, not this
        check's target -- and every zero there is technically 'between' other
        zeros only, never a gap next to a nonzero, so it must not double-count."""
        rows = _ladder(zeros_at={0, 1, 2, 3, 4}, n=5)
        out = interior_zero_violations(rows)
        assert out["n_violations"] == 0
        assert out["n_all_zero_ladders"] == 1

    def test_ladder_below_min_size_is_skipped_not_judged(self):
        """With 2 brackets there is no way to tell an interior gap from a
        tail -- must not guess."""
        rows = _ladder(zeros_at={0}, n=2)
        out = interior_zero_violations(rows)
        assert out["n_too_small"] == 1
        assert out["n_ladders_checked"] == 0

    def test_different_poll_snapshots_are_not_merged_into_one_ladder(self):
        """Two different hourly snapshots of the same station-day must not be
        treated as one combined ladder -- that would fabricate contiguity."""
        rows = (
            _ladder(zeros_at={0}, n=3, poll_ts="2026-02-01T12:00:00+00:00")
            + _ladder(zeros_at={2}, n=3, poll_ts="2026-02-01T13:00:00+00:00")
        )
        out = interior_zero_violations(rows)
        assert out["n_ladders_checked"] == 2
        assert out["n_violations"] == 0   # both are tail zeros within their own snapshot

    def test_different_settlement_dates_are_not_merged(self):
        """Same-day and next-day markets for the same station, polled in the
        same hour, are different ladders and must not be combined."""
        rows = (
            _ladder(zeros_at={0}, n=3, settlement_date="2026-02-01")
            + _ladder(zeros_at={2}, n=3, settlement_date="2026-02-02")
        )
        out = interior_zero_violations(rows)
        assert out["n_ladders_checked"] == 2
        assert out["n_violations"] == 0

    def test_row_missing_bracket_low_is_counted_not_silently_dropped(self):
        row = _eval_row(bracket_low=None, p_yes_raw=0.0)
        out = interior_zero_violations([row])
        assert out["n_undatable"] == 1

    def test_report_calls_out_violations_loudly(self):
        rows = _ladder(zeros_at={2}, n=5)
        report = build_report(
            rail_concentration(rows), {}, zero_artifact_rate(rows),
            {"available": False, "by_sigma_source": {}},
            {"available": False, "by_model": {}}, "2026-02-15", None,
            inv=interior_zero_violations(rows), rows_for_ceiling=rows,
        )
        assert "#820 IS NOT FULLY CLOSED" in report
        assert "*64.0-66.0: 0.0*" in report   # the flagged bracket, marked

    def test_report_confirms_a_clean_population(self):
        rows = _ladder(zeros_at={0, 4}, n=5)
        report = build_report(
            rail_concentration(rows), {}, zero_artifact_rate(rows),
            {"available": False, "by_sigma_source": {}},
            {"available": False, "by_model": {}}, "2026-02-15", None,
            inv=interior_zero_violations(rows), rows_for_ceiling=rows,
        )
        assert "No interior-zero gaps" in report

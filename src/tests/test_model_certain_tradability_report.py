"""Tests for src/scripts/model_certain_tradability_report.py (issue #1063).

Covers:
- `ev_per_contract`: hand-computed examples, including the exact-breakeven
  `no_ask=99, p_yes=0.01` case from the pre-registration
  (docs/REMEDIATION_PLAN.md, "M3b").
- `bucket_no_ask` / `bucket_minutes`: boundary edge cases.
- `select_model_certain_population`: reuses `is_model_certain_price` and does
  NOT additionally filter rail rows (issue #1063's explicit requirement).
- The "no Brier machinery" guard: `market_p_yes` is never imported or used,
  mirroring `certainty_exclusion_check.py`'s own guard test for `compute_bss`
  / `brier_score`.
- `group_ev_stats`: point/upper/2x EV, Wilson CI, unresolved rows excluded
  from rates.
- `worst_drawdown_cents`: chronological replay and max drawdown.
- `apply_stopping_rule`: both PASS and FAIL paths, plus the n<1000 and
  fewer-than-3-buckets edge cases.
- `build_report`: assembles without raising and states the DOES-NOT-REOPEN-M3
  disclaimer plus the stopping-rule verdict.

All dates are synthetic (2026-0x-xx), matching the repo convention.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.scripts.model_certain_tradability_report import (
    EV_PASS_THRESHOLD_CENTS,
    MIN_POSITIVE_BUCKETS,
    MIN_RESOLVED_N,
    apply_stopping_rule,
    bucket_minutes,
    bucket_no_ask,
    build_report,
    concentration_by,
    ev_per_contract,
    group_ev_stats,
    group_rows,
    ladder_kind,
    realized_pnl_cents,
    select_model_certain_population,
    worst_drawdown_cents,
)


def _row(p_yes_raw=0.0, yes_ask=None, no_ask=98.0, station="KORD", ticker="0x1",
         end_date="2026-08-10", ts="2026-08-10T18:00:00+00:00",
         minutes_to_settlement=30.0, bracket_low=60.0, bracket_high=62.0,
         yes_won=None, **extra):
    row = {
        "p_yes_raw": p_yes_raw, "yes_ask": yes_ask, "no_ask": no_ask,
        "station": station, "ticker": ticker, "end_date": end_date,
        "settlement_date": end_date, "ts": ts,
        "minutes_to_settlement": minutes_to_settlement,
        "bracket_low": bracket_low, "bracket_high": bracket_high,
    }
    if yes_won is not None:
        row["yes_won"] = yes_won
    row.update(extra)
    return row


class TestEvPerContract:
    def test_exact_breakeven_case_from_the_preregistration(self):
        """docs/REMEDIATION_PLAN.md, 'M3b': no_ask=99, p_yes=1% -> exactly 0.0c."""
        assert ev_per_contract(0.01, 99.0) == pytest.approx(0.0, abs=1e-9)

    def test_positive_edge_example_from_the_preregistration(self):
        """no_ask=98, p_yes=1% -> +1.00c."""
        assert ev_per_contract(0.01, 98.0) == pytest.approx(1.0)

    def test_certain_loss_at_p_yes_one(self):
        assert ev_per_contract(1.0, 50.0) == pytest.approx(-50.0)

    def test_certain_win_at_p_yes_zero(self):
        assert ev_per_contract(0.0, 50.0) == pytest.approx(50.0)


class TestBucketNoAsk:
    def test_boundary_values_land_in_the_correct_bucket(self):
        assert bucket_no_ask(95.0) == "<=95"
        assert bucket_no_ask(95.4) == "96"  # strictly above 95 -> next bucket
        assert bucket_no_ask(96.0) == "96"
        assert bucket_no_ask(96.5) == "97"  # between 96 and 97 -> next bucket
        assert bucket_no_ask(97.0) == "97"
        assert bucket_no_ask(98.0) == "98"
        assert bucket_no_ask(99.0) == "99"
        assert bucket_no_ask(50.0) == "<=95"

    def test_missing_is_none(self):
        assert bucket_no_ask(None) is None

    def test_above_the_rail_still_lands_somewhere(self):
        """no_ask is clamped to <= 99 everywhere upstream, but the bucketer
        must not silently drop an out-of-range value rather than classify it."""
        assert bucket_no_ask(150.0) == "99"


class TestBucketMinutes:
    def test_boundaries_are_half_open(self):
        assert bucket_minutes(0.0) == "0-60"
        assert bucket_minutes(59.999) == "0-60"
        assert bucket_minutes(60.0) == "60-180"
        assert bucket_minutes(180.0) == "180-360"
        assert bucket_minutes(360.0) == "360-720"
        assert bucket_minutes(720.0) == "720+"
        assert bucket_minutes(10_000.0) == "720+"

    def test_missing_or_negative_is_undeterminable(self):
        assert bucket_minutes(None) is None
        assert bucket_minutes(-5.0) is None


class TestLadderKind:
    def test_delegates_to_m3_window_diagnostics_classify_width(self):
        row_2f = _row(bracket_low=60.0, bracket_high=62.0)
        row_1_8c = _row(bracket_low=60.0, bracket_high=61.8)
        row_missing = _row(bracket_low=None)
        assert ladder_kind(row_2f) == "degF post-#917 (2.0F)"
        assert "degC" in ladder_kind(row_1_8c)
        assert ladder_kind(row_missing) == "other"


class TestSelectModelCertainPopulation:
    def test_keeps_only_exact_zero_p_yes_raw(self):
        rows = [_row(p_yes_raw=0.0), _row(p_yes_raw=0.3), _row(p_yes_raw=None)]
        kept = select_model_certain_population(rows)
        assert len(kept) == 1
        assert kept[0]["p_yes_raw"] == 0.0

    def test_does_not_filter_out_rail_rows(self):
        """Issue #1063: model-certain rows that are ALSO at the 1c/99c rail
        must stay in-scope -- `apply_exclusions` runs the zero-check before
        the rail check, so this population's job is exactly the reverse of
        the report's exclusion funnel."""
        rail_and_certain = _row(p_yes_raw=0.0, yes_ask=1.0, no_ask=99.0)
        kept = select_model_certain_population([rail_and_certain])
        assert kept == [rail_and_certain]

    def test_missing_p_yes_raw_is_not_model_certain(self):
        assert select_model_certain_population([_row(p_yes_raw=None)]) == []


class TestNoBrierMachineryGuard:
    """This is an EV report, not a skill report. `market_p_yes` (and any
    Brier machinery) must never be importable or callable from here --
    mirrors `certainty_exclusion_check.py`'s own guard test for `compute_bss`
    / `brier_score`."""

    def test_market_p_yes_is_not_an_attribute_of_this_module(self):
        import src.scripts.model_certain_tradability_report as mod
        assert not hasattr(mod, "market_p_yes")
        assert not hasattr(mod, "compute_bss")
        assert not hasattr(mod, "brier_score")

    def test_market_p_yes_is_never_imported_or_called_with_an_argument(self):
        import re

        import src.scripts.model_certain_tradability_report as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        # Mentioned only in prose (docstrings) explaining what is NOT used,
        # e.g. "`market_p_yes()`" with an EMPTY argument list -- never as a
        # live import, and never called WITH an argument (a real call site
        # always passes a row).
        assert "import market_p_yes" not in source
        assert re.search(r"market_p_yes\([^)]", source) is None


class TestGroupEvStats:
    def test_point_upper_and_2x_ev(self):
        rows = [_row(no_ask=98.0, yes_won=False) for _ in range(99)] + \
               [_row(no_ask=98.0, yes_won=True)]
        stats = group_ev_stats(rows)
        assert stats["n_resolved"] == 100
        assert stats["observed_yes"] == pytest.approx(0.01)
        assert stats["ev_point"] == pytest.approx(ev_per_contract(0.01, 98.0))
        # Upper bound of the CI is strictly above the point estimate, so its
        # EV must be strictly lower (more YES = worse for a NO seller).
        assert stats["ev_upper"] < stats["ev_point"]
        assert stats["ev_2x"] == pytest.approx(ev_per_contract(0.02, 98.0))

    def test_unresolved_rows_are_excluded_from_the_rate(self):
        rows = [_row(yes_won=True), _row(ticker="b")]  # second has no yes_won
        stats = group_ev_stats(rows)
        assert stats["n"] == 2
        assert stats["n_resolved"] == 1

    def test_no_resolved_rows_yields_none_not_zero(self):
        stats = group_ev_stats([_row(), _row(ticker="b")])
        assert stats["observed_yes"] is None
        assert stats["ev_point"] is None
        assert stats["ci"] is None

    def test_missing_no_ask_is_excluded_from_resolved(self):
        rows = [_row(no_ask=None, yes_won=True), _row(no_ask=98.0, yes_won=False)]
        stats = group_ev_stats(rows)
        assert stats["n_resolved"] == 1


class TestGroupRows:
    def test_drops_rows_the_key_function_cannot_classify(self):
        rows = [_row(minutes_to_settlement=30.0), _row(minutes_to_settlement=None)]
        grouped = group_rows(rows, lambda r: bucket_minutes(r.get("minutes_to_settlement")))
        assert sum(len(v) for v in grouped.values()) == 1


class TestConcentrationBy:
    def test_share_sums_to_one_across_two_stations(self):
        rows = (
            [_row(station="KORD", no_ask=98.0, yes_won=False, ticker=f"a{i}") for i in range(90)]
            + [_row(station="KORD", no_ask=98.0, yes_won=True, ticker=f"b{i}") for i in range(10)]
            + [_row(station="KJFK", no_ask=98.0, yes_won=False, ticker=f"c{i}") for i in range(90)]
            + [_row(station="KJFK", no_ask=98.0, yes_won=True, ticker=f"d{i}") for i in range(10)]
        )
        by_bucket = group_rows(rows, lambda r: bucket_no_ask(r.get("no_ask")))
        conc = concentration_by(rows, by_bucket)
        assert conc["top_station"] is not None
        _, _, share = conc["top_station"]
        # Symmetric population -> ~50% each, neither dominates.
        assert 0.4 < share < 0.6

    def test_no_priced_rows_returns_none_shares(self):
        conc = concentration_by([], {})
        assert conc["top_station"] is None
        assert conc["top_no_ask_bucket"] is None


class TestWorstDrawdown:
    def test_all_losses_drawdown_equals_total_loss(self):
        rows = [
            _row(ts="2026-08-01T00:00:00+00:00", no_ask=98.0, yes_won=True),
            _row(ts="2026-08-02T00:00:00+00:00", no_ask=98.0, yes_won=True),
        ]
        dd = worst_drawdown_cents(rows)
        assert dd["n"] == 2
        assert dd["max_drawdown_cents"] == pytest.approx(196.0)
        assert dd["final_cumulative_cents"] == pytest.approx(-196.0)

    def test_recovery_after_a_loss_caps_drawdown_at_the_loss(self):
        rows = [
            _row(ts="2026-08-01T00:00:00+00:00", no_ask=98.0, yes_won=True),   # -98
            _row(ts="2026-08-02T00:00:00+00:00", no_ask=98.0, yes_won=False),  # +2
            _row(ts="2026-08-03T00:00:00+00:00", no_ask=98.0, yes_won=False),  # +2
        ]
        dd = worst_drawdown_cents(rows)
        assert dd["max_drawdown_cents"] == pytest.approx(98.0)

    def test_unresolvable_rows_are_excluded_from_the_replay(self):
        rows = [_row(no_ask=None, yes_won=True), _row(ticker="b")]
        assert realized_pnl_cents(rows[0]) is None
        assert realized_pnl_cents(rows[1]) is None
        dd = worst_drawdown_cents(rows)
        assert dd["n"] == 0


class TestStoppingRule:
    def _pooled(self, n_resolved=1200, ev_upper=0.6):
        return {"n_resolved": n_resolved, "ev_upper": ev_upper}

    def _buckets(self, n_positive=3, n_total=5):
        buckets = {}
        for i in range(n_total):
            buckets[f"b{i}"] = {"ev_upper": 0.1 if i < n_positive else -0.1}
        return buckets

    def test_pass_when_all_three_conditions_hold(self):
        verdict, reasoning = apply_stopping_rule(self._pooled(), self._buckets())
        assert verdict == "PASS"
        assert "0.6" in reasoning or "+0.600" in reasoning

    def test_fail_on_ev_below_threshold(self):
        verdict, _ = apply_stopping_rule(
            self._pooled(ev_upper=EV_PASS_THRESHOLD_CENTS - 0.01), self._buckets())
        assert verdict == "FAIL"

    def test_fail_on_n_below_1000(self):
        verdict, reasoning = apply_stopping_rule(
            self._pooled(n_resolved=MIN_RESOLVED_N - 1), self._buckets())
        assert verdict == "FAIL"
        assert str(MIN_RESOLVED_N - 1) in reasoning

    def test_fail_on_fewer_than_3_positive_buckets(self):
        verdict, reasoning = apply_stopping_rule(
            self._pooled(), self._buckets(n_positive=MIN_POSITIVE_BUCKETS - 1))
        assert verdict == "FAIL"

    def test_none_upper_bound_is_a_fail_not_a_crash(self):
        verdict, reasoning = apply_stopping_rule(
            self._pooled(ev_upper=None), self._buckets())
        assert verdict == "FAIL"
        assert "n/a" in reasoning

    def test_exact_threshold_values_pass(self):
        """>= on both the EV bar and the n bar -- equality must not fail."""
        verdict, _ = apply_stopping_rule(
            self._pooled(n_resolved=MIN_RESOLVED_N, ev_upper=EV_PASS_THRESHOLD_CENTS),
            self._buckets(n_positive=MIN_POSITIVE_BUCKETS))
        assert verdict == "PASS"


class TestBuildReport:
    def _samples(self):
        rows = (
            [_row(no_ask=98.0, yes_won=False, ticker=f"a{i}", station="KORD") for i in range(20)]
            + [_row(no_ask=98.0, yes_won=True, ticker=f"b{i}", station="KORD") for i in range(2)]
        )
        return rows

    def test_report_states_it_does_not_reopen_m3(self):
        report = build_report(self._samples(), "2026-08-26", "2026-08-06",
                              {"deduped": 22, "model_certain": 22, "also_rail": 0}, {})
        assert "DOES NOT REOPEN M3" in report
        assert "-0.4123" in report

    def test_report_never_calls_market_p_yes_with_an_argument(self):
        import re

        report = build_report(self._samples(), "2026-08-26", "2026-08-06",
                              {"deduped": 22, "model_certain": 22, "also_rail": 0}, {})
        assert re.search(r"market_p_yes\([^)]", report) is None

    def test_report_states_a_stopping_rule_verdict(self):
        report = build_report(self._samples(), "2026-08-26", "2026-08-06",
                              {"deduped": 22, "model_certain": 22, "also_rail": 0}, {})
        assert "### Verdict: **FAIL**" in report or "### Verdict: **PASS**" in report

    def test_report_states_no_live_trading_disclaimer(self):
        report = build_report(self._samples(), "2026-08-26", "2026-08-06",
                              {"deduped": 22, "model_certain": 22, "also_rail": 0}, {})
        assert "#1053/#1054" in report

"""Tests for the certainty-exclusion check (issue #822).

The check exists to settle, before the M3 gate runs, whether the BSS report's
two certainty exclusions still earn their place. Two properties matter more
than any individual number it prints:

* it must never emit a skill number on the retained population, or running it
  before the gate would spoil the pre-registration;
* its verdict must follow the pre-registered rule mechanically, including the
  symmetry constraint that stops one side's easy wins being restored alone.

Both are asserted here, alongside the arithmetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.scripts.certainty_exclusion_check import (
    CLASS_BOTH_CERTAIN,
    CLASS_CONTESTED,
    CLASS_MARKET_CERTAIN,
    CLASS_MODEL_CERTAIN,
    HONESTY_BAR,
    attach_outcomes,
    build_report,
    class_stats,
    classify_certainty,
    is_market_certain,
    is_model_certain,
    outcome_key,
    provenance_split,
    run_check,
    verdict,
    wilson_interval,
)


def _row(p_yes_raw=0.3, yes_ask=20.0, no_ask=82.0, station="KORD", ticker="0x1",
         end_date="2026-07-25", yes_won=None, **extra):
    row = {"p_yes_raw": p_yes_raw, "yes_ask": yes_ask, "no_ask": no_ask,
           "station": station, "ticker": ticker, "end_date": end_date,
           "settlement_date": end_date, "minutes_to_settlement": 10.0,
           "bracket_low": 60.0, "bracket_high": 65.0, "question": "",
           "ts": end_date + "T18:00:00+00:00"}
    if yes_won is not None:
        row["yes_won"] = yes_won
    row.update(extra)
    return row


class TestWilsonInterval:
    def test_zero_successes_still_has_width(self):
        """The normal approximation collapses to zero width at k=0 and would
        claim certainty from no evidence. Wilson does not."""
        lo, hi = wilson_interval(0, 100)
        assert lo == 0.0
        assert hi > 0.03

    def test_interval_tightens_as_n_grows(self):
        _, hi_small = wilson_interval(0, 100)
        _, hi_large = wilson_interval(0, 10_000)
        assert hi_large < hi_small

    def test_bounds_stay_inside_zero_one(self):
        for k, n in [(0, 1), (1, 1), (5, 10), (999, 1000)]:
            lo, hi = wilson_interval(k, n)
            assert 0.0 <= lo <= hi <= 1.0

    def test_empty_sample_is_none_not_a_rate(self):
        assert wilson_interval(0, 0) is None


class TestClassification:
    def test_model_certain_is_exact_zero_only(self):
        assert is_model_certain(_row(p_yes_raw=0.0))
        assert not is_model_certain(_row(p_yes_raw=1e-9))

    def test_rail_detected_on_either_side_of_the_book(self):
        assert is_market_certain(_row(yes_ask=1.0, no_ask=99.0))
        assert is_market_certain(_row(yes_ask=99.0, no_ask=1.0))
        assert not is_market_certain(_row(yes_ask=20.0, no_ask=82.0))

    def test_the_four_classes_partition_the_rows(self):
        rows = [
            _row(p_yes_raw=0.0, yes_ask=20.0, no_ask=82.0),      # model only
            _row(p_yes_raw=0.3, yes_ask=1.0, no_ask=99.0),       # market only
            _row(p_yes_raw=0.0, yes_ask=1.0, no_ask=99.0),       # both
            _row(p_yes_raw=0.3, yes_ask=20.0, no_ask=82.0),      # contested
        ]
        classes, counts = classify_certainty(rows)
        assert [len(classes[c]) for c in (
            CLASS_MODEL_CERTAIN, CLASS_MARKET_CERTAIN,
            CLASS_BOTH_CERTAIN, CLASS_CONTESTED)] == [1, 1, 1, 1]
        assert counts["input_rows"] == 4

    def test_rows_missing_inputs_are_dropped_not_assumed(self):
        """Neither certainty question is askable without both a model
        probability and a market price."""
        classes, counts = classify_certainty([
            _row(p_yes_raw=None), _row(yes_ask=None), _row(no_ask=None)])
        assert sum(len(v) for v in classes.values()) == 0
        assert counts["undiagnosable_missing_p_yes_raw"] == 1
        assert counts["undiagnosable_missing_market_price"] == 2


class TestOutcomeJoin:
    """The resolver rebuilds rows rather than mutating them, so the join is by
    value. An identity-keyed join matches nothing and silently reports every
    rate as INDETERMINATE -- the failure this guards."""

    def test_outcomes_attach_across_rebuilt_dicts(self):
        row = _row(ticker="0xAA")
        classes = {CLASS_MODEL_CERTAIN: [row], CLASS_MARKET_CERTAIN: [],
                   CLASS_BOTH_CERTAIN: [], CLASS_CONTESTED: []}
        rebuilt = [{**row, "yes_won": True}]        # a copy, as the resolver returns
        assert attach_outcomes(classes, rebuilt) == 1
        assert row["yes_won"] is True

    def test_unresolvable_rows_keep_no_outcome(self):
        row = _row(ticker="0xAA")
        classes = {CLASS_MODEL_CERTAIN: [row], CLASS_MARKET_CERTAIN: [],
                   CLASS_BOTH_CERTAIN: [], CLASS_CONTESTED: []}
        assert attach_outcomes(classes, []) == 0
        assert "yes_won" not in row

    def test_key_tolerates_end_date_or_settlement_date(self):
        a = {"station": "KORD", "ticker": "0x1", "end_date": "2026-07-25"}
        b = {"station": "KORD", "ticker": "0x1", "settlement_date": "2026-07-25"}
        assert outcome_key(a) == outcome_key(b)


class TestClassStats:
    def test_rate_and_station_days(self):
        rows = [_row(yes_won=True, end_date="2026-07-25", ticker="a"),
                _row(yes_won=False, end_date="2026-07-25", ticker="b"),
                _row(yes_won=False, end_date="2026-07-26", ticker="c")]
        s = class_stats(rows)
        assert s["n_resolved"] == 3
        assert s["n_yes"] == 1
        assert s["observed_yes"] == pytest.approx(1 / 3)
        assert s["station_days"] == 2

    def test_unresolved_rows_are_excluded_from_the_rate(self):
        s = class_stats([_row(yes_won=True), _row(ticker="b")])
        assert s["n_total"] == 2
        assert s["n_resolved"] == 1
        assert s["observed_yes"] == 1.0

    def test_no_resolved_rows_yields_no_rate_rather_than_zero(self):
        """A rate of 0.0 would read as 'perfectly calibrated'. Absent evidence
        must not be indistinguishable from evidence of correctness."""
        s = class_stats([_row(), _row(ticker="b")])
        assert s["observed_yes"] is None
        assert s["ci"] is None

    def test_honesty_gap_is_measured_against_the_markets_own_price(self):
        """A rail row can sit at either end of the book, so zero is the wrong
        reference for the market class."""
        s = class_stats([_row(yes_ask=99.0, no_ask=1.0, yes_won=True)])
        assert s["mean_market_p"] == pytest.approx(0.99)
        assert s["honesty_gap"] == pytest.approx(0.01)


class TestVerdictFollowsTheRule:
    def _stats(self, observed, ci=(0.0, 0.001)):
        return {"observed_yes": observed, "n_resolved": 1000, "ci": ci}

    def test_false_certainty_keeps_the_exclusion(self):
        label, why = self._verdict(0.24, bit_exact=1000, ci=(0.21, 0.27))
        assert "KEEP" in label and "FALSE" in label
        assert "24.0%" in why

    def test_honest_but_bit_exact_keeps_the_exclusion(self):
        label, _ = self._verdict(0.003, bit_exact=1000)
        assert "KEEP" in label and "provenance" in label

    def test_honest_and_computed_drops_it_only_as_a_pair(self):
        label, _ = self._verdict(0.003, bit_exact=0)
        assert "DROP" in label
        assert "market-certain" in label

    def test_rate_exactly_at_the_bar_is_not_a_failure(self):
        """The bar is 'above 2%', so equality must not trip the false branch."""
        label, _ = self._verdict(HONESTY_BAR, bit_exact=0)
        assert "DROP" in label

    def test_no_resolvable_rows_is_indeterminate_not_a_pass(self):
        label, _ = self._verdict(None, bit_exact=0)
        assert "INDETERMINATE" in label

    def test_interval_straddling_the_bar_is_underpowered_not_honest(self):
        """A point estimate under the bar with an interval over it is exactly
        the underpowered-BSS mistake in a different costume."""
        label, why = self._verdict(0.008, bit_exact=0, ci=(0.003, 0.022))
        assert "UNDERPOWERED" in label
        assert "DROP" not in label
        assert "2.2%" in why

    def test_the_power_check_runs_before_the_calibration_branches(self):
        """Ordering matters: a straddling interval must not be overridden by a
        clean-looking point estimate."""
        underpowered, _ = self._verdict(0.008, bit_exact=0, ci=(0.003, 0.022))
        decided, _ = self._verdict(0.008, bit_exact=0, ci=(0.005, 0.012))
        assert "UNDERPOWERED" in underpowered
        assert "DROP" in decided

    def _verdict(self, observed, bit_exact, ci=(0.0, 0.001)):
        return verdict(self._stats(observed, ci),
                       {"bit_exact_zero": bit_exact, "n": 1000})


class TestProvenanceSplit:
    def test_counts_bit_exact_zeros(self):
        p = provenance_split([_row(p_yes_raw=0.0), _row(p_yes_raw=0.0),
                              _row(p_yes_raw=1e-12)])
        assert p == {"bit_exact_zero": 2, "n": 3}


class TestReportContainsNoSkillNumber:
    """Running this before the gate must not leak the answer the gate exists to
    produce. The guarantee is structural -- no BSS is ever computed -- and is
    pinned here so a later edit cannot quietly add one."""

    def _report(self):
        rows = [_row(p_yes_raw=0.0, yes_won=False, ticker="a"),
                _row(p_yes_raw=0.3, yes_ask=1.0, no_ask=99.0, yes_won=False, ticker="b"),
                _row(p_yes_raw=0.3, yes_won=True, ticker="c")]
        classes, counts = classify_certainty(rows)
        return build_report(classes, counts, "2026-07-29", "all-bracket", {})

    def test_no_skill_figure_is_printed(self):
        """Naming the BSS report is fine; printing a Brier quantity is not."""
        report = self._report()
        for banned in ("BS_model", "BS_market", "Brier", "skill score"):
            assert banned not in report

    def test_the_module_cannot_compute_a_skill_number(self):
        """The strongest form of the guarantee: the machinery is not imported,
        so no future edit can print a BSS without first adding the import."""
        import src.scripts.certainty_exclusion_check as mod
        assert not hasattr(mod, "compute_bss")
        assert not hasattr(mod, "brier_score")
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "compute_bss" not in source
        assert "brier_score" not in source

    def test_states_the_rule_before_the_figures(self):
        report = self._report()
        assert report.index("pre-registered rule") < report.index("## Certainty classes")

    def test_states_the_symmetry_constraint(self):
        assert "Symmetry constraint" in self._report()

    def test_names_what_it_cannot_settle(self):
        report = self._report()
        assert "What this check cannot tell you" in report
        assert "not conclusive from data" in report


class TestEndToEnd:
    def test_writes_a_report_and_reads_bracket_evals(self, tmp_path, monkeypatch):
        evals = tmp_path / "logs" / "bracket_evals.jsonl"
        evals.parent.mkdir(parents=True)
        with evals.open("w", encoding="utf-8") as fh:
            for i, p in enumerate([0.0, 0.0, 0.3, 0.45]):
                fh.write(json.dumps({
                    "poll_ts": f"2026-07-2{5 + i % 2}T18:00:00+00:00",
                    "station": "KORD", "ticker": f"0x{i}",
                    "settlement_date": f"2026-07-2{5 + i % 2}",
                    "bracket_low": 60.0, "bracket_high": 65.0,
                    "yes_ask": 20.0, "no_ask": 82.0,
                    "p_yes_raw": p, "minutes_to_settlement": 10.0,
                    "is_next_day": 0,
                }) + "\n")

        monkeypatch.setattr(
            "src.scripts.certainty_exclusion_check.resolve_candidate_outcomes",
            lambda rows, db, **kw: (
                [{**r, "yes_won": False} for r in rows],
                {"resolved_from_gamma": len(rows), "resolved_from_metar": 0,
                 "n_unresolvable": 0},
            ),
        )

        rc = run_check(tmp_path / "db.sqlite", tmp_path / "out", "2026-07-29",
                       population="all-bracket", bracket_evals=evals,
                       use_gamma=False, allow_network=False)
        assert rc == 0
        out = (tmp_path / "out" / "certainty_exclusion_check_2026-07-29.md").read_text()
        assert "Certainty-exclusion check" in out
        # Two exact-zero rows, both resolving NO. The point estimate is 0%, but
        # two rows cannot place it against a 2% bar -- the tool must say so
        # rather than read a clean-looking rate off a sample this small.
        assert "UNDERPOWERED" in out
        assert "DROP" not in out

    def test_empty_population_is_an_error_not_an_empty_verdict(self, tmp_path):
        rc = run_check(tmp_path / "db.sqlite", tmp_path / "out", "2026-07-29",
                       population="all-bracket",
                       bracket_evals=tmp_path / "missing.jsonl",
                       use_gamma=False, allow_network=False)
        assert rc == 1

    def test_a_join_that_matches_nothing_fails_loudly(self, tmp_path, monkeypatch):
        """The silent-failure mode: rates computed off an empty join would read
        as INDETERMINATE rather than as the bug it is."""
        evals = tmp_path / "logs" / "bracket_evals.jsonl"
        evals.parent.mkdir(parents=True)
        evals.write_text(json.dumps({
            "poll_ts": "2026-07-25T18:00:00+00:00", "station": "KORD",
            "ticker": "0x1", "settlement_date": "2026-07-25",
            "bracket_low": 60.0, "bracket_high": 65.0, "yes_ask": 20.0,
            "no_ask": 82.0, "p_yes_raw": 0.0, "minutes_to_settlement": 10.0,
            "is_next_day": 0,
        }) + "\n", encoding="utf-8")

        monkeypatch.setattr(
            "src.scripts.certainty_exclusion_check.resolve_candidate_outcomes",
            lambda rows, db, **kw: (
                [{**r, "ticker": "MISMATCH", "yes_won": False} for r in rows], {}),
        )
        rc = run_check(tmp_path / "db.sqlite", tmp_path / "out", "2026-07-29",
                       population="all-bracket", bracket_evals=evals,
                       use_gamma=False, allow_network=False)
        assert rc == 1

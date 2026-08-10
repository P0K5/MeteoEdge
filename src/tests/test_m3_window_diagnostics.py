"""Tests for the M3 window diagnostics (#917 / #920 / #921).

This tool exists to answer whether the clean-data clock may restart. The
failure that matters is not a wrong number in a table -- it is saying "clean"
when the window is not, so the assertions concentrate on the conservative
directions: absent evidence must never read as a pass, and a day is clean only
when BOTH ladder populations conserve mass.
"""

from __future__ import annotations

import json

from src.scripts.m3_window_diagnostics import (
    LADDER_KINDS,
    MASS_LOW,
    build_report,
    classify_width,
    conserves,
    deploy_day,
    earliest_clean_day,
    group_ladders,
    ladder_stats,
    mass_by_day,
    scoreable_progress,
    width_census,
)


def _b(p, lo, hi, station="KATL", ts="2026-07-29T13:00:00+00:00",
       end_date="2026-07-29", is_next_day=0, yes_ask=30.0, no_ask=70.0):
    return {"p_yes_raw": p, "bracket_low": lo, "bracket_high": hi,
            "station": station, "ts": ts, "end_date": end_date,
            "is_next_day_flag": is_next_day, "yes_ask": yes_ask,
            "no_ask": no_ask, "ticker": f"0x{lo}"}


def _ladder(masses, width=2.0, start=80.0, **kw):
    """A bare bracket run, with NO open-ended tail brackets.

    Fine for testing ladder_stats mechanics (widths, zero runs, gaps). NOT
    valid for testing a mass verdict: without open ends such a ladder is
    coverage-limited, so a deficit on it is deliberately excused and the test
    would assert nothing. Use ``_prod_ladder`` for anything that reaches
    ``conserves`` / ``mass_by_day`` / a report verdict.
    """
    return [_b(m, start + i * width, start + (i + 1) * width, **kw)
            for i, m in enumerate(masses)]


def _prod_ladder(masses, cap_lo=0.0, cap_hi=0.0, width=2.0, start=80.0, **kw):
    """A ladder shaped like production: open-ended brackets at both tails.

    Real ladders carry ``[-50, lo)`` and ``[hi, 200)`` -- that is where the
    distribution's tails live, and their presence is what makes a ladder
    judgeable. ``cap_lo`` defaults to 0.0, which also makes the ladder read as
    censored (a leading zero run); pass a non-zero ``cap_lo`` for an
    uncensored one.
    """
    end = start + len(masses) * width
    return ([_b(cap_lo, -50.0, start, **kw)]
            + [_b(m, start + i * width, start + (i + 1) * width, **kw)
               for i, m in enumerate(masses)]
            + [_b(cap_hi, end, 200.0, **kw)])


class TestParserFingerprint:
    """Bracket width identifies which parser wrote the ladder, which is how
    #917's deploy date is read out of the data rather than out of systemd."""

    def test_the_three_known_widths(self):
        assert classify_width(1.0) == LADDER_KINDS[0]     # pre-#917 degF
        assert classify_width(2.0) == LADDER_KINDS[1]     # post-#917 degF
        assert classify_width(1.8) == LADDER_KINDS[2]     # degC, always fine

    def test_conversion_noise_still_classifies(self):
        """degC->degF conversion leaves float residue; exact matching would
        push real ladders into 'other'."""
        assert classify_width(1.7999999) == LADDER_KINDS[2]
        assert classify_width(2.0000004) == LADDER_KINDS[1]

    def test_unknown_width_is_not_forced_into_a_bucket(self):
        assert classify_width(7.0) == "other"
        assert classify_width(None) == "other"

    def test_deploy_day_is_the_first_day_without_a_buggy_ladder(self):
        census = {
            "2026-07-29": {LADDER_KINDS[0]: 5, LADDER_KINDS[1]: 0},
            "2026-07-30": {LADDER_KINDS[0]: 3, LADDER_KINDS[1]: 0},
            "2026-07-31": {LADDER_KINDS[0]: 0, LADDER_KINDS[1]: 4},
        }
        assert deploy_day(census) == "2026-07-31"

    def test_never_deployed_returns_none(self):
        census = {"2026-07-29": {LADDER_KINDS[0]: 5},
                  "2026-07-30": {LADDER_KINDS[0]: 3}}
        assert deploy_day(census) is None

    def test_no_buggy_ladder_at_all_returns_none(self):
        """Nothing to date -- must not report the first day as a deploy."""
        census = {"2026-07-29": {LADDER_KINDS[0]: 0, LADDER_KINDS[1]: 2}}
        assert deploy_day(census) is None


class TestCensoringDetection:
    """`bracket_evals` has no current_high, so censoring is read from ladder
    shape: #920 leaves a contiguous run of zeros at the bottom."""

    def test_leading_zeros_are_counted_and_flagged(self):
        s = ladder_stats(_ladder([0.0, 0.0, 0.14, 0.10]))
        assert s["leading_zeros"] == 2
        assert s["censored"] is True

    def test_an_uncensored_ladder_is_not_flagged(self):
        s = ladder_stats(_ladder([0.2, 0.3, 0.3, 0.2]))
        assert s["leading_zeros"] == 0
        assert s["censored"] is False

    def test_an_all_zero_ladder_is_dead_not_censored(self):
        """Counting it as maximally censored would drag the censored
        population's mean toward zero for an unrelated reason."""
        s = ladder_stats(_ladder([0.0, 0.0, 0.0]))
        assert s["censored"] is False

    def test_a_zero_above_live_mass_is_not_censoring(self):
        """#920 zeroes from the bottom. An interior zero is a different
        defect and must not be attributed to it."""
        s = ladder_stats(_ladder([0.3, 0.0, 0.4]))
        assert s["leading_zeros"] == 0
        assert s["censored"] is False

    def test_brackets_are_ordered_before_the_prefix_is_read(self):
        """Log order is not ladder order."""
        shuffled = [_b(0.14, 86.0, 88.0), _b(0.0, 80.0, 82.0), _b(0.0, 82.0, 84.0)]
        assert ladder_stats(shuffled)["leading_zeros"] == 2


class TestLadderGrouping:
    def test_same_day_and_next_day_ladders_do_not_pool(self):
        """Pooling two distributions would manufacture a mass excess that is
        not in the data -- and #921 is an excess investigation."""
        rows = _ladder([0.5, 0.5], is_next_day=0) + _ladder([0.5, 0.5], is_next_day=1)
        ladders = group_ladders(rows)
        assert len(ladders) == 2
        assert all(abs(lad["mass"] - 1.0) < 1e-9 for lad in ladders)

    def test_separate_polls_are_separate_ladders(self):
        rows = (_ladder([0.5, 0.5], ts="2026-07-29T13:00:00+00:00")
                + _ladder([0.5, 0.5], ts="2026-07-29T14:00:00+00:00"))
        assert len(group_ladders(rows)) == 2

    def test_rows_without_a_probability_do_not_sink_the_mass(self):
        rows = _ladder([0.5, 0.5]) + [_b(None, 90.0, 92.0)]
        lad = group_ladders(rows)[0]
        assert abs(lad["mass"] - 1.0) < 1e-9


class TestMassVerdicts:
    def test_absent_data_is_not_a_pass(self):
        """The conservative direction: no ladders must never read as clean."""
        assert conserves(None) is False
        assert conserves({"n": 0, "n_judged": 0, "mean": 1.0, "deficient": 0}) is False

    def test_a_deficient_ladder_fails_even_if_the_mean_looks_fine(self):
        assert conserves({"n": 10, "n_judged": 10, "mean": 1.0, "deficient": 1}) is False

    def test_healthy_population_passes(self):
        assert conserves({"n": 10, "n_judged": 10, "mean": 0.99, "deficient": 0}) is True

    def test_clean_day_requires_both_populations(self):
        """A day healthy only when uncensored is a day #920 is still live --
        today's expected state, and it must not be called clean."""
        per_day = {"2026-08-01": {
            "uncensored": {"n": 5, "n_judged": 5, "mean": 1.0, "deficient": 0},
            "censored": {"n": 5, "n_judged": 5, "mean": 0.31, "deficient": 5},
        }}
        assert earliest_clean_day(per_day) is None

    def test_clean_day_found_when_both_conserve(self):
        per_day = {
            "2026-08-01": {"uncensored": {"n": 5, "n_judged": 5, "mean": 1.0,
                                          "deficient": 0},
                           "censored": {"n": 5, "n_judged": 5, "mean": 0.31,
                                        "deficient": 5}},
            "2026-08-02": {"uncensored": {"n": 5, "n_judged": 5, "mean": 1.0,
                                          "deficient": 0},
                           "censored": {"n": 5, "n_judged": 5, "mean": 0.98,
                                        "deficient": 0}},
        }
        assert earliest_clean_day(per_day) == "2026-08-02"

    def test_censored_and_uncensored_are_reported_apart(self):
        rows = (_prod_ladder([0.0, 0.0, 0.14, 0.10], station="RKSI")
                + _prod_ladder([0.24] * 4, cap_lo=0.02, cap_hi=0.02,
                               station="KATL"))
        by_day = mass_by_day(group_ladders(rows))["2026-07-29"]
        assert by_day["censored"]["n"] == 1
        assert by_day["uncensored"]["n"] == 1
        assert by_day["uncensored"]["deficient"] == 0
        assert by_day["censored"]["deficient"] == 1


class TestScoreableProgress:
    """One counter, shared with the health report.

    A second implementation of "station-days toward the 300 bar" is how this
    went wrong four separate times. The version that lived here partitioned by
    POLL day (so its figures could not be summed -- a settlement date is polled
    as next-day and again as same-day) and excluded rows WITHOUT
    de-duplicating first. On 2026-08-10 it printed "58.8/day, 300 takes ~5
    days" against a measured 111 in five days.

    It now delegates to ``daily_health_report._scoreable_pairs``. These tests
    exist to keep it delegating.
    """

    def _row(self, ts, p, station="KATL", settle="2026-08-07", ticker="0xA",
             yes_ask=30.0, no_ask=70.0):
        return {"station": station, "ts": ts, "end_date": settle,
                "ticker": ticker, "is_next_day_flag": 0, "p_yes_raw": p,
                "bracket_low": 80.0, "bracket_high": 82.0,
                "yes_ask": yes_ask, "no_ask": no_ask}

    def test_the_gates_exclusions_are_applied(self):
        rows = [self._row("2026-08-06T13:00:00+00:00", 0.0, station="A"),
                self._row("2026-08-06T13:00:00+00:00", 0.2, station="B",
                          yes_ask=1.0),
                self._row("2026-08-06T13:00:00+00:00", 0.2, station="C")]
        assert scoreable_progress(rows, "2026-08-06")["station_days"] == 1

    def test_it_de_duplicates_before_excluding(self):
        """The gate's order. Contested at 09:00, exact-zero at the last poll
        -> the gate drops the bracket-day, so this must too."""
        rows = [self._row("2026-08-07T09:00:00+00:00", 0.25),
                self._row("2026-08-07T15:00:00+00:00", 0.0)]
        assert scoreable_progress(rows, "2026-08-06")["station_days"] == 0

    def test_a_settlement_date_polled_on_two_days_counts_ONCE(self):
        """Polled as next-day and again as same-day. Counting per poll day and
        summing was the 57+56=113-against-85 error."""
        rows = [self._row("2026-08-06T13:00:00+00:00", 0.25,
                          settle="2026-08-07"),
                self._row("2026-08-07T13:00:00+00:00", 0.25,
                          settle="2026-08-07")]
        assert scoreable_progress(rows, "2026-08-06")["station_days"] == 1

    def test_progress_is_cumulative_and_carries_the_marginal_rate(self):
        rows = []
        for st in range(28):
            for settle in ("2026-08-06", "2026-08-07"):
                rows.append(self._row("2026-08-07T13:00:00+00:00", 0.25,
                                      station=f"ST{st}", settle=settle))
        got = scoreable_progress(rows, "2026-08-06")
        assert got["station_days"] == 56
        assert got["rate"] == 28
        # ceiling: (300-56)/28 = 8.7 -> 9. Rounding down promises the bar early.
        assert got["days_to_bar"] == 9
        assert got["by_settlement"] == {"2026-08-06": 28, "2026-08-07": 28}

    def test_it_agrees_with_the_health_report_exactly(self):
        """Same question, same answer -- or the two will drift again."""
        from src.scripts.daily_health_report import _scoreable_pairs
        rows = [self._row("2026-08-06T13:00:00+00:00", 0.25, station="A"),
                self._row("2026-08-07T09:00:00+00:00", 0.25, station="B"),
                self._row("2026-08-07T15:00:00+00:00", 0.0, station="B")]
        assert (scoreable_progress(rows, "2026-08-06")["station_days"]
                == len(_scoreable_pairs("2026-08-06", rows=rows)))


class TestReport:
    def test_it_refuses_to_clear_a_window_that_is_still_censoring(self):
        rows = (_prod_ladder([0.24] * 4, cap_lo=0.02, cap_hi=0.02,
                             station="KATL")
                + _prod_ladder([0.0, 0.0, 0.14, 0.10], station="RKSI"))
        report = build_report(rows, "2026-07-24")
        assert "NO day in range conserves mass" in report
        assert "must not run" in report
        assert "#920's signature" in report

    def test_it_says_plainly_when_917_never_deployed(self):
        rows = _ladder([0.25] * 4, width=1.0)
        report = build_report(rows, "2026-07-24")
        assert "has NOT reached production" in report
        assert "Restart" in report

    def test_it_dates_the_deploy_when_the_transition_is_present(self):
        rows = (_ladder([0.25] * 4, width=1.0, ts="2026-07-29T13:00:00+00:00",
                        end_date="2026-07-29")
                + _ladder([0.25] * 4, width=2.0, ts="2026-07-31T13:00:00+00:00",
                          end_date="2026-07-31"))
        report = build_report(rows, "2026-07-24")
        assert "#917 reached production on 2026-07-31" in report

    def test_empty_window_is_stated_not_silently_clean(self):
        assert "No ladders" in build_report(_ladder([0.25] * 4), "2027-01-01")

    def test_brief_reaches_the_same_verdict_as_the_full_report(self):
        """A smaller rendering, not a different analysis."""
        rows = (_prod_ladder([0.24] * 4, cap_lo=0.02, cap_hi=0.02,
                             station="KATL")
                + _prod_ladder([0.0, 0.0, 0.14, 0.10], station="RKSI"))
        from src.scripts.m3_window_diagnostics import brief_report
        assert "no clean day" in brief_report(rows, "2026-07-24")
        assert "NO day in range conserves mass" in build_report(rows, "2026-07-24")

    def test_brief_stays_short_enough_to_relay(self):
        from src.scripts.m3_window_diagnostics import brief_report
        rows = []
        for d in ["2026-07-2%d" % i for i in range(4, 10)]:
            rows += _ladder([0.25] * 4, ts=f"{d}T13:00:00+00:00", end_date=d)
        assert len(brief_report(rows, "2026-07-24").splitlines()) <= 12

    def test_brief_splits_around_the_917_transition(self):
        from src.scripts.m3_window_diagnostics import brief_report
        rows = (_ladder([0.13] * 4, width=1.0, ts="2026-07-29T13:00:00+00:00",
                        end_date="2026-07-29")
                + _ladder([0.25] * 4, width=2.0, ts="2026-07-31T13:00:00+00:00",
                          end_date="2026-07-31"))
        out = brief_report(rows, "2026-07-24")
        assert "#917 live from 2026-07-31" in out
        assert "pre" in out and "post" in out

    def test_brief_flags_a_missing_deploy(self):
        from src.scripts.m3_window_diagnostics import brief_report
        out = brief_report(_ladder([0.25] * 4, width=1.0), "2026-07-24")
        assert "NOT in production" in out

    def test_end_to_end_from_a_jsonl_file(self, tmp_path):
        from src.scripts.m3_window_diagnostics import main
        path = tmp_path / "bracket_evals.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for i, p in enumerate([0.0, 0.0, 0.14, 0.10]):
                fh.write(json.dumps({
                    "station": "RKSI", "ticker": f"0x{i}",
                    "poll_ts": "2026-07-29T13:00:00+00:00",
                    "settlement_date": "2026-07-29",
                    "bracket_low": 80.0 + i * 1.8,
                    "bracket_high": 81.8 + i * 1.8,
                    "yes_ask": 30, "no_ask": 70, "p_yes_raw": p,
                    "minutes_to_settlement": 300.0, "is_next_day": 0,
                }) + "\n")
        assert main(["--bracket-evals", str(path), "--since", "2026-07-24"]) == 0

    def test_since_defaults_to_the_live_clean_data_clock(self):
        """The default window must track the clock, not a frozen literal.

        The clock has already moved twice as probability defects landed
        mid-window. A hard-coded default here is how this tool would quietly
        go on reporting over a contaminated range after the next move.
        """
        from src.scripts.daily_health_report import M3_CLEAN_DATA_CLOCK_START
        from src.scripts.m3_window_diagnostics import build_parser

        args = build_parser().parse_args([])
        assert args.since == M3_CLEAN_DATA_CLOCK_START


class TestRegressionAfterTheClockStarts:
    """The failure this class exists for: the tool said "mass conserved from
    2026-08-06" on a run whose own day table showed 2026-08-07 as BAD.

    ``earliest_clean_day`` stops at the first success, so it structurally
    cannot see a day that breaks afterwards. That was the right question while
    the question was "when may the clock start" and the wrong one the moment
    the clock started -- which is exactly when an operator is relying on this
    to notice a regression.
    """

    def _days(self, **days):
        """Build a mass_by_day-shaped dict: {day: {pop: stats}}."""
        def stats(mean, deficient):
            return {"n": 10, "n_judged": 10, "mean": mean,
                    "deficient": deficient}
        return {day: {"censored": stats(*c), "uncensored": stats(*u)}
                for day, (c, u) in days.items()}

    def test_a_bad_day_after_the_clean_day_is_reported(self):
        from src.scripts.m3_window_diagnostics import deficient_days_since
        per_day = self._days(**{
            "2026-08-06": ((1.00, 0), (1.00, 0)),
            "2026-08-07": ((0.99, 1), (1.00, 0)),    # one deficient ladder
        })
        assert deficient_days_since(per_day, "2026-08-06") == ["2026-08-07"]

    def test_bad_days_BEFORE_the_clean_day_are_not_reported(self):
        """The void window is expected to be dirty -- re-flagging it would
        bury the signal that matters under known history."""
        from src.scripts.m3_window_diagnostics import deficient_days_since
        per_day = self._days(**{
            "2026-08-04": ((0.80, 9), (0.99, 0)),
            "2026-08-06": ((1.00, 0), (1.00, 0)),
        })
        assert deficient_days_since(per_day, "2026-08-06") == []

    def test_no_clean_day_means_nothing_to_regress_from(self):
        from src.scripts.m3_window_diagnostics import deficient_days_since
        per_day = self._days(**{"2026-08-04": ((0.80, 9), (0.99, 0))})
        assert deficient_days_since(per_day, None) == []

    def test_the_brief_verdict_says_REGRESSION_not_conserved(self):
        """The whole point: the one-line verdict an operator reads from a
        phone must not say "conserved" while a later day is deficient."""
        from src.scripts.m3_window_diagnostics import brief_report
        # A clean day needs BOTH populations healthy: an uncensored ladder
        # and a censored one (leading zeros) that each sum to 1.0.
        clean_unc = _ladder([0.25] * 4, ts="2026-08-06T13:00:00+00:00",
                            end_date="2026-08-06")
        clean_cen = _ladder([0.0, 0.5, 0.5], station="KDEN",
                            ts="2026-08-06T13:00:00+00:00",
                            end_date="2026-08-06")
        # 0.50 -- far outside the 0.90-1.10 band. Production-shaped, so both
        # tails are open and the deficit CANNOT be excused as missing coverage.
        broken = _prod_ladder([0.0, 0.25, 0.25], station="KORD",
                              ts="2026-08-07T13:00:00+00:00",
                              end_date="2026-08-07")
        out = brief_report(clean_unc + clean_cen + broken, "2026-08-06")
        assert "REGRESSION" in out
        assert "2026-08-07" in out
        assert "mass conserved from" not in out

    def test_the_offending_ladder_is_named_so_it_can_be_chased(self):
        from src.scripts.m3_window_diagnostics import deficient_ladders
        good = _prod_ladder([0.24] * 4, cap_lo=0.02, cap_hi=0.02)
        bad = _prod_ladder([0.0, 0.25, 0.25], station="KORD")
        worst = deficient_ladders(group_ladders(good + bad))
        assert len(worst) == 1
        assert worst[0]["station"] == "KORD"


class TestCoverageLimitedLadders:
    """A ladder missing an open-ended tail bracket cannot reach 1.0 however
    correct the arithmetic is -- the market offers nowhere for that tail to go.

    Judging such a ladder measures Polymarket's listing rather than our
    probabilities. Next-day markets are listed incrementally, so judging them
    fires most mornings; a check that cries wolf daily is a check that gets
    ignored, which is how #917 and #920 both survived for weeks.

    The exemption is dangerous in exactly one direction -- excusing a real
    defect -- so every test here pins a boundary of it.
    """

    def test_the_RCSS_case_is_excused(self):
        """2026-08-07: next-day ladder, 10 brackets topping out at 89.60 with
        no open cap, mass 0.813. Polymarket had not listed the upper brackets
        yet. Reported, not judged."""
        rows = [_b(0.0, -50.0, 73.4), _b(0.0, 73.4, 75.2), _b(0.0, 75.2, 77.0),
                _b(0.0, 77.0, 78.8), _b(0.0004, 78.8, 80.6),
                _b(0.0057, 80.6, 82.4), _b(0.0427, 82.4, 84.2),
                _b(0.1608, 84.2, 86.0), _b(0.3069, 86.0, 87.8),
                _b(0.2969, 87.8, 89.6)]
        lad = group_ladders(rows)[0]
        assert abs(lad["mass"] - 0.8134) < 1e-6
        assert lad["open_bottom"] is True      # [-50, 73.4] is open
        assert lad["open_top"] is False        # nothing above 89.6
        assert lad["coverage_limited"] is True

        stats = mass_by_day([lad])["2026-07-29"]["censored"]
        assert stats["deficient"] == 0, "an excused ladder is not deficient"
        assert stats["n_excused"] == 1
        assert conserves(stats) is False, "one excused ladder is no evidence"

    def test_a_920_SHAPED_LADDER_IS_STILL_JUDGED(self):
        """The exemption must not reopen the hole it was written beside.

        #920 zeroed brackets that still EXISTED, so both tails stay open and
        the ladder remains fully judgeable. If this ever passes, the check has
        stopped catching the defect it was built for.
        """
        # Open at both ends, zeroed top and bottom, 0.80 -- degC "both" shape.
        lad = group_ladders(
            _prod_ladder([0.0, 0.0, 0.30, 0.50, 0.0, 0.0]))[0]
        assert lad["open_top"] is True
        assert lad["open_bottom"] is True
        assert lad["coverage_limited"] is False
        assert lad["mass"] < MASS_LOW

        stats = mass_by_day([lad])["2026-07-29"]["censored"]
        assert stats["deficient"] == 1
        assert stats["n_excused"] == 0
        assert conserves(stats) is False

    def test_a_coverage_limited_ladder_that_CONSERVES_still_counts(self):
        """Being excusable is not being ignored. A short-range ladder summing
        to 1.0 is ordinary evidence and must not be discarded -- discarding it
        would shrink the judged population for no reason."""
        lad = group_ladders(_ladder([0.25] * 4))[0]     # closed, sums 1.0
        assert lad["coverage_limited"] is True
        stats = mass_by_day([lad])["2026-07-29"]["uncensored"]
        assert stats["n_judged"] == 1
        assert stats["n_excused"] == 0
        assert conserves(stats) is True

    def test_an_EXCESS_is_never_excused(self):
        """Missing brackets can only lose mass. An excess has no coverage
        explanation, so coverage-limited or not, it is judged."""
        lad = group_ladders(_ladder([0.4] * 4))[0]      # 1.60, closed
        assert lad["coverage_limited"] is True
        stats = mass_by_day([lad])["2026-07-29"]["uncensored"]
        assert stats["n_excused"] == 0
        assert stats["excessive"] == 1

    def test_a_day_of_only_excused_ladders_does_not_pass(self):
        """No judgeable ladder means no evidence, and absent evidence has
        never been a pass in this tool."""
        short = [_b(0.30, -50.0, 82.0), _b(0.20, 82.0, 84.0)]   # no open top
        lad = group_ladders(short)[0]
        assert lad["coverage_limited"] is True
        stats = mass_by_day([lad])["2026-07-29"]["uncensored"]
        assert stats["n_judged"] == 0
        assert stats["mean"] is None
        assert conserves(stats) is False

    def test_an_excused_ladder_does_not_trigger_REGRESSION(self):
        from src.scripts.m3_window_diagnostics import brief_report
        healthy = (_prod_ladder([0.24] * 4, cap_lo=0.02, cap_hi=0.02,
                                ts="2026-08-06T13:00:00+00:00",
                                end_date="2026-08-06")
                   + _prod_ladder([0.0, 0.5, 0.5],
                                  ts="2026-08-06T13:00:00+00:00",
                                  end_date="2026-08-06", station="KDEN"))
        # next day: short, no open top -> excusable
        excused = [_b(0.30, -50.0, 82.0, station="RCSS",
                      ts="2026-08-07T06:00:00+00:00", end_date="2026-08-08"),
                   _b(0.20, 82.0, 84.0, station="RCSS",
                      ts="2026-08-07T06:00:00+00:00", end_date="2026-08-08")]
        out = brief_report(healthy + excused, "2026-08-06")
        assert "REGRESSION" not in out
        assert "excused" in out, "excused ladders must stay visible"

    def test_a_closed_ladder_short_after_the_clock_STILL_regresses(self):
        """The other side of the same boundary: an open-ended ladder that is
        short after the clock started is still a regression."""
        from src.scripts.m3_window_diagnostics import brief_report
        healthy = (_prod_ladder([0.24] * 4, cap_lo=0.02, cap_hi=0.02,
                                ts="2026-08-06T13:00:00+00:00",
                                end_date="2026-08-06")
                   + _prod_ladder([0.0, 0.5, 0.5],
                                  ts="2026-08-06T13:00:00+00:00",
                                  end_date="2026-08-06", station="KDEN"))
        broken = _prod_ladder([0.0, 0.30, 0.30], station="KORD",
                              ts="2026-08-07T13:00:00+00:00",
                              end_date="2026-08-07")
        out = brief_report(healthy + broken, "2026-08-06")
        assert "REGRESSION" in out
        assert "2026-08-07" in out


class TestTableAlignment:
    """The kind tables are fixed-width, so a cell wider than its column does
    not fail -- it silently shifts every column to its right. That is exactly
    what happened when the "+Nexc" suffix was added to the cell and not to the
    header, and it is invisible to every other test in this file.
    """

    def _worst_cell(self):
        """The widest cell the formatter can produce at production scale."""
        from src.scripts.m3_window_diagnostics import _period_stats
        # 9999 judged (one bad) + 999 excused, all in one kind/population
        bad = _prod_ladder([0.0, 0.10, 0.10])            # judged, deficient
        good = _prod_ladder([0.0, 0.5, 0.5])             # judged, conserving
        short = [_b(0.30, -50.0, 82.0), _b(0.20, 82.0, 84.0)]   # excused
        ladders = group_ladders(bad)[:1] * 1
        ladders += group_ladders(good) * 1
        ladders += group_ladders(short) * 1
        return _period_stats(ladders, censored=True)

    def test_a_cell_fits_its_column(self):
        from src.scripts.m3_window_diagnostics import KIND_COL_W, _period_stats
        assert len(self._worst_cell()) <= KIND_COL_W
        # and the synthetic extreme the constant was sized for
        extreme = "1.00 (9999/9999 bad+999exc)"
        assert len(extreme) <= KIND_COL_W, (
            "KIND_COL_W must fit the widest cell _period_stats can emit")
        assert _period_stats([], censored=True).strip() == "--"

    def test_header_and_data_columns_start_at_the_same_offset(self):
        """Render a report that actually contains an excused ladder, then
        check the second column begins at the same character offset in the
        header and in the data rows."""
        rows = (_prod_ladder([0.0, 0.5, 0.5], station="KDEN")
                + _prod_ladder([0.24] * 4, cap_lo=0.02, cap_hi=0.02)
                + [_b(0.30, -50.0, 82.0, station="RCSS"),
                   _b(0.20, 82.0, 84.0, station="RCSS")])
        from src.scripts.m3_window_diagnostics import KIND_SHORT
        report = build_report(rows, "2026-07-24")
        lines = report.splitlines()
        labels = tuple(label for label, _ in KIND_SHORT)

        # Both fixed-width tables: the per-day one and the per-kind one. Prose
        # in this report mentions "uncensored ladders", so headers are matched
        # on their leading field rather than on the word appearing anywhere.
        checked = 0
        for i, ln in enumerate(lines):
            if not (ln.startswith("   day ") or ln.startswith("   kind ")):
                continue
            if "uncensored" not in ln:
                continue
            # NB "uncensored" contains "censored", so the search must start
            # past the end of the first header, not one char into it.
            first = ln.index("uncensored")
            offset = ln.index("censored", first + len("uncensored"))
            for row in lines[i + 1:i + 9]:
                body = row.strip()
                if not body:
                    break
                if not (body.startswith("2026-") or body.startswith(labels)):
                    continue
                # the second cell must begin exactly where its header does, so
                # the char before it is padding, not the tail of cell one
                # The character just before the next column's start must be
                # padding. If cell one overflowed, its tail sits there instead
                # -- which is exactly how "ok  exc=1" ran into "1.000 n=1".
                # A cell may legitimately BEGIN with spaces (a "--" cell), so
                # only the collision is asserted, not the cell's first char.
                assert len(row) > offset, f"row shorter than header: {row!r}"
                assert row[offset - 1] == " ", f"column collision: {row!r}"
                checked += 1
        assert checked >= 2, f"expected rows from both tables, got {checked}"

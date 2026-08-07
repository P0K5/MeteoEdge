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
    build_report,
    classify_width,
    conserves,
    deploy_day,
    earliest_clean_day,
    group_ladders,
    ladder_stats,
    mass_by_day,
    scoreable_station_days,
    width_census,
)


def _b(p, lo, hi, station="KATL", ts="2026-07-29T13:00:00+00:00",
       end_date="2026-07-29", is_next_day=0, yes_ask=30.0, no_ask=70.0):
    return {"p_yes_raw": p, "bracket_low": lo, "bracket_high": hi,
            "station": station, "ts": ts, "end_date": end_date,
            "is_next_day_flag": is_next_day, "yes_ask": yes_ask,
            "no_ask": no_ask, "ticker": f"0x{lo}"}


def _ladder(masses, width=2.0, start=80.0, **kw):
    return [_b(m, start + i * width, start + (i + 1) * width, **kw)
            for i, m in enumerate(masses)]


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
        assert conserves({"n": 0, "mean": 1.0, "deficient": 0}) is False

    def test_a_deficient_ladder_fails_even_if_the_mean_looks_fine(self):
        assert conserves({"n": 10, "mean": 1.0, "deficient": 1}) is False

    def test_healthy_population_passes(self):
        assert conserves({"n": 10, "mean": 0.99, "deficient": 0}) is True

    def test_clean_day_requires_both_populations(self):
        """A day healthy only when uncensored is a day #920 is still live --
        today's expected state, and it must not be called clean."""
        per_day = {"2026-08-01": {
            "uncensored": {"n": 5, "mean": 1.0, "deficient": 0},
            "censored": {"n": 5, "mean": 0.31, "deficient": 5},
        }}
        assert earliest_clean_day(per_day) is None

    def test_clean_day_found_when_both_conserve(self):
        per_day = {
            "2026-08-01": {"uncensored": {"n": 5, "mean": 1.0, "deficient": 0},
                           "censored": {"n": 5, "mean": 0.31, "deficient": 5}},
            "2026-08-02": {"uncensored": {"n": 5, "mean": 1.0, "deficient": 0},
                           "censored": {"n": 5, "mean": 0.98, "deficient": 0}},
        }
        assert earliest_clean_day(per_day) == "2026-08-02"

    def test_censored_and_uncensored_are_reported_apart(self):
        rows = (_ladder([0.0, 0.0, 0.14, 0.10], station="RKSI")
                + _ladder([0.25, 0.25, 0.25, 0.25], station="KATL"))
        by_day = mass_by_day(group_ladders(rows))["2026-07-29"]
        assert by_day["censored"]["n"] == 1
        assert by_day["uncensored"]["n"] == 1
        assert by_day["uncensored"]["deficient"] == 0
        assert by_day["censored"]["deficient"] == 1


class TestScoreableAccrual:
    def test_exclusions_are_applied(self):
        """The 300 bar counts scoreable station-days, not raw ones (#932)."""
        rows = [
            _b(0.0, 80.0, 82.0, station="A"),                        # zero -> out
            _b(0.2, 80.0, 82.0, station="B", yes_ask=1.0),           # rail -> out
            _b(0.2, 80.0, 82.0, station="C"),                        # kept
        ]
        assert scoreable_station_days(rows) == {"2026-07-29": 1}

    def test_a_station_day_counts_once_across_polls(self):
        rows = [_b(0.2, 80.0, 82.0, ts="2026-07-29T13:00:00+00:00"),
                _b(0.2, 82.0, 84.0, ts="2026-07-29T14:00:00+00:00")]
        assert scoreable_station_days(rows) == {"2026-07-29": 1}


class TestReport:
    def test_it_refuses_to_clear_a_window_that_is_still_censoring(self):
        rows = (_ladder([0.25] * 4, station="KATL")
                + _ladder([0.0, 0.0, 0.14, 0.10], station="RKSI"))
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
        rows = (_ladder([0.25] * 4, station="KATL")
                + _ladder([0.0, 0.0, 0.14, 0.10], station="RKSI"))
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

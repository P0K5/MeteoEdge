"""Tests for src/scripts/bss_market_vs_model_report.py (issue #822, Pass 1).

Covers:
- apply_exclusions: p_yes_raw missing/0.0 (#820 artifact), missing market
  price, 1c/99c rail rows.
- dedupe_one_per_bracket_day: keeps the lowest-minutes_to_settlement row per
  (station, ticker, end_date).
- join_outcomes: the LEGACY settlements join, dropping rows with no
  definitive match (still reachable via --outcome-source settlements).
- resolve_candidate_outcomes (issue #865): the DEFAULT outcome path --
  Gamma-first with an observed-daily-high fallback, scoring every evaluated
  bracket instead of only the ~156 MeteoEdge actually traded. Covers the
  end_date -> settlement_date mapping, the gamma/metar provenance split, the
  never-guess drop, station-day counting, and the #861 multi-YES diagnostic.
- classify_day_segment / utc_offset_bucket: derived same-day/next-day and
  UTC-offset segmentation (the CSV carries neither column directly).
- market_p_yes / compute_bss / verdict_label: exact BSS math against
  hand-computed values, plus the BS_market == 0 degenerate case.
- Self-gating end-to-end: no local candidates data -> no report written
  (never a fabricated/synthetic number); real synthetic fixture data (built
  purely to exercise the pipeline, not presented as a finding) -> report
  written with the mandatory Pass-1 disclaimer.

All dates are synthetic (2026-0x-xx), matching the repo convention of never
anchoring test fixtures to a real "today".
"""
from __future__ import annotations

import csv
import gzip
import json
import sqlite3
from unittest.mock import patch

import pytest

from src.scripts.bss_market_vs_model_report import (
    LOW_MARKET_ROLLBACK_DATE,
    POPULATION_ALL_BRACKET,
    POPULATION_GATE_SELECTED,
    OUTCOME_SOURCE_RESOLVER,
    OUTCOME_SOURCE_SETTLEMENTS,
    REQUIRED_DISCLAIMER,
    apply_exclusions,
    build_report,
    classify_day_segment,
    compute_bss,
    dedupe_one_per_bracket_day,
    filter_rows_since,
    join_outcomes,
    load_bracket_eval_rows,
    load_candidate_rows,
    load_settlement_outcomes,
    market_p_yes,
    resolve_candidate_outcomes,
    run_report,
    sample_date_span,
    sharpness_histogram,
    station_local_date,
    utc_offset_bucket,
    verdict_label,
)

CANDIDATE_FIELDS = [
    "ts", "station", "question", "end_date", "ticker", "bracket_low",
    "bracket_high", "yes_ask", "no_ask", "p_yes", "p_yes_raw",
    "ev_yes", "ev_no", "ev_yes_raw", "ev_no_raw", "flagged_side",
    "flagged_edge", "flagged_price", "flagged_confidence",
    "minutes_to_settlement",
]


def _row(**overrides) -> dict:
    base = {
        "ts": "2026-02-01T12:00:00+00:00",
        "station": "KORD",
        "question": "Highest temperature in Chicago on Feb 1?",
        "end_date": "2026-02-01",
        "ticker": "0xabc001",
        "bracket_low": "60",
        "bracket_high": "65",
        "yes_ask": "20",
        "no_ask": "82",
        "p_yes": "0.2",
        "p_yes_raw": "0.2",
        "ev_yes": "1", "ev_no": "1", "ev_yes_raw": "1", "ev_no_raw": "1",
        "flagged_side": "NO", "flagged_edge": "5", "flagged_price": "82",
        "flagged_confidence": "0.8", "minutes_to_settlement": "300",
    }
    base.update(overrides)
    return base


def _write_candidates_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CANDIDATE_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _write_settlements_db(path, settlements):
    """settlements: list of (ticker, resolved_yes)"""
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE settlements (id INTEGER PRIMARY KEY, ts TEXT, station TEXT, "
        "ticker TEXT UNIQUE, bracket_low REAL, bracket_high REAL, actual_high_f REAL, "
        "resolved_yes INTEGER, market_final_price INTEGER, source TEXT, direction TEXT)"
    )
    for ticker, resolved_yes in settlements:
        con.execute(
            "INSERT INTO settlements (ts, station, ticker, bracket_low, bracket_high, "
            "actual_high_f, resolved_yes, source, direction) VALUES "
            "('2026-02-02T00:00:00Z','KORD',?,60,65,66.0,?,'polymarket','high')",
            (ticker, int(resolved_yes)),
        )
    con.commit()
    con.close()


def _write_observations_db(path, observations, settlements=None):
    """observations: list of (station, ts, temp_f). Mirrors the fixture shape in
    test_resolve_bracket_outcomes.py so both suites exercise the same schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if settlements is not None:
        _write_settlements_db(path, settlements)
        con = sqlite3.connect(str(path))
    else:
        con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE IF NOT EXISTS observations (id INTEGER PRIMARY KEY, ts TEXT, "
        "station TEXT, temp_f REAL, temp_native REAL, unit TEXT, current_high REAL, "
        "source TEXT, raw_json TEXT)"
    )
    for station, ts, temp_f in observations:
        con.execute(
            "INSERT INTO observations (ts, station, temp_f, temp_native, unit, source) "
            "VALUES (?, ?, ?, ?, 'F', 'metar')",
            (ts, station, temp_f, temp_f),
        )
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# apply_exclusions
# ---------------------------------------------------------------------------

class TestApplyExclusions:
    def test_missing_p_yes_raw_excluded(self):
        rows = [{"p_yes_raw": None, "yes_ask": 20.0, "no_ask": 82.0}]
        kept, counts = apply_exclusions(rows)
        assert kept == []
        assert counts["missing_p_yes_raw"] == 1

    def test_p_yes_raw_zero_artifact_excluded(self):
        rows = [{"p_yes_raw": 0.0, "yes_ask": 20.0, "no_ask": 82.0}]
        kept, counts = apply_exclusions(rows)
        assert kept == []
        assert counts["p_yes_raw_zero_artifact"] == 1

    def test_missing_market_price_excluded(self):
        rows = [{"p_yes_raw": 0.2, "yes_ask": None, "no_ask": 82.0}]
        kept, counts = apply_exclusions(rows)
        assert kept == []
        assert counts["missing_market_price"] == 1

    @pytest.mark.parametrize("yes_ask,no_ask", [(1.0, 82.0), (99.0, 82.0),
                                                (20.0, 1.0), (20.0, 99.0)])
    def test_rail_rows_excluded(self, yes_ask, no_ask):
        rows = [{"p_yes_raw": 0.2, "yes_ask": yes_ask, "no_ask": no_ask}]
        kept, counts = apply_exclusions(rows)
        assert kept == []
        assert counts["rail_1c_99c"] == 1

    def test_clean_row_kept(self):
        rows = [{"p_yes_raw": 0.2, "yes_ask": 20.0, "no_ask": 82.0}]
        kept, counts = apply_exclusions(rows)
        assert len(kept) == 1
        assert counts["kept_after_row_exclusions"] == 1
        assert counts["input_rows"] == 1

    def test_edge_rail_boundary_not_excluded(self):
        """yes_ask/no_ask strictly inside (1, 99) survive -- boundary itself excluded."""
        rows = [{"p_yes_raw": 0.2, "yes_ask": 2.0, "no_ask": 98.0}]
        kept, _ = apply_exclusions(rows)
        assert len(kept) == 1


# ---------------------------------------------------------------------------
# dedupe_one_per_bracket_day
# ---------------------------------------------------------------------------

class TestDedupe:
    def test_keeps_lowest_minutes_to_settlement(self):
        rows = [
            {"station": "KORD", "ticker": "0xabc", "end_date": "2026-02-01",
             "minutes_to_settlement": 600, "p_yes_raw": 0.30},
            {"station": "KORD", "ticker": "0xabc", "end_date": "2026-02-01",
             "minutes_to_settlement": 60, "p_yes_raw": 0.10},
            {"station": "KORD", "ticker": "0xabc", "end_date": "2026-02-01",
             "minutes_to_settlement": 300, "p_yes_raw": 0.20},
        ]
        out = dedupe_one_per_bracket_day(rows)
        assert len(out) == 1
        assert out[0]["p_yes_raw"] == 0.10

    def test_different_brackets_kept_separate(self):
        rows = [
            {"station": "KORD", "ticker": "0xaaa", "end_date": "2026-02-01",
             "minutes_to_settlement": 100, "p_yes_raw": 0.1},
            {"station": "KORD", "ticker": "0xbbb", "end_date": "2026-02-01",
             "minutes_to_settlement": 100, "p_yes_raw": 0.2},
        ]
        out = dedupe_one_per_bracket_day(rows)
        assert len(out) == 2

    def test_same_ticker_different_settlement_date_kept_separate(self):
        rows = [
            {"station": "KORD", "ticker": "0xabc", "end_date": "2026-02-01",
             "minutes_to_settlement": 100, "p_yes_raw": 0.1},
            {"station": "KORD", "ticker": "0xabc", "end_date": "2026-02-02",
             "minutes_to_settlement": 100, "p_yes_raw": 0.2},
        ]
        out = dedupe_one_per_bracket_day(rows)
        assert len(out) == 2


# ---------------------------------------------------------------------------
# join_outcomes / load_settlement_outcomes
# ---------------------------------------------------------------------------

class TestJoinOutcomes:
    def test_drops_rows_with_no_settlement(self):
        rows = [{"ticker": "0xabc"}, {"ticker": "0xdef"}]
        outcomes = {"0xabc": True}
        out, dropped = join_outcomes(rows, outcomes)
        assert len(out) == 1 and out[0]["yes_won"] is True
        assert dropped == 1

    def test_load_settlement_outcomes_from_sqlite(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        _write_settlements_db(db_path, [("0xabc", True), ("0xdef", False)])
        outcomes = load_settlement_outcomes(db_path)
        assert outcomes == {"0xabc": True, "0xdef": False}

    def test_load_settlement_outcomes_missing_db_returns_empty(self, tmp_path):
        assert load_settlement_outcomes(tmp_path / "does_not_exist.db") == {}


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------

class TestSegmentation:
    def test_same_day(self):
        row = {"ts": "2026-02-01T12:00:00+00:00", "station": "KORD",
               "end_date": "2026-02-01"}
        assert classify_day_segment(row) == "same_day"

    def test_next_day(self):
        # 23:30 UTC on Feb 1 is 17:30 America/Chicago (CST, UTC-6) on Feb 1,
        # settling Feb 2 -- next_day.
        row = {"ts": "2026-02-01T23:30:00+00:00", "station": "KORD",
               "end_date": "2026-02-02"}
        assert classify_day_segment(row) == "next_day"

    def test_other_when_gap_exceeds_one_day(self):
        row = {"ts": "2026-02-01T12:00:00+00:00", "station": "KORD",
               "end_date": "2026-02-05"}
        assert classify_day_segment(row) == "other"

    def test_none_when_station_unknown(self):
        row = {"ts": "2026-02-01T12:00:00+00:00", "station": "ZZZZ",
               "end_date": "2026-02-01"}
        assert classify_day_segment(row) is None

    def test_utc_offset_bucket_chicago_winter(self):
        row = {"ts": "2026-02-01T12:00:00+00:00", "station": "KORD"}
        assert utc_offset_bucket(row) == "UTC-6"

    def test_utc_offset_bucket_unknown_station(self):
        assert utc_offset_bucket({"ts": "2026-02-01T12:00:00+00:00", "station": "ZZZZ"}) is None

    def test_station_local_date_none_without_ts(self):
        assert station_local_date("", "KORD") is None


# ---------------------------------------------------------------------------
# market_p_yes / compute_bss / verdict_label
# ---------------------------------------------------------------------------

class TestBssMath:
    def test_market_p_yes_symmetrized(self):
        # yes_ask=20 -> 0.20 ; no_ask=82 -> 1-0.82=0.18 ; average 0.19
        assert market_p_yes({"yes_ask": 20.0, "no_ask": 82.0}) == pytest.approx(0.19)

    def test_compute_bss_exact_values(self):
        # Model perfectly right (BS_model=0), market always says 0.5 (BS_market=0.25)
        # -> BSS = 1 - 0/0.25 = 1.0
        samples = [
            {"p_yes_raw": 0.0, "yes_ask": 50.0, "no_ask": 50.0, "yes_won": False},
            {"p_yes_raw": 1.0, "yes_ask": 50.0, "no_ask": 50.0, "yes_won": True},
        ]
        stats = compute_bss(samples)
        assert stats["n"] == 2
        assert stats["bs_model"] == pytest.approx(0.0)
        assert stats["bs_market"] == pytest.approx(0.25)
        assert stats["bss"] == pytest.approx(1.0)

    def test_compute_bss_negative_when_model_worse_than_market(self):
        # Model always confidently wrong; market always right (implied via symmetrized price).
        samples = [
            {"p_yes_raw": 0.99, "yes_ask": 1.0, "no_ask": 99.0, "yes_won": False},
            {"p_yes_raw": 0.01, "yes_ask": 99.0, "no_ask": 1.0, "yes_won": True},
        ]
        stats = compute_bss(samples)
        assert stats["bss"] < 0

    def test_compute_bss_degenerate_market_returns_none_bss(self):
        # Market symmetrized price is exactly 0 or 1 and always right -> BS_market == 0.
        samples = [
            {"p_yes_raw": 0.5, "yes_ask": 0.0, "no_ask": 100.0, "yes_won": False},
        ]
        stats = compute_bss(samples)
        assert stats["bs_market"] == pytest.approx(0.0)
        assert stats["bss"] is None

    def test_compute_bss_empty_returns_none(self):
        stats = compute_bss([])
        assert stats["n"] == 0
        assert stats["bs_model"] is None
        assert stats["bss"] is None

    def test_verdict_label_thresholds(self):
        assert "edge appears real" in verdict_label(0.10)
        assert "marginal" in verdict_label(0.03)
        assert "no edge" in verdict_label(-0.1)
        assert "n/a" in verdict_label(None)

    def test_sharpness_histogram_counts(self):
        rows = sharpness_histogram([0.01, 0.02, 0.5, 0.99])
        total = sum(r["n"] for r in rows)
        assert total == 4


# ---------------------------------------------------------------------------
# End-to-end (self-gating + full pipeline on synthetic fixtures)
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_run_report_no_candidates_writes_no_report(self, tmp_path):
        """No logs/ directory at all (the common fresh-checkout / sandbox case):
        run_report() must exit 0 having written nothing -- never a fabricated
        report."""
        rc = run_report(
            candidates_csv=tmp_path / "logs" / "candidates.csv",
            db_path=tmp_path / "data" / "meteoedge.db",
            out_dir=tmp_path / "backtest_results",
        )
        assert rc == 0
        assert not (tmp_path / "backtest_results").exists()

    def test_run_report_no_settlements_writes_no_report(self, tmp_path):
        """Candidate data exists but the settlements table is empty/missing --
        still no fabricated report."""
        candidates_csv = tmp_path / "logs" / "candidates.csv"
        _write_candidates_csv(candidates_csv, [_row()])
        rc = run_report(
            candidates_csv=candidates_csv,
            db_path=tmp_path / "data" / "meteoedge.db",
            out_dir=tmp_path / "backtest_results",
            outcome_source=OUTCOME_SOURCE_SETTLEMENTS,
        )
        assert rc == 0
        assert not (tmp_path / "backtest_results").exists()

    def test_resolver_path_gates_on_missing_db_without_network(self, tmp_path):
        """Resolver path with no database must bail BEFORE any Gamma request --
        a sandbox run must cost zero HTTP calls (issue #865)."""
        candidates_csv = tmp_path / "logs" / "candidates.csv"
        _write_candidates_csv(candidates_csv, [_row()])
        with patch(
            "src.scripts.resolve_bracket_outcomes.fetch_market_resolution"
        ) as mock_fetch:
            rc = run_report(
                candidates_csv=candidates_csv,
                db_path=tmp_path / "data" / "meteoedge.db",
                out_dir=tmp_path / "backtest_results",
                outcome_source=OUTCOME_SOURCE_RESOLVER,
            )
        assert rc == 0
        assert not (tmp_path / "backtest_results").exists()
        mock_fetch.assert_not_called()

    def test_run_report_rejects_unknown_outcome_source(self, tmp_path):
        with pytest.raises(ValueError, match="outcome_source"):
            run_report(
                candidates_csv=tmp_path / "logs" / "candidates.csv",
                db_path=tmp_path / "data" / "meteoedge.db",
                out_dir=tmp_path / "backtest_results",
                outcome_source="settlments",  # typo -- must not silently fall back
            )

    def test_run_report_writes_report_with_disclaimer_on_synthetic_fixture(self, tmp_path):
        """Purely a pipeline smoke test on fabricated fixture rows (never presented
        as a real Pass-1 finding) -- confirms the mandatory disclaimer is present
        and the report reflects the exclusion funnel correctly."""
        candidates_csv = tmp_path / "logs" / "candidates.csv"
        rows = [
            _row(ticker="0x001", p_yes_raw="0.10", yes_ask="15", no_ask="87",
                 minutes_to_settlement="600"),
            _row(ticker="0x001", p_yes_raw="0.08", yes_ask="12", no_ask="90",
                 minutes_to_settlement="60"),  # final poll for 0x001 -- kept by dedupe
            _row(ticker="0x002", p_yes_raw="0.0", yes_ask="20", no_ask="82"),  # #820 artifact
            _row(ticker="0x003", p_yes_raw="0.30", yes_ask="1", no_ask="99"),  # rail
            _row(ticker="0x004", p_yes_raw="0.40", yes_ask="35", no_ask="67"),  # no settlement
        ]
        _write_candidates_csv(candidates_csv, rows)

        db_path = tmp_path / "data" / "meteoedge.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _write_settlements_db(db_path, [("0x001", False), ("0x003", True)])

        out_dir = tmp_path / "backtest_results"
        rc = run_report(candidates_csv=candidates_csv, db_path=db_path,
                        out_dir=out_dir, run_date="2026-02-15",
                        outcome_source=OUTCOME_SOURCE_SETTLEMENTS)
        assert rc == 0
        report_path = out_dir / "bss_market_vs_model_pass1_2026-02-15.md"
        assert report_path.exists()
        text = report_path.read_text(encoding="utf-8")
        assert "PASS 1 -- NOT THE DECISION GATE" in text
        assert "gate-selected" in text.lower() or "gate-selected" in REQUIRED_DISCLAIMER.lower()
        # Only 0x001 survives every filter (0x002 artifact, 0x003 rail, 0x004 no
        # settlement); dedupe keeps its final (60-minute) poll.
        assert "| **Final de-duplicated sample (n)** | **1** |" in text

    def test_load_candidates_reads_gzip_rotated_file(self, tmp_path):
        gz_path = tmp_path / "logs" / "candidates.2026-02-01.csv.gz"
        gz_path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(gz_path, "wt", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CANDIDATE_FIELDS)
            w.writeheader()
            w.writerow(_row())
        rows = load_candidate_rows(tmp_path / "logs" / "candidates.csv")
        assert len(rows) == 1
        assert rows[0]["p_yes_raw"] == 0.2


# ---------------------------------------------------------------------------
# resolve_candidate_outcomes -- the default outcome path (issue #865)
# ---------------------------------------------------------------------------

def _cand(**overrides) -> dict:
    """A single already-de-duplicated candidate row, normalized as
    load_candidate_rows() would return it (floats, not CSV strings)."""
    base = {
        "ts": "2026-02-01T18:00:00+00:00",
        "station": "KORD",
        "ticker": "0xabc001",
        "end_date": "2026-02-01",
        "bracket_low": 60.0,
        "bracket_high": 65.0,
        "yes_ask": 20.0,
        "no_ask": 82.0,
        "p_yes_raw": 0.2,
        "minutes_to_settlement": 300.0,
    }
    base.update(overrides)
    return base


class TestResolveCandidateOutcomes:
    """The core of #865: outcomes come from weather + Polymarket, not from
    whether MeteoEdge happened to trade the bracket."""

    def test_scores_a_bracket_that_was_never_traded(self, tmp_path):
        """The whole point. This ticker has no `settlements` row at all -- the
        legacy join would drop it (that is how the 2026-07-25 run fell to
        n=20); the resolver scores it from the observed daily high."""
        db_path = tmp_path / "meteoedge.db"
        _write_observations_db(db_path, [("KORD", "2026-02-01T20:00:00+00:00", 62.0)])

        rows, counts = resolve_candidate_outcomes(
            [_cand(ticker="0xnever_traded")], db_path,
            use_gamma=False, gamma_cache_path=tmp_path / "cache.json",
        )
        assert len(rows) == 1
        assert rows[0]["yes_won"] is True          # 62.0 lies in [60, 65]
        assert rows[0]["resolution_source"] == "metar"
        assert counts["n_unresolvable"] == 0

    def test_gamma_takes_precedence_over_observed_high(self, tmp_path):
        """Mirrors settle.py's precedence exactly (#860): the official on-chain
        outcome wins even when METAR would say otherwise."""
        db_path = tmp_path / "meteoedge.db"
        _write_observations_db(db_path, [("KORD", "2026-02-01T20:00:00+00:00", 62.0)])

        with patch(
            "src.scripts.resolve_bracket_outcomes.fetch_market_resolution",
            return_value=False,
        ):
            rows, counts = resolve_candidate_outcomes(
                [_cand()], db_path, gamma_cache_path=tmp_path / "cache.json",
            )
        assert rows[0]["yes_won"] is False          # gamma NO beats METAR YES
        assert rows[0]["resolution_source"] == "gamma"
        assert counts["resolved_from_gamma"] == 1
        assert counts.get("resolved_from_metar", 0) == 0

    def test_falls_back_to_observed_high_when_gamma_indecisive(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        _write_observations_db(db_path, [("KORD", "2026-02-01T20:00:00+00:00", 70.0)])

        with patch(
            "src.scripts.resolve_bracket_outcomes.fetch_market_resolution",
            return_value=None,
        ):
            rows, counts = resolve_candidate_outcomes(
                [_cand()], db_path, gamma_cache_path=tmp_path / "cache.json",
            )
        assert rows[0]["yes_won"] is False          # 70.0 outside [60, 65]
        assert rows[0]["resolution_source"] == "metar"
        assert counts["resolved_from_metar"] == 1

    def test_unresolvable_row_is_dropped_never_guessed(self, tmp_path):
        """No gamma resolution and no observation for that station-day: the row
        leaves the sample. A guessed outcome would corrupt the Brier score."""
        db_path = tmp_path / "meteoedge.db"
        _write_observations_db(db_path, [])         # no observations at all

        with patch(
            "src.scripts.resolve_bracket_outcomes.fetch_market_resolution",
            return_value=None,
        ):
            rows, counts = resolve_candidate_outcomes(
                [_cand()], db_path, gamma_cache_path=tmp_path / "cache.json",
            )
        assert rows == []
        assert counts["n_unresolvable"] == 1

    def test_end_date_is_mapped_to_settlement_date(self, tmp_path):
        """The one field that needs adapting between the two row shapes."""
        db_path = tmp_path / "meteoedge.db"
        _write_observations_db(db_path, [("KORD", "2026-02-01T20:00:00+00:00", 62.0)])

        rows, _ = resolve_candidate_outcomes(
            [_cand(end_date="2026-02-01T00:00:00Z")], db_path,
            use_gamma=False, gamma_cache_path=tmp_path / "cache.json",
        )
        assert rows[0]["settlement_date"] == "2026-02-01"

    def test_row_without_end_date_is_counted_not_crashed(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        _write_observations_db(db_path, [("KORD", "2026-02-01T20:00:00+00:00", 62.0)])

        rows, counts = resolve_candidate_outcomes(
            [_cand(end_date=""), _cand(ticker="0xok")], db_path,
            use_gamma=False, gamma_cache_path=tmp_path / "cache.json",
        )
        assert len(rows) == 1
        assert counts["n_missing_end_date"] == 1
        assert counts["n_unresolvable"] == 1

    def test_station_days_counted_not_bracket_rows(self, tmp_path):
        """Effective sample size: 3 brackets on one station-day is ONE
        independent draw, not three (docs/REMEDIATION_PLAN.md)."""
        db_path = tmp_path / "meteoedge.db"
        _write_observations_db(db_path, [("KORD", "2026-02-01T20:00:00+00:00", 62.0)])

        rows, counts = resolve_candidate_outcomes(
            [
                _cand(ticker="0x1", bracket_low=55.0, bracket_high=59.0),
                _cand(ticker="0x2", bracket_low=60.0, bracket_high=65.0),
                _cand(ticker="0x3", bracket_low=66.0, bracket_high=70.0),
            ],
            db_path, use_gamma=False, gamma_cache_path=tmp_path / "cache.json",
        )
        assert len(rows) == 3
        assert counts["n_station_days"] == 1
        assert sum(1 for r in rows if r["yes_won"]) == 1

    def test_no_network_issues_zero_requests(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        _write_observations_db(db_path, [("KORD", "2026-02-01T20:00:00+00:00", 62.0)])

        with patch(
            "src.scripts.resolve_bracket_outcomes.fetch_market_resolution"
        ) as mock_fetch:
            rows, counts = resolve_candidate_outcomes(
                [_cand()], db_path, allow_network=False,
                gamma_cache_path=tmp_path / "cache.json",
            )
        mock_fetch.assert_not_called()
        assert rows[0]["resolution_source"] == "metar"
        assert counts["gamma"]["n_skipped_offline"] == 1


class TestResolverEndToEnd:
    def _setup(self, tmp_path, rows, observations):
        candidates_csv = tmp_path / "logs" / "candidates.csv"
        _write_candidates_csv(candidates_csv, rows)
        db_path = tmp_path / "data" / "meteoedge.db"
        _write_observations_db(db_path, observations)
        return candidates_csv, db_path

    def test_report_scores_untraded_brackets_and_states_provenance(self, tmp_path):
        """End-to-end on synthetic fixture rows (never a real finding): the
        report must be written, name its ground truth, and count station-days."""
        rows = [
            _row(ticker="0x001", bracket_low="60", bracket_high="65", p_yes_raw="0.20"),
            _row(ticker="0x002", bracket_low="66", bracket_high="70", p_yes_raw="0.40",
                 yes_ask="35", no_ask="67"),
        ]
        candidates_csv, db_path = self._setup(
            tmp_path, rows, [("KORD", "2026-02-01T20:00:00+00:00", 62.0)]
        )
        out_dir = tmp_path / "backtest_results"

        with patch(
            "src.scripts.resolve_bracket_outcomes.fetch_market_resolution",
            return_value=None,
        ):
            rc = run_report(
                candidates_csv=candidates_csv, db_path=db_path, out_dir=out_dir,
                run_date="2026-02-15", outcome_source=OUTCOME_SOURCE_RESOLVER,
                gamma_cache_path=tmp_path / "cache.json",
            )
        assert rc == 0
        text = (out_dir / "bss_market_vs_model_pass1_2026-02-15.md").read_text(encoding="utf-8")

        # The Pass-1 caveat survives the change -- this is still not the M3 gate.
        assert "PASS 1 -- NOT THE DECISION GATE" in text
        # Neither bracket has a settlements row; both are still scored.
        assert "| **Final de-duplicated sample (n)** | **2** |" in text
        assert "| **Effective sample size (station-days)** | **1** |" in text
        assert "## Outcome ground truth" in text
        assert "Observed daily high fallback (`metar`) | 2" in text
        assert "Not joined to `settlements`" in text

    def test_boundary_collision_no_longer_occurs_after_861_fix(self, tmp_path):
        """#861: adjacent Celsius-derived brackets sharing an edge (84.2-86.0
        and 86.0-87.8, which are 29-30C and 30-31C) with observed_high=86.0
        no longer produce a multi-YES collision — the [lo, hi) upper-bound
        exclusive convention puts 86.0 in the second bracket only."""
        rows = [
            _row(ticker="0x001", bracket_low="84.2", bracket_high="86.0", p_yes_raw="0.30"),
            _row(ticker="0x002", bracket_low="86.0", bracket_high="87.8", p_yes_raw="0.30"),
        ]
        candidates_csv, db_path = self._setup(
            tmp_path, rows, [("KORD", "2026-02-01T20:00:00+00:00", 86.0)]
        )
        out_dir = tmp_path / "backtest_results"

        rc = run_report(
            candidates_csv=candidates_csv, db_path=db_path, out_dir=out_dir,
            run_date="2026-02-15", outcome_source=OUTCOME_SOURCE_RESOLVER,
            use_gamma=False, gamma_cache_path=tmp_path / "cache.json",
        )
        assert rc == 0
        text = (out_dir / "bss_market_vs_model_pass1_2026-02-15.md").read_text(encoding="utf-8")
        # The fix eliminates boundary collisions entirely; the
        # Impossible-outcome-exposure section is only emitted when >0.
        assert "Impossible-outcome exposure" not in text
        # Both brackets are still scored (n=2 sample, 1 station-day).
        assert "| **Final de-duplicated sample (n)** | **2** |" in text

    def test_disjoint_collision_is_attributed_to_867_not_861(self, tmp_path):
        """The defect in the 2026-07-26 report: brackets 3.6F apart cannot be an
        interval-convention problem, and must not be filed under #861."""
        rows = [
            _row(ticker="0x001", bracket_low="75.2", bracket_high="77.0", p_yes_raw="0.30"),
            _row(ticker="0x002", bracket_low="80.6", bracket_high="82.4", p_yes_raw="0.30"),
        ]
        candidates_csv, db_path = self._setup(
            tmp_path, rows, [("KORD", "2026-02-01T20:00:00+00:00", 81.0)]
        )
        out_dir = tmp_path / "backtest_results"

        # Gamma returns YES for both -- the #867 signature.
        with patch(
            "src.scripts.resolve_bracket_outcomes.fetch_market_resolution",
            return_value=True,
        ):
            rc = run_report(
                candidates_csv=candidates_csv, db_path=db_path, out_dir=out_dir,
                run_date="2026-02-15", outcome_source=OUTCOME_SOURCE_RESOLVER,
                gamma_cache_path=tmp_path / "cache.json",
            )
        assert rc == 0
        text = (out_dir / "bss_market_vs_model_pass1_2026-02-15.md").read_text(encoding="utf-8")
        assert "| `disjoint` — brackets do not touch, same direction | 1 |" in text
        assert "| `boundary` — brackets touch or overlap | 0 |" in text
        assert "`disjoint`" in text
        assert "#867" in text

    def test_clean_data_reports_zero_boundary_exposure(self, tmp_path):
        rows = [_row(ticker="0x001", bracket_low="60", bracket_high="65", p_yes_raw="0.20")]
        candidates_csv, db_path = self._setup(
            tmp_path, rows, [("KORD", "2026-02-01T20:00:00+00:00", 62.0)]
        )
        out_dir = tmp_path / "backtest_results"

        rc = run_report(
            candidates_csv=candidates_csv, db_path=db_path, out_dir=out_dir,
            run_date="2026-02-15", outcome_source=OUTCOME_SOURCE_RESOLVER,
            use_gamma=False, gamma_cache_path=tmp_path / "cache.json",
        )
        assert rc == 0
        text = (out_dir / "bss_market_vs_model_pass1_2026-02-15.md").read_text(encoding="utf-8")
        assert "Impossible-outcome check (issues #861 / #867): **0** station-days" in text

    def test_settlements_source_still_reproduces_legacy_report(self, tmp_path):
        """--outcome-source settlements must keep working unchanged, so the
        2026-07-25 n=20 report stays reproducible."""
        rows = [_row(ticker="0x001", p_yes_raw="0.20")]
        candidates_csv = tmp_path / "logs" / "candidates.csv"
        _write_candidates_csv(candidates_csv, rows)
        db_path = tmp_path / "data" / "meteoedge.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _write_settlements_db(db_path, [("0x001", True)])
        out_dir = tmp_path / "backtest_results"

        rc = run_report(
            candidates_csv=candidates_csv, db_path=db_path, out_dir=out_dir,
            run_date="2026-02-15", outcome_source=OUTCOME_SOURCE_SETTLEMENTS,
        )
        assert rc == 0
        text = (out_dir / "bss_market_vs_model_pass1_2026-02-15.md").read_text(encoding="utf-8")
        assert "joined to `settlements`" in text
        assert "Excluded: no definitive settlement match" in text
        assert "## Outcome ground truth" not in text


# ---------------------------------------------------------------------------
# Pass 2 / M3 decision gate (issue #822)
# ---------------------------------------------------------------------------

def _eval_row(**overrides) -> dict:
    """A bracket_evals JSONL row as _write_bracket_evaluations emits it."""
    base = {
        "station": "KORD",
        "ticker": "0xabc001",
        "bracket_low": 60.0,
        "bracket_high": 65.0,
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
        "direction": "high",
    }
    base.update(overrides)
    return base


def _write_bracket_evals(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class TestLoadBracketEvalRows:
    """Pass 2's loader must normalize onto the SAME row shape Pass 1 produces,
    so every downstream stage is shared verbatim between the passes."""

    def test_field_renames(self, tmp_path):
        path = tmp_path / "logs" / "bracket_evals.jsonl"
        _write_bracket_evals(path, [_eval_row()])
        rows = load_bracket_eval_rows(path)
        assert rows[0]["ts"] == "2026-02-01T18:00:00+00:00"     # from poll_ts
        assert rows[0]["end_date"] == "2026-02-01"              # from settlement_date
        assert rows[0]["p_yes_raw"] == 0.2

    def test_settlement_date_is_truncated_to_a_date(self, tmp_path):
        path = tmp_path / "logs" / "bracket_evals.jsonl"
        _write_bracket_evals(path, [_eval_row(settlement_date="2026-02-01T00:00:00Z")])
        assert load_bracket_eval_rows(path)[0]["end_date"] == "2026-02-01"

    def test_direction_is_carried_for_the_resolver(self, tmp_path):
        """#867: low-direction markets must score against the observed daily LOW."""
        path = tmp_path / "logs" / "bracket_evals.jsonl"
        _write_bracket_evals(path, [_eval_row(direction="low")])
        assert load_bracket_eval_rows(path)[0]["direction"] == "low"

    def test_rows_survive_the_shared_exclusion_and_dedupe_stages(self, tmp_path):
        """The whole design: Pass 2 rows flow through Pass 1's pipeline unchanged."""
        path = tmp_path / "logs" / "bracket_evals.jsonl"
        _write_bracket_evals(path, [
            _eval_row(ticker="0x1", p_yes_raw=0.2, minutes_to_settlement=600.0),
            _eval_row(ticker="0x1", p_yes_raw=0.3, minutes_to_settlement=60.0),
            _eval_row(ticker="0x2", p_yes_raw=0.0),      # #820 artifact
        ])
        kept, counts = apply_exclusions(load_bracket_eval_rows(path))
        assert counts["p_yes_raw_zero_artifact"] == 1
        deduped = dedupe_one_per_bracket_day(kept)
        assert len(deduped) == 1
        assert deduped[0]["p_yes_raw"] == 0.3    # lowest minutes_to_settlement


class TestPass2Segmentation:
    """bracket_evals RECORDS is_next_day; Pass 1 had to reconstruct it. A
    reconstruction that disagrees with what the scanner actually decided would
    mis-segment the decision gate."""

    def test_recorded_flag_is_preferred_over_derivation(self):
        # ts/end_date would derive 'same_day'; the recorded flag says next_day.
        row = {"ts": "2026-02-01T18:00:00+00:00", "station": "KORD",
               "end_date": "2026-02-01", "is_next_day_flag": 1}
        assert classify_day_segment(row) == "next_day"

    def test_recorded_zero_means_same_day(self):
        row = {"ts": "2026-02-01T18:00:00+00:00", "station": "KORD",
               "end_date": "2026-02-02", "is_next_day_flag": 0}
        assert classify_day_segment(row) == "same_day"

    def test_falls_back_to_derivation_when_flag_absent(self):
        """Pass 1 rows carry no flag and must keep working unchanged."""
        row = {"ts": "2026-02-01T12:00:00+00:00", "station": "KORD",
               "end_date": "2026-02-01"}
        assert classify_day_segment(row) == "same_day"


class TestDecisionGateSection:
    """The rule is pre-registered in docs/REMEDIATION_PLAN.md. The report must
    state it, and must refuse to call an underpowered number a verdict."""

    def _report(self, station_days, bss_rows):
        return build_report(
            bss_rows, {"input_rows": len(bss_rows)}, 0, "2026-08-05",
            outcome_meta={"source": OUTCOME_SOURCE_RESOLVER,
                          "counts": {"n_station_days": station_days}},
            population=POPULATION_ALL_BRACKET,
        )

    def _sample(self, p, won):
        return {"station": "KORD", "ticker": "0x1", "end_date": "2026-02-01",
                "ts": "2026-02-01T18:00:00+00:00", "p_yes_raw": p,
                "yes_ask": 20.0, "no_ask": 82.0, "yes_won": won,
                "bracket_low": 60.0, "bracket_high": 65.0,
                "settlement_date": "2026-02-01", "is_next_day_flag": 0}

    def test_pass2_uses_the_gate_disclaimer_not_pass1s(self):
        report = self._report(300, [self._sample(0.2, False)])
        assert "THIS IS THE M3 DECISION GATE" in report
        assert "PASS 1 -- NOT THE DECISION GATE" not in report

    def test_decision_rule_is_stated_before_the_verdict(self):
        report = self._report(300, [self._sample(0.2, False)])
        assert "pre-registered rule" in report
        assert report.index("BSS > 0.05") < report.index("### Verdict")

    def test_underpowered_run_is_not_a_verdict(self):
        """The n=20 Pass-1 trap: a BSS on too few station-days is not a weaker
        verdict, it is not a verdict at all."""
        report = self._report(111, [self._sample(0.2, False)])
        assert "UNDERPOWERED -- THIS IS NOT A VERDICT" in report
        assert "### Verdict" not in report
        assert "37%" in report            # 111/300

    def test_powered_run_states_the_verdict(self):
        report = self._report(300, [self._sample(0.2, False)])
        assert "### Verdict" in report
        assert "UNDERPOWERED" not in report

    def test_pass1_report_has_no_gate_section(self):
        report = build_report(
            [self._sample(0.2, False)], {"input_rows": 1}, 0, "2026-02-15",
            outcome_meta={"source": OUTCOME_SOURCE_RESOLVER,
                          "counts": {"n_station_days": 300}},
            population=POPULATION_GATE_SELECTED,
        )
        # The Pass-1 disclaimer legitimately mentions the gate when pointing
        # forward to Pass 2 -- assert on the SECTION, not the phrase.
        assert "## M3 decision gate -- the pre-registered rule" not in report
        assert "### Verdict" not in report
        assert "PASS 1 -- NOT THE DECISION GATE" in report


class TestMethodologyNoteMatchesTheResolver:
    """The methodology note is what a reader trusts instead of reading the code.

    It previously described only the HIGH path and an inclusive interval, both
    of which the resolver stopped doing (#867 direction dispatch, #861 `[lo,
    hi)`). That prose was mistaken for a defect report and cost a wrongly-filed
    issue (#902), so the note's two load-bearing claims are pinned here.
    """

    def _report(self):
        sample = {"station": "KORD", "ticker": "0x1", "end_date": "2026-07-25",
                  "ts": "2026-07-25T18:00:00+00:00", "p_yes_raw": 0.2,
                  "yes_ask": 20.0, "no_ask": 82.0, "yes_won": False,
                  "bracket_low": 60.0, "bracket_high": 65.0,
                  "settlement_date": "2026-07-25", "is_next_day_flag": 0}
        return build_report(
            [sample], {"input_rows": 1}, 0, "2026-07-29",
            outcome_meta={"source": OUTCOME_SOURCE_RESOLVER,
                          "counts": {"n_station_days": 300}},
            population=POPULATION_ALL_BRACKET,
        )

    def test_it_documents_the_low_direction_path(self):
        report = self._report()
        assert "dispatched on market direction" in report
        assert "observed daily LOW" in report

    def test_it_states_the_upper_bound_is_exclusive(self):
        """`resolve_outcome` is `[lo, hi)`. The note used to print `[lo, hi]`."""
        report = self._report()
        assert "[bracket_low, bracket_high)" in report
        assert "[bracket_low, bracket_high]" not in report


class TestDirectionGapNote:
    """Unknown-direction rows are scored against the daily HIGH, so the report
    has to say whether a LOW market could be hiding among them.

    The answer is a DATE question, never a flag question: ``ENABLE_LOW_MARKETS``
    was introduced by #733/#734 (2026-07-17) to switch low markets OFF, and they
    were scanned by default before that. Reading the flag's current value tells
    you nothing about a population collected earlier.
    """

    def _report(self, end_date, direction_counts):
        sample = {"station": "KORD", "ticker": "0x1", "end_date": end_date,
                  "ts": end_date + "T18:00:00+00:00", "p_yes_raw": 0.2,
                  "yes_ask": 20.0, "no_ask": 82.0, "yes_won": False,
                  "bracket_low": 60.0, "bracket_high": 65.0,
                  "settlement_date": end_date, "is_next_day_flag": 0}
        return build_report(
            [sample], {"input_rows": 1}, 0, "2026-07-29",
            outcome_meta={"source": OUTCOME_SOURCE_RESOLVER,
                          "counts": {"n_station_days": 300,
                                     "direction": direction_counts}},
            population=POPULATION_ALL_BRACKET,
        )

    def test_post_rollback_population_is_cleared_by_its_dates(self):
        report = self._report("2026-07-25", {"high": 1, "unknown": 5})
        assert "Not a contamination risk" in report
        assert "2026-07-25" in report
        assert "CONTAMINATION RISK" not in report

    def test_pre_rollback_population_is_flagged_as_contaminated(self):
        report = self._report("2026-07-10", {"high": 1, "unknown": 5})
        assert "CONTAMINATION RISK" in report
        assert LOW_MARKET_ROLLBACK_DATE in report
        assert "Not a contamination risk" not in report

    def test_the_safety_claim_is_never_the_flags_current_value(self):
        """Guards the exact error this note was rewritten to remove: citing
        ``ENABLE_LOW_MARKETS`` being off *now* as evidence about the past."""
        report = self._report("2026-07-25", {"high": 1, "unknown": 5})
        assert "safe only while low-direction markets are disabled" not in report.lower()

    def test_no_unknown_rows_means_no_note(self):
        report = self._report("2026-07-10", {"high": 6, "unknown": 0})
        assert "CONTAMINATION RISK" not in report
        assert "Not a contamination risk" not in report

    def test_span_spanning_the_rollback_is_treated_as_contaminated(self):
        """A population that merely *ends* after the rollback is not cleared --
        the earliest date is what decides it."""
        assert sample_date_span([{"end_date": "2026-07-10"},
                                 {"end_date": "2026-07-25"}]) == ("2026-07-10", "2026-07-25")

    def test_undatable_rows_are_ignored_by_the_span(self):
        assert sample_date_span([{"end_date": ""}, {"end_date": "2026-07-25"}]) == \
            ("2026-07-25", "2026-07-25")
        assert sample_date_span([{"end_date": ""}]) is None


class TestPass2EndToEnd:
    def test_writes_a_pass2_filename_and_gate_report(self, tmp_path):
        evals = tmp_path / "logs" / "bracket_evals.jsonl"
        _write_bracket_evals(evals, [
            _eval_row(ticker="0x1", bracket_low=60.0, bracket_high=65.0, p_yes_raw=0.20),
            _eval_row(ticker="0x2", bracket_low=66.0, bracket_high=70.0, p_yes_raw=0.40,
                      yes_ask=35.0, no_ask=67.0),
        ])
        db_path = tmp_path / "data" / "meteoedge.db"
        _write_observations_db(db_path, [("KORD", "2026-02-01T20:00:00+00:00", 62.0)])
        out_dir = tmp_path / "backtest_results"

        rc = run_report(
            candidates_csv=tmp_path / "nope.csv", db_path=db_path, out_dir=out_dir,
            run_date="2026-08-05", outcome_source=OUTCOME_SOURCE_RESOLVER,
            use_gamma=False, gamma_cache_path=tmp_path / "cache.json",
            population=POPULATION_ALL_BRACKET, bracket_evals=evals,
        )
        assert rc == 0
        report_path = out_dir / "bss_market_vs_model_pass2_2026-08-05.md"
        assert report_path.exists(), "Pass 2 must not overwrite the Pass 1 report"
        assert not (out_dir / "bss_market_vs_model_pass1_2026-08-05.md").exists()
        text = report_path.read_text(encoding="utf-8")
        assert "PASS 2 / M3 DECISION GATE" in text
        assert "UNDERPOWERED" in text          # 1 station-day of fixture data

    def test_no_bracket_evals_writes_nothing(self, tmp_path):
        rc = run_report(
            candidates_csv=tmp_path / "nope.csv",
            db_path=tmp_path / "data" / "meteoedge.db",
            out_dir=tmp_path / "backtest_results",
            population=POPULATION_ALL_BRACKET,
            bracket_evals=tmp_path / "logs" / "bracket_evals.jsonl",
        )
        assert rc == 0
        assert not (tmp_path / "backtest_results").exists()

    def test_rejects_unknown_population(self, tmp_path):
        with pytest.raises(ValueError, match="population"):
            run_report(
                candidates_csv=tmp_path / "nope.csv",
                db_path=tmp_path / "db", out_dir=tmp_path / "out",
                population="everything",
            )


class TestSinceFilter:
    """`bracket_evals` spans three incompatible probability eras -- pre-#917
    half-width °F ladders, pre-#920 truncation leaks, and clean rows from
    2026-08-06. Without a poll-date cutoff the M3 gate scores them together:
    run on 2026-08-11 that would have been ~72% pre-#920 rows, silently undoing
    the fix at the last step.
    """

    def _row(self, ts, ticker="0x1"):
        return {"ts": ts, "station": "KORD", "ticker": ticker,
                "end_date": ts[:10], "settlement_date": ts[:10],
                "p_yes_raw": 0.2, "yes_ask": 20.0, "no_ask": 82.0,
                "minutes_to_settlement": 10.0, "bracket_low": 60.0,
                "bracket_high": 65.0, "question": "", "is_next_day_flag": 0}

    def test_keeps_rows_polled_on_or_after_the_cutoff(self):
        rows = [self._row("2026-08-05T18:00:00+00:00"),
                self._row("2026-08-06T18:00:00+00:00"),
                self._row("2026-08-07T18:00:00+00:00")]
        kept, dropped = filter_rows_since(rows, "2026-08-06")
        assert [r["ts"][:10] for r in kept] == ["2026-08-06", "2026-08-07"]
        assert dropped == 1

    def test_the_cutoff_date_itself_is_included(self):
        kept, dropped = filter_rows_since([self._row("2026-08-06T00:00:00+00:00")],
                                          "2026-08-06")
        assert len(kept) == 1 and dropped == 0

    def test_none_keeps_everything(self):
        rows = [self._row("2026-07-01T18:00:00+00:00")]
        assert filter_rows_since(rows, None) == (rows, 0)

    def test_filters_on_poll_time_not_settlement_date(self):
        """A bracket polled before the cutoff for a settlement after it was
        still computed by the contaminated code. The weather that settled it is
        not in question; the probability is."""
        row = self._row("2026-08-05T22:00:00+00:00")
        row["end_date"] = row["settlement_date"] = "2026-08-06"
        kept, dropped = filter_rows_since([row], "2026-08-06")
        assert kept == [] and dropped == 1

    def test_report_states_the_window(self):
        report = build_report(
            [self._row("2026-08-06T18:00:00+00:00", ticker="a") | {"yes_won": False}],
            {"input_rows": 1, "input_rows_all_dates": 100,
             "dropped_before_since": 99, "since": "2026-08-06"},
            0, "2026-08-11",
            outcome_meta={"source": OUTCOME_SOURCE_RESOLVER,
                          "counts": {"n_station_days": 300}},
            population=POPULATION_ALL_BRACKET,
        )
        assert "Poll-date window" in report
        assert "on or after **2026-08-06**" in report
        assert "99 of 100 earlier rows excluded" in report

    def test_an_unwindowed_gate_run_is_flagged_not_silent(self):
        """The failure this guards: a gate run over every era looks exactly like
        a clean one in the output. It must not."""
        report = build_report(
            [self._row("2026-08-06T18:00:00+00:00", ticker="a") | {"yes_won": False}],
            {"input_rows": 1}, 0, "2026-08-11",
            outcome_meta={"source": OUTCOME_SOURCE_RESOLVER,
                          "counts": {"n_station_days": 300}},
            population=POPULATION_ALL_BRACKET,
        )
        assert "No `--since` given" in report
        assert "not legitimate for the M3 gate" in report

    def test_run_report_declines_an_empty_window(self, tmp_path):
        """A cutoff past every row must not write a report at all -- an empty
        BSS is worse than none, because it looks like a result."""
        evals = tmp_path / "logs" / "bracket_evals.jsonl"
        _write_bracket_evals(evals, [
            {"poll_ts": "2026-07-01T18:00:00+00:00", "station": "KORD",
             "ticker": "0x1", "settlement_date": "2026-07-01",
             "bracket_low": 60.0, "bracket_high": 65.0, "yes_ask": 20,
             "no_ask": 82, "p_yes_raw": 0.2, "minutes_to_settlement": 10.0,
             "is_next_day": 0},
        ])
        rc = run_report(tmp_path / "c.csv", tmp_path / "db.sqlite", tmp_path / "out",
                        "2026-08-11", population=POPULATION_ALL_BRACKET,
                        bracket_evals=evals, since="2026-08-06",
                        use_gamma=False, allow_network=False)
        assert rc == 0
        assert not list((tmp_path / "out").glob("*.md")) if (tmp_path / "out").exists() else True

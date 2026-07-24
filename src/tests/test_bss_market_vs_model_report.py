"""Tests for src/scripts/bss_market_vs_model_report.py (issue #822, Pass 1).

Covers:
- apply_exclusions: p_yes_raw missing/0.0 (#820 artifact), missing market
  price, 1c/99c rail rows.
- dedupe_one_per_bracket_day: keeps the lowest-minutes_to_settlement row per
  (station, ticker, end_date).
- join_outcomes: settlements join, dropping rows with no definitive match.
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
import sqlite3

import pytest

from src.scripts.bss_market_vs_model_report import (
    REQUIRED_DISCLAIMER,
    apply_exclusions,
    build_report,
    classify_day_segment,
    compute_bss,
    dedupe_one_per_bracket_day,
    join_outcomes,
    load_candidate_rows,
    load_settlement_outcomes,
    market_p_yes,
    run_report,
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
        )
        assert rc == 0
        assert not (tmp_path / "backtest_results").exists()

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
                        out_dir=out_dir, run_date="2026-02-15")
        assert rc == 0
        report_path = out_dir / "bss_market_vs_model_pass1_2026-02-15.md"
        assert report_path.exists()
        text = report_path.read_text()
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

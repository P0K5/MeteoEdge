"""Tests for scripts/prob_cap_shadow_report.py (issue #570).

Covers:
- Self-gating: below --min-days, exits cleanly with no report file; at/above,
  writes the markdown report.
- Clamp math parity with src/strategy/scanner.py's symmetric clamp.
- Clamp saturation rate + raw-p_yes distribution over a synthetic population.
- Cap simulation: the already-admitted population is invariant to cap choice,
  and newly-admitted candidates discovered from snapshots.jsonl are correctly
  attributed to the "edge" channel (the "gate_headroom" channel is exercised
  directly via classify_new_admission_channel() rather than through the full
  simulation, since it is mathematically unreachable for caps >= 0.95 against
  a fixed 0.05 gate -- see the module docstring).
- RANK_ON_RAW_PROB ordering simulation picks the higher-raw-edge candidate and
  reports the realized PnL delta correctly.
- The change/hold/extend-window recommendation heuristic.

All dates used are synthetic (2026-01-DD), unrelated to the real calendar
date the tests run on, per the #565 lesson: self-gating counts distinct
dates *present in the data*, never a window anchored to real "today".
"""
from __future__ import annotations

import csv
import json

from scripts.prob_cap_shadow_report import (
    classify_new_admission_channel,
    clamp_p_yes,
    clamp_saturation_stats,
    distinct_dates_with_raw_data,
    distribution_stats,
    recommend,
    render_report,
    run_report,
    simulate_cap_values,
    simulate_rank_on_raw_prob,
)

CANDIDATE_FIELDS = [
    "ts", "station", "ticker", "bracket_low", "bracket_high",
    "flagged_side", "flagged_price", "p_yes", "p_yes_raw",
    "ev_no", "ev_no_raw",
]
SETTLEMENT_FIELDS = CANDIDATE_FIELDS + ["actual_high", "yes_won", "candidate_won", "pnl_cents"]


def _candidate_row(**kw) -> dict:
    row = {
        "ts": "2026-01-01T10:00:00+00:00",
        "station": "KTEST",
        "ticker": "TICK-1",
        "bracket_low": 80.0,
        "bracket_high": 84.0,
        "flagged_side": "NO",
        "flagged_price": 79,
        "p_yes": 0.05,
        "p_yes_raw": 0.01,
        "ev_no": 15.0,
        "ev_no_raw": 20.0,
    }
    row.update(kw)
    return row


def _settlement_row(**kw) -> dict:
    row = _candidate_row()
    row.update({
        "actual_high": 90.0,
        "yes_won": False,
        "candidate_won": True,
        "pnl_cents": 21.0,
    })
    row.update(kw)
    return row


def _write_csv(path, fieldnames, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _write_jsonl(path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _norm_row(**kw) -> dict:
    """A row in the normalized shape produced by load_settled_candidates()."""
    row = {
        "ts": "2026-01-01T10:00:00+00:00",
        "date": "2026-01-01",
        "station": "KTEST",
        "ticker": "TICK-1",
        "side": "NO",
        "bracket_low": 80.0,
        "bracket_high": 84.0,
        "price_cents": 79.0,
        "p_yes": 0.05,
        "p_yes_raw": 0.01,
        "ev_no": 15.0,
        "ev_no_raw": 20.0,
        "actual_high": 90.0,
        "yes_won": False,
        "pnl_cents": 21.0,
    }
    row.update(kw)
    return row


def _snapshot_row(**kw) -> dict:
    row = {
        "ts": "2026-01-01T10:00:00+00:00",
        "date": "2026-01-01",
        "station": "KTEST",
        "ticker": "TICK-NEW",
        "bracket_low": 80.0,
        "bracket_high": 84.0,
        "no_ask": 79,
        "raw_p_yes": 0.01,
    }
    row.update(kw)
    return row


# ---------------------------------------------------------------------------
# Self-gating
# ---------------------------------------------------------------------------

class TestSelfGating:
    def test_below_min_days_no_report_written(self, tmp_path):
        candidates_csv = tmp_path / "logs" / "candidates.csv"
        for day in range(1, 4):  # only 3 distinct dates
            _write_csv(
                tmp_path / "logs" / f"candidates.2026-01-0{day}.csv",
                CANDIDATE_FIELDS,
                [_candidate_row(ts=f"2026-01-0{day}T10:00:00+00:00")],
            )
        out_dir = tmp_path / "backtest_results"
        rc = run_report(
            candidates_csv=candidates_csv,
            settlements_csv=tmp_path / "logs" / "settlements.csv",
            snapshots_jsonl=tmp_path / "logs" / "snapshots.jsonl",
            out_dir=out_dir,
            min_days=7,
            dry_run=False,
        )
        assert rc == 0
        assert not out_dir.exists() or list(out_dir.iterdir()) == []

    def test_at_min_days_writes_report(self, tmp_path):
        candidates_csv = tmp_path / "logs" / "candidates.csv"
        settlements_csv = tmp_path / "logs" / "settlements.csv"
        snapshots_jsonl = tmp_path / "logs" / "snapshots.jsonl"

        rows = []
        for day in range(1, 8):  # 7 distinct dates
            ts = f"2026-01-0{day}T10:00:00+00:00"
            _write_csv(
                tmp_path / "logs" / f"candidates.2026-01-0{day}.csv",
                CANDIDATE_FIELDS,
                [_candidate_row(ts=ts)],
            )
            rows.append(_settlement_row(ts=ts, ticker=f"TICK-{day}"))
        _write_csv(settlements_csv, SETTLEMENT_FIELDS, rows)
        _write_jsonl(snapshots_jsonl, [])

        out_dir = tmp_path / "backtest_results"
        rc = run_report(
            candidates_csv=candidates_csv,
            settlements_csv=settlements_csv,
            snapshots_jsonl=snapshots_jsonl,
            out_dir=out_dir,
            min_days=7,
            dry_run=False,
            report_date="2026-01-08",
        )
        assert rc == 0
        out_file = out_dir / "prob_cap_shadow_2026-01-08.md"
        assert out_file.exists()
        content = out_file.read_text()
        assert "# Prob-cap shadow report -- 2026-01-08" in content
        assert "## Cap simulation" in content

    def test_dry_run_prints_and_writes_nothing(self, tmp_path, capsys):
        candidates_csv = tmp_path / "logs" / "candidates.csv"
        settlements_csv = tmp_path / "logs" / "settlements.csv"
        snapshots_jsonl = tmp_path / "logs" / "snapshots.jsonl"
        for day in range(1, 8):
            ts = f"2026-02-0{day}T10:00:00+00:00"
            _write_csv(
                tmp_path / "logs" / f"candidates.2026-02-0{day}.csv",
                CANDIDATE_FIELDS,
                [_candidate_row(ts=ts)],
            )
        _write_csv(settlements_csv, SETTLEMENT_FIELDS, [])
        _write_jsonl(snapshots_jsonl, [])

        out_dir = tmp_path / "backtest_results"
        rc = run_report(
            candidates_csv=candidates_csv,
            settlements_csv=settlements_csv,
            snapshots_jsonl=snapshots_jsonl,
            out_dir=out_dir,
            min_days=7,
            dry_run=True,
            report_date="2026-02-08",
        )
        assert rc == 0
        assert not out_dir.exists()
        captured = capsys.readouterr()
        assert "# Prob-cap shadow report" in captured.out

    def test_distinct_dates_ignores_rows_without_raw(self, tmp_path):
        candidates_csv = tmp_path / "logs" / "candidates.csv"
        _write_csv(
            tmp_path / "logs" / "candidates.2026-03-01.csv",
            CANDIDATE_FIELDS,
            [_candidate_row(ts="2026-03-01T10:00:00+00:00", p_yes_raw="")],
        )
        _write_csv(
            tmp_path / "logs" / "candidates.2026-03-02.csv",
            CANDIDATE_FIELDS,
            [_candidate_row(ts="2026-03-02T10:00:00+00:00")],
        )
        dates = distinct_dates_with_raw_data(candidates_csv)
        assert dates == {"2026-03-02"}


# ---------------------------------------------------------------------------
# Clamp math parity with scanner.py
# ---------------------------------------------------------------------------

class TestClampMath:
    def test_floor_clamp(self):
        assert clamp_p_yes(0.0, 0.95) == 0.05
        assert clamp_p_yes(0.02, 0.97) == 0.03

    def test_ceiling_clamp(self):
        assert clamp_p_yes(1.0, 0.95) == 0.95

    def test_no_clamp_within_bounds(self):
        assert clamp_p_yes(0.5, 0.95) == 0.5

    def test_ieee_creep_guard(self):
        # 1.0 - 0.95 == 0.050000000000000044 without the round() guard.
        assert clamp_p_yes(0.049999999999, 0.95) == 0.05


# ---------------------------------------------------------------------------
# Clamp saturation + distribution
# ---------------------------------------------------------------------------

class TestClampSaturation:
    def test_saturation_rate_by_side_and_station(self):
        rows = [
            {"p_yes_raw": 0.01, "p_yes": 0.05, "side": "NO", "station": "KORD"},   # clamped
            {"p_yes_raw": 0.05, "p_yes": 0.05, "side": "NO", "station": "KORD"},   # not clamped
            {"p_yes_raw": 0.99, "p_yes": 0.95, "side": "YES", "station": "KMIA"},  # clamped
        ]
        stats = clamp_saturation_stats(rows)
        assert stats["total"] == 3
        assert stats["total_clamped"] == 2
        assert stats["by_side"]["NO"] == [1, 2]
        assert stats["by_side"]["YES"] == [1, 1]
        assert stats["by_station"]["KORD"] == [1, 2]
        assert stats["by_station"]["KMIA"] == [1, 1]
        assert sorted(stats["clamped_raw_values"]) == [0.01, 0.99]

    def test_rows_without_raw_excluded(self):
        rows = [{"p_yes_raw": None, "p_yes": 0.05, "side": "NO", "station": "KORD"}]
        stats = clamp_saturation_stats(rows)
        assert stats["total"] == 0


class TestDistributionStats:
    def test_basic_stats(self):
        stats = distribution_stats([0.01, 0.02, 0.03, 0.04])
        assert stats["count"] == 4
        assert stats["min"] == 0.01
        assert stats["max"] == 0.04
        assert abs(stats["mean"] - 0.025) < 1e-9
        assert "p25" in stats and "p75" in stats

    def test_empty(self):
        assert distribution_stats([]) == {"count": 0}


# ---------------------------------------------------------------------------
# Cap simulation + two-channel breakout
# ---------------------------------------------------------------------------

class TestClassifyChannel:
    def test_gate_headroom_when_gate_newly_passes(self):
        assert classify_new_admission_channel(False, True) == "gate_headroom"

    def test_edge_when_gate_already_passed(self):
        assert classify_new_admission_channel(True, True) == "edge"


class TestSimulateCapValues:
    def test_already_admitted_population_invariant_to_cap(self):
        """A settled NO row stays admitted (and counted once) at every cap."""
        settled = [_norm_row(ticker="TICK-BASE")]
        results = simulate_cap_values(settled, snapshot_rows=[], cap_values=(0.95, 0.97, 0.98))
        for cap in (0.95, 0.97, 0.98):
            assert results[cap]["already_admitted_count"] == 1
            assert results[cap]["newly_admitted_count"] == 0
            assert results[cap]["gate_headroom_channel_count"] == 0

    def test_edge_channel_discovers_new_no_candidate(self):
        """A snapshot with no matching settled row, sub-threshold at the deployed
        cap (ev_no=95-79-fee<15) but newly clears MIN_EDGE_CENTS at cap=0.97/0.98
        (ev_no=97-79-fee / 98-79-fee), is discovered as an "edge" admission.
        """
        # Seed the (station, date) -> actual_high lookup via an unrelated settled row.
        baseline = _norm_row(ticker="TICK-BASELINE", actual_high=90.0, yes_won=False)
        snapshot = _snapshot_row(ticker="TICK-NEW", no_ask=79, raw_p_yes=0.01)

        results = simulate_cap_values([baseline], [snapshot], cap_values=(0.95, 0.97, 0.98))

        assert results[0.95]["newly_admitted_count"] == 0
        for cap in (0.97, 0.98):
            r = results[cap]
            assert r["newly_admitted_count"] == 1
            assert r["edge_channel_count"] == 1
            assert r["gate_headroom_channel_count"] == 0
            # actual_high=90 outside bracket 80-84 -> NO wins -> pnl = 100 - 79 = 21
            assert r["total_pnl_cents"] == baseline["pnl_cents"] + 21.0

    def test_unresolved_new_candidate_excluded_from_pnl(self):
        """A discovered candidate whose (station, date) has no settlement data
        is counted as unresolved and excluded from win-rate/PnL."""
        snapshot = _snapshot_row(ticker="TICK-ORPHAN", station="KORPHAN", no_ask=79, raw_p_yes=0.01)
        results = simulate_cap_values([], [snapshot], cap_values=(0.95, 0.97))
        assert results[0.97]["newly_admitted_count"] == 0
        assert results[0.97]["unresolved_new_count"] == 1


# ---------------------------------------------------------------------------
# RANK_ON_RAW_PROB simulation
# ---------------------------------------------------------------------------

class TestRankOnRawProbSimulation:
    def test_divergent_pick_and_pnl_delta(self):
        ts = "2026-01-01T10:00:00+00:00"
        scan_first = _norm_row(
            ts=ts, ticker="TICK-A", ev_no_raw=15.0, yes_won=True, pnl_cents=-70.0,
        )
        raw_preferred = _norm_row(
            ts=ts, ticker="TICK-B", ev_no_raw=20.0, yes_won=False, pnl_cents=25.0,
        )
        result = simulate_rank_on_raw_prob([scan_first, raw_preferred])
        assert result["polls_with_multiple_no_candidates"] == 1
        assert result["divergent_picks"] == 1
        assert result["pnl_delta_cents"] == 25.0 - (-70.0)
        assert result["raw_pick_wins"] == 1
        assert result["capped_pick_wins"] == 0

    def test_no_divergence_with_single_candidate(self):
        result = simulate_rank_on_raw_prob([_norm_row()])
        assert result["divergent_picks"] == 0


# ---------------------------------------------------------------------------
# Recommendation heuristic
# ---------------------------------------------------------------------------

class TestRecommend:
    _baseline = {"win_rate": 0.6, "total_pnl_cents": 100.0, "newly_admitted_count": 0,
                 "gate_headroom_channel_count": 0}

    def test_candidate_to_raise(self):
        results = {
            0.95: self._baseline,
            0.97: {"win_rate": 0.62, "total_pnl_cents": 150.0,
                   "newly_admitted_count": 10, "gate_headroom_channel_count": 0},
        }
        rec = recommend(results, baseline_cap=0.95, min_new_sample=5)
        assert "CANDIDATE TO RAISE CAP" in rec

    def test_hold_on_pnl_regression(self):
        results = {
            0.95: self._baseline,
            0.97: {"win_rate": 0.3, "total_pnl_cents": 80.0,
                   "newly_admitted_count": 10, "gate_headroom_channel_count": 0},
        }
        rec = recommend(results, baseline_cap=0.95, min_new_sample=5)
        assert "HOLD" in rec

    def test_extend_window_on_small_sample(self):
        results = {
            0.95: self._baseline,
            0.97: {"win_rate": 0.6, "total_pnl_cents": 120.0,
                   "newly_admitted_count": 2, "gate_headroom_channel_count": 0},
        }
        rec = recommend(results, baseline_cap=0.95, min_new_sample=5)
        assert "EXTEND WINDOW" in rec


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class TestRenderReport:
    def test_contains_expected_sections(self):
        saturation = {"total": 0, "total_clamped": 0, "by_side": {}, "by_station": {}}
        cap_results = {
            0.95: {"trade_count": 0, "already_admitted_count": 0, "newly_admitted_count": 0,
                   "unresolved_new_count": 0, "win_rate": None, "total_pnl_cents": 0.0,
                   "edge_channel_count": 0, "gate_headroom_channel_count": 0},
        }
        rank_sim = {"polls_with_multiple_no_candidates": 0, "divergent_picks": 0,
                    "pnl_delta_cents": 0.0, "raw_pick_wins": 0, "capped_pick_wins": 0}
        report = render_report(
            "2026-01-08", 7, saturation, {"count": 0}, cap_results, rank_sim,
            "HOLD -- test", (0.95,),
        )
        assert "# Prob-cap shadow report -- 2026-01-08" in report
        assert "## Clamp saturation" in report
        assert "## Distribution of p_yes_raw" in report
        assert "## Cap simulation" in report
        assert "## RANK_ON_RAW_PROB=true simulated ordering effect" in report
        assert "## Recommendation" in report
        assert "HOLD -- test" in report

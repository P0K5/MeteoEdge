"""Tests for scripts/prob_cap_shadow_report.py (issue #570, DB path #682).

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
- Issue #682: the DB-backed path (meteoedge.db::trades for settled candidates,
  analytics.db::snapshot_archive + live jsonl for population saturation,
  guardrail_events as a cross-check, observations-derived actual highs for
  newly-discovered candidates) against small synthetic SQLite fixtures, plus
  the empty-environment degrade-gracefully behavior (missing DB file, or a
  DB file that exists but has none of the expected tables yet).

All dates used are synthetic (2026-01-DD) or fixed historical dates already
in the past relative to any real run (2026-07-0x), never a window anchored to
real "today", per the #565 lesson: self-gating counts distinct dates
*present in the data*.
"""
from __future__ import annotations

import csv
import json
import sqlite3

import pytest

from scripts.prob_cap_shadow_report import (
    classify_new_admission_channel,
    clamp_p_yes,
    clamp_saturation_stats,
    distinct_dates_with_raw_data,
    distinct_dates_with_raw_data_db,
    distribution_stats,
    guardrail_cap_applied_count,
    load_settled_candidates_db,
    merge_saturation_dicts,
    population_saturation_from_archive,
    population_saturation_from_recent_jsonl,
    recommend,
    render_report,
    run_report,
    simulate_cap_values,
    simulate_rank_on_raw_prob,
    synthetic_no_pnl_cents,
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


def _make_trades_db(path, rows) -> None:
    """Minimal synthetic meteoedge.db::trades fixture (issue #682 DB path).

    is_next_day defaults to 0 (issue #704) for every row that doesn't
    explicitly pass it, matching the real trades table's migration default.
    """
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE trades (ts TEXT, station TEXT, ticker TEXT, bracket_low REAL, "
        "bracket_high REAL, side TEXT, p_yes_raw REAL, actual_price INTEGER, "
        "pnl REAL, settled_at TEXT, is_next_day INTEGER NOT NULL DEFAULT 0)"
    )
    for r in rows:
        con.execute(
            "INSERT INTO trades (ts, station, ticker, bracket_low, bracket_high, side, "
            "p_yes_raw, actual_price, pnl, settled_at, is_next_day) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                r.get("ts"), r.get("station"), r.get("ticker"), r.get("bracket_low"),
                r.get("bracket_high"), r.get("side"), r.get("p_yes_raw"),
                r.get("actual_price"), r.get("pnl"), r.get("settled_at"),
                r.get("is_next_day", 0),
            ),
        )
    con.commit()
    con.close()


def _make_snapshot_archive_db(path, rows) -> None:
    """Minimal synthetic analytics.db::snapshot_archive fixture (issue #682)."""
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE snapshot_archive (ts TEXT, station TEXT, ticker TEXT, "
        "raw_p_yes REAL, capped_p_yes REAL)"
    )
    for r in rows:
        con.execute(
            "INSERT INTO snapshot_archive (ts, station, ticker, raw_p_yes, capped_p_yes) "
            "VALUES (?,?,?,?,?)",
            (r.get("ts"), r.get("station"), r.get("ticker"), r.get("raw_p_yes"),
             r.get("capped_p_yes")),
        )
    con.commit()
    con.close()


def _make_guardrail_events_db(path, rows) -> None:
    """Minimal synthetic meteoedge.db::guardrail_events fixture (issue #682)."""
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE guardrail_events (ts TEXT, event_type TEXT, station TEXT)")
    for r in rows:
        con.execute(
            "INSERT INTO guardrail_events (ts, event_type, station) VALUES (?,?,?)",
            (r.get("ts"), r.get("event_type", "cap_applied"), r.get("station")),
        )
    con.commit()
    con.close()


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


class TestSyntheticNoPnl:
    def test_win_pays_hundred_minus_price(self):
        assert synthetic_no_pnl_cents(79.0, no_won=True) == pytest.approx(21.0)

    def test_loss_is_negative_price(self):
        assert synthetic_no_pnl_cents(79.0, no_won=False) == pytest.approx(-79.0)


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
        cap but newly clears MIN_EDGE_CENTS at higher cap values due to lower
        0.05% weather fee (vs old 0.07 crypto rate + 1c floor).
        With deployed_cap=0.95 and no_ask=80: ev_no = (95-80) - fee(80) = 15 - 0.8 = 14.2 < 15
        At cap=0.97: ev_no = (97-80) - 0.8 = 16.2 > 15 (NEWLY ADMITTED)
        """
        # Seed the (station, date) -> actual_high lookup via an unrelated settled row.
        baseline = _norm_row(ticker="TICK-BASELINE", actual_high=90.0, yes_won=False)
        # Use no_ask=80c instead of 79c so it remains sub-threshold at deployed_cap=0.95
        snapshot = _snapshot_row(ticker="TICK-NEW", no_ask=80, raw_p_yes=0.01)

        results = simulate_cap_values([baseline], [snapshot], cap_values=(0.95, 0.97, 0.98))

        assert results[0.95]["newly_admitted_count"] == 0
        for cap in (0.97, 0.98):
            r = results[cap]
            assert r["newly_admitted_count"] == 1
            assert r["edge_channel_count"] == 1
            assert r["gate_headroom_channel_count"] == 0
            # actual_high=90 outside bracket 80-84 -> NO wins -> pnl = 100 - 80 = 20
            assert r["total_pnl_cents"] == baseline["pnl_cents"] + 20.0

    def test_unresolved_new_candidate_excluded_from_pnl(self):
        """A discovered candidate whose (station, date) has no settlement data
        is counted as unresolved and excluded from win-rate/PnL.
        Use no_ask=80c to avoid admission at deployed_cap=0.95."""
        snapshot = _snapshot_row(ticker="TICK-ORPHAN", station="KORPHAN", no_ask=80, raw_p_yes=0.01)
        results = simulate_cap_values([], [snapshot], cap_values=(0.95, 0.97))
        assert results[0.97]["newly_admitted_count"] == 0
        assert results[0.97]["unresolved_new_count"] == 1

    def test_pnl_uses_synthetic_basis_not_real_account_pnl(self):
        """Issue #723: the cap-simulation PnL total must be the per-$1-notional
        synthetic value (from entry price + outcome), NOT the real account
        `pnl_cents`, so it is comparable across caps and with newly-admitted
        rows. Uses a MIXED fixture where the two bases deliberately differ.

        Two already-admitted NO rows, no snapshots (so the population is
        cap-invariant and no synthetic newly-admitted rows are added):
          * winner:  entry 80c, NO won  -> synthetic 100-80 = +20.0; real +0.40
          * loser:   entry 80c, NO lost -> synthetic     -80 = -80.0; real -2.00
        Synthetic total = 20.0 - 80.0 = -60.0 (differs from the real -1.60).
        """
        winner = _norm_row(ticker="W", price_cents=80.0, yes_won=False, pnl_cents=0.40)
        loser = _norm_row(ticker="L", price_cents=80.0, yes_won=True, pnl_cents=-2.00)
        results = simulate_cap_values([winner, loser], snapshot_rows=[], cap_values=(0.95,))
        r = results[0.95]

        # Total PnL is the synthetic per-$1 basis, not the real account sum.
        assert r["total_pnl_cents"] == pytest.approx(-60.0)
        assert r["total_pnl_cents"] != pytest.approx(-1.60)
        # Real account PnL is reported separately, already-admitted only.
        assert r["already_admitted_real_pnl"] == pytest.approx(-1.60)

    def test_real_pnl_is_none_when_a_resolved_row_lacks_stored_pnl(self):
        """already_admitted_real_pnl degrades to None if any resolved row is
        missing its real pnl_cents, while total_pnl_cents (synthetic) still
        computes from the entry price + outcome."""
        row = _norm_row(ticker="X", price_cents=75.0, yes_won=False, pnl_cents=None)
        results = simulate_cap_values([row], snapshot_rows=[], cap_values=(0.95,))
        r = results[0.95]
        assert r["already_admitted_real_pnl"] is None
        assert r["total_pnl_cents"] == pytest.approx(25.0)  # 100 - 75, NO won


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

    def test_pnl_columns_and_basis_note_present(self):
        """Issue #723: the normalized per-$1 column, the separate real-account
        column, and the updated basis note all render; the stale mixed-units
        caveat does not."""
        saturation = {"total": 0, "total_clamped": 0, "by_side": {}, "by_station": {}}
        cap_results = {
            0.95: {"trade_count": 2, "already_admitted_count": 2, "newly_admitted_count": 0,
                   "unresolved_new_count": 0, "win_rate": 0.5, "total_pnl_cents": -60.0,
                   "already_admitted_real_pnl": -1.60,
                   "edge_channel_count": 0, "gate_headroom_channel_count": 0},
        }
        rank_sim = {"polls_with_multiple_no_candidates": 0, "divergent_picks": 0,
                    "pnl_delta_cents": 0.0, "raw_pick_wins": 0, "capped_pick_wins": 0}
        report = render_report(
            "2026-01-08", 7, saturation, {"count": 0}, cap_results, rank_sim,
            "HOLD -- test", (0.95,),
        )
        assert "PnL/$1 notional (c)" in report
        assert "Real PnL (acct, ref)" in report
        assert "**PnL basis (issue #723):**" in report
        assert "-1.60" in report  # real account PnL rendered
        assert "Unit caveat (DB path, issue #682)" not in report

    def test_population_saturation_section_only_when_provided(self):
        saturation = {"total": 0, "total_clamped": 0, "by_side": {}, "by_station": {}}
        cap_results = {
            0.95: {"trade_count": 0, "already_admitted_count": 0, "newly_admitted_count": 0,
                   "unresolved_new_count": 0, "win_rate": None, "total_pnl_cents": 0.0,
                   "edge_channel_count": 0, "gate_headroom_channel_count": 0},
        }
        rank_sim = {"polls_with_multiple_no_candidates": 0, "divergent_picks": 0,
                    "pnl_delta_cents": 0.0, "raw_pick_wins": 0, "capped_pick_wins": 0}

        no_pop = render_report(
            "2026-01-08", 7, saturation, {"count": 0}, cap_results, rank_sim,
            "HOLD -- test", (0.95,),
        )
        assert "## Population-level clamp saturation" not in no_pop

        pop = {"total": 10, "total_clamped": 8, "by_station": {"KORD": [8, 10]},
               "guardrail_cap_applied": 8}
        with_pop = render_report(
            "2026-01-08", 7, saturation, {"count": 0}, cap_results, rank_sim,
            "HOLD -- test", (0.95,), population_saturation=pop,
        )
        assert "## Population-level clamp saturation" in with_pop
        assert "8/10" in with_pop
        assert "KORD" in with_pop


# ---------------------------------------------------------------------------
# DB-backed loading (issue #682, 2026-07-10 diagnosis)
# ---------------------------------------------------------------------------

class TestDistinctDatesWithRawDataDb:
    def test_counts_distinct_dates_with_raw(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        _make_trades_db(db_path, [
            {"ts": "2026-07-03T10:00:00+00:00", "p_yes_raw": 0.01},
            {"ts": "2026-07-04T10:00:00+00:00", "p_yes_raw": 0.02},
            {"ts": "2026-07-05T10:00:00+00:00", "p_yes_raw": None},  # excluded
        ])
        assert distinct_dates_with_raw_data_db(db_path) == {"2026-07-03", "2026-07-04"}

    def test_since_ts_filter(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        _make_trades_db(db_path, [
            {"ts": "2026-07-01T10:00:00+00:00", "p_yes_raw": 0.01},
            {"ts": "2026-07-05T10:00:00+00:00", "p_yes_raw": 0.02},
        ])
        dates = distinct_dates_with_raw_data_db(db_path, since_ts="2026-07-03")
        assert dates == {"2026-07-05"}

    def test_missing_db_file_returns_empty(self, tmp_path):
        assert distinct_dates_with_raw_data_db(tmp_path / "nope.db") == set()

    def test_db_exists_but_no_trades_table_returns_empty(self, tmp_path):
        db_path = tmp_path / "empty.db"
        sqlite3.connect(str(db_path)).close()
        assert distinct_dates_with_raw_data_db(db_path) == set()


class TestLoadSettledCandidatesDb:
    def test_reconstructs_p_yes_and_no_side_outcome(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        _make_trades_db(db_path, [{
            "ts": "2026-07-05T10:00:00+00:00", "station": "KORD", "ticker": "TICK-1",
            "bracket_low": 80.0, "bracket_high": 84.0, "side": "NO",
            "p_yes_raw": 0.001, "actual_price": 22, "pnl": 0.78,
            "settled_at": "2026-07-06T00:00:00+00:00",
        }])
        rows = load_settled_candidates_db(db_path, since_ts="2026-07-01")
        assert len(rows) == 1
        r = rows[0]
        assert r["p_yes_raw"] == 0.001
        assert r["p_yes"] == 0.05  # clamp_p_yes(0.001, 0.95)
        assert r["price_cents"] == 22.0
        assert r["pnl_cents"] == 0.78
        # NO-side trade won money (pnl > 0) -> YES did not happen -> yes_won False
        assert r["yes_won"] is False

    def test_yes_side_outcome_mapping(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        _make_trades_db(db_path, [{
            "ts": "2026-07-05T10:00:00+00:00", "station": "KORD", "ticker": "TICK-2",
            "bracket_low": 80.0, "bracket_high": 84.0, "side": "YES",
            "p_yes_raw": 0.99, "actual_price": 90, "pnl": 1.5,
            "settled_at": "2026-07-06T00:00:00+00:00",
        }])
        rows = load_settled_candidates_db(db_path, since_ts="2026-07-01")
        assert rows[0]["yes_won"] is True

    def test_excludes_unsettled_and_missing_raw_rows(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        _make_trades_db(db_path, [
            {"ts": "2026-07-05T10:00:00+00:00", "station": "KORD", "ticker": "T1",
             "side": "NO", "p_yes_raw": 0.01, "actual_price": 20, "pnl": 0.5,
             "settled_at": None},
            {"ts": "2026-07-05T10:00:00+00:00", "station": "KORD", "ticker": "T2",
             "side": "NO", "p_yes_raw": None, "actual_price": 20, "pnl": 0.5,
             "settled_at": "2026-07-06T00:00:00+00:00"},
        ])
        assert load_settled_candidates_db(db_path, since_ts="2026-07-01") == []

    def test_missing_db_returns_empty_list(self, tmp_path):
        assert load_settled_candidates_db(tmp_path / "nope.db", since_ts="2026-07-01") == []

    def test_excludes_next_day_rows(self, tmp_path):
        """Issue #704: this is exactly the settled-candidates population
        Amendment 1 (#682) was written to keep clean -- a next-day shadow
        row (different sigma/lead-time regime, #687) must not appear here
        even though it otherwise satisfies settled_at/p_yes_raw."""
        db_path = tmp_path / "meteoedge.db"
        _make_trades_db(db_path, [
            {"ts": "2026-07-05T10:00:00+00:00", "station": "KORD", "ticker": "SAME-DAY",
             "side": "NO", "p_yes_raw": 0.01, "actual_price": 20, "pnl": 0.5,
             "settled_at": "2026-07-06T00:00:00+00:00", "is_next_day": 0},
            {"ts": "2026-07-05T10:00:00+00:00", "station": "KORD", "ticker": "NEXT-DAY",
             "side": "NO", "p_yes_raw": 0.01, "actual_price": 20, "pnl": 0.5,
             "settled_at": "2026-07-06T00:00:00+00:00", "is_next_day": 1},
        ])
        rows = load_settled_candidates_db(db_path, since_ts="2026-07-01")
        assert len(rows) == 1
        assert rows[0]["ticker"] == "SAME-DAY"


class TestPopulationSaturation:
    def test_from_archive(self, tmp_path):
        db_path = tmp_path / "analytics.db"
        _make_snapshot_archive_db(db_path, [
            {"ts": "2026-07-05T10:00:00+00:00", "station": "KORD", "ticker": "T1",
             "raw_p_yes": 0.01, "capped_p_yes": 0.05},
            {"ts": "2026-07-05T10:05:00+00:00", "station": "KORD", "ticker": "T2",
             "raw_p_yes": 0.5, "capped_p_yes": 0.5},
        ])
        stats = population_saturation_from_archive(db_path, since_ts="2026-07-01")
        assert stats["total"] == 2
        assert stats["total_clamped"] == 1
        assert stats["by_station"]["KORD"] == [1, 2]

    def test_missing_analytics_db_returns_zeroed_stats(self, tmp_path):
        stats = population_saturation_from_archive(tmp_path / "nope.db", since_ts="2026-07-01")
        assert stats["total"] == 0
        assert stats["total_clamped"] == 0

    def test_recent_jsonl_only_counts_rows_after_cutoff(self, tmp_path):
        snapshots = tmp_path / "logs" / "snapshots.jsonl"
        _write_jsonl(snapshots, [
            {"ts": "2026-07-05T10:00:00+00:00", "station": "KORD", "ticker": "OLD",
             "no_ask": 20, "raw_p_yes": 0.01},
            {"ts": "2026-07-05T12:00:00+00:00", "station": "KORD", "ticker": "NEW",
             "no_ask": 20, "raw_p_yes": 0.4},
        ])
        stats = population_saturation_from_recent_jsonl(
            snapshots, since_ts="2026-07-01", after_ts="2026-07-05T11:00:00+00:00",
        )
        assert stats["total"] == 1

    def test_merge_sums_across_sources(self):
        a = {"total": 2, "total_clamped": 1, "by_station": {"KORD": [1, 2]},
             "clamped_raw_values": [0.01]}
        b = {"total": 3, "total_clamped": 2, "by_station": {"KORD": [1, 1], "KMIA": [1, 2]},
             "clamped_raw_values": [0.02, 0.03]}
        merged = merge_saturation_dicts(a, b)
        assert merged["total"] == 5
        assert merged["total_clamped"] == 3
        assert merged["by_station"]["KORD"] == [2, 3]
        assert merged["by_station"]["KMIA"] == [1, 2]
        assert sorted(merged["clamped_raw_values"]) == [0.01, 0.02, 0.03]


class TestGuardrailCrossCheck:
    def test_counts_cap_applied_events_in_window(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        _make_guardrail_events_db(db_path, [
            {"ts": "2026-07-05T10:00:00+00:00", "event_type": "cap_applied"},
            {"ts": "2026-07-05T10:01:00+00:00", "event_type": "cap_applied"},
            {"ts": "2026-07-05T10:02:00+00:00", "event_type": "correction_applied"},
            {"ts": "2026-06-01T10:00:00+00:00", "event_type": "cap_applied"},  # before window
        ])
        assert guardrail_cap_applied_count(db_path, since_ts="2026-07-01") == 2

    def test_missing_table_returns_none(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        sqlite3.connect(str(db_path)).close()
        assert guardrail_cap_applied_count(db_path, since_ts="2026-07-01") is None


class TestSimulateCapValuesExtraActualHighLookup:
    def test_extra_lookup_resolves_newly_discovered_candidate(self):
        """DB path has no actual_high on settled rows; the caller (run_report)
        supplies it separately via observations-derived highs. Mirrors
        test_edge_channel_discovers_new_no_candidate using extra_actual_high_lookup.
        Use no_ask=80c so it remains sub-threshold at deployed_cap=0.95.
        """
        snapshot = _snapshot_row(ticker="TICK-NEW", station="KNEW", no_ask=80, raw_p_yes=0.01)
        extra_lookup = {("KNEW", "2026-01-01"): 90.0}  # outside bracket 80-84 -> NO wins

        results = simulate_cap_values(
            [], [snapshot], cap_values=(0.95, 0.97), extra_actual_high_lookup=extra_lookup,
        )
        assert results[0.95]["newly_admitted_count"] == 0
        r = results[0.97]
        assert r["newly_admitted_count"] == 1
        assert r["unresolved_new_count"] == 0
        assert r["edge_channel_count"] == 1
        assert r["total_pnl_cents"] == 20.0  # 100 - no_ask(80)

    def test_settled_row_actual_high_takes_precedence_over_extra_lookup(self):
        """If a (station, date) is resolvable both ways, the settled-row value
        (the outcome of an actual trade) wins over the supplementary
        observations-derived lookup, per simulate_cap_values()'s documented
        precedence. Chosen so the two sources disagree on which side won:
        settled actual_high=82 falls inside bracket 80-84 (YES won, NO loses,
        pnl=-no_ask); the extra lookup's 90 would fall outside (NO wins,
        pnl=+20) if it were used instead.
        Use no_ask=80c so it's newly admitted at cap=0.97.
        """
        baseline = _norm_row(ticker="TICK-BASELINE", station="KNEW", actual_high=82.0)
        snapshot = _snapshot_row(ticker="TICK-NEW", station="KNEW", no_ask=80, raw_p_yes=0.01)
        extra_lookup = {("KNEW", "2026-01-01"): 90.0}

        results = simulate_cap_values(
            [baseline], [snapshot], cap_values=(0.97,), extra_actual_high_lookup=extra_lookup,
        )
        r = results[0.97]
        assert r["newly_admitted_count"] == 1
        # -80 (settled row's actual_high used) not +20 (extra lookup's value)
        assert r["total_pnl_cents"] == baseline["pnl_cents"] + (-80.0)


class TestRunReportDbPath:
    def test_db_path_end_to_end(self, tmp_path):
        meteoedge_db = tmp_path / "meteoedge.db"
        analytics_db = tmp_path / "analytics.db"
        rows = [
            {
                "ts": f"2026-07-{day:02d}T10:00:00+00:00", "station": "KORD",
                "ticker": f"TICK-{day}", "bracket_low": 80.0, "bracket_high": 84.0,
                "side": "NO", "p_yes_raw": 0.01, "actual_price": 20, "pnl": 0.5,
                "settled_at": f"2026-07-{day:02d}T12:00:00+00:00",
            }
            for day in range(3, 11)  # 2026-07-03..2026-07-10 -> 8 distinct dates
        ]
        _make_trades_db(meteoedge_db, rows)
        _make_snapshot_archive_db(analytics_db, [
            {"ts": "2026-07-05T10:00:00+00:00", "station": "KORD", "ticker": "S1",
             "raw_p_yes": 0.01, "capped_p_yes": 0.05},
        ])

        out_dir = tmp_path / "backtest_results"
        rc = run_report(
            candidates_csv=tmp_path / "logs" / "candidates.csv",
            settlements_csv=tmp_path / "logs" / "settlements.csv",
            snapshots_jsonl=tmp_path / "logs" / "snapshots.jsonl",
            out_dir=out_dir,
            min_days=7,
            dry_run=False,
            report_date="2026-07-11",
            meteoedge_db=meteoedge_db,
            analytics_db=analytics_db,
            since_ts="2026-07-03",
        )
        assert rc == 0
        out_file = out_dir / "prob_cap_shadow_2026-07-11.md"
        assert out_file.exists()
        content = out_file.read_text()
        assert "## Population-level clamp saturation" in content
        assert "## Cap simulation" in content
        assert "8 distinct date(s)" in content

    def test_missing_meteoedge_db_falls_back_to_csv_path(self, tmp_path):
        """meteoedge_db path doesn't exist -> use_db False -> legacy CSV path,
        which itself has no data in this empty temp dir -> clean self-gate
        skip, matching the pre-#682 empty-environment behavior exactly.
        """
        out_dir = tmp_path / "backtest_results"
        rc = run_report(
            candidates_csv=tmp_path / "logs" / "candidates.csv",
            settlements_csv=tmp_path / "logs" / "settlements.csv",
            snapshots_jsonl=tmp_path / "logs" / "snapshots.jsonl",
            out_dir=out_dir,
            min_days=7,
            dry_run=False,
            meteoedge_db=tmp_path / "does_not_exist.db",
        )
        assert rc == 0
        assert not out_dir.exists()

    def test_db_file_exists_but_no_tables_degrades_cleanly(self, tmp_path):
        """A DB file exists (e.g. an empty dev container's fresh sqlite file)
        but has none of the expected tables yet -- must not crash.
        """
        meteoedge_db = tmp_path / "empty.db"
        sqlite3.connect(str(meteoedge_db)).close()
        out_dir = tmp_path / "backtest_results"
        rc = run_report(
            candidates_csv=tmp_path / "logs" / "candidates.csv",
            settlements_csv=tmp_path / "logs" / "settlements.csv",
            snapshots_jsonl=tmp_path / "logs" / "snapshots.jsonl",
            out_dir=out_dir,
            min_days=7,
            dry_run=False,
            meteoedge_db=meteoedge_db,
        )
        assert rc == 0
        assert not out_dir.exists()


class TestBoughtSideBasis738:
    """Issue #738: after #737 stores the NO cost in trades.actual_price, an
    already-admitted NO row and a newly-admitted snapshot row settle on ONE
    bought-side basis -- win pays (100 - cost), loss pays -cost. Pre-#737 the
    already-admitted side used yes_ask (~22c) so a win read ~+78c; here the
    corrected cost (78c) makes a win read +22c, matching the snapshot no_ask
    basis."""

    def test_already_admitted_no_win_uses_bought_side_cost(self):
        settled = [_norm_row(ticker="TICK-A", price_cents=78.0,
                             yes_won=False, pnl_cents=22.0)]
        results = simulate_cap_values(settled, [], cap_values=(0.95,))
        # win contribution = 100 - 78 = 22 (NOT 100 - 22 = 78 under the old bug)
        assert results[0.95]["total_pnl_cents"] == pytest.approx(22.0)

    def test_already_admitted_no_loss_charges_full_cost(self):
        settled = [_norm_row(ticker="TICK-B", price_cents=78.0,
                             yes_won=True, pnl_cents=-78.0)]
        results = simulate_cap_values(settled, [], cap_values=(0.95,))
        assert results[0.95]["total_pnl_cents"] == pytest.approx(-78.0)

    def test_both_populations_same_basis(self):
        """The already-admitted contribution for a NO win at 78c equals the
        synthetic basis newly-admitted rows are scored on for the same cost and
        outcome -- i.e. both populations use one bought-side basis."""
        settled = [_norm_row(ticker="TICK-OLD", price_cents=78.0,
                             yes_won=False, pnl_cents=22.0)]
        already_total = simulate_cap_values(settled, [], cap_values=(0.95,))[0.95]["total_pnl_cents"]
        newly_one = synthetic_no_pnl_cents(78.0, no_won=True)  # newly-admitted basis
        assert already_total == pytest.approx(newly_one)
        assert already_total == pytest.approx(22.0)

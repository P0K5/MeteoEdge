"""Tests for src/scripts/resolve_bracket_outcomes.py (issue #850).

Covers:
- dedupe_one_per_bracket_day: keeps the lowest-minutes_to_settlement row per
  (station, ticker, settlement_date).
- compute_observed_highs: MAX(temp_f) grouped by the station's LOCAL calendar
  day (STATION_TZ), including the near-local-midnight case (issue #810 --
  grouping by raw UTC date instead is exactly that bug).
- resolve_outcome: bracket-in-range boolean logic, None on missing inputs.
- resolve_bracket_rows / resolve_bracket_outcomes: end-to-end resolution,
  independent of the settlements table (no settlements table needed to get a
  non-trivial n).
- load_settlement_outcomes / cross_check_against_settlements: the
  correctness check against settlements.resolved_yes on the overlap.
- run_dry_run: self-gating (no bracket_evals data -> no report; no matching
  observations -> no report) and report-writing on synthetic fixture data.

All dates are synthetic (2026-0x-xx), matching the repo convention of never
anchoring test fixtures to a real "today".
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from src.scripts.resolve_bracket_outcomes import (
    build_dry_run_report,
    compute_observed_highs,
    cross_check_against_settlements,
    cross_check_against_settlements_direct,
    dedupe_one_per_bracket_day,
    load_bracket_eval_rows,
    load_settlement_outcomes,
    load_settlement_rows,
    resolve_bracket_outcomes,
    resolve_bracket_rows,
    resolve_outcome,
    run_dry_run,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _eval_row(**overrides) -> dict:
    base = {
        "station": "KORD",
        "ticker": "KORD-high-81-83",
        "bracket_low": 81.0,
        "bracket_high": 83.0,
        "poll_ts": "2026-07-05T14:00:00+00:00",
        "yes_ask": 40,
        "no_ask": 60,
        "p_yes": 0.28,
        "p_yes_raw": 0.31,
        "emos_mode": "legacy",
        "is_next_day": 0,
        "minutes_to_settlement": 90.0,
        "execution_mode": "live",
        "settlement_date": "2026-07-05",
    }
    base.update(overrides)
    return base


def _write_bracket_evals_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _write_observations_db(path, observations):
    """observations: list of (station, ts, temp_f) tuples."""
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE observations (id INTEGER PRIMARY KEY, ts TEXT, station TEXT, "
        "temp_f REAL, temp_native REAL, unit TEXT, current_high REAL, source TEXT, raw_json TEXT)"
    )
    for station, ts, temp_f in observations:
        con.execute(
            "INSERT INTO observations (ts, station, temp_f, temp_native, unit, source) "
            "VALUES (?, ?, ?, ?, 'F', 'metar')",
            (ts, station, temp_f, temp_f),
        )
    con.commit()
    con.close()


def _write_settlements_db(path, settlements):
    """settlements: list of (ticker, station, bracket_low, bracket_high, actual_high_f, resolved_yes)."""
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE settlements (id INTEGER PRIMARY KEY, ts TEXT, station TEXT, "
        "ticker TEXT UNIQUE, bracket_low REAL, bracket_high REAL, actual_high_f REAL, "
        "resolved_yes INTEGER, market_final_price INTEGER, source TEXT, direction TEXT)"
    )
    for ticker, station, bracket_low, bracket_high, actual_high_f, resolved_yes in settlements:
        con.execute(
            "INSERT INTO settlements (ts, station, ticker, bracket_low, bracket_high, "
            "actual_high_f, resolved_yes, source, direction) VALUES "
            "('2026-07-05T00:00:00Z', ?, ?, ?, ?, ?, ?, 'polymarket', 'high')",
            (station, ticker, bracket_low, bracket_high, actual_high_f, int(resolved_yes)),
        )
    con.commit()
    con.close()


def _write_trades_table(con, trades):
    """trades: list of (ticker, station, ts, end_date) tuples. end_date may be
    None to exercise the station-local-ts fallback in resolve_trade_date().
    """
    con.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, ts TEXT, station TEXT, ticker TEXT, "
        "bracket_low REAL, bracket_high REAL, side TEXT, predicted_price INTEGER, "
        "actual_price INTEGER, predicted_edge REAL, mode TEXT, capital_before REAL, "
        "end_date TEXT)"
    )
    for ticker, station, ts, end_date in trades:
        con.execute(
            "INSERT INTO trades (ts, station, ticker, bracket_low, bracket_high, side, "
            "predicted_price, actual_price, predicted_edge, mode, capital_before, end_date) "
            "VALUES (?, ?, ?, 0, 0, 'YES', 50, 50, 0, 'live', 1, ?)",
            (ts, station, ticker, end_date),
        )


def _write_settlements_direct_db(path, observations=(), settlements=(), trades=()):
    """Build a DB with (a subset of) observations/settlements/trades tables,
    for the settlements-direct correctness check (issue #858).

    settlements: list of (ticker, station, bracket_low, bracket_high,
        actual_high_f, resolved_yes) tuples -- settlements.ts is deliberately
        NOT the settlement date (always a fixed run timestamp) to prove the
        direct check does not rely on it.
    trades: list of (ticker, station, ts, end_date) tuples -- the source of
        truth for each settlement's actual settlement date.
    """
    if observations:
        _write_observations_db(path, observations)
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE settlements (id INTEGER PRIMARY KEY, ts TEXT, station TEXT, "
        "ticker TEXT UNIQUE, bracket_low REAL, bracket_high REAL, actual_high_f REAL, "
        "resolved_yes INTEGER, market_final_price INTEGER, source TEXT, direction TEXT)"
    )
    for ticker, station, bracket_low, bracket_high, actual_high_f, resolved_yes in settlements:
        con.execute(
            "INSERT INTO settlements (ts, station, ticker, bracket_low, bracket_high, "
            "actual_high_f, resolved_yes, source, direction) VALUES "
            # Deliberately a run timestamp far from any settlement date under
            # test -- if the direct check ever fell back to reading this, the
            # date-derived tests below would fail loudly.
            "('2099-01-01T00:00:00Z', ?, ?, ?, ?, ?, ?, 'polymarket', 'high')",
            (station, ticker, bracket_low, bracket_high, actual_high_f, int(resolved_yes)),
        )
    if trades:
        _write_trades_table(con, trades)
    con.commit()
    con.close()


def _write_combined_db(path, observations, settlements):
    _write_observations_db(path, observations)
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE settlements (id INTEGER PRIMARY KEY, ts TEXT, station TEXT, "
        "ticker TEXT UNIQUE, bracket_low REAL, bracket_high REAL, actual_high_f REAL, "
        "resolved_yes INTEGER, market_final_price INTEGER, source TEXT, direction TEXT)"
    )
    for ticker, station, bracket_low, bracket_high, actual_high_f, resolved_yes in settlements:
        con.execute(
            "INSERT INTO settlements (ts, station, ticker, bracket_low, bracket_high, "
            "actual_high_f, resolved_yes, source, direction) VALUES "
            "('2026-07-05T00:00:00Z', ?, ?, ?, ?, ?, ?, 'polymarket', 'high')",
            (station, ticker, bracket_low, bracket_high, actual_high_f, int(resolved_yes)),
        )
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# load_bracket_eval_rows
# ---------------------------------------------------------------------------

class TestLoadBracketEvalRows:
    def test_loads_rows_from_jsonl(self, tmp_path):
        path = tmp_path / "bracket_evals.jsonl"
        _write_bracket_evals_jsonl(path, [_eval_row(), _eval_row(ticker="t2")])
        rows = load_bracket_eval_rows(path)
        assert len(rows) == 2

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_bracket_eval_rows(tmp_path / "does_not_exist.jsonl") == []


# ---------------------------------------------------------------------------
# dedupe_one_per_bracket_day
# ---------------------------------------------------------------------------

class TestDedupe:
    def test_keeps_lowest_minutes_to_settlement(self):
        rows = [
            _eval_row(minutes_to_settlement=600),
            _eval_row(minutes_to_settlement=60),
            _eval_row(minutes_to_settlement=300),
        ]
        out = dedupe_one_per_bracket_day(rows)
        assert len(out) == 1
        assert out[0]["minutes_to_settlement"] == 60

    def test_different_brackets_kept_separate(self):
        rows = [
            _eval_row(ticker="KORD-high-79-81", bracket_low=79, bracket_high=81),
            _eval_row(ticker="KORD-high-81-83", bracket_low=81, bracket_high=83),
        ]
        out = dedupe_one_per_bracket_day(rows)
        assert len(out) == 2

    def test_same_ticker_different_settlement_date_kept_separate(self):
        rows = [
            _eval_row(settlement_date="2026-07-05"),
            _eval_row(settlement_date="2026-07-06"),
        ]
        out = dedupe_one_per_bracket_day(rows)
        assert len(out) == 2

    def test_different_stations_same_hour_kept_separate(self):
        rows = [
            _eval_row(station="KORD", ticker="KORD-high-81-83"),
            _eval_row(station="KMIA", ticker="KMIA-high-82-84"),
        ]
        out = dedupe_one_per_bracket_day(rows)
        assert len(out) == 2

    def test_missing_key_fields_dropped(self):
        rows = [_eval_row(station=""), _eval_row(ticker=""), _eval_row(settlement_date="")]
        assert dedupe_one_per_bracket_day(rows) == []


# ---------------------------------------------------------------------------
# compute_observed_highs
# ---------------------------------------------------------------------------

class TestComputeObservedHighs:
    def test_simple_utc_grouping(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        _write_observations_db(db_path, [
            ("KORD", "2026-07-05T14:00:00+00:00", 75.0),
            ("KORD", "2026-07-05T16:00:00+00:00", 82.0),
        ])
        highs = compute_observed_highs(db_path, {("KORD", "2026-07-05")})
        assert highs == {("KORD", "2026-07-05"): 82.0}

    def test_near_local_midnight_grouped_to_correct_local_day(self, tmp_path):
        """Issue #810 regression: an observation just before local midnight
        must attribute to the PREVIOUS local day, not the UTC day.

        KATL is America/New_York (UTC-4 in July). 2026-07-06T03:30:00 UTC is
        2026-07-05T23:30:00 EDT local -- must count toward 2026-07-05, not
        2026-07-06.
        """
        db_path = tmp_path / "meteoedge.db"
        _write_observations_db(db_path, [
            ("KATL", "2026-07-06T03:30:00+00:00", 90.0),  # local 2026-07-05 23:30
            ("KATL", "2026-07-05T18:00:00+00:00", 70.0),  # local 2026-07-05 14:00
        ])
        highs = compute_observed_highs(db_path, {("KATL", "2026-07-05"), ("KATL", "2026-07-06")})
        assert highs.get(("KATL", "2026-07-05")) == 90.0
        assert ("KATL", "2026-07-06") not in highs

    def test_unknown_station_skipped(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        _write_observations_db(db_path, [("ZZZZ", "2026-07-05T14:00:00+00:00", 80.0)])
        highs = compute_observed_highs(db_path, {("ZZZZ", "2026-07-05")})
        assert highs == {}

    def test_no_observations_for_requested_day_absent(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        _write_observations_db(db_path, [("KORD", "2026-07-01T14:00:00+00:00", 70.0)])
        highs = compute_observed_highs(db_path, {("KORD", "2026-07-05")})
        assert ("KORD", "2026-07-05") not in highs

    def test_empty_station_dates_returns_empty_without_opening_db(self, tmp_path):
        # DB path doesn't even need to exist -- short-circuits before opening.
        assert compute_observed_highs(tmp_path / "does_not_exist.db", set()) == {}

    def test_missing_db_returns_empty(self, tmp_path):
        highs = compute_observed_highs(tmp_path / "does_not_exist.db", {("KORD", "2026-07-05")})
        assert highs == {}

    def test_multiple_stations_independent(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        _write_observations_db(db_path, [
            ("KORD", "2026-07-05T18:00:00+00:00", 84.0),
            ("KMIA", "2026-07-05T18:00:00+00:00", 91.0),
        ])
        highs = compute_observed_highs(db_path, {("KORD", "2026-07-05"), ("KMIA", "2026-07-05")})
        assert highs == {("KORD", "2026-07-05"): 84.0, ("KMIA", "2026-07-05"): 91.0}


# ---------------------------------------------------------------------------
# resolve_outcome
# ---------------------------------------------------------------------------

class TestResolveOutcome:
    def test_yes_when_high_inside_bracket(self):
        assert resolve_outcome(81.0, 83.0, 82.0) is True

    def test_yes_on_boundary_low(self):
        assert resolve_outcome(81.0, 83.0, 81.0) is True

    def test_yes_on_boundary_high(self):
        assert resolve_outcome(81.0, 83.0, 83.0) is True

    def test_no_when_high_outside_bracket(self):
        assert resolve_outcome(81.0, 83.0, 84.0) is False
        assert resolve_outcome(81.0, 83.0, 80.9) is False

    def test_none_on_missing_inputs(self):
        assert resolve_outcome(None, 83.0, 82.0) is None
        assert resolve_outcome(81.0, None, 82.0) is None
        assert resolve_outcome(81.0, 83.0, None) is None


# ---------------------------------------------------------------------------
# resolve_bracket_rows
# ---------------------------------------------------------------------------

class TestResolveBracketRows:
    def test_resolves_yes_and_no_across_brackets_on_same_station_day(self):
        rows = [
            _eval_row(ticker="KORD-high-79-81", bracket_low=79, bracket_high=81),
            _eval_row(ticker="KORD-high-81-83", bracket_low=81, bracket_high=83),
            _eval_row(ticker="KORD-high-83-85", bracket_low=83, bracket_high=85),
        ]
        observed_highs = {("KORD", "2026-07-05"): 82.0}
        resolved, counts = resolve_bracket_rows(rows, observed_highs)
        assert len(resolved) == 3
        by_ticker = {r["ticker"]: r["resolved_yes"] for r in resolved}
        assert by_ticker["KORD-high-79-81"] is False
        assert by_ticker["KORD-high-81-83"] is True
        assert by_ticker["KORD-high-83-85"] is False
        assert counts["resolved_rows"] == 3

    def test_drops_rows_with_no_observed_high(self):
        rows = [_eval_row(settlement_date="2026-07-09")]
        resolved, counts = resolve_bracket_rows(rows, {})
        assert resolved == []
        assert counts["no_observed_high"] == 1

    def test_drops_rows_missing_station_or_settlement_date(self):
        rows = [_eval_row(station=""), _eval_row(settlement_date="")]
        resolved, counts = resolve_bracket_rows(rows, {("KORD", "2026-07-05"): 82.0})
        assert resolved == []
        assert counts["missing_station_or_settlement_date"] == 2

    def test_observed_high_is_attached_to_row(self):
        rows = [_eval_row()]
        resolved, _ = resolve_bracket_rows(rows, {("KORD", "2026-07-05"): 82.0})
        assert resolved[0]["observed_high"] == 82.0


# ---------------------------------------------------------------------------
# resolve_bracket_outcomes -- end-to-end, independent of settlements
# ---------------------------------------------------------------------------

class TestResolveBracketOutcomesEndToEnd:
    def test_resolves_full_population_without_any_settlements_table(self, tmp_path):
        """The core #850 fix: n must come from ALL evaluated brackets with
        observed weather, not the ~156-row settlements-joined population.
        No settlements table exists at all here -- resolution still works.
        """
        bracket_evals = tmp_path / "bracket_evals.jsonl"
        db_path = tmp_path / "meteoedge.db"

        # 3 station-days x 11 brackets = 33 bracket-rows, no settlements table.
        rows = []
        # Highs deliberately off bracket boundaries (75, 77, 79, ... 97) so
        # exactly one 2-degree-wide bracket contains each -- an on-boundary
        # high (e.g. 79.0) would land in both adjacent brackets by design
        # (resolve_outcome is inclusive on both ends), which is a property of
        # the fixture's bracket construction, not the resolver.
        stations_days = [
            ("KORD", "2026-07-05", 82.3),
            ("KORD", "2026-07-06", 79.4),
            ("KMIA", "2026-07-05", 91.2),
        ]
        observations = []
        for station, sdate, high in stations_days:
            observations.append((station, f"{sdate}T18:00:00+00:00", high))
            for low in range(75, 97, 2):  # 11 brackets, 2-degree width
                rows.append(_eval_row(
                    station=station,
                    ticker=f"{station}-high-{low}-{low+2}-{sdate}",
                    bracket_low=float(low),
                    bracket_high=float(low + 2),
                    settlement_date=sdate,
                ))
        _write_bracket_evals_jsonl(bracket_evals, rows)
        _write_observations_db(db_path, observations)

        resolved_rows, counts = resolve_bracket_outcomes(bracket_evals, db_path)

        assert counts["n_bracket_rows"] == 33
        assert counts["n_station_days"] == 3
        # Exactly one bracket per station-day should resolve YES.
        yes_by_day = {}
        for r in resolved_rows:
            if r["resolved_yes"]:
                key = (r["station"], r["settlement_date"])
                yes_by_day[key] = yes_by_day.get(key, 0) + 1
        assert yes_by_day == {
            ("KORD", "2026-07-05"): 1,
            ("KORD", "2026-07-06"): 1,
            ("KMIA", "2026-07-05"): 1,
        }

    def test_no_db_path_returns_no_resolved_rows(self, tmp_path):
        bracket_evals = tmp_path / "bracket_evals.jsonl"
        _write_bracket_evals_jsonl(bracket_evals, [_eval_row()])
        resolved_rows, counts = resolve_bracket_outcomes(bracket_evals, tmp_path / "no.db")
        assert resolved_rows == []
        assert counts["n_bracket_rows"] == 0

    def test_hundreds_of_station_days_not_collapsed_to_twenty(self, tmp_path):
        """Directly demonstrates the n-collapse fix from the issue: with 200
        station-days of clean observations and NO settlements table, n must
        be ~200, not ~20.
        """
        bracket_evals = tmp_path / "bracket_evals.jsonl"
        db_path = tmp_path / "meteoedge.db"
        rows = []
        observations = []
        for i in range(200):
            sdate = f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}"
            rows.append(_eval_row(
                station="KORD", ticker=f"KORD-high-81-83-{i}",
                bracket_low=81.0, bracket_high=83.0, settlement_date=sdate,
            ))
            observations.append(("KORD", f"{sdate}T18:00:00+00:00", 82.0))
        _write_bracket_evals_jsonl(bracket_evals, rows)
        _write_observations_db(db_path, observations)

        resolved_rows, counts = resolve_bracket_outcomes(bracket_evals, db_path)
        assert counts["n_station_days"] == 200
        assert counts["n_station_days"] > 20 * 5  # nowhere near the ~20 settlements-joined n


# ---------------------------------------------------------------------------
# load_settlement_outcomes / cross_check_against_settlements
# ---------------------------------------------------------------------------

class TestCrossCheck:
    def test_load_settlement_outcomes_from_sqlite(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        _write_settlements_db(db_path, [
            ("t1", "KORD", 81, 83, 82.0, True),
            ("t2", "KORD", 79, 81, 82.0, False),
        ])
        outcomes = load_settlement_outcomes(db_path)
        assert outcomes == {"t1": True, "t2": False}

    def test_load_settlement_outcomes_missing_db_returns_empty(self, tmp_path):
        assert load_settlement_outcomes(tmp_path / "does_not_exist.db") == {}

    def test_cross_check_matches(self):
        resolved_rows = [
            {"ticker": "t1", "resolved_yes": True, "station": "KORD",
             "settlement_date": "2026-07-05", "bracket_low": 81, "bracket_high": 83,
             "observed_high": 82.0},
            {"ticker": "t2", "resolved_yes": False, "station": "KORD",
             "settlement_date": "2026-07-05", "bracket_low": 79, "bracket_high": 81,
             "observed_high": 82.0},
        ]
        settlement_outcomes = {"t1": True, "t2": False}
        result = cross_check_against_settlements(resolved_rows, settlement_outcomes)
        assert result["n_overlap"] == 2
        assert result["n_match"] == 2
        assert result["n_mismatch"] == 0
        assert result["mismatches"] == []

    def test_cross_check_reports_mismatch(self):
        resolved_rows = [
            {"ticker": "t1", "resolved_yes": True, "station": "KORD",
             "settlement_date": "2026-07-05", "bracket_low": 81, "bracket_high": 83,
             "observed_high": 82.0},
        ]
        settlement_outcomes = {"t1": False}  # disagrees with our resolver
        result = cross_check_against_settlements(resolved_rows, settlement_outcomes)
        assert result["n_overlap"] == 1
        assert result["n_match"] == 0
        assert result["n_mismatch"] == 1
        assert result["mismatches"][0]["ticker"] == "t1"
        assert result["mismatches"][0]["resolver_resolved_yes"] is True
        assert result["mismatches"][0]["settlements_resolved_yes"] is False

    def test_rows_without_a_settlement_are_ignored(self):
        resolved_rows = [
            {"ticker": "untraded", "resolved_yes": True, "station": "KORD",
             "settlement_date": "2026-07-05", "bracket_low": 81, "bracket_high": 83,
             "observed_high": 82.0},
        ]
        result = cross_check_against_settlements(resolved_rows, {})
        assert result["n_overlap"] == 0
        assert result["n_match"] == 0
        assert result["n_mismatch"] == 0


# ---------------------------------------------------------------------------
# cross_check_against_settlements_direct (issue #858) -- resolves directly
# from settlements' own fields, bypassing bracket_evals entirely
# ---------------------------------------------------------------------------

class TestLoadSettlementRows:
    def test_loads_full_columns(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        _write_settlements_direct_db(db_path, settlements=[
            ("t1", "KORD", 81.0, 83.0, 82.0, True),
        ])
        rows = load_settlement_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["ticker"] == "t1"
        assert rows[0]["station"] == "KORD"
        assert rows[0]["bracket_low"] == 81.0
        assert rows[0]["bracket_high"] == 83.0
        assert rows[0]["actual_high_f"] == 82.0
        assert rows[0]["resolved_yes"] == 1

    def test_missing_db_returns_empty(self, tmp_path):
        assert load_settlement_rows(tmp_path / "does_not_exist.db") == []

    def test_missing_settlements_table_returns_empty(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        _write_observations_db(db_path, [("KORD", "2026-07-05T14:00:00+00:00", 80.0)])
        assert load_settlement_rows(db_path) == []


class TestCrossCheckDirect:
    def test_correctly_resolves_yes(self, tmp_path):
        """Settlement row: bracket [81,83] contains the observed high 82.0 --
        settlements.resolved_yes=True agrees with the independently
        recomputed outcome. Dated via trades.end_date, NOT settlements.ts
        (which is a deliberately wrong fixed run timestamp)."""
        db_path = tmp_path / "meteoedge.db"
        _write_settlements_direct_db(
            db_path,
            observations=[("KORD", "2026-07-05T18:00:00+00:00", 82.0)],
            settlements=[("t-yes", "KORD", 81.0, 83.0, 82.0, True)],
            trades=[("t-yes", "KORD", "2026-07-05T12:00:00+00:00", "2026-07-05")],
        )
        result = cross_check_against_settlements_direct(db_path)
        assert result["n_settlement_rows"] == 1
        assert result["n_no_trade_date"] == 0
        assert result["n_no_observed_high"] == 0
        assert result["n_checked"] == 1
        assert result["n_match"] == 1
        assert result["n_mismatch"] == 0
        assert result["mismatches"] == []

    def test_correctly_resolves_no(self, tmp_path):
        """Bracket [70,72] does NOT contain the observed high 82.0 --
        settlements.resolved_yes=False agrees with the recomputed outcome."""
        db_path = tmp_path / "meteoedge.db"
        _write_settlements_direct_db(
            db_path,
            observations=[("KORD", "2026-07-05T18:00:00+00:00", 82.0)],
            settlements=[("t-no", "KORD", 70.0, 72.0, 82.0, False)],
            trades=[("t-no", "KORD", "2026-07-05T12:00:00+00:00", "2026-07-05")],
        )
        result = cross_check_against_settlements_direct(db_path)
        assert result["n_checked"] == 1
        assert result["n_match"] == 1
        assert result["n_mismatch"] == 0

    def test_catches_a_real_mismatch(self, tmp_path):
        """Proves the check actually catches disagreement, not just always
        passing: bracket [81,83] contains the observed high 82.0 (so the
        independently-recomputed outcome is YES), but settlements.resolved_yes
        is stored as False -- a genuine disagreement that must be reported."""
        db_path = tmp_path / "meteoedge.db"
        _write_settlements_direct_db(
            db_path,
            observations=[("KORD", "2026-07-05T18:00:00+00:00", 82.0)],
            settlements=[("t-bad", "KORD", 81.0, 83.0, 82.0, False)],
            trades=[("t-bad", "KORD", "2026-07-05T12:00:00+00:00", "2026-07-05")],
        )
        result = cross_check_against_settlements_direct(db_path)
        assert result["n_checked"] == 1
        assert result["n_match"] == 0
        assert result["n_mismatch"] == 1
        mismatch = result["mismatches"][0]
        assert mismatch["ticker"] == "t-bad"
        assert mismatch["station"] == "KORD"
        assert mismatch["settlement_date"] == "2026-07-05"
        assert mismatch["observed_high"] == 82.0
        assert mismatch["direct_resolved_yes"] is True
        assert mismatch["settlements_resolved_yes"] is False

    def test_date_derived_from_trades_end_date_not_settlements_ts(self, tmp_path):
        """settlements.ts in the fixture is always the wrong, fixed
        2099-01-01 run timestamp -- if the direct check used it, it would
        never find a matching observation and the row would be excluded as
        n_no_observed_high instead of checked. This proves date derivation
        goes through trades.end_date."""
        db_path = tmp_path / "meteoedge.db"
        _write_settlements_direct_db(
            db_path,
            observations=[("KORD", "2026-07-05T18:00:00+00:00", 82.0)],
            settlements=[("t-yes", "KORD", 81.0, 83.0, 82.0, True)],
            trades=[("t-yes", "KORD", "2026-07-06T09:00:00+00:00", "2026-07-05")],
        )
        result = cross_check_against_settlements_direct(db_path)
        assert result["n_checked"] == 1
        assert result["n_no_observed_high"] == 0

    def test_date_falls_back_to_station_local_ts_when_end_date_missing(self, tmp_path):
        """Legacy trades rows (pre-#609) have no end_date -- resolve_trade_date
        falls back to the trade row's own station-local ts day. KATL is
        America/New_York (UTC-4 in July): 2026-07-06T03:30:00 UTC is
        2026-07-05T23:30:00 EDT local, i.e. still settlement date 2026-07-05."""
        db_path = tmp_path / "meteoedge.db"
        _write_settlements_direct_db(
            db_path,
            observations=[("KATL", "2026-07-05T18:00:00+00:00", 82.0)],
            settlements=[("t-legacy", "KATL", 81.0, 83.0, 82.0, True)],
            trades=[("t-legacy", "KATL", "2026-07-06T03:30:00+00:00", None)],
        )
        result = cross_check_against_settlements_direct(db_path)
        assert result["n_checked"] == 1
        assert result["n_match"] == 1

    def test_no_matching_trade_row_is_skipped_not_guessed(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        _write_settlements_direct_db(
            db_path,
            observations=[("KORD", "2026-07-05T18:00:00+00:00", 82.0)],
            settlements=[("t-orphan", "KORD", 81.0, 83.0, 82.0, True)],
            trades=[],
        )
        result = cross_check_against_settlements_direct(db_path)
        assert result["n_settlement_rows"] == 1
        assert result["n_no_trade_date"] == 1
        assert result["n_checked"] == 0
        assert result["n_match"] == 0
        assert result["n_mismatch"] == 0

    def test_no_observation_for_dated_row_is_skipped_not_guessed(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        _write_settlements_direct_db(
            db_path,
            observations=[("KORD", "2026-01-01T18:00:00+00:00", 40.0)],  # wrong date
            settlements=[("t-nodata", "KORD", 81.0, 83.0, 82.0, True)],
            trades=[("t-nodata", "KORD", "2026-07-05T12:00:00+00:00", "2026-07-05")],
        )
        result = cross_check_against_settlements_direct(db_path)
        assert result["n_no_trade_date"] == 0
        assert result["n_no_observed_high"] == 1
        assert result["n_checked"] == 0

    def test_empty_settlements_table_returns_zeros(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        _write_settlements_direct_db(db_path, settlements=[], trades=[])
        result = cross_check_against_settlements_direct(db_path)
        assert result == {
            "n_settlement_rows": 0, "n_no_trade_date": 0, "n_no_observed_high": 0,
            "n_checked": 0, "n_match": 0, "n_mismatch": 0, "mismatches": [],
        }

    def test_missing_db_returns_zeros(self, tmp_path):
        result = cross_check_against_settlements_direct(tmp_path / "does_not_exist.db")
        assert result["n_settlement_rows"] == 0
        assert result["n_checked"] == 0

    def test_multiple_rows_mixed_outcomes(self, tmp_path):
        """One YES, one NO, one mismatch, in the same run -- demonstrates the
        check discriminates between rows rather than trivially passing/failing
        everything the same way."""
        db_path = tmp_path / "meteoedge.db"
        _write_settlements_direct_db(
            db_path,
            observations=[("KORD", "2026-07-05T18:00:00+00:00", 82.0)],
            settlements=[
                ("t-yes", "KORD", 81.0, 83.0, 82.0, True),
                ("t-no", "KORD", 70.0, 72.0, 82.0, False),
                ("t-bad", "KORD", 81.0, 83.0, 82.0, False),
            ],
            trades=[
                ("t-yes", "KORD", "2026-07-05T12:00:00+00:00", "2026-07-05"),
                ("t-no", "KORD", "2026-07-05T12:00:00+00:00", "2026-07-05"),
                ("t-bad", "KORD", "2026-07-05T12:00:00+00:00", "2026-07-05"),
            ],
        )
        result = cross_check_against_settlements_direct(db_path)
        assert result["n_checked"] == 3
        assert result["n_match"] == 2
        assert result["n_mismatch"] == 1
        assert result["mismatches"][0]["ticker"] == "t-bad"


# ---------------------------------------------------------------------------
# build_dry_run_report / run_dry_run
# ---------------------------------------------------------------------------

class TestDryRunReport:
    def test_report_contains_headline_counts(self):
        resolved_rows = [
            {"ticker": "t1", "resolved_yes": True, "station": "KORD",
             "settlement_date": "2026-07-05", "bracket_low": 81, "bracket_high": 83,
             "observed_high": 82.0},
        ]
        counts = {
            "raw_rows": 10, "deduped_bracket_rows": 5,
            "missing_station_or_settlement_date": 0, "no_observed_high": 4,
            "missing_bracket_bounds": 0, "n_bracket_rows": 1, "n_station_days": 1,
        }
        cross_check = {"n_overlap": 1, "n_match": 1, "n_mismatch": 0, "mismatches": []}
        direct_check = {
            "n_settlement_rows": 2, "n_no_trade_date": 0, "n_no_observed_high": 0,
            "n_checked": 2, "n_match": 2, "n_mismatch": 0, "mismatches": [],
        }
        report = build_dry_run_report(resolved_rows, counts, cross_check, direct_check, "2026-07-25")
        assert "Bracket Outcome Resolution" in report
        assert "**1**" in report  # n_bracket_rows / n_station_days both 1
        assert "settlements" in report.lower()
        assert "Correctness check #2" in report
        assert "issue #858" in report

    def test_report_includes_mismatch_table_when_present(self):
        counts = {"raw_rows": 1, "deduped_bracket_rows": 1, "n_bracket_rows": 1, "n_station_days": 1}
        cross_check = {
            "n_overlap": 1, "n_match": 0, "n_mismatch": 1,
            "mismatches": [{
                "station": "KORD", "ticker": "t1", "settlement_date": "2026-07-05",
                "bracket_low": 81, "bracket_high": 83, "observed_high": 82.0,
                "resolver_resolved_yes": True, "settlements_resolved_yes": False,
            }],
        }
        direct_check = {
            "n_settlement_rows": 0, "n_no_trade_date": 0, "n_no_observed_high": 0,
            "n_checked": 0, "n_match": 0, "n_mismatch": 0, "mismatches": [],
        }
        report = build_dry_run_report([], counts, cross_check, direct_check, "2026-07-25")
        assert "Mismatches" in report
        assert "t1" in report

    def test_report_includes_direct_check_mismatch_table_when_present(self):
        counts = {"raw_rows": 0, "deduped_bracket_rows": 0, "n_bracket_rows": 0, "n_station_days": 0}
        cross_check = {"n_overlap": 0, "n_match": 0, "n_mismatch": 0, "mismatches": []}
        direct_check = {
            "n_settlement_rows": 1, "n_no_trade_date": 0, "n_no_observed_high": 0,
            "n_checked": 1, "n_match": 0, "n_mismatch": 1,
            "mismatches": [{
                "station": "KORD", "ticker": "t-bad", "settlement_date": "2026-07-05",
                "bracket_low": 81, "bracket_high": 83, "observed_high": 82.0,
                "direct_resolved_yes": True, "settlements_resolved_yes": False,
            }],
        }
        report = build_dry_run_report([], counts, cross_check, direct_check, "2026-07-25")
        assert "t-bad" in report
        assert report.count("Mismatches") >= 1


class TestRunDryRun:
    def test_no_bracket_evals_data_writes_no_report(self, tmp_path):
        rc = run_dry_run(tmp_path / "no_bracket_evals.jsonl", tmp_path / "no.db", tmp_path / "out")
        assert rc == 0
        assert not (tmp_path / "out").exists()

    def test_no_matching_observations_writes_no_report(self, tmp_path):
        bracket_evals = tmp_path / "bracket_evals.jsonl"
        _write_bracket_evals_jsonl(bracket_evals, [_eval_row(settlement_date="2026-09-01")])
        db_path = tmp_path / "meteoedge.db"
        _write_observations_db(db_path, [("KORD", "2026-01-01T14:00:00+00:00", 40.0)])

        rc = run_dry_run(bracket_evals, db_path, tmp_path / "out")
        assert rc == 0
        assert not (tmp_path / "out").exists()

    def test_writes_report_with_cross_check_on_synthetic_fixture(self, tmp_path):
        bracket_evals = tmp_path / "bracket_evals.jsonl"
        db_path = tmp_path / "meteoedge.db"

        rows = [
            _eval_row(ticker="t-yes", bracket_low=81, bracket_high=83, settlement_date="2026-07-05"),
            _eval_row(ticker="t-no", bracket_low=70, bracket_high=72, settlement_date="2026-07-05"),
        ]
        _write_bracket_evals_jsonl(bracket_evals, rows)
        _write_combined_db(
            db_path,
            observations=[("KORD", "2026-07-05T18:00:00+00:00", 82.0)],
            settlements=[("t-yes", "KORD", 81, 83, 82.0, True)],
        )

        rc = run_dry_run(bracket_evals, db_path, tmp_path / "out", run_date="2026-07-25")
        assert rc == 0
        out_file = tmp_path / "out" / "bracket_outcome_resolution_dryrun_2026-07-25.md"
        assert out_file.exists()
        text = out_file.read_text()
        assert "Resolved bracket-rows" in text
        assert "Overlap with settlements" in text
        # The settlements-direct check (issue #858) section is present, even
        # though this fixture has no `trades` table (so it reads zeros --
        # self-gated, not fabricated).
        assert "Correctness check #2" in text
        assert "Settlements rows read" in text

    def test_report_written_from_settlements_direct_even_with_empty_bracket_evals(self, tmp_path):
        """The exact production scenario from issue #858: bracket_evals has
        NO data at all (as it didn't before #826), but settlements/trades/
        observations do. The report must still be written, carrying the
        settlements-direct check's real numbers -- not silently skipped just
        because bracket_evals is empty.
        """
        bracket_evals = tmp_path / "no_bracket_evals.jsonl"
        db_path = tmp_path / "meteoedge.db"
        _write_settlements_direct_db(
            db_path,
            observations=[("KORD", "2026-07-05T18:00:00+00:00", 82.0)],
            settlements=[("t-yes", "KORD", 81.0, 83.0, 82.0, True)],
            trades=[("t-yes", "KORD", "2026-07-05T12:00:00+00:00", "2026-07-05")],
        )

        rc = run_dry_run(bracket_evals, db_path, tmp_path / "out", run_date="2026-07-25")
        assert rc == 0
        out_file = tmp_path / "out" / "bracket_outcome_resolution_dryrun_2026-07-25.md"
        assert out_file.exists()
        text = out_file.read_text()
        assert "Correctness check #2" in text
        assert "| Settlements rows read (n) | 1 |" in text
        assert "| **Checked (n)** | **1** |" in text
        assert "| Matches | 1 |" in text
        # No bracket_evals data -- the first check's headline n is zero, but
        # that must not have suppressed the whole report.
        assert "| **Resolved bracket-rows (n)** | **0** |" in text

    def test_report_surfaces_a_settlements_direct_mismatch(self, tmp_path):
        bracket_evals = tmp_path / "no_bracket_evals.jsonl"
        db_path = tmp_path / "meteoedge.db"
        _write_settlements_direct_db(
            db_path,
            observations=[("KORD", "2026-07-05T18:00:00+00:00", 82.0)],
            settlements=[("t-bad", "KORD", 81.0, 83.0, 82.0, False)],  # wrong: should be YES
            trades=[("t-bad", "KORD", "2026-07-05T12:00:00+00:00", "2026-07-05")],
        )

        rc = run_dry_run(bracket_evals, db_path, tmp_path / "out", run_date="2026-07-25")
        assert rc == 0
        out_file = tmp_path / "out" / "bracket_outcome_resolution_dryrun_2026-07-25.md"
        text = out_file.read_text()
        assert "| Mismatches | 1 |" in text
        assert "t-bad" in text

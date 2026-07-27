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

from unittest.mock import patch

from src.scripts.resolve_bracket_outcomes import (
    COLLISION_BOUNDARY,
    COLLISION_DISJOINT,
    COLLISION_UNKNOWN,
    build_dry_run_report,
    classify_multi_yes,
    compute_observed_highs,
    cross_check_against_settlements,
    cross_check_against_settlements_direct,
    cross_check_gamma_vs_metar,
    dedupe_one_per_bracket_day,
    detect_multi_yes_station_days,
    detect_zero_yes_station_days,
    ladder_completeness,
    load_bracket_eval_rows,
    load_gamma_cache,
    load_settlement_outcomes,
    load_settlement_rows,
    resolve_bracket_outcomes,
    resolve_bracket_rows,
    resolve_gamma_outcomes,
    resolve_outcome,
    run_dry_run,
    save_gamma_cache,
)


@pytest.fixture(autouse=True)
def _never_hit_the_network(tmp_path_factory):
    """Hard guarantee that NO test in this module makes a real Polymarket call.

    ``resolve_bracket_outcomes`` is Gamma-first by default (issue #860), so
    without this an unsuspecting test with bracket_evals rows would fire real
    HTTP requests. Returning None means "not decisively resolved", i.e. every
    test falls back to METAR unless it patches this itself. The cache is also
    redirected into a temp dir so no test can read or write the real
    ``logs/gamma_resolution_cache.json``.
    """
    cache_dir = tmp_path_factory.mktemp("gamma_cache_guard")
    with (
        patch("src.scripts.resolve_bracket_outcomes.fetch_market_resolution", return_value=None),
        patch(
            "src.scripts.resolve_bracket_outcomes.DEFAULT_GAMMA_CACHE_PATH",
            cache_dir / "gamma_resolution_cache.json",
        ),
    ):
        yield


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


# ---------------------------------------------------------------------------
# Gamma-first ground truth (issue #860)
# ---------------------------------------------------------------------------

def _write_settlements_db_with_source(path, settlements, trades=(), observations=()):
    """Like _write_settlements_direct_db but the settlements table HAS the
    resolution_source column (it arrived as a migration, so both shapes exist
    in the wild -- the no-column shape is covered by the helpers above).

    settlements: (ticker, station, bracket_low, bracket_high, actual_high_f,
                  resolved_yes, resolution_source)
    """
    if observations:
        _write_observations_db(path, observations)
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE settlements (id INTEGER PRIMARY KEY, ts TEXT, station TEXT, "
        "ticker TEXT UNIQUE, bracket_low REAL, bracket_high REAL, actual_high_f REAL, "
        "resolved_yes INTEGER, market_final_price INTEGER, source TEXT, direction TEXT, "
        "resolution_source TEXT)"
    )
    for tk, st, lo, hi, actual, yes, src in settlements:
        con.execute(
            "INSERT INTO settlements (ts, station, ticker, bracket_low, bracket_high, "
            "actual_high_f, resolved_yes, source, direction, resolution_source) VALUES "
            "('2099-01-01T00:00:00Z', ?, ?, ?, ?, ?, ?, 'polymarket', 'high', ?)",
            (st, tk, lo, hi, actual, int(yes), src),
        )
    if trades:
        _write_trades_table(con, trades)
    con.commit()
    con.close()


class TestGammaCache:
    def test_roundtrip(self, tmp_path):
        p = tmp_path / "cache.json"
        save_gamma_cache(p, {"0xaaa": True, "0xbbb": False})
        assert load_gamma_cache(p) == {"0xaaa": True, "0xbbb": False}

    def test_missing_file_is_empty_cache(self, tmp_path):
        assert load_gamma_cache(tmp_path / "nope.json") == {}

    def test_none_path_is_empty_cache(self):
        assert load_gamma_cache(None) == {}

    def test_corrupt_cache_degrades_to_empty_without_raising(self, tmp_path):
        """A cache is an optimisation -- losing it may cost time, never
        correctness, and must never crash a run."""
        p = tmp_path / "cache.json"
        p.write_text("{not json at all")
        assert load_gamma_cache(p) == {}

    def test_non_dict_cache_is_ignored(self, tmp_path):
        p = tmp_path / "cache.json"
        p.write_text('["not", "a", "dict"]')
        assert load_gamma_cache(p) == {}

    def test_non_bool_values_are_dropped(self, tmp_path):
        p = tmp_path / "cache.json"
        p.write_text('{"0xaaa": true, "0xjunk": "maybe"}')
        assert load_gamma_cache(p) == {"0xaaa": True}

    def test_save_to_unwritable_path_does_not_raise(self, tmp_path):
        blocker = tmp_path / "afile"
        blocker.write_text("x")
        save_gamma_cache(blocker / "nested" / "cache.json", {"0xaaa": True})


class TestResolveGammaOutcomes:
    def test_decisive_yes_and_no_are_returned_and_cached(self, tmp_path):
        cache_path = tmp_path / "cache.json"
        with patch(
            "src.scripts.resolve_bracket_outcomes.fetch_market_resolution",
            side_effect=lambda t: True if t == "0xyes" else False,
        ):
            res, stats = resolve_gamma_outcomes({"0xyes", "0xno"}, cache_path=cache_path)
        assert res == {"0xyes": True, "0xno": False}
        assert stats["n_newly_resolved"] == 2
        assert load_gamma_cache(cache_path) == {"0xyes": True, "0xno": False}

    def test_cache_hit_makes_no_network_call(self, tmp_path):
        """The whole point of the cache (#860): a settled market's outcome
        never changes, so a cached ticker must never be fetched again."""
        cache_path = tmp_path / "cache.json"
        save_gamma_cache(cache_path, {"0xaaa": True})
        with patch(
            "src.scripts.resolve_bracket_outcomes.fetch_market_resolution"
        ) as mock_fetch:
            res, stats = resolve_gamma_outcomes({"0xaaa"}, cache_path=cache_path)
        mock_fetch.assert_not_called()
        assert res == {"0xaaa": True}
        assert stats["n_cache_hits"] == 1
        assert stats.get("n_fetched", 0) == 0

    def test_indecisive_is_not_cached_so_it_retries_next_run(self, tmp_path):
        """A market that hasn't settled yet may settle tomorrow -- caching
        'unknown' forever would permanently freeze it out of the population."""
        cache_path = tmp_path / "cache.json"
        with patch(
            "src.scripts.resolve_bracket_outcomes.fetch_market_resolution", return_value=None
        ):
            res, stats = resolve_gamma_outcomes({"0xopen"}, cache_path=cache_path)
        assert res == {}
        assert stats["n_indecisive"] == 1
        assert load_gamma_cache(cache_path) == {}

        # Later run, now decisive -> picked up.
        with patch(
            "src.scripts.resolve_bracket_outcomes.fetch_market_resolution", return_value=True
        ):
            res2, _ = resolve_gamma_outcomes({"0xopen"}, cache_path=cache_path)
        assert res2 == {"0xopen": True}

    def test_fetch_error_degrades_to_metar_and_does_not_raise(self, tmp_path):
        with patch(
            "src.scripts.resolve_bracket_outcomes.fetch_market_resolution",
            side_effect=RuntimeError("gamma is down"),
        ):
            res, stats = resolve_gamma_outcomes({"0xaaa"}, cache_path=tmp_path / "c.json")
        assert res == {}
        assert stats["n_fetch_errors"] == 1

    def test_one_bad_ticker_does_not_stop_the_others(self, tmp_path):
        def _flaky(ticker):
            if ticker == "0xbad":
                raise RuntimeError("boom")
            return True

        with patch(
            "src.scripts.resolve_bracket_outcomes.fetch_market_resolution", side_effect=_flaky
        ):
            res, stats = resolve_gamma_outcomes(
                {"0xbad", "0xgood"}, cache_path=tmp_path / "c.json"
            )
        assert res == {"0xgood": True}
        assert stats["n_fetch_errors"] == 1

    def test_no_network_makes_zero_calls_and_still_serves_cache(self, tmp_path):
        cache_path = tmp_path / "cache.json"
        save_gamma_cache(cache_path, {"0xcached": True})
        with patch(
            "src.scripts.resolve_bracket_outcomes.fetch_market_resolution"
        ) as mock_fetch:
            res, stats = resolve_gamma_outcomes(
                {"0xcached", "0xuncached"}, cache_path=cache_path, allow_network=False
            )
        mock_fetch.assert_not_called()
        assert res == {"0xcached": True}
        assert stats["n_skipped_offline"] == 1


class TestGammaPrecedence:
    """Gamma must win over METAR -- that is the entire point of #860."""

    def _rows_and_highs(self):
        rows = [_eval_row(ticker="0xaaa", bracket_low=81.0, bracket_high=83.0)]
        highs = {("KORD", "2026-07-05"): 82.0}  # METAR says YES
        return rows, highs

    def test_gamma_no_overrides_metar_yes(self):
        rows, highs = self._rows_and_highs()
        out, counts = resolve_bracket_rows(rows, highs, {"0xaaa": False})
        assert out[0]["resolved_yes"] is False
        assert out[0]["resolution_source"] == "gamma"
        assert counts["resolved_from_gamma"] == 1

    def test_gamma_yes_overrides_metar_no(self):
        rows = [_eval_row(ticker="0xaaa", bracket_low=81.0, bracket_high=83.0)]
        highs = {("KORD", "2026-07-05"): 95.0}  # METAR says NO
        out, _ = resolve_bracket_rows(rows, highs, {"0xaaa": True})
        assert out[0]["resolved_yes"] is True
        assert out[0]["resolution_source"] == "gamma"

    def test_falls_back_to_metar_when_gamma_has_no_answer(self):
        rows, highs = self._rows_and_highs()
        out, counts = resolve_bracket_rows(rows, highs, {})
        assert out[0]["resolved_yes"] is True
        assert out[0]["resolution_source"] == "metar"
        assert counts["resolved_from_metar"] == 1

    def test_gamma_resolves_a_row_with_no_observation_at_all(self):
        """An official resolution doesn't need METAR to corroborate it, so
        switching Gamma on can only grow the population, never shrink it."""
        rows = [_eval_row(ticker="0xaaa")]
        out, counts = resolve_bracket_rows(rows, {}, {"0xaaa": True})
        assert len(out) == 1
        assert out[0]["resolved_yes"] is True
        assert out[0]["observed_high"] is None
        assert counts.get("no_observed_high", 0) == 0

    def test_row_with_neither_gamma_nor_observation_is_dropped_not_guessed(self):
        rows = [_eval_row(ticker="0xaaa")]
        out, counts = resolve_bracket_rows(rows, {}, {})
        assert out == []
        assert counts["no_observed_high"] == 1


class TestResolveBracketOutcomesGammaWiring:
    def test_use_gamma_false_makes_no_network_call(self, tmp_path):
        bracket_evals = tmp_path / "bracket_evals.jsonl"
        _write_bracket_evals_jsonl(bracket_evals, [_eval_row(ticker="0xaaa")])
        db_path = tmp_path / "meteoedge.db"
        _write_observations_db(db_path, [("KORD", "2026-07-05T18:00:00+00:00", 82.0)])

        with patch(
            "src.scripts.resolve_bracket_outcomes.fetch_market_resolution"
        ) as mock_fetch:
            rows, counts = resolve_bracket_outcomes(
                bracket_evals, db_path, use_gamma=False, gamma_cache_path=tmp_path / "c.json"
            )
        mock_fetch.assert_not_called()
        assert counts["gamma_enabled"] is False
        assert rows[0]["resolution_source"] == "metar"

    def test_gamma_result_flows_end_to_end(self, tmp_path):
        bracket_evals = tmp_path / "bracket_evals.jsonl"
        _write_bracket_evals_jsonl(bracket_evals, [_eval_row(ticker="0xaaa")])
        db_path = tmp_path / "meteoedge.db"
        # METAR would say YES (82 is inside 81-83); gamma overrules with NO.
        _write_observations_db(db_path, [("KORD", "2026-07-05T18:00:00+00:00", 82.0)])

        with patch(
            "src.scripts.resolve_bracket_outcomes.fetch_market_resolution", return_value=False
        ):
            rows, counts = resolve_bracket_outcomes(
                bracket_evals, db_path, gamma_cache_path=tmp_path / "c.json"
            )
        assert rows[0]["resolved_yes"] is False
        assert rows[0]["resolution_source"] == "gamma"
        assert counts["resolved_from_gamma"] == 1


class TestDetectMultiYesStationDays:
    def test_flags_two_yes_brackets_on_one_station_day(self):
        """One station-day has one daily high, so two YES brackets is a
        logical impossibility (the boundary-inclusivity issue, #861)."""
        rows = [
            {"station": "ZGSZ", "settlement_date": "2026-06-12", "resolved_yes": True,
             "bracket_low": 84.2, "bracket_high": 86.0, "observed_high": 86.0,
             "resolution_source": "metar"},
            {"station": "ZGSZ", "settlement_date": "2026-06-12", "resolved_yes": True,
             "bracket_low": 86.0, "bracket_high": 87.8, "observed_high": 86.0,
             "resolution_source": "metar"},
        ]
        out = detect_multi_yes_station_days(rows)
        assert len(out) == 1
        assert out[0]["station"] == "ZGSZ"
        assert out[0]["n_yes"] == 2
        # Adjacent brackets sharing edge 86.0 -- the interval-convention question.
        assert out[0]["collision_kind"] == COLLISION_BOUNDARY

    def test_single_yes_is_not_flagged(self):
        rows = [
            {"station": "KORD", "settlement_date": "2026-07-05", "resolved_yes": True,
             "bracket_low": 81.0, "bracket_high": 83.0, "observed_high": 82.0,
             "resolution_source": "metar"},
            {"station": "KORD", "settlement_date": "2026-07-05", "resolved_yes": False,
             "bracket_low": 84.0, "bracket_high": 85.0, "observed_high": 82.0,
             "resolution_source": "metar"},
        ]
        assert detect_multi_yes_station_days(rows) == []

    def test_different_days_are_not_conflated(self):
        rows = [
            {"station": "KORD", "settlement_date": "2026-07-05", "resolved_yes": True,
             "bracket_low": 81.0, "bracket_high": 83.0, "observed_high": 82.0,
             "resolution_source": "metar"},
            {"station": "KORD", "settlement_date": "2026-07-06", "resolved_yes": True,
             "bracket_low": 81.0, "bracket_high": 83.0, "observed_high": 82.0,
             "resolution_source": "metar"},
        ]
        assert detect_multi_yes_station_days(rows) == []


class TestClassifyMultiYes:
    """A multi-YES station-day is impossible either way, but the SHAPE of the
    collision says which bug caused it -- #861 (interval convention) or #867
    (the resolution source itself). Conflating them sends readers to the wrong
    issue, which is exactly what the 2026-07-26 Pass-1 report did."""

    def test_adjacent_brackets_sharing_an_edge_are_boundary(self):
        # ZGSZ 29-30C and 30-31C in Fahrenheit -- share edge 86.0.
        assert classify_multi_yes([
            (84.2, 86.0, "metar"), (86.0, 87.8, "metar"),
        ]) == COLLISION_BOUNDARY

    def test_overlapping_brackets_are_boundary(self):
        assert classify_multi_yes([
            (80.0, 86.0, "metar"), (84.0, 90.0, "metar"),
        ]) == COLLISION_BOUNDARY

    def test_brackets_that_do_not_touch_are_disjoint(self):
        # RKSI 2026-07-05: 3.6F apart, both Gamma-resolved. No interval
        # convention produces both -- issue #867.
        assert classify_multi_yes([
            (75.2, 77.0, "gamma"), (80.6, 82.4, "gamma"),
        ]) == COLLISION_DISJOINT

    def test_far_apart_brackets_are_disjoint(self):
        # LFPB 2026-07-03: 21.6F apart.
        assert classify_multi_yes([
            (59.0, 60.8, "gamma"), (82.4, 84.2, "gamma"),
        ]) == COLLISION_DISJOINT

    def test_unsorted_input_is_classified_on_the_number_line_not_input_order(self):
        # RJTT 2026-07-06 as it appeared in the report: higher bracket first.
        assert classify_multi_yes([
            (78.8, 80.6, "gamma"), (69.8, 71.6, "gamma"),
        ]) == COLLISION_DISJOINT

    def test_three_brackets_with_one_gap_are_disjoint(self):
        """Any gap anywhere makes the day unexplainable by an interval rule."""
        assert classify_multi_yes([
            (60.0, 62.0, "gamma"), (62.0, 64.0, "gamma"), (80.0, 82.0, "gamma"),
        ]) == COLLISION_DISJOINT

    def test_missing_bound_is_unknown_not_forced_into_a_bucket(self):
        assert classify_multi_yes([
            (None, 86.0, "metar"), (86.0, 87.8, "metar"),
        ]) == COLLISION_UNKNOWN

    def test_non_numeric_bound_is_unknown(self):
        assert classify_multi_yes([
            ("n/a", 86.0, "metar"), (86.0, 87.8, "metar"),
        ]) == COLLISION_UNKNOWN

    def test_single_bracket_is_unknown(self):
        assert classify_multi_yes([(84.2, 86.0, "metar")]) == COLLISION_UNKNOWN

    def test_detect_attaches_kind_and_sources(self):
        rows = [
            {"station": "RKSI", "settlement_date": "2026-07-05", "resolved_yes": True,
             "bracket_low": 75.2, "bracket_high": 77.0, "observed_high": 80.6,
             "resolution_source": "gamma"},
            {"station": "RKSI", "settlement_date": "2026-07-05", "resolved_yes": True,
             "bracket_low": 80.6, "bracket_high": 82.4, "observed_high": 80.6,
             "resolution_source": "gamma"},
        ]
        out = detect_multi_yes_station_days(rows)
        assert out[0]["collision_kind"] == COLLISION_DISJOINT
        assert out[0]["sources"] == ["gamma"]

    def test_entry_contract_is_complete_for_both_consumers(self):
        """``detect_multi_yes_station_days`` has exactly two consumers --
        ``build_dry_run_report`` here and ``bss_market_vs_model_report.
        _ground_truth_section`` -- and both index these keys directly. Pinning
        the contract means adding a third consumer, or dropping a key, fails
        here rather than at report-render time on the production host.
        """
        rows = [
            {"station": "ZGSZ", "settlement_date": "2026-06-12", "resolved_yes": True,
             "bracket_low": 84.2, "bracket_high": 86.0, "observed_high": 86.0,
             "resolution_source": "metar"},
            {"station": "ZGSZ", "settlement_date": "2026-06-12", "resolved_yes": True,
             "bracket_low": 86.0, "bracket_high": 87.8, "observed_high": 86.0,
             "resolution_source": "metar"},
        ]
        entry = detect_multi_yes_station_days(rows)[0]
        assert set(entry) == {
            "station", "settlement_date", "n_yes", "observed_high", "brackets",
            "collision_kind", "sources",
        }
        # brackets is the (low, high, source) triple both reports unpack.
        assert all(len(b) == 3 for b in entry["brackets"])

    def test_dry_run_report_splits_the_two_shapes(self):
        """The report must not present one conflated count."""
        rows = [
            # boundary pair (#861)
            {"station": "ZGSZ", "settlement_date": "2026-06-12", "resolved_yes": True,
             "bracket_low": 84.2, "bracket_high": 86.0, "observed_high": 86.0,
             "resolution_source": "metar"},
            {"station": "ZGSZ", "settlement_date": "2026-06-12", "resolved_yes": True,
             "bracket_low": 86.0, "bracket_high": 87.8, "observed_high": 86.0,
             "resolution_source": "metar"},
            # disjoint pair (#867)
            {"station": "RKSI", "settlement_date": "2026-07-05", "resolved_yes": True,
             "bracket_low": 75.2, "bracket_high": 77.0, "observed_high": 80.6,
             "resolution_source": "gamma"},
            {"station": "RKSI", "settlement_date": "2026-07-05", "resolved_yes": True,
             "bracket_low": 80.6, "bracket_high": 82.4, "observed_high": 80.6,
             "resolution_source": "gamma"},
        ]
        report = build_dry_run_report(
            rows, {"n_bracket_rows": 4, "n_station_days": 2}, {}, {}, "2026-02-15"
        )
        assert "| `boundary` (adjacent/overlapping) | 1 |" in report
        assert "| `disjoint` (brackets do not touch) | 1 |" in report
        assert "#867" in report


class TestGroundTruthQuality:
    """Issue #870 -- M3 is ~95% Gamma-scored, and until these run we do not
    know that ground truth's error rate on the population it will be scored on."""

    def test_gamma_vs_metar_disagreement_is_measured_on_the_full_population(self):
        rows = [
            # Gamma YES, observed high inside the bracket -> agree.
            {"station": "KORD", "settlement_date": "2026-07-05", "resolved_yes": True,
             "bracket_low": 80.0, "bracket_high": 82.0, "observed_high": 81.0,
             "resolution_source": "gamma"},
            # Gamma YES, observed high far outside -> disagree (the #867 shape).
            {"station": "RKSI", "settlement_date": "2026-07-05", "resolved_yes": True,
             "bracket_low": 75.2, "bracket_high": 77.0, "observed_high": 80.6,
             "resolution_source": "gamma"},
        ]
        out = cross_check_gamma_vs_metar(rows)
        assert out["n_comparable"] == 2
        assert out["n_disagree"] == 1
        assert out["disagreement_rate"] == 0.5
        assert out["n_gamma_yes_metar_no"] == 1
        assert out["by_station"]["RKSI"]["rate"] == 1.0

    def test_metar_resolved_rows_are_not_self_compared(self):
        """A METAR row would trivially agree with itself and dilute the rate."""
        rows = [
            {"station": "KORD", "settlement_date": "2026-07-05", "resolved_yes": True,
             "bracket_low": 80.0, "bracket_high": 82.0, "observed_high": 81.0,
             "resolution_source": "metar"},
        ]
        assert cross_check_gamma_vs_metar(rows)["n_comparable"] == 0

    def test_gamma_row_without_observed_high_is_counted_not_compared(self):
        rows = [
            {"station": "KORD", "settlement_date": "2026-07-05", "resolved_yes": True,
             "bracket_low": 80.0, "bracket_high": 82.0, "observed_high": None,
             "resolution_source": "gamma"},
        ]
        out = cross_check_gamma_vs_metar(rows)
        assert out["n_comparable"] == 0
        assert out["n_gamma_rows_without_observed_high"] == 1
        assert out["disagreement_rate"] is None

    def test_both_disagreement_directions_are_tracked(self):
        rows = [
            {"station": "A", "settlement_date": "2026-07-05", "resolved_yes": False,
             "bracket_low": 80.0, "bracket_high": 82.0, "observed_high": 81.0,
             "resolution_source": "gamma"},   # gamma NO, metar YES
            {"station": "B", "settlement_date": "2026-07-05", "resolved_yes": True,
             "bracket_low": 80.0, "bracket_high": 82.0, "observed_high": 90.0,
             "resolution_source": "gamma"},   # gamma YES, metar NO
        ]
        out = cross_check_gamma_vs_metar(rows)
        assert out["n_gamma_no_metar_yes"] == 1
        assert out["n_gamma_yes_metar_no"] == 1

    def test_zero_yes_day_with_high_in_a_gap_is_not_flagged_suspicious(self):
        """US integer-F ladders have real gaps (#861) -- a high landing in one
        correctly resolves everything NO. That is geometry, not a bug."""
        rows = [
            {"station": "KORD", "settlement_date": "2026-07-05", "resolved_yes": False,
             "bracket_low": 76.0, "bracket_high": 77.0, "observed_high": 77.5,
             "resolution_source": "metar"},
            {"station": "KORD", "settlement_date": "2026-07-05", "resolved_yes": False,
             "bracket_low": 78.0, "bracket_high": 79.0, "observed_high": 77.5,
             "resolution_source": "metar"},
        ]
        out = detect_zero_yes_station_days(rows)
        assert len(out) == 1
        assert out[0]["observed_high_in_a_gap"] is True

    def test_zero_yes_day_with_a_covered_high_is_flagged_suspicious(self):
        """A high inside an evaluated bracket MUST resolve exactly one YES."""
        rows = [
            {"station": "RKSI", "settlement_date": "2026-07-05", "resolved_yes": False,
             "bracket_low": 80.0, "bracket_high": 82.0, "observed_high": 81.0,
             "resolution_source": "gamma"},
        ]
        out = detect_zero_yes_station_days(rows)
        assert out[0]["observed_high_in_a_gap"] is False
        assert out[0]["sources"] == ["gamma"]

    def test_day_with_a_yes_is_not_returned(self):
        rows = [
            {"station": "KORD", "settlement_date": "2026-07-05", "resolved_yes": True,
             "bracket_low": 80.0, "bracket_high": 82.0, "observed_high": 81.0,
             "resolution_source": "metar"},
        ]
        assert detect_zero_yes_station_days(rows) == []

    def test_zero_yes_without_observed_high_is_undeterminable(self):
        rows = [
            {"station": "KORD", "settlement_date": "2026-07-05", "resolved_yes": False,
             "bracket_low": 80.0, "bracket_high": 82.0, "observed_high": None,
             "resolution_source": "gamma"},
        ]
        assert detect_zero_yes_station_days(rows)[0]["observed_high_in_a_gap"] is None

    def test_ladder_completeness_distribution(self):
        rows = (
            [{"station": "KORD", "settlement_date": "2026-07-05"} for _ in range(11)]
            + [{"station": "KLAX", "settlement_date": "2026-07-05"} for _ in range(3)]
        )
        out = ladder_completeness(rows)
        assert out["n_station_days"] == 2
        assert out["min"] == 3
        assert out["max"] == 11
        assert out["histogram"] == {3: 1, 11: 1}

    def test_ladder_completeness_on_empty_input(self):
        assert ladder_completeness([])["n_station_days"] == 0

    def test_dry_run_report_renders_all_three_checks(self):
        rows = [
            {"station": "RKSI", "settlement_date": "2026-07-05", "resolved_yes": True,
             "bracket_low": 75.2, "bracket_high": 77.0, "observed_high": 80.6,
             "resolution_source": "gamma"},
            {"station": "KORD", "settlement_date": "2026-07-06", "resolved_yes": False,
             "bracket_low": 80.0, "bracket_high": 82.0, "observed_high": 81.0,
             "resolution_source": "gamma"},
        ]
        report = build_dry_run_report(
            rows, {"n_bracket_rows": 2, "n_station_days": 2}, {}, {}, "2026-02-15"
        )
        assert "Ground-truth quality 1/3: Gamma vs METAR" in report
        assert "Ground-truth quality 2/3: station-days with NO YES bracket" in report
        assert "Ground-truth quality 3/3: ladder completeness" in report
        # Both rows disagree with METAR -> 100%.
        assert "| **Disagreement rate** | **100.0%** |" in report
        # KORD 2026-07-06: high covered by the bracket yet nothing resolved YES.
        assert "**Suspicious**" in report


class TestDirectCheckResolutionSource:
    def test_mismatches_are_attributed_to_the_settlements_row_source(self, tmp_path):
        """A gamma-settled row disagreeing with our METAR recomputation is the
        EXPECTED ~22% divergence (#644); a metar-settled row disagreeing is
        unexplained. The report must be able to tell them apart (#860)."""
        db_path = tmp_path / "meteoedge.db"
        _write_settlements_db_with_source(
            db_path,
            observations=[
                ("KORD", "2026-07-05T18:00:00+00:00", 82.0),
                ("KLAX", "2026-07-05T18:00:00+00:00", 82.0),
            ],
            settlements=[
                # Both disagree with METAR (which says YES for 81-83 @ 82.0),
                # but for different reasons.
                ("0xgamma", "KORD", 81.0, 83.0, 82.0, False, "gamma"),
                ("0xmetar", "KLAX", 81.0, 83.0, 82.0, False, "metar"),
            ],
            trades=[
                ("0xgamma", "KORD", "2026-07-05T12:00:00+00:00", "2026-07-05"),
                ("0xmetar", "KLAX", "2026-07-05T12:00:00+00:00", "2026-07-05"),
            ],
        )
        result = cross_check_against_settlements_direct(db_path)
        assert result["n_mismatch"] == 2
        assert result["mismatch_by_source"] == {"gamma": 1, "metar": 1}

    def test_settlements_actual_high_is_reported_alongside_our_recomputation(self, tmp_path):
        """When the two temperatures differ, the disagreement is about the
        temperature; when they're equal, it's about the resolution rule."""
        db_path = tmp_path / "meteoedge.db"
        _write_settlements_db_with_source(
            db_path,
            observations=[("KORD", "2026-07-05T18:00:00+00:00", 82.0)],
            settlements=[("0xaaa", "KORD", 81.0, 83.0, 91.4, False, "gamma")],
            trades=[("0xaaa", "KORD", "2026-07-05T12:00:00+00:00", "2026-07-05")],
        )
        result = cross_check_against_settlements_direct(db_path)
        m = result["mismatches"][0]
        assert m["observed_high"] == 82.0
        assert m["settlements_actual_high_f"] == 91.4

    def test_db_without_resolution_source_column_still_runs(self, tmp_path):
        """The column arrived as a migration, so older DBs lack it. Probing
        for it must degrade to 'unknown', NOT zero out the whole check."""
        db_path = tmp_path / "meteoedge.db"
        _write_settlements_direct_db(
            db_path,
            observations=[("KORD", "2026-07-05T18:00:00+00:00", 82.0)],
            settlements=[("0xaaa", "KORD", 81.0, 83.0, 82.0, False)],
            trades=[("0xaaa", "KORD", "2026-07-05T12:00:00+00:00", "2026-07-05")],
        )
        result = cross_check_against_settlements_direct(db_path)
        assert result["n_checked"] == 1
        assert result["n_mismatch"] == 1
        assert result["mismatch_by_source"] == {"unknown": 1}


class TestReportGammaSections:
    def test_report_includes_ground_truth_and_multi_yes_sections(self, tmp_path):
        bracket_evals = tmp_path / "bracket_evals.jsonl"
        _write_bracket_evals_jsonl(bracket_evals, [_eval_row(ticker="0xaaa")])
        db_path = tmp_path / "meteoedge.db"
        _write_observations_db(db_path, [("KORD", "2026-07-05T18:00:00+00:00", 82.0)])

        with patch(
            "src.scripts.resolve_bracket_outcomes.fetch_market_resolution", return_value=True
        ):
            rc = run_dry_run(
                bracket_evals, db_path, tmp_path / "out", run_date="2026-07-25",
                gamma_cache_path=tmp_path / "c.json",
            )
        assert rc == 0
        text = (tmp_path / "out" / "bracket_outcome_resolution_dryrun_2026-07-25.md").read_text()
        assert "## Ground truth used (issue #860)" in text
        assert "Resolved from **gamma**" in text
        assert "more than one YES bracket" in text

    def test_report_warns_loudly_when_gamma_is_disabled(self, tmp_path):
        bracket_evals = tmp_path / "bracket_evals.jsonl"
        _write_bracket_evals_jsonl(bracket_evals, [_eval_row(ticker="0xaaa")])
        db_path = tmp_path / "meteoedge.db"
        _write_observations_db(db_path, [("KORD", "2026-07-05T18:00:00+00:00", 82.0)])

        rc = run_dry_run(
            bracket_evals, db_path, tmp_path / "out", run_date="2026-07-25",
            use_gamma=False, gamma_cache_path=tmp_path / "c.json",
        )
        assert rc == 0
        text = (tmp_path / "out" / "bracket_outcome_resolution_dryrun_2026-07-25.md").read_text()
        assert "Gamma resolution DISABLED" in text
        assert "Not suitable for the #822 M3 verdict" in text


class TestGammaRepairAttribution:
    """gamma_repair rendered as a blank '--' on the 2026-07-25 production run,
    leaving 28 of 30 mismatches unexplained in the report even though they are
    authoritative gamma truth (written by repair_settlements_from_gamma's
    official-final-price back-fill)."""

    def _report_with_sources(self, tmp_path, settlements, trades, observations):
        db_path = tmp_path / "meteoedge.db"
        _write_settlements_db_with_source(
            db_path, settlements=settlements, trades=trades, observations=observations
        )
        direct = cross_check_against_settlements_direct(db_path)
        return build_dry_run_report(
            [], {}, {"n_overlap": 0, "n_match": 0, "n_mismatch": 0, "mismatches": []},
            direct, "2026-07-25",
        )

    def test_gamma_repair_is_labelled_not_blank(self, tmp_path):
        text = self._report_with_sources(
            tmp_path,
            settlements=[("0xaaa", "KORD", 81.0, 83.0, 82.0, False, "gamma_repair")],
            trades=[("0xaaa", "KORD", "2026-07-05T12:00:00+00:00", "2026-07-05")],
            observations=[("KORD", "2026-07-05T18:00:00+00:00", 82.0)],
        )
        assert "| `gamma_repair` | 1 |" in text
        assert "| `gamma_repair` | 1 | -- |" not in text
        assert "Expected." in text

    def test_unknown_gamma_family_source_still_reads_as_gamma(self, tmp_path):
        text = self._report_with_sources(
            tmp_path,
            settlements=[("0xaaa", "KORD", 81.0, 83.0, 82.0, False, "gamma_v2_future")],
            trades=[("0xaaa", "KORD", "2026-07-05T12:00:00+00:00", "2026-07-05")],
            observations=[("KORD", "2026-07-05T18:00:00+00:00", 82.0)],
        )
        assert "| `gamma_v2_future` | 1 | -- |" not in text
        assert "Expected." in text

    def test_unexplained_count_excludes_gamma_family(self, tmp_path):
        text = self._report_with_sources(
            tmp_path,
            settlements=[
                ("0xa", "KORD", 81.0, 83.0, 82.0, False, "gamma_repair"),
                ("0xb", "KLAX", 81.0, 83.0, 82.0, False, "gamma"),
                ("0xc", "KATL", 81.0, 83.0, 82.0, False, "metar"),
            ],
            trades=[
                ("0xa", "KORD", "2026-07-05T12:00:00+00:00", "2026-07-05"),
                ("0xb", "KLAX", "2026-07-05T12:00:00+00:00", "2026-07-05"),
                ("0xc", "KATL", "2026-07-05T12:00:00+00:00", "2026-07-05"),
            ],
            observations=[
                ("KORD", "2026-07-05T18:00:00+00:00", 82.0),
                ("KLAX", "2026-07-05T18:00:00+00:00", 82.0),
                ("KATL", "2026-07-05T18:00:00+00:00", 82.0),
            ],
        )
        # Only the metar-sourced one counts as a defect signal.
        assert "**Unexplained mismatches (non-gamma-sourced): 1.**" in text

    def test_all_gamma_sourced_reports_zero_unexplained(self, tmp_path):
        """The 2026-07-25 production shape: every mismatch gamma-sourced, so
        the resolver itself has no evidence against it."""
        text = self._report_with_sources(
            tmp_path,
            settlements=[
                ("0xa", "KORD", 81.0, 83.0, 82.0, False, "gamma_repair"),
                ("0xb", "KLAX", 81.0, 83.0, 82.0, False, "gamma"),
            ],
            trades=[
                ("0xa", "KORD", "2026-07-05T12:00:00+00:00", "2026-07-05"),
                ("0xb", "KLAX", "2026-07-05T12:00:00+00:00", "2026-07-05"),
            ],
            observations=[
                ("KORD", "2026-07-05T18:00:00+00:00", 82.0),
                ("KLAX", "2026-07-05T18:00:00+00:00", 82.0),
            ],
        )
        assert "**Unexplained mismatches (non-gamma-sourced): 0.**" in text

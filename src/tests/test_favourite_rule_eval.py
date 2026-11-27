"""Tests for src/scripts/favourite_rule_eval.py (issue #1545).

Covers the three groups the issue's own implementation plan calls for:
- first-qualifying-poll selection and one entry per market
  (select_first_qualifying_entries)
- payoff and fee maths (fee_fraction, build_trade_rows)
- clustering key (build_trade_rows's cluster_key; clustered_bootstrap only
  ever draws from existing clusters)

Plus, from the "Amended 2026-11-27 (PM)" rule H4-C: _first_poll_per_ticker_hour
and select_first_qualifying_entries(hourly=True), including the required
"qualifies only mid-hour" A-vs-C divergence case.

Plus: load_snapshots end-to-end against a tiny synthetic on-disk snapshot
archive (since/until/direction filtering, plain + dated + gzipped files),
load_outcomes' DB-then-Gamma-cache fallback, is_open_ended's sentinel
convention, build_splits, a format_report/evaluate smoke test (both rules),
and the daily_health_report.py (db, today) adapter contract.

All dates are synthetic (2026-11-2x), matching the repo convention of never
anchoring test fixtures to a real "today".
"""
from __future__ import annotations

import gzip
import json
import sqlite3

import pytest

from src.scripts.favourite_rule_eval import (
    ENTRY_HI_CENTS,
    ENTRY_LO_CENTS,
    ENTRY_RULE_BOTH,
    ENTRY_RULE_EVERY_POLL,
    ENTRY_RULE_HOURLY,
    _first_poll_per_ticker_hour,
    ask_band,
    build_splits,
    build_trade_rows,
    clustered_bootstrap,
    evaluate,
    fee_fraction,
    format_report,
    is_open_ended,
    load_outcomes,
    load_snapshots,
    section,
    select_first_qualifying_entries,
)


def _snap(ticker="0xaaa", ts="2026-11-24T01:00:00+00:00", station="KORD",
          date="2026-11-24", yes_ask=50, no_ask=50, is_next_day=0,
          bracket_low=70.0, bracket_high=72.0, direction="high"):
    return {
        "ticker": ticker, "ts": ts, "station": station, "date": date,
        "yes_ask": yes_ask, "no_ask": no_ask, "is_next_day": is_next_day,
        "bracket_low": bracket_low, "bracket_high": bracket_high,
        "direction": direction,
    }


def _entry(ticker="0xaaa", station="KORD", date="2026-11-24", side="YES",
           ask_cents=90, is_next_day=False, bracket_low=70.0, bracket_high=72.0):
    return {
        "ticker": ticker, "station": station, "date": date, "side": side,
        "ask_cents": ask_cents, "is_next_day": is_next_day,
        "bracket_low": bracket_low, "bracket_high": bracket_high, "ts": "t",
    }


# ---------------------------------------------------------------------------
# select_first_qualifying_entries
# ---------------------------------------------------------------------------

class TestSelectFirstQualifyingEntries:
    def test_picks_earliest_qualifying_poll_only(self):
        snaps = [
            _snap(ts="2026-11-24T01:00:00+00:00", yes_ask=50, no_ask=50),  # not qualifying
            _snap(ts="2026-11-24T02:00:00+00:00", yes_ask=85, no_ask=15),  # qualifies (YES)
            _snap(ts="2026-11-24T03:00:00+00:00", yes_ask=92, no_ask=8),   # later -- ignored
        ]
        entries = select_first_qualifying_entries(snaps)
        assert len(entries) == 1
        e = entries[0]
        assert e["side"] == "YES" and e["ask_cents"] == 85
        assert e["ts"] == "2026-11-24T02:00:00+00:00"

    def test_one_entry_per_market_multiple_tickers(self):
        snaps = [
            _snap(ticker="0xaaa", yes_ask=85, no_ask=15),
            _snap(ticker="0xbbb", yes_ask=10, no_ask=90),
        ]
        entries = select_first_qualifying_entries(snaps)
        assert {e["ticker"] for e in entries} == {"0xaaa", "0xbbb"}
        bbb = next(e for e in entries if e["ticker"] == "0xbbb")
        assert bbb["side"] == "NO" and bbb["ask_cents"] == 90

    def test_no_qualifying_poll_is_absent(self):
        assert select_first_qualifying_entries([_snap(yes_ask=50, no_ask=50)]) == []

    def test_band_boundaries_inclusive(self):
        snaps = [
            _snap(ticker="0xlo", yes_ask=ENTRY_LO_CENTS, no_ask=100 - ENTRY_LO_CENTS),
            _snap(ticker="0xhi", yes_ask=ENTRY_HI_CENTS, no_ask=100 - ENTRY_HI_CENTS),
        ]
        entries = select_first_qualifying_entries(snaps)
        assert {e["ticker"] for e in entries} == {"0xlo", "0xhi"}

    def test_just_outside_band_excluded(self):
        snaps = [
            _snap(ticker="0xlo", yes_ask=ENTRY_LO_CENTS - 1, no_ask=100 - (ENTRY_LO_CENTS - 1)),
            _snap(ticker="0xhi", yes_ask=ENTRY_HI_CENTS + 1, no_ask=100 - (ENTRY_HI_CENTS + 1)),
        ]
        assert select_first_qualifying_entries(snaps) == []

    def test_missing_keys_dropped(self):
        row = _snap(yes_ask=85, no_ask=15)
        del row["station"]
        assert select_first_qualifying_entries([row]) == []

    def test_unparseable_ts_dropped(self):
        row = _snap(yes_ask=85, no_ask=15)
        row["ts"] = "not-a-date"
        assert select_first_qualifying_entries([row]) == []


# ---------------------------------------------------------------------------
# Rule H4-C: _first_poll_per_ticker_hour / select_first_qualifying_entries(hourly=True)
# (issue #1545, "Amended 2026-11-27 (PM)")
# ---------------------------------------------------------------------------

class TestFirstPollPerTickerHour:
    def test_keeps_earliest_per_ticker_hour(self):
        snaps = [
            _snap(ticker="0xaaa", ts="2026-11-24T05:10:00+00:00"),
            _snap(ticker="0xaaa", ts="2026-11-24T05:00:00+00:00"),  # earlier, same hour
            _snap(ticker="0xaaa", ts="2026-11-24T06:05:00+00:00"),  # next hour
            _snap(ticker="0xbbb", ts="2026-11-24T05:03:00+00:00"),
        ]
        reduced = _first_poll_per_ticker_hour(snaps)
        kept_ts = sorted(r["ts"] for r in reduced)
        assert kept_ts == [
            "2026-11-24T05:00:00+00:00", "2026-11-24T05:03:00+00:00", "2026-11-24T06:05:00+00:00",
        ]

    def test_missing_ticker_or_ts_dropped(self):
        row = _snap()
        del row["ticker"]
        assert _first_poll_per_ticker_hour([row]) == []


class TestHourlyRuleCDivergesFromRuleA:
    def test_mid_hour_only_qualifying_poll_selected_by_a_not_c(self):
        # Top-of-hour poll (05:00) does NOT qualify; a mid-hour poll (05:30)
        # does -- A (every poll eligible) picks it. C only ever considers
        # each hour's top-of-hour poll, so it skips straight to the NEXT
        # hour's top-of-hour qualifying poll (06:00).
        snaps = [
            _snap(ts="2026-11-24T05:00:00+00:00", yes_ask=50, no_ask=50),
            _snap(ts="2026-11-24T05:30:00+00:00", yes_ask=85, no_ask=15),
            _snap(ts="2026-11-24T06:00:00+00:00", yes_ask=90, no_ask=10),
        ]
        entries_a = select_first_qualifying_entries(snaps, hourly=False)
        entries_c = select_first_qualifying_entries(snaps, hourly=True)

        assert len(entries_a) == 1
        assert entries_a[0]["ts"] == "2026-11-24T05:30:00+00:00"
        assert entries_a[0]["ask_cents"] == 85

        assert len(entries_c) == 1
        assert entries_c[0]["ts"] == "2026-11-24T06:00:00+00:00"
        assert entries_c[0]["ask_cents"] == 90

    def test_top_of_hour_qualifying_poll_agrees_for_both_rules(self):
        snaps = [_snap(ts="2026-11-24T05:00:00+00:00", yes_ask=85, no_ask=15)]
        entries_a = select_first_qualifying_entries(snaps, hourly=False)
        entries_c = select_first_qualifying_entries(snaps, hourly=True)
        assert entries_a == entries_c


# ---------------------------------------------------------------------------
# fee / payoff maths
# ---------------------------------------------------------------------------

class TestFeeAndPayoff:
    def test_fee_fraction_formula(self):
        assert fee_fraction(0.9, 0.01) == pytest.approx(0.01 * 0.9 * 0.1)

    def test_payoff_yes_win(self):
        rows = build_trade_rows([_entry(side="YES", ask_cents=90)], {"0xaaa": True}, fee_rate=0.01)
        assert len(rows) == 1
        r = rows[0]
        p = 0.90
        fee = 0.01 * p * (1 - p)
        expected_payoff = 1.0 - p - fee
        assert r["won"] is True
        assert r["payoff"] == pytest.approx(expected_payoff)
        assert r["roi"] == pytest.approx(expected_payoff / p)

    def test_payoff_no_loses_when_resolved_yes(self):
        rows = build_trade_rows(
            [_entry(ticker="0xbbb", side="NO", ask_cents=85)], {"0xbbb": True}, fee_rate=0.01,
        )
        r = rows[0]
        p = 0.85
        fee = 0.01 * p * (1 - p)
        expected_payoff = 0.0 - p - fee
        assert r["won"] is False
        assert r["payoff"] == pytest.approx(expected_payoff)

    def test_payoff_no_wins_when_resolved_no(self):
        rows = build_trade_rows(
            [_entry(ticker="0xccc", side="NO", ask_cents=85)], {"0xccc": False}, fee_rate=0.01,
        )
        r = rows[0]
        assert r["won"] is True
        assert r["payoff"] == pytest.approx(1.0 - 0.85 - fee_fraction(0.85, 0.01))

    def test_unresolved_ticker_dropped(self):
        assert build_trade_rows([_entry(ticker="0xddd")], {}) == []

    def test_different_fee_rate_applied(self):
        rows_1pct = build_trade_rows([_entry(ask_cents=90)], {"0xaaa": True}, fee_rate=0.01)
        rows_2pct = build_trade_rows([_entry(ask_cents=90)], {"0xaaa": True}, fee_rate=0.02)
        assert rows_2pct[0]["fee"] == pytest.approx(rows_1pct[0]["fee"] * 2)
        assert rows_2pct[0]["payoff"] < rows_1pct[0]["payoff"]

    def test_ask_band_splits(self):
        assert ask_band(80) == "80-84"
        assert ask_band(84) == "80-84"
        assert ask_band(85) == "85-89"
        assert ask_band(89) == "85-89"
        assert ask_band(90) == "90-97"
        assert ask_band(97) == "90-97"


# ---------------------------------------------------------------------------
# is_open_ended
# ---------------------------------------------------------------------------

class TestIsOpenEnded:
    def test_open_ended_low(self):
        assert is_open_ended(-50.0, 48.2) is True

    def test_open_ended_high(self):
        assert is_open_ended(98.0, 199.0) is True

    def test_interior(self):
        assert is_open_ended(70.0, 72.0) is False

    def test_missing_bounds_is_unknown(self):
        assert is_open_ended(None, 72.0) is None


# ---------------------------------------------------------------------------
# Clustering key
# ---------------------------------------------------------------------------

class TestClustering:
    def test_cluster_key_is_station_date(self):
        entries = [
            _entry(ticker="0xaaa", station="KORD", date="2026-11-24"),
            _entry(ticker="0xbbb", station="KORD", date="2026-11-24", side="NO", ask_cents=85),
        ]
        rows = build_trade_rows(entries, {"0xaaa": True, "0xbbb": True})
        assert all(r["cluster_key"] == ("KORD", "2026-11-24") for r in rows)

    def test_bootstrap_only_draws_existing_clusters(self):
        # Two clusters: {A: [1.0, 1.0]}, {B: [-1.0]}. Resampling CLUSTERS (not
        # individual trades) with replacement from 2 clusters can only ever
        # produce one of three possible resample means: 1.0 (A,A), -1.0
        # (B,B), or 1/3 ((A,B) or (B,A): [1,1,-1]/3). A per-trade bootstrap
        # could produce other values (e.g. drawing [1, -1] -> mean 0.0).
        rows = [
            {"roi": 1.0, "cluster_key": ("A", "d1")},
            {"roi": 1.0, "cluster_key": ("A", "d1")},
            {"roi": -1.0, "cluster_key": ("B", "d2")},
        ]
        result = clustered_bootstrap(rows, n_resamples=500, seed=1)
        assert result["n"] == 3
        assert result["mean_roi"] == pytest.approx(1 / 3)
        for key in ("ci_lo", "ci_hi", "p5"):
            value = result[key]
            assert value == pytest.approx(1.0) or value == pytest.approx(-1.0) \
                or value == pytest.approx(1 / 3)

    def test_empty_rows(self):
        assert clustered_bootstrap([]) == {
            "n": 0, "mean_roi": None, "ci_lo": None, "ci_hi": None, "p5": None,
        }


# ---------------------------------------------------------------------------
# build_splits
# ---------------------------------------------------------------------------

class TestBuildSplits:
    def test_splits_by_side_band_sameday_openended(self):
        rows = build_trade_rows(
            [
                _entry(ticker="0x1", side="YES", ask_cents=90, is_next_day=False,
                       bracket_low=-50.0, bracket_high=48.2),
                _entry(ticker="0x2", side="NO", ask_cents=85, is_next_day=True,
                       bracket_low=70.0, bracket_high=72.0),
            ],
            {"0x1": True, "0x2": False},
        )
        splits = build_splits(rows)
        assert splits["side=YES"]["n"] == 1
        assert splits["side=NO"]["n"] == 1
        assert splits["band=90-97"]["n"] == 1
        assert splits["band=85-89"]["n"] == 1
        assert splits["same_day"]["n"] == 1
        assert splits["next_day"]["n"] == 1
        assert splits["open_ended"]["n"] == 1
        assert splits["interior"]["n"] == 1
        # Both won (0x1 is YES and resolved_yes=True; 0x2 is NO and resolved_yes=False)
        assert splits["side=YES"]["win_rate"] == 1.0
        assert splits["side=NO"]["win_rate"] == 1.0


# ---------------------------------------------------------------------------
# load_snapshots (on-disk, rotated archive)
# ---------------------------------------------------------------------------

class TestLoadSnapshots:
    def test_reads_jsonl_and_gz_filters_since_until_direction(self, tmp_path):
        base = tmp_path / "snapshots.jsonl"
        base.write_text(json.dumps(_snap(ticker="0xplain", ts="2026-11-23T23:00:00+00:00")) + "\n")

        day1 = tmp_path / "snapshots.2026-11-24.jsonl"
        day1_rows = [
            _snap(ticker="0xday1", ts="2026-11-24T05:00:00+00:00"),
            _snap(ticker="0xlow", ts="2026-11-24T06:00:00+00:00", direction="low"),
        ]
        day1.write_text("\n".join(json.dumps(r) for r in day1_rows) + "\n")

        day2 = tmp_path / "snapshots.2026-11-25.jsonl.gz"
        day2_rows = [
            _snap(ticker="0xday2", ts="2026-11-25T05:00:00+00:00"),
            _snap(ticker="0xtoolate", ts="2026-11-26T05:00:00+00:00"),
        ]
        with gzip.open(day2, "wt", encoding="utf-8") as f:
            for r in day2_rows:
                f.write(json.dumps(r) + "\n")

        rows = load_snapshots(
            since="2026-11-24T00:00:00+00:00", until="2026-11-26T00:00:00+00:00", base=base,
        )
        assert {r["ticker"] for r in rows} == {"0xday1", "0xday2"}

    def test_missing_logs_dir_returns_empty(self, tmp_path):
        base = tmp_path / "does_not_exist" / "snapshots.jsonl"
        assert load_snapshots(base=base) == []

    def test_out_of_range_dated_file_never_read(self, tmp_path):
        # Adversarial fixture: the file's OWN rotation date (2026-01-01, way
        # outside [since, until)) disagrees with the row's ts field (claims
        # to be in range). File-level pruning trusts the filename, so this
        # row must be excluded even though a per-row-only filter would have
        # kept it -- this is what distinguishes file-level pruning from the
        # (already-tested) per-row ts filter above.
        base = tmp_path / "snapshots.jsonl"
        stale = tmp_path / "snapshots.2026-01-01.jsonl"
        stale.write_text(
            json.dumps(_snap(ticker="0xmislabeled", ts="2026-11-24T05:00:00+00:00")) + "\n"
        )
        in_range = tmp_path / "snapshots.2026-11-24.jsonl"
        in_range.write_text(
            json.dumps(_snap(ticker="0xok", ts="2026-11-24T06:00:00+00:00")) + "\n"
        )

        rows = load_snapshots(
            since="2026-11-24T00:00:00+00:00", until="2026-11-25T00:00:00+00:00", base=base,
        )
        assert {r["ticker"] for r in rows} == {"0xok"}


# ---------------------------------------------------------------------------
# load_outcomes (DB -> Gamma-cache fallback)
# ---------------------------------------------------------------------------

class TestLoadOutcomes:
    def test_db_then_cache_fallback(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        con = sqlite3.connect(db_path)
        con.execute(
            "CREATE TABLE market_resolutions (market TEXT PRIMARY KEY, "
            "resolved_yes INTEGER NOT NULL, cached_at TEXT NOT NULL)"
        )
        con.execute(
            "INSERT INTO market_resolutions VALUES (?, ?, ?)",
            ("0xaaa", 1, "2026-11-24T00:00:00+00:00"),
        )
        con.commit()
        con.close()

        cache_path = tmp_path / "gamma_resolution_cache.v2.json"
        cache_path.write_text(json.dumps({"0xbbb": False}))

        out = load_outcomes(
            {"0xaaa", "0xbbb", "0xccc"}, db_path=db_path, gamma_cache_path=cache_path,
        )
        assert out == {"0xaaa": True, "0xbbb": False}

    def test_missing_db_falls_back_to_cache_only(self, tmp_path):
        cache_path = tmp_path / "gamma_resolution_cache.v2.json"
        cache_path.write_text(json.dumps({"0xbbb": True}))
        out = load_outcomes({"0xbbb"}, db_path=tmp_path / "nope.db", gamma_cache_path=cache_path)
        assert out == {"0xbbb": True}

    def test_empty_tickers_short_circuits(self):
        assert load_outcomes(set()) == {}


# ---------------------------------------------------------------------------
# evaluate() / format_report() end-to-end smoke test
# ---------------------------------------------------------------------------

class TestEvaluateEndToEnd:
    def test_evaluate_and_format_report_both_rules(self, tmp_path):
        base = tmp_path / "snapshots.jsonl"
        rows = [
            _snap(ticker="0xwin", ts="2026-11-24T05:00:00+00:00", station="KORD",
                  date="2026-11-24", yes_ask=90, no_ask=10),
            _snap(ticker="0xlose", ts="2026-11-24T06:00:00+00:00", station="KJFK",
                  date="2026-11-24", yes_ask=85, no_ask=15),
            _snap(ticker="0xpending", ts="2026-11-25T05:00:00+00:00", station="KORD",
                  date="2026-11-25", yes_ask=95, no_ask=5),
        ]
        base.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

        cache_path = tmp_path / "gamma_resolution_cache.v2.json"
        cache_path.write_text(json.dumps({"0xwin": True, "0xlose": True}))

        result = evaluate(
            since="2026-11-24T00:00:00+00:00",
            db_path=None,
            snapshot_base=base,
            gamma_cache_path=cache_path,
            n_resamples=200,
        )
        assert result["entry_rule"] == ENTRY_RULE_BOTH
        assert set(result["rules"]) == {ENTRY_RULE_EVERY_POLL, ENTRY_RULE_HOURLY}

        # Every poll in this fixture is already its own top-of-hour poll, so
        # A and C agree here -- the divergence case lives in
        # TestHourlyRuleCDivergesFromRuleA above.
        for rk in (ENTRY_RULE_EVERY_POLL, ENTRY_RULE_HOURLY):
            r = result["rules"][rk]
            assert r["n_entries"] == 3
            assert r["n_resolved"] == 2
            assert r["mean_roi"] is not None
            assert r["n_days"] == 1  # only the 2 resolved rows share settlement_date 2026-11-24
            assert r["markets_per_day"] == pytest.approx(2.0)

        assert result["rules"][ENTRY_RULE_EVERY_POLL]["label"] == "A"
        assert result["rules"][ENTRY_RULE_HOURLY]["label"] == "C"

        report = format_report(result)
        assert "[Rule A]" in report and "[Rule C]" in report
        assert report.count("n entries (qualifying, incl. unresolved): 3") == 2
        assert report.count("n resolved (scored): 2") == 2
        assert report.count("Splits:") == 2

    def test_evaluate_single_rule_only_returns_that_rule(self, tmp_path):
        base = tmp_path / "snapshots.jsonl"
        result = evaluate(
            db_path=None, snapshot_base=base, gamma_cache_path=tmp_path / "nope.json",
            entry_rule=ENTRY_RULE_EVERY_POLL,
        )
        assert set(result["rules"]) == {ENTRY_RULE_EVERY_POLL}

    def test_evaluate_rejects_unknown_entry_rule(self, tmp_path):
        with pytest.raises(ValueError):
            evaluate(snapshot_base=tmp_path / "snapshots.jsonl", entry_rule="bogus")

    def test_evaluate_no_data_is_graceful(self, tmp_path):
        base = tmp_path / "snapshots.jsonl"
        result = evaluate(db_path=None, snapshot_base=base, gamma_cache_path=tmp_path / "nope.json")
        for r in result["rules"].values():
            assert r["n_entries"] == 0
            assert r["mean_roi"] is None
        report = format_report(result)
        assert "no resolved trades yet" in report


# ---------------------------------------------------------------------------
# section() -- daily_health_report.py adapter
# ---------------------------------------------------------------------------

def _fake_rule_result(label, n_entries=5, n_resolved=3, mean_roi=0.012,
                       ci_lo=-0.01, ci_hi=0.03, p5=-0.005):
    return {
        "label": label, "n_entries": n_entries, "n_resolved": n_resolved,
        "mean_roi": mean_roi, "ci_lo": ci_lo, "ci_hi": ci_hi, "p5": p5,
        "n_days": 1, "markets_per_day": 1.0, "eur_per_day_at_5": 0.1, "splits": {},
    }


class TestSectionAdapter:
    def test_section_returns_lines_and_flags_tuple_both_rules(self, monkeypatch):
        def _fake_evaluate(**kwargs):
            return {
                "checkpoint_n": 2000,
                "rules": {
                    ENTRY_RULE_EVERY_POLL: _fake_rule_result("A"),
                    ENTRY_RULE_HOURLY: _fake_rule_result("C", n_resolved=2, mean_roi=0.02),
                },
            }
        monkeypatch.setattr("src.scripts.favourite_rule_eval.evaluate", _fake_evaluate)
        lines, flags = section(db=None, today=None)
        assert flags == {}
        text = "\n".join(lines)
        assert "H4 Favourite Rule Forward Test" in text
        assert "[Rule A] n=3/2000" in text
        assert "[Rule A] mean ROI +1.20%" in text
        assert "[Rule C] n=2/2000" in text
        assert "[Rule C] mean ROI +2.00%" in text

    def test_section_handles_no_resolved_trades(self, monkeypatch):
        def _fake_evaluate(**kwargs):
            return {
                "checkpoint_n": 2000,
                "rules": {
                    ENTRY_RULE_EVERY_POLL: _fake_rule_result(
                        "A", n_entries=0, n_resolved=0, mean_roi=None, ci_lo=None, ci_hi=None, p5=None,
                    ),
                    ENTRY_RULE_HOURLY: _fake_rule_result(
                        "C", n_entries=0, n_resolved=0, mean_roi=None, ci_lo=None, ci_hi=None, p5=None,
                    ),
                },
            }
        monkeypatch.setattr("src.scripts.favourite_rule_eval.evaluate", _fake_evaluate)
        lines, _flags = section(db=None, today=None)
        text = "\n".join(lines)
        assert "[Rule A] No resolved entries yet." in text
        assert "[Rule C] No resolved entries yet." in text

    def test_section_degrades_gracefully_on_error(self, monkeypatch):
        def _boom(**kwargs):
            raise RuntimeError("boom")
        monkeypatch.setattr("src.scripts.favourite_rule_eval.evaluate", _boom)
        lines, flags = section(db=None, today=None)
        assert flags == {}
        assert "(unavailable -- query error)" in "\n".join(lines)


# ---------------------------------------------------------------------------
# daily_health_report.py wiring
# ---------------------------------------------------------------------------

class TestDailyHealthReportWiring:
    def test_adapter_is_imported_and_callable(self, monkeypatch):
        from src.scripts import daily_health_report

        def _fake_evaluate(**kwargs):
            return {
                "n_entries": 0, "n_resolved": 0, "mean_roi": None,
                "ci_lo": None, "ci_hi": None, "p5": None, "checkpoint_n": 2000,
            }
        monkeypatch.setattr("src.scripts.favourite_rule_eval.evaluate", _fake_evaluate)
        lines, flags = daily_health_report._build_favourite_rule_status(None, None)
        assert flags == {}
        assert "H4 Favourite Rule Forward Test" in "\n".join(lines)

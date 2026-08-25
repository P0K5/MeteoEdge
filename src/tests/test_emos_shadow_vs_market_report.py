"""Tests for src/scripts/emos_shadow_vs_market_report.py (issue #1041).

Covers:
- Self-gating (no bracket_evals / no readable DB -> no report written).
- The report writes to backtest_results/emos_shadow_vs_market_<date>.md,
  NEVER a bss_market_vs_model_pass1_*/pass2_* filename.
- The two mandatory caveats (undertrained samples, fixed-sigma mislabeling)
  are always rendered, verbatim, into the report.
- Default --since matches M3's window (2026-08-06).
- Regression guard: importing/using this module does not change
  bss_market_vs_model_report's own (untouched) default output one bit.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from src.data.db import Database
from src.scripts.emos_shadow_vs_market_report import DEFAULT_SINCE, run_report

STATION = "KORD"
CITY = "Chicago"
DATE = "2026-08-10"


def _eval_row(**overrides) -> dict:
    base = {
        "station": STATION,
        "ticker": "0xabc001",
        "bracket_low": 79.0,
        "bracket_high": 83.0,
        "poll_ts": "2026-08-10T13:30:00+00:00",
        "yes_ask": 30.0,
        "no_ask": 72.0,
        "p_yes": 0.2,
        "p_yes_raw": 0.2,
        "current_high": 78.0,
        "latest_temp": 79.0,
        "forecast_high": 80.0,
        "emos_mode": "emos_shadow",
        "is_next_day": 0,
        "minutes_to_settlement": 300.0,
        "execution_mode": "paper",
        "settlement_date": DATE,
        "direction": "high",
    }
    base.update(overrides)
    return base


def _write_bracket_evals(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _seed_db(path, observed_high: float = 81.0) -> None:
    db = Database(str(path))
    db.upsert_forecast_log_v2(
        station=STATION, model="nws", date=DATE, forecast_high_f=80.0,
        lead_hours=6, issued_at="2026-08-10T12:00:00+00:00",
    )
    db.upsert_forecast_log_v2(
        station=STATION, model="open_meteo", date=DATE, forecast_high_f=82.0,
        lead_hours=6, issued_at="2026-08-10T12:00:00+00:00",
    )
    db.upsert_emos_coefficients(
        city=CITY, model_mode="emos_shadow",
        a=1.0, b=0.9, c=0.5, d=1.1,
        crps_score=1.2, trained_at=datetime.utcnow().isoformat(),
        ready_for_promotion=0, lead_hours=6,
    )
    db._conn.commit()
    db._conn.close()

    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE IF NOT EXISTS observations (id INTEGER PRIMARY KEY, ts TEXT, "
        "station TEXT, temp_f REAL, temp_native REAL, unit TEXT, current_high REAL, "
        "source TEXT, raw_json TEXT)"
    )
    con.execute(
        "INSERT INTO observations (ts, station, temp_f, temp_native, unit, source) "
        "VALUES (?, ?, ?, ?, 'F', 'metar')",
        (f"{DATE}T23:00:00+00:00", STATION, observed_high, observed_high),
    )
    con.commit()
    con.close()


class TestSelfGating:
    def test_no_bracket_evals_writes_nothing(self, tmp_path):
        rc = run_report(
            bracket_evals=tmp_path / "logs" / "bracket_evals.jsonl",
            db_path=tmp_path / "data" / "meteoedge.db",
            out_dir=tmp_path / "backtest_results",
            since=None,
        )
        assert rc == 0
        assert not (tmp_path / "backtest_results").exists()

    def test_no_readable_db_writes_nothing(self, tmp_path):
        evals = tmp_path / "logs" / "bracket_evals.jsonl"
        _write_bracket_evals(evals, [_eval_row()])
        rc = run_report(
            bracket_evals=evals,
            db_path=tmp_path / "data" / "meteoedge.db",
            out_dir=tmp_path / "backtest_results",
            since=None,
        )
        assert rc == 0
        assert not (tmp_path / "backtest_results").exists()


class TestReportWritesToItsOwnFile:
    def test_writes_emos_shadow_filename_never_pass1_pass2(self, tmp_path):
        evals = tmp_path / "logs" / "bracket_evals.jsonl"
        _write_bracket_evals(evals, [_eval_row()])
        db_path = tmp_path / "data" / "meteoedge.db"
        _seed_db(db_path)
        out_dir = tmp_path / "backtest_results"

        rc = run_report(
            bracket_evals=evals, db_path=db_path, out_dir=out_dir,
            run_date="2026-08-11", since=None, use_gamma=False,
        )
        assert rc == 0
        report_path = out_dir / "emos_shadow_vs_market_2026-08-11.md"
        assert report_path.exists()
        assert not (out_dir / "bss_market_vs_model_pass1_2026-08-11.md").exists()
        assert not (out_dir / "bss_market_vs_model_pass2_2026-08-11.md").exists()

    def test_report_contains_both_mandatory_caveats(self, tmp_path):
        evals = tmp_path / "logs" / "bracket_evals.jsonl"
        _write_bracket_evals(evals, [_eval_row()])
        db_path = tmp_path / "data" / "meteoedge.db"
        _seed_db(db_path)
        out_dir = tmp_path / "backtest_results"

        run_report(
            bracket_evals=evals, db_path=db_path, out_dir=out_dir,
            run_date="2026-08-11", since=None, use_gamma=False,
        )
        text = (out_dir / "emos_shadow_vs_market_2026-08-11.md").read_text(encoding="utf-8")
        assert "Undertrained model" in text or "undertrained" in text.lower()
        assert "EMOS_MIN_SAMPLES_PROMOTION" in text
        assert "Fixed-sigma mislabeling" in text or "fixed-sigma" in text.lower()
        assert "ensemble_sigma_f` was never populated" in text
        assert "NOT THE M3 DECISION GATE" in text

    def test_report_never_claims_to_be_the_m3_gate(self, tmp_path):
        evals = tmp_path / "logs" / "bracket_evals.jsonl"
        _write_bracket_evals(evals, [_eval_row()])
        db_path = tmp_path / "data" / "meteoedge.db"
        _seed_db(db_path)
        out_dir = tmp_path / "backtest_results"
        run_report(
            bracket_evals=evals, db_path=db_path, out_dir=out_dir,
            run_date="2026-08-11", since=None, use_gamma=False,
        )
        text = (out_dir / "emos_shadow_vs_market_2026-08-11.md").read_text(encoding="utf-8")
        assert "M3 DECISION GATE" not in text.replace("NOT THE M3 DECISION GATE", "")


class TestDefaultSinceWindow:
    def test_default_since_matches_m3_window(self):
        assert DEFAULT_SINCE == "2026-08-06"

    def test_default_since_excludes_pre_window_rows(self, tmp_path):
        evals = tmp_path / "logs" / "bracket_evals.jsonl"
        _write_bracket_evals(evals, [
            _eval_row(ticker="0xold", poll_ts="2026-07-01T13:30:00+00:00",
                      settlement_date="2026-07-01"),
        ])
        db_path = tmp_path / "data" / "meteoedge.db"
        _seed_db(db_path)
        out_dir = tmp_path / "backtest_results"
        rc = run_report(
            bracket_evals=evals, db_path=db_path, out_dir=out_dir,
            run_date="2026-08-11",  # since defaults to DEFAULT_SINCE
            use_gamma=False,
        )
        assert rc == 0
        # Pre-window row is dropped by filter_rows_since -- nothing left to
        # reconstruct or score, so no report is written.
        assert not (out_dir / "emos_shadow_vs_market_2026-08-11.md").exists()


class TestDoesNotAffectBssMarketVsModelReport:
    """Regression guard (issue #1041 non-negotiable constraint): this module
    imports from bss_market_vs_model_report but must never change its
    behaviour. bss_market_vs_model_report.py itself is untouched by this
    issue's diff, so this is a belt-and-braces behavioural check, not a
    substitute for that fact."""

    def test_default_pass1_output_is_unaffected_by_this_module_being_imported(self, tmp_path):
        import src.scripts.emos_shadow_vs_market_report  # noqa: F401 -- forces import
        from src.scripts.bss_market_vs_model_report import run_report as bss_run_report

        rc = bss_run_report(
            candidates_csv=tmp_path / "nope.csv",
            db_path=tmp_path / "data" / "meteoedge.db",
            out_dir=tmp_path / "backtest_results",
        )
        # Same self-gating behaviour as before this issue's diff: no local
        # candidates data -> no report, rc == 0.
        assert rc == 0
        assert not (tmp_path / "backtest_results").exists()

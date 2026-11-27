"""Unit tests for src/scripts/backfill_trade_market_date.py (issue #1543).

Covers:
- scan_decisions-joined fill is preferred and costs no HTTP call.
- Gamma fallback batch lookup fills rows with no matching scan_decisions row,
  parsing the settlement date from the market's `question` text.
- Repeated condition_ids= query keys, never comma-joined (same regression
  guard as fetch_market_resolutions_batch).
- --dry-run reports counts without writing anything to the DB.
- Idempotent: rerunning after a successful pass has nothing left to fill.
- Rows that can't be resolved by either source are counted "unresolved" and
  left NULL -- never guessed.
"""
from unittest.mock import MagicMock, patch

from src.data.db import Database
from src.scripts import backfill_trade_market_date as backfill


def _fresh_db() -> Database:
    return Database(":memory:")


def _insert_shadow_trade(db, ticker, station="KORD", bracket_low=81.0, bracket_high=83.0):
    return db.upsert_shadow_trade(
        ts="2026-11-16T22:00:00Z", station=station, ticker=ticker,
        bracket_low=bracket_low, bracket_high=bracket_high, side="NO",
        predicted_price=5, actual_price=6, predicted_edge=15.0,
    )


def _mock_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else []
    resp.raise_for_status.return_value = None
    return resp


class TestFillsFromScanDecisions:
    def test_matching_scan_decision_date_is_used_no_http_call(self):
        db = _fresh_db()
        _insert_shadow_trade(db, "SD-1")
        db.upsert_scan_decision(
            ts="2026-11-16T22:00:00Z", station="KORD", ticker="SD-1",
            date="2026-11-17", bracket_low=81.0, bracket_high=83.0,
            gate_verdict="shadow_only", poll_ts="2026-11-16T22:00:00Z",
        )

        with patch("src.scripts.backfill_trade_market_date.fetch") as mock_fetch:
            summary = backfill.run_backfill(db, dry_run=False)

        assert mock_fetch.call_count == 0
        assert summary == {
            "total": 1, "from_scan_decisions": 1, "from_gamma": 0, "unresolved": 0,
        }
        row = db._conn.execute(
            "SELECT market_date FROM trades WHERE ticker='SD-1'"
        ).fetchone()
        assert row[0] == "2026-11-17"


class TestDryRun:
    def test_dry_run_reports_without_writing(self):
        db = _fresh_db()
        _insert_shadow_trade(db, "DRY-1")
        db.upsert_scan_decision(
            ts="2026-11-16T22:00:00Z", station="KORD", ticker="DRY-1",
            date="2026-11-17", bracket_low=81.0, bracket_high=83.0,
            gate_verdict="shadow_only", poll_ts="2026-11-16T22:00:00Z",
        )

        summary = backfill.run_backfill(db, dry_run=True)

        assert summary["from_scan_decisions"] == 1
        row = db._conn.execute(
            "SELECT market_date FROM trades WHERE ticker='DRY-1'"
        ).fetchone()
        assert row[0] is None  # nothing written


class TestGammaFallback:
    def test_falls_back_to_gamma_question_text_when_no_scan_decision(self):
        db = _fresh_db()
        _insert_shadow_trade(db, "0xgamma1")
        market = {
            "conditionId": "0xgamma1",
            "question": "Will the highest temperature in Chicago be 81-83°F on November 17?",
            "endDate": "2026-11-17T04:00:00Z",
        }
        mock_resp = _mock_response(200, [market])
        with patch("src.scripts.backfill_trade_market_date.fetch", return_value=mock_resp) as mock_fetch:
            summary = backfill.run_backfill(db, dry_run=False)

        assert mock_fetch.call_count == 1
        assert summary == {
            "total": 1, "from_scan_decisions": 0, "from_gamma": 1, "unresolved": 0,
        }
        row = db._conn.execute(
            "SELECT market_date FROM trades WHERE ticker='0xgamma1'"
        ).fetchone()
        assert row[0] == "2026-11-17"

    def test_uses_repeated_condition_ids_keys_not_comma_joined(self):
        db = _fresh_db()
        _insert_shadow_trade(db, "0xgamma2", bracket_low=1.0, bracket_high=2.0)
        _insert_shadow_trade(db, "0xgamma3", bracket_low=3.0, bracket_high=4.0)
        markets = [
            {"conditionId": "0xgamma2", "question": "... on November 17?",
             "endDate": "2026-11-17T04:00:00Z"},
            {"conditionId": "0xgamma3", "question": "... on November 17?",
             "endDate": "2026-11-17T04:00:00Z"},
        ]
        mock_resp = _mock_response(200, markets)
        with patch("src.scripts.backfill_trade_market_date.fetch", return_value=mock_resp) as mock_fetch:
            backfill.run_backfill(db, dry_run=False)

        called_url = mock_fetch.call_args[0][0]
        assert called_url.count("condition_ids=") == 2
        assert "0xgamma2,0xgamma3" not in called_url

    def test_year_wraps_around_when_question_month_precedes_enddate_month(self):
        """Question says 'December 31' but endDate has already rolled into
        January UTC -- year must be taken as the PRIOR year."""
        db = _fresh_db()
        _insert_shadow_trade(db, "0xyearwrap")
        market = {
            "conditionId": "0xyearwrap",
            "question": "... on December 31?",
            "endDate": "2027-01-01T05:00:00Z",
        }
        mock_resp = _mock_response(200, [market])
        with patch("src.scripts.backfill_trade_market_date.fetch", return_value=mock_resp):
            backfill.run_backfill(db, dry_run=False)
        row = db._conn.execute(
            "SELECT market_date FROM trades WHERE ticker='0xyearwrap'"
        ).fetchone()
        assert row[0] == "2026-12-31"

    def test_unmatched_ticker_is_unresolved_not_guessed(self):
        db = _fresh_db()
        _insert_shadow_trade(db, "0xmissing")
        mock_resp = _mock_response(200, [])  # nothing comes back
        with patch("src.scripts.backfill_trade_market_date.fetch", return_value=mock_resp):
            summary = backfill.run_backfill(db, dry_run=False)
        assert summary == {
            "total": 1, "from_scan_decisions": 0, "from_gamma": 0, "unresolved": 1,
        }
        row = db._conn.execute(
            "SELECT market_date FROM trades WHERE ticker='0xmissing'"
        ).fetchone()
        assert row[0] is None

    def test_gamma_request_failure_marks_chunk_unresolved(self):
        db = _fresh_db()
        _insert_shadow_trade(db, "0xfail")
        with patch("src.scripts.backfill_trade_market_date.fetch",
                   side_effect=ConnectionError("timeout")):
            summary = backfill.run_backfill(db, dry_run=False)
        assert summary["from_gamma"] == 0
        assert summary["unresolved"] == 1


class TestIdempotent:
    def test_rerun_after_success_has_nothing_left(self):
        db = _fresh_db()
        _insert_shadow_trade(db, "IDEMPOTENT-1")
        db.upsert_scan_decision(
            ts="2026-11-16T22:00:00Z", station="KORD", ticker="IDEMPOTENT-1",
            date="2026-11-17", bracket_low=81.0, bracket_high=83.0,
            gate_verdict="shadow_only", poll_ts="2026-11-16T22:00:00Z",
        )

        first = backfill.run_backfill(db, dry_run=False)
        assert first["total"] == 1

        with patch("src.scripts.backfill_trade_market_date.fetch") as mock_fetch:
            second = backfill.run_backfill(db, dry_run=False)
        assert second == {
            "total": 0, "from_scan_decisions": 0, "from_gamma": 0, "unresolved": 0,
        }
        assert mock_fetch.call_count == 0


class TestNoRowsToFill:
    def test_no_null_rows_is_a_clean_noop(self):
        db = _fresh_db()
        summary = backfill.run_backfill(db, dry_run=False)
        assert summary == {
            "total": 0, "from_scan_decisions": 0, "from_gamma": 0, "unresolved": 0,
        }

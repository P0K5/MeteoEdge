"""Unit tests for src/scripts/quarantine_mislabeled_shadow_trades.py (issue #610).

Covers:
- Matching rows are quarantined: direction set to 'low', settled_at set,
  pnl set to NULL, close_reason set to the quarantine marker.
- Running twice is a no-op the second time (idempotent).
- A row whose station/side/bracket does not match the expected values is
  left untouched and the script reports failure.
- Missing row ids are reported but do not abort processing of the rest.
- --dry-run makes no DB changes.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.scripts.quarantine_mislabeled_shadow_trades import (
    _CLOSE_REASON,
    _ROWS_TO_QUARANTINE,
    quarantine,
)


def _make_db(tmp_path: Path, rows: list[dict]) -> Path:
    """Create a minimal trades table with the given rows (dicts with an
    explicit 'id')."""
    db_path = tmp_path / "meteoedge.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE trades (
            id              INTEGER PRIMARY KEY,
            ts              TEXT NOT NULL,
            station         TEXT NOT NULL,
            ticker          TEXT NOT NULL,
            bracket_low     REAL NOT NULL,
            bracket_high    REAL NOT NULL,
            side            TEXT NOT NULL,
            predicted_price INTEGER NOT NULL,
            actual_price    INTEGER NOT NULL,
            predicted_edge  REAL NOT NULL,
            mode            TEXT NOT NULL,
            outcome         TEXT,
            pnl             REAL,
            capital_before  REAL NOT NULL,
            capital_after   REAL,
            settled_at      TEXT,
            direction       TEXT NOT NULL DEFAULT 'high',
            close_reason    TEXT
        )
        """
    )
    for r in rows:
        conn.execute(
            "INSERT INTO trades (id, ts, station, ticker, bracket_low, bracket_high, "
            "side, predicted_price, actual_price, predicted_edge, mode, capital_before, "
            "direction) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r["id"], r.get("ts", "2026-07-03T10:00:00+00:00"), r["station"],
                r.get("ticker", "0xTEST"), r["bracket_low"], r["bracket_high"],
                r["side"], r.get("predicted_price", 10), r.get("actual_price", 30),
                r.get("predicted_edge", 5.0), r.get("mode", "shadow"),
                r.get("capital_before", 0.0), r.get("direction", "high"),
            ),
        )
    conn.commit()
    conn.close()
    return db_path


def _all_rows_matching_expected(tmp_path: Path) -> Path:
    """Build a DB with all 5 rows from _ROWS_TO_QUARANTINE present and matching."""
    rows = [
        {"id": tid, "station": station, "side": side, "bracket_low": low, "bracket_high": high}
        for tid, station, side, low, high in _ROWS_TO_QUARANTINE
    ]
    return _make_db(tmp_path, rows)


def _read_row(db_path: Path, trade_id: int) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    conn.close()
    return dict(row)


class TestQuarantineHappyPath:
    def test_all_matching_rows_quarantined(self, tmp_path):
        db_path = _all_rows_matching_expected(tmp_path)

        exit_code = quarantine(db_path, dry_run=False)

        assert exit_code == 0
        for tid, *_ in _ROWS_TO_QUARANTINE:
            row = _read_row(db_path, tid)
            assert row["direction"] == "low"
            assert row["settled_at"] is not None
            assert row["pnl"] is None
            assert row["close_reason"] == _CLOSE_REASON

    def test_idempotent_second_run_is_no_op(self, tmp_path):
        db_path = _all_rows_matching_expected(tmp_path)

        first_exit = quarantine(db_path, dry_run=False)
        row_after_first = _read_row(db_path, _ROWS_TO_QUARANTINE[0][0])
        settled_at_first = row_after_first["settled_at"]

        second_exit = quarantine(db_path, dry_run=False)
        row_after_second = _read_row(db_path, _ROWS_TO_QUARANTINE[0][0])

        assert first_exit == 0
        assert second_exit == 0
        # settled_at must not be re-stamped with a new timestamp on re-run
        assert row_after_second["settled_at"] == settled_at_first
        assert row_after_second["direction"] == "low"
        assert row_after_second["close_reason"] == _CLOSE_REASON


class TestQuarantineSafety:
    def test_mismatched_row_is_not_touched_and_reports_failure(self, tmp_path):
        rows = [
            {"id": tid, "station": station, "side": side, "bracket_low": low, "bracket_high": high}
            for tid, station, side, low, high in _ROWS_TO_QUARANTINE
        ]
        # Corrupt one row so it no longer matches the expected station.
        rows[0] = {**rows[0], "station": "WRONG"}
        db_path = _make_db(tmp_path, rows)

        exit_code = quarantine(db_path, dry_run=False)

        assert exit_code == 1
        mismatched_id = _ROWS_TO_QUARANTINE[0][0]
        row = _read_row(db_path, mismatched_id)
        assert row["direction"] == "high"  # untouched
        assert row["close_reason"] is None  # untouched

        # The other 4 rows should still be processed correctly.
        for tid, *_ in _ROWS_TO_QUARANTINE[1:]:
            row = _read_row(db_path, tid)
            assert row["direction"] == "low"
            assert row["close_reason"] == _CLOSE_REASON

    def test_missing_row_does_not_abort_processing(self, tmp_path):
        rows = [
            {"id": tid, "station": station, "side": side, "bracket_low": low, "bracket_high": high}
            for tid, station, side, low, high in _ROWS_TO_QUARANTINE[1:]  # omit the first
        ]
        db_path = _make_db(tmp_path, rows)

        exit_code = quarantine(db_path, dry_run=False)

        assert exit_code == 0
        for tid, *_ in _ROWS_TO_QUARANTINE[1:]:
            row = _read_row(db_path, tid)
            assert row["direction"] == "low"


class TestQuarantineDryRun:
    def test_dry_run_makes_no_changes(self, tmp_path):
        db_path = _all_rows_matching_expected(tmp_path)

        exit_code = quarantine(db_path, dry_run=True)

        assert exit_code == 0
        for tid, *_ in _ROWS_TO_QUARANTINE:
            row = _read_row(db_path, tid)
            assert row["direction"] == "high"
            assert row["settled_at"] is None
            assert row["close_reason"] is None

    def test_missing_db_returns_nonzero(self, tmp_path):
        missing_path = tmp_path / "does_not_exist.db"
        assert quarantine(missing_path, dry_run=False) == 1

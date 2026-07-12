"""Tests for src/scripts/check_shadow_health.py's calibration check
(issue #704, Gap 1).

check_calibration()'s "4a. Shadow trade ensemble calibration" query reads
directly from meteoedge.db::trades via a raw sqlite3.Connection (no
Database()/is_next_day-aware wrapper). Once NEXT_DAY_EVALUATION lands,
next-day shadow rows (different sigma/lead-time regime, #687) would
otherwise silently mix into the same-day calibration buckets -- this test
proves the `AND is_next_day = 0` filter keeps them out.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from src.scripts.check_shadow_health import check_calibration, _CAL_MIN_SAMPLES


def _make_trades_conn(rows: list[dict]) -> sqlite3.Connection:
    """In-memory trades table mirroring the real schema's relevant columns."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE trades (ts TEXT, mode TEXT, predicted_price INTEGER, "
        "pnl REAL, is_next_day INTEGER NOT NULL DEFAULT 0)"
    )
    for r in rows:
        conn.execute(
            "INSERT INTO trades (ts, mode, predicted_price, pnl, is_next_day) "
            "VALUES (?,?,?,?,?)",
            (
                r.get("ts"), r.get("mode", "shadow"), r.get("predicted_price"),
                r.get("pnl"), r.get("is_next_day", 0),
            ),
        )
    conn.commit()
    return conn


def _recent_ts() -> str:
    return (date.today() - timedelta(days=1)).isoformat() + "T10:00:00"


class TestCheckCalibrationExcludesNextDay:
    def test_next_day_rows_excluded_from_bucket_count(self):
        """5 same-day wins + 5 next-day wins at the same predicted_price
        bucket -- the reported COUNT must be 5 (same-day only), not 10."""
        ts = _recent_ts()
        rows = (
            [
                {"ts": ts, "predicted_price": 75, "pnl": 1.0, "is_next_day": 0}
                for _ in range(_CAL_MIN_SAMPLES)
            ]
            + [
                {"ts": ts, "predicted_price": 75, "pnl": -1.0, "is_next_day": 1}
                for _ in range(_CAL_MIN_SAMPLES)
            ]
        )
        conn = _make_trades_conn(rows)

        lines, ok = check_calibration(conn, cal_days=7)

        assert ok is True
        joined = "\n".join(lines)
        assert "5 settled" in joined, joined
        # 100% win rate: if the 5 losing next-day rows had leaked in, the
        # bucket's win rate would be 50%, not 100%.
        assert "100.0%" in joined, joined

    def test_only_next_day_rows_yields_no_calibration(self):
        """A DB with ONLY next-day settled shadow rows must report "no
        settled shadow trades" -- the same-day filter must not silently
        fall back to counting them."""
        ts = _recent_ts()
        rows = [
            {"ts": ts, "predicted_price": 75, "pnl": 1.0, "is_next_day": 1}
            for _ in range(_CAL_MIN_SAMPLES)
        ]
        conn = _make_trades_conn(rows)

        lines, ok = check_calibration(conn, cal_days=7)

        assert ok is True
        assert any("No settled shadow trades yet" in line for line in lines)

    def test_same_day_rows_still_counted_without_next_day_rows(self):
        """Regression guard: the is_next_day filter must not accidentally
        exclude legitimate same-day rows (default 0)."""
        ts = _recent_ts()
        rows = [
            {"ts": ts, "predicted_price": 75, "pnl": 1.0, "is_next_day": 0}
            for _ in range(_CAL_MIN_SAMPLES)
        ]
        conn = _make_trades_conn(rows)

        lines, ok = check_calibration(conn, cal_days=7)

        assert ok is True
        joined = "\n".join(lines)
        assert "5 settled" in joined, joined
        assert "100.0%" in joined, joined

"""Unit tests for shadow-trade deduplication (Issue #376).

Covers:
- Insert shadow candidate A on day D → row created.
- Insert candidate A again on day D → no new row; actual_price updated.
- Insert candidate A on day D+1 → new row created.
- Migration script on fixture with 5 duplicate rows → 1 row remains, earliest ts preserved.
- Migration script refuses to run if backup already exists.
- Partial-index violation raises sqlite3.IntegrityError.
- No change to live-mode row counts after migration (regression test).
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.data.db import Database


def _fresh_db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "test.db"))


def _insert_shadow(
    db: Database,
    *,
    ts: str,
    station: str = "KORD",
    ticker: str = "KORD-test-76-78",
    bracket_low: float = 76.0,
    bracket_high: float = 78.0,
    side: str = "YES",
    predicted_price: int = 30,
    actual_price: int = 32,
    predicted_edge: float = 8.0,
) -> tuple[int, bool]:
    return db.upsert_shadow_trade(
        ts=ts,
        station=station,
        ticker=ticker,
        bracket_low=bracket_low,
        bracket_high=bracket_high,
        side=side,
        predicted_price=predicted_price,
        actual_price=actual_price,
        predicted_edge=predicted_edge,
        capital_before=0.0,
    )


class TestUpsertShadowTrade:
    """Core dedup behaviour for upsert_shadow_trade()."""

    def test_first_insert_creates_row(self, tmp_path):
        db = _fresh_db(tmp_path)
        row_id, created = _insert_shadow(db, ts="2026-06-20T10:00:00+00:00")
        assert created is True
        assert row_id > 0
        rows = db.get_trades(limit=None, mode="shadow")
        assert len(rows) == 1

    def test_second_insert_same_day_updates_actual_price(self, tmp_path):
        db = _fresh_db(tmp_path)
        row_id1, created1 = _insert_shadow(db, ts="2026-06-20T10:00:00+00:00", actual_price=30)
        assert created1 is True

        row_id2, created2 = _insert_shadow(db, ts="2026-06-20T10:05:00+00:00", actual_price=35)
        assert created2 is False
        assert row_id2 == row_id1  # same row

        # Verify actual_price was updated
        rows = db.get_trades(limit=None, mode="shadow")
        assert len(rows) == 1
        assert rows[0]["actual_price"] == 35

    def test_insert_different_day_creates_new_row(self, tmp_path):
        db = _fresh_db(tmp_path)
        row_id1, created1 = _insert_shadow(db, ts="2026-06-20T10:00:00+00:00")
        assert created1 is True

        row_id2, created2 = _insert_shadow(db, ts="2026-06-21T10:00:00+00:00")
        assert created2 is True
        assert row_id2 != row_id1

        rows = db.get_trades(limit=None, mode="shadow")
        assert len(rows) == 2

    def test_different_station_creates_separate_rows(self, tmp_path):
        db = _fresh_db(tmp_path)
        _insert_shadow(db, ts="2026-06-20T10:00:00+00:00", station="KORD")
        _insert_shadow(db, ts="2026-06-20T10:00:00+00:00", station="KJFK")
        rows = db.get_trades(limit=None, mode="shadow")
        assert len(rows) == 2

    def test_different_side_creates_separate_rows(self, tmp_path):
        db = _fresh_db(tmp_path)
        _insert_shadow(db, ts="2026-06-20T10:00:00+00:00", side="YES")
        _insert_shadow(db, ts="2026-06-20T10:00:00+00:00", side="NO")
        rows = db.get_trades(limit=None, mode="shadow")
        assert len(rows) == 2

    def test_dedup_does_not_touch_live_rows(self, tmp_path):
        db = _fresh_db(tmp_path)
        # Insert a live row
        db.insert_trade(
            ts="2026-06-20T10:00:00+00:00",
            station="KORD",
            ticker="KORD-test-76-78",
            bracket_low=76.0,
            bracket_high=78.0,
            side="YES",
            predicted_price=30,
            actual_price=32,
            predicted_edge=8.0,
            mode="live",
            capital_before=100.0,
        )
        # Insert shadow with same dedup key
        _insert_shadow(db, ts="2026-06-20T10:00:00+00:00")
        _insert_shadow(db, ts="2026-06-20T10:05:00+00:00")

        live_rows = db.get_trades(limit=None, mode="live")
        shadow_rows = db.get_trades(limit=None, mode="shadow")
        assert len(live_rows) == 1, "live row count must not change"
        assert len(shadow_rows) == 1, "only one shadow row should exist (deduped)"


class TestPartialIndexViolation:
    """The UNIQUE index should prevent duplicate shadow rows at the DB level."""

    def test_direct_insert_duplicate_same_direction_raises_integrity_error(self, tmp_path):
        db = _fresh_db(tmp_path)
        _insert_shadow(db, ts="2026-06-20T10:00:00+00:00")

        # Bypass upsert_shadow_trade and insert directly with same direction
        # (both default to 'high') to trigger the index violation
        with pytest.raises(sqlite3.IntegrityError):
            db._conn.execute(
                "INSERT INTO trades"
                "(ts,station,ticker,bracket_low,bracket_high,side,"
                "predicted_price,actual_price,slippage,predicted_edge,mode,"
                "order_id,outcome,pnl,capital_before,capital_after,settled_at,direction) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "2026-06-20T11:00:00+00:00", "KORD", "KORD-test-76-78",
                    76.0, 78.0, "YES", 30, 33, None, 8.0, "shadow",
                    None, None, None, 0.0, None, None, "high",
                ),
            )
            db._conn.commit()

    def test_different_direction_rows_both_persist(self, tmp_path):
        """Rows with same (station, bracket, side, day) but different direction both persist."""
        db = _fresh_db(tmp_path)

        # Insert first row with direction='high'
        db._conn.execute(
            "INSERT INTO trades"
            "(ts,station,ticker,bracket_low,bracket_high,side,"
            "predicted_price,actual_price,slippage,predicted_edge,mode,"
            "order_id,outcome,pnl,capital_before,capital_after,settled_at,direction) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "2026-06-20T10:00:00+00:00", "KORD", "KORD-test-76-78",
                76.0, 78.0, "YES", 30, 32, None, 8.0, "shadow",
                None, None, None, 0.0, None, None, "high",
            ),
        )
        db._conn.commit()

        # Insert second row with same (station, bracket, side, day) but direction='low'
        # Should NOT raise IntegrityError (index includes direction)
        db._conn.execute(
            "INSERT INTO trades"
            "(ts,station,ticker,bracket_low,bracket_high,side,"
            "predicted_price,actual_price,slippage,predicted_edge,mode,"
            "order_id,outcome,pnl,capital_before,capital_after,settled_at,direction) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "2026-06-20T11:00:00+00:00", "KORD", "KORD-test-76-78",
                76.0, 78.0, "YES", 30, 33, None, 8.0, "shadow",
                None, None, None, 0.0, None, None, "low",
            ),
        )
        db._conn.commit()

        # Verify both rows persist
        rows = db.get_trades(limit=None, mode="shadow")
        assert len(rows) == 2
        directions = {r["direction"] for r in rows}
        assert directions == {"high", "low"}


class TestMigrationScript:
    """dedupe_shadow_trades.py migration logic."""

    def _make_db_with_duplicates(self, tmp_path: Path) -> Path:
        """Create a test DB with 5 shadow rows for the same group and 1 live row.

        Simulates a pre-dedup database by dropping the partial UNIQUE index before
        bulk-inserting duplicates, then restoring it (as the migration script would
        encounter it on a production DB that pre-dates the index).
        """
        db_path = tmp_path / "meteoedge.db"
        # Open raw connection to bypass the UNIQUE index for fixture setup
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=OFF")

        # Create minimal trades table without the partial unique index
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              TEXT NOT NULL,
                station         TEXT NOT NULL,
                ticker          TEXT NOT NULL,
                bracket_low     REAL NOT NULL,
                bracket_high    REAL NOT NULL,
                side            TEXT NOT NULL,
                predicted_price INTEGER NOT NULL,
                actual_price    INTEGER NOT NULL,
                slippage        INTEGER,
                predicted_edge  REAL NOT NULL,
                mode            TEXT NOT NULL,
                order_id        TEXT,
                outcome         TEXT,
                pnl             REAL,
                capital_before  REAL NOT NULL,
                capital_after   REAL,
                settled_at      TEXT,
                actual_fee_cents REAL,
                size_eur        REAL
            )
            """
        )

        # Insert 5 shadow rows for same (station, bracket_low, bracket_high, side, day)
        for i in range(5):
            conn.execute(
                "INSERT INTO trades"
                "(ts,station,ticker,bracket_low,bracket_high,side,"
                "predicted_price,actual_price,slippage,predicted_edge,mode,"
                "order_id,outcome,pnl,capital_before,capital_after,settled_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"2026-06-20T{10 + i:02d}:00:00+00:00",
                    "KORD", "KORD-dup-76-78",
                    76.0, 78.0, "YES", 30, 32 + i, None, 8.0, "shadow",
                    None, None, None, 0.0, None, None,
                ),
            )
        # Insert one live row (must not be touched)
        conn.execute(
            "INSERT INTO trades"
            "(ts,station,ticker,bracket_low,bracket_high,side,"
            "predicted_price,actual_price,slippage,predicted_edge,mode,"
            "order_id,outcome,pnl,capital_before,capital_after,settled_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "2026-06-20T10:00:00+00:00",
                "KORD", "KORD-dup-76-78",
                76.0, 78.0, "YES", 30, 32, None, 8.0, "live",
                None, None, None, 100.0, None, None,
            ),
        )
        conn.commit()
        conn.close()
        return db_path

    def _count_rows(self, db_path: Path, mode: str) -> int:
        """Count trades rows by mode using raw sqlite3 (avoids running _migrate())."""
        conn = sqlite3.connect(str(db_path))
        cur = conn.execute("SELECT COUNT(*) FROM trades WHERE mode=?", (mode,))
        n = cur.fetchone()[0]
        conn.close()
        return n

    def _get_shadow_ts_list(self, db_path: Path) -> list[str]:
        conn = sqlite3.connect(str(db_path))
        cur = conn.execute(
            "SELECT ts FROM trades WHERE mode='shadow' ORDER BY ts ASC"
        )
        rows = [r[0] for r in cur.fetchall()]
        conn.close()
        return rows

    def test_dry_run_makes_no_changes(self, tmp_path, monkeypatch):
        db_path = self._make_db_with_duplicates(tmp_path)

        import src.scripts.dedupe_shadow_trades as m
        monkeypatch.setattr(m, "_DB_PATH", db_path)
        bak_path = tmp_path / "meteoedge.db.pre_366J.bak"
        monkeypatch.setattr(m, "_BAK_PATH", bak_path)

        m.dedupe(dry_run=True)

        # No backup created
        assert not bak_path.exists()

        # Row count unchanged
        assert self._count_rows(db_path, "shadow") == 5
        assert self._count_rows(db_path, "live") == 1

    def test_live_run_dedupes_to_one_row_with_earliest_ts(self, tmp_path, monkeypatch):
        db_path = self._make_db_with_duplicates(tmp_path)

        import src.scripts.dedupe_shadow_trades as m
        monkeypatch.setattr(m, "_DB_PATH", db_path)
        bak_path = tmp_path / "meteoedge.db.pre_366J.bak"
        monkeypatch.setattr(m, "_BAK_PATH", bak_path)

        m.dedupe(dry_run=False)

        # Backup was created
        assert bak_path.exists()

        # Only 1 shadow row remains; live rows intact
        assert self._count_rows(db_path, "shadow") == 1, "Expected 1 shadow row after dedup"
        assert self._count_rows(db_path, "live") == 1, "live row count must not change"

        # Earliest ts preserved
        ts_list = self._get_shadow_ts_list(db_path)
        assert len(ts_list) == 1
        assert ts_list[0].startswith("2026-06-20T10:")

    def test_refuses_to_run_if_backup_exists(self, tmp_path, monkeypatch):
        db_path = self._make_db_with_duplicates(tmp_path)
        bak_path = tmp_path / "meteoedge.db.pre_366J.bak"
        bak_path.write_text("dummy backup")

        import src.scripts.dedupe_shadow_trades as m
        monkeypatch.setattr(m, "_DB_PATH", db_path)
        monkeypatch.setattr(m, "_BAK_PATH", bak_path)

        with pytest.raises(SystemExit):
            m.dedupe(dry_run=False)

        # Shadow rows should be unchanged (script aborted)
        assert self._count_rows(db_path, "shadow") == 5

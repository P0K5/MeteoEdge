"""Unit tests for shadow-mode schema migration in src/data/db.py.

Covers:
- _migrate() upgrades a DB with old CHECK(mode IN ('paper','live')) to the new
  CHECK(mode IN ('paper','live','shadow')) without data loss.
- Migration is idempotent (running twice causes no error and no duplicate data).
- Fresh DB (created via _DDL) accepts mode='shadow' inserts.
- update_trade_by_id updates the correct row and returns the rowcount.
- get_unsettled_shadow_trades returns only the right rows.
"""
import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.data.db import Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_db(tmp_path: Path) -> Database:
    """Return a fresh Database backed by a temp file."""
    return Database(path=str(tmp_path / "test.db"))


def _old_db(tmp_path: Path) -> Database:
    """Return a Database that starts with the old CHECK constraint (no 'shadow').

    We create it normally (which already has the new DDL), then manually
    rebuild the trades table with the old CHECK to simulate a pre-migration DB,
    and re-open it so _migrate() runs against that state.
    """
    db_path = tmp_path / "old.db"
    # Step 1: create normally so all other tables exist
    db = Database(path=str(db_path))
    db.close()

    # Step 2: rebuild trades with old constraint
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DROP TABLE IF EXISTS trades")
    conn.execute("""
        CREATE TABLE trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT NOT NULL,
            station         TEXT NOT NULL,
            ticker          TEXT NOT NULL,
            bracket_low     REAL NOT NULL,
            bracket_high    REAL NOT NULL,
            side            TEXT NOT NULL CHECK(side IN ('YES','NO')),
            predicted_price INTEGER NOT NULL,
            actual_price    INTEGER NOT NULL,
            slippage        INTEGER,
            predicted_edge  REAL NOT NULL,
            mode            TEXT NOT NULL CHECK(mode IN ('paper','live')),
            order_id        TEXT,
            outcome         TEXT,
            pnl             REAL,
            capital_before  REAL NOT NULL,
            capital_after   REAL,
            settled_at      TEXT,
            actual_fee_cents REAL
        )
    """)
    # Insert a live trade row with the old schema
    conn.execute(
        "INSERT INTO trades "
        "(ts,station,ticker,bracket_low,bracket_high,side,"
        "predicted_price,actual_price,predicted_edge,mode,capital_before) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("2025-01-01T00:00:00+00:00", "KORD", "0xABC",
         75.0, 77.0, "NO", 10, 12, 5.0, "live", 100.0),
    )
    conn.commit()
    conn.close()

    # Step 3: re-open so _migrate() runs
    return Database(path=str(db_path))


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------

class TestShadowMigration:
    def test_migration_upgrades_old_db_to_accept_shadow(self, tmp_path):
        """After migration, an old DB accepts mode='shadow' inserts."""
        db = _old_db(tmp_path)
        # Should not raise
        trade_id = db.insert_trade(
            ts="2025-01-02T00:00:00+00:00",
            station="KORD",
            ticker="0xDEF",
            bracket_low=76.0,
            bracket_high=78.0,
            side="YES",
            predicted_price=30,
            actual_price=32,
            predicted_edge=8.0,
            mode="shadow",
            capital_before=0.0,
        )
        assert trade_id > 0
        db.close()

    def test_migration_preserves_existing_data(self, tmp_path):
        """Existing live rows survive the migration without data loss."""
        db = _old_db(tmp_path)
        trades = db.get_trades(limit=None)
        live_rows = [t for t in trades if t["mode"] == "live"]
        assert len(live_rows) == 1
        assert live_rows[0]["ticker"] == "0xABC"
        db.close()

    def test_migration_is_idempotent(self, tmp_path):
        """Running _migrate() twice on a new-schema DB causes no error and no duplicate data."""
        db = _old_db(tmp_path)
        db._migrate()  # second run
        trades = db.get_trades(limit=None)
        assert len(trades) == 1  # no duplicates
        db.close()

    def test_fresh_db_accepts_shadow_mode(self, tmp_path):
        """A DB created from scratch (via _DDL) already accepts mode='shadow'."""
        db = _fresh_db(tmp_path)
        trade_id = db.insert_trade(
            ts="2025-06-01T10:00:00+00:00",
            station="KMIA",
            ticker="0xSHADOW",
            bracket_low=88.0,
            bracket_high=90.0,
            side="YES",
            predicted_price=25,
            actual_price=28,
            predicted_edge=10.0,
            mode="shadow",
            capital_before=0.0,
        )
        assert trade_id > 0
        db.close()

    def test_fresh_db_still_rejects_invalid_mode(self, tmp_path):
        """The CHECK constraint rejects unknown mode values."""
        db = _fresh_db(tmp_path)
        with pytest.raises(Exception):
            db.insert_trade(
                ts="2025-06-01T10:00:00+00:00",
                station="KMIA",
                ticker="0xBAD",
                bracket_low=88.0,
                bracket_high=90.0,
                side="YES",
                predicted_price=25,
                actual_price=28,
                predicted_edge=10.0,
                mode="invalid_mode",
                capital_before=0.0,
            )
        db.close()


# ---------------------------------------------------------------------------
# update_trade_by_id tests
# ---------------------------------------------------------------------------

class TestUpdateTradeById:
    def _insert_shadow(self, db: Database) -> int:
        return db.insert_trade(
            ts="2025-06-01T10:00:00+00:00",
            station="KORD",
            ticker="0xSHADOW1",
            bracket_low=80.0,
            bracket_high=82.0,
            side="YES",
            predicted_price=30,
            actual_price=32,
            predicted_edge=9.0,
            mode="shadow",
            capital_before=0.0,
        )

    def test_updates_pnl_and_settled_at(self, tmp_path):
        db = _fresh_db(tmp_path)
        tid = self._insert_shadow(db)
        rows_updated = db.update_trade_by_id(
            tid,
            outcome="filled",
            pnl=0.68,
            capital_after=0.68,
            settled_at="2025-06-02T09:00:00+00:00",
        )
        assert rows_updated == 1
        trades = db.get_trades(limit=None)
        t = next(r for r in trades if r["id"] == tid)
        assert t["outcome"] == "filled"
        assert abs(t["pnl"] - 0.68) < 1e-6
        assert t["settled_at"] == "2025-06-02T09:00:00+00:00"
        db.close()

    def test_returns_zero_for_nonexistent_id(self, tmp_path):
        db = _fresh_db(tmp_path)
        rows_updated = db.update_trade_by_id(99999, outcome="filled", pnl=1.0)
        assert rows_updated == 0
        db.close()

    def test_no_op_when_all_none(self, tmp_path):
        db = _fresh_db(tmp_path)
        tid = self._insert_shadow(db)
        rows_updated = db.update_trade_by_id(tid)
        assert rows_updated == 0
        db.close()


# ---------------------------------------------------------------------------
# get_unsettled_shadow_trades tests
# ---------------------------------------------------------------------------

class TestGetUnsettledShadowTrades:
    def test_returns_shadow_rows_for_date(self, tmp_path):
        db = _fresh_db(tmp_path)
        db.insert_trade(
            ts="2025-06-01T10:00:00+00:00",
            station="KORD",
            ticker="0xSH1",
            bracket_low=80.0,
            bracket_high=82.0,
            side="YES",
            predicted_price=30,
            actual_price=32,
            predicted_edge=9.0,
            mode="shadow",
            capital_before=0.0,
        )
        rows = db.get_unsettled_shadow_trades("2025-06-01")
        assert len(rows) == 1
        assert rows[0]["mode"] == "shadow"
        db.close()

    def test_excludes_already_settled_rows(self, tmp_path):
        db = _fresh_db(tmp_path)
        tid = db.insert_trade(
            ts="2025-06-01T10:00:00+00:00",
            station="KORD",
            ticker="0xSH2",
            bracket_low=80.0,
            bracket_high=82.0,
            side="YES",
            predicted_price=30,
            actual_price=32,
            predicted_edge=9.0,
            mode="shadow",
            capital_before=0.0,
        )
        db.update_trade_by_id(tid, settled_at="2025-06-02T09:00:00+00:00")
        rows = db.get_unsettled_shadow_trades("2025-06-01")
        assert len(rows) == 0
        db.close()

    def test_excludes_live_and_paper_rows(self, tmp_path):
        db = _fresh_db(tmp_path)
        for mode in ("live", "paper"):
            db.insert_trade(
                ts="2025-06-01T10:00:00+00:00",
                station="KORD",
                ticker=f"0x{mode}",
                bracket_low=80.0,
                bracket_high=82.0,
                side="NO",
                predicted_price=30,
                actual_price=32,
                predicted_edge=9.0,
                mode=mode,
                capital_before=10.0,
            )
        rows = db.get_unsettled_shadow_trades("2025-06-01")
        assert len(rows) == 0
        db.close()

    def test_excludes_different_date(self, tmp_path):
        db = _fresh_db(tmp_path)
        db.insert_trade(
            ts="2025-06-02T10:00:00+00:00",
            station="KORD",
            ticker="0xSH3",
            bracket_low=80.0,
            bracket_high=82.0,
            side="YES",
            predicted_price=30,
            actual_price=32,
            predicted_edge=9.0,
            mode="shadow",
            capital_before=0.0,
        )
        rows = db.get_unsettled_shadow_trades("2025-06-01")
        assert len(rows) == 0
        db.close()

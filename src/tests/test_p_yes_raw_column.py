"""Tests for the p_yes_raw column on candidates/trades (issue #551, stage 1).

Covers:
- Fresh DB has p_yes_raw on both candidates and trades
- Migration adds the column idempotently to a pre-existing DB missing it
- insert_candidate / insert_trade / upsert_shadow_trade accept and persist
  p_yes_raw, defaulting to NULL when omitted
- The legacy trades-table rebuild (CHECK-constraint migration, db.py ~L373+)
  preserves p_yes_raw instead of silently dropping it
"""
from src.data.db import Database


def _fresh_db() -> Database:
    return Database(":memory:")


def _col_names(db: Database, table: str) -> set[str]:
    rows = db._conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


class TestPYesRawFreshInstall:
    def test_candidates_has_p_yes_raw(self):
        db = _fresh_db()
        assert "p_yes_raw" in _col_names(db, "candidates")

    def test_trades_has_p_yes_raw(self):
        db = _fresh_db()
        assert "p_yes_raw" in _col_names(db, "trades")


class TestPYesRawMigration:
    def _legacy_db(self) -> Database:
        """Simulate a pre-p_yes_raw DB by dropping the column after init."""
        db = _fresh_db()
        db._conn.execute("PRAGMA foreign_keys=OFF")
        for table in ("candidates", "trades"):
            info = db._conn.execute(f"PRAGMA table_info({table})").fetchall()
            cols_without = [r[1] for r in info if r[1] != "p_yes_raw"]
            cols_def = ", ".join(cols_without)
            db._conn.executescript(f"""
                CREATE TABLE {table}_bak AS SELECT {cols_def} FROM {table};
                DROP TABLE {table};
                ALTER TABLE {table}_bak RENAME TO {table};
            """)
        db._conn.execute("PRAGMA foreign_keys=ON")
        db._conn.commit()
        return db

    def test_migration_adds_p_yes_raw_to_candidates(self):
        db = self._legacy_db()
        assert "p_yes_raw" not in _col_names(db, "candidates")
        db._migrate()
        assert "p_yes_raw" in _col_names(db, "candidates")

    def test_migration_adds_p_yes_raw_to_trades(self):
        db = self._legacy_db()
        assert "p_yes_raw" not in _col_names(db, "trades")
        db._migrate()
        assert "p_yes_raw" in _col_names(db, "trades")

    def test_migration_is_idempotent(self):
        db = _fresh_db()
        db._migrate()
        db._migrate()
        assert "p_yes_raw" in _col_names(db, "trades")
        assert "p_yes_raw" in _col_names(db, "candidates")


class TestPYesRawReadWrite:
    def test_insert_candidate_with_p_yes_raw(self):
        db = _fresh_db()
        db.insert_candidate(
            ts="2026-07-01T10:00:00Z", station="KORD",
            ticker="TEST-1", bracket_low=81.0, bracket_high=83.0,
            side="NO", predicted_price=5, predicted_edge=15.0,
            market_price=79, confidence=0.95, minutes_to_settlement=120.0,
            p_yes_raw=0.0,
        )
        row = db._conn.execute(
            "SELECT p_yes_raw FROM candidates WHERE ticker='TEST-1'"
        ).fetchone()
        assert row[0] == 0.0

    def test_insert_candidate_without_p_yes_raw_is_null(self):
        db = _fresh_db()
        db.insert_candidate(
            ts="2026-07-01T10:00:00Z", station="KORD",
            ticker="TEST-2", bracket_low=81.0, bracket_high=83.0,
            side="NO", predicted_price=5, predicted_edge=15.0,
            market_price=79, confidence=0.95, minutes_to_settlement=120.0,
        )
        row = db._conn.execute(
            "SELECT p_yes_raw FROM candidates WHERE ticker='TEST-2'"
        ).fetchone()
        assert row[0] is None

    def test_insert_trade_with_p_yes_raw(self):
        db = _fresh_db()
        trade_id = db.insert_trade(
            ts="2026-07-01T10:00:00Z", station="KORD", ticker="TEST-3",
            bracket_low=81.0, bracket_high=83.0, side="NO",
            predicted_price=5, actual_price=79, predicted_edge=15.0,
            mode="paper", capital_before=100.0, p_yes_raw=0.0,
        )
        rows = db.get_trades(limit=None)
        row = next(r for r in rows if r["id"] == trade_id)
        assert row["p_yes_raw"] == 0.0

    def test_insert_trade_without_p_yes_raw_is_null(self):
        db = _fresh_db()
        trade_id = db.insert_trade(
            ts="2026-07-01T10:00:00Z", station="KORD", ticker="TEST-4",
            bracket_low=81.0, bracket_high=83.0, side="NO",
            predicted_price=5, actual_price=79, predicted_edge=15.0,
            mode="paper", capital_before=100.0,
        )
        rows = db.get_trades(limit=None)
        row = next(r for r in rows if r["id"] == trade_id)
        assert row["p_yes_raw"] is None

    def test_upsert_shadow_trade_with_p_yes_raw(self):
        db = _fresh_db()
        row_id, created = db.upsert_shadow_trade(
            ts="2026-07-01T10:00:00Z", station="KORD", ticker="TEST-5",
            bracket_low=81.0, bracket_high=83.0, side="NO",
            predicted_price=5, actual_price=79, predicted_edge=15.0,
            p_yes_raw=0.0,
        )
        assert created is True
        rows = db.get_trades(limit=None)
        row = next(r for r in rows if r["id"] == row_id)
        assert row["p_yes_raw"] == 0.0


class TestPYesRawLegacyTradesRebuildPreservesColumn:
    """The trades.mode CHECK-constraint rebuild (db.py) must not drop p_yes_raw.

    Simulates a DB whose trades table predates the 'shadow' mode CHECK
    constraint -- this triggers the DROP/CREATE trades_new rebuild path in
    _migrate(), which previously only copied a fixed column list forward.
    """

    def _pre_shadow_db(self) -> Database:
        db = _fresh_db()
        db._conn.execute("PRAGMA foreign_keys=OFF")
        db._conn.executescript("""
            CREATE TABLE trades_old (
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
                settled_at      TEXT
            );
            INSERT INTO trades_old
                (ts, station, ticker, bracket_low, bracket_high, side,
                 predicted_price, actual_price, predicted_edge, mode, capital_before)
            VALUES
                ('2026-07-01T09:00:00Z', 'KORD', 'LEGACY-1', 81.0, 83.0, 'NO',
                 5, 79, 15.0, 'paper', 100.0);
            DROP TABLE trades;
            ALTER TABLE trades_old RENAME TO trades;
        """)
        db._conn.execute("PRAGMA foreign_keys=ON")
        db._conn.commit()
        return db

    def test_rebuild_preserves_p_yes_raw_column_and_accepts_writes(self):
        db = self._pre_shadow_db()
        assert "p_yes_raw" not in _col_names(db, "trades")
        db._migrate()
        assert "p_yes_raw" in _col_names(db, "trades")

        # New shadow-mode insert with p_yes_raw must succeed post-rebuild.
        trade_id = db.insert_trade(
            ts="2026-07-01T10:00:00Z", station="KORD", ticker="TEST-6",
            bracket_low=81.0, bracket_high=83.0, side="NO",
            predicted_price=5, actual_price=79, predicted_edge=15.0,
            mode="shadow", capital_before=0.0, p_yes_raw=0.0,
        )
        rows = db.get_trades(limit=None)
        row = next(r for r in rows if r["id"] == trade_id)
        assert row["p_yes_raw"] == 0.0

        # Pre-existing legacy row survives the rebuild (p_yes_raw NULL).
        legacy_row = next(r for r in rows if r["ticker"] == "LEGACY-1")
        assert legacy_row["p_yes_raw"] is None

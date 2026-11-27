"""Tests for the candidates.market_date / trades.market_date columns
(issue #1543).

Covers:
- Fresh DB has market_date on both tables, NULL by default
- Migration adds the column idempotently to a pre-existing DB missing it
- insert_candidate() accepts and persists market_date
- upsert_shadow_trade() accepts and persists market_date, and does not
  overwrite it on the dedup-hit UPDATE path (same non-overwrite contract as
  is_next_day/p_yes_raw)

Purely additive: no test here changes is_next_day semantics, gates, or
probabilities.
"""
from src.data.db import Database


def _fresh_db() -> Database:
    return Database(":memory:")


def _col_names(db: Database, table: str) -> set[str]:
    rows = db._conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


class TestMarketDateFreshInstall:
    def test_candidates_has_market_date(self):
        db = _fresh_db()
        assert "market_date" in _col_names(db, "candidates")

    def test_trades_has_market_date(self):
        db = _fresh_db()
        assert "market_date" in _col_names(db, "trades")


class TestMarketDateMigrationCandidates:
    def _legacy_db(self) -> Database:
        db = _fresh_db()
        db._conn.execute("PRAGMA foreign_keys=OFF")
        info = db._conn.execute("PRAGMA table_info(candidates)").fetchall()
        cols_without = [r[1] for r in info if r[1] != "market_date"]
        cols_def = ", ".join(cols_without)
        db._conn.executescript(f"""
            CREATE TABLE candidates_bak AS SELECT {cols_def} FROM candidates;
            DROP TABLE candidates;
            ALTER TABLE candidates_bak RENAME TO candidates;
        """)
        db._conn.execute("PRAGMA foreign_keys=ON")
        db._conn.commit()
        return db

    def test_migration_adds_market_date(self):
        db = self._legacy_db()
        assert "market_date" not in _col_names(db, "candidates")
        db._migrate()
        assert "market_date" in _col_names(db, "candidates")

    def test_migration_is_idempotent(self):
        db = _fresh_db()
        db._migrate()
        db._migrate()
        assert "market_date" in _col_names(db, "candidates")


class TestMarketDateMigrationTrades:
    def _legacy_db(self) -> Database:
        """Simulate a pre-#1543 DB missing trades.market_date.

        Recreates the real DDL (minus market_date) rather than using
        "CREATE TABLE ... AS SELECT" -- that construct drops CHECK
        constraints, which would make the unrelated trades.mode CHECK
        migration misfire (same rationale as test_next_day_column.py's
        TestTradesIsNextDayMigration._legacy_db).
        """
        db = _fresh_db()
        db._conn.execute("PRAGMA foreign_keys=OFF")
        db._conn.executescript(
            """
            DROP TABLE trades;
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
                mode            TEXT NOT NULL CHECK(mode IN ('paper','live','shadow')),
                order_id        TEXT,
                outcome         TEXT,
                pnl             REAL,
                capital_before  REAL NOT NULL,
                capital_after   REAL,
                settled_at      TEXT,
                direction       TEXT NOT NULL DEFAULT 'high',
                is_next_day     INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        db._conn.execute("PRAGMA foreign_keys=ON")
        db._conn.commit()
        return db

    def test_migration_adds_market_date(self):
        db = self._legacy_db()
        assert "market_date" not in _col_names(db, "trades")
        db._migrate()
        assert "market_date" in _col_names(db, "trades")

    def test_migration_is_idempotent(self):
        db = _fresh_db()
        db._migrate()
        db._migrate()
        assert "market_date" in _col_names(db, "trades")


class TestInsertCandidateMarketDate:
    def test_defaults_to_null(self):
        db = _fresh_db()
        db.insert_candidate(
            ts="2026-11-16T22:00:00Z", station="KORD",
            ticker="MD-1", bracket_low=81.0, bracket_high=83.0,
            side="NO", predicted_price=5, predicted_edge=15.0,
            market_price=79, confidence=0.95, minutes_to_settlement=120.0,
        )
        row = db._conn.execute(
            "SELECT market_date FROM candidates WHERE ticker='MD-1'"
        ).fetchone()
        assert row[0] is None

    def test_persists_market_date(self):
        db = _fresh_db()
        db.insert_candidate(
            ts="2026-11-16T22:00:00Z", station="KORD",
            ticker="MD-2", bracket_low=81.0, bracket_high=83.0,
            side="NO", predicted_price=5, predicted_edge=15.0,
            market_price=79, confidence=0.95, minutes_to_settlement=120.0,
            market_date="2026-11-17",
        )
        row = db._conn.execute(
            "SELECT market_date FROM candidates WHERE ticker='MD-2'"
        ).fetchone()
        assert row[0] == "2026-11-17"


class TestUpsertShadowTradeMarketDate:
    def _upsert(self, db, ticker, **kw):
        defaults = dict(
            ts="2026-11-16T22:00:00Z", station="KORD", ticker=ticker,
            bracket_low=81.0, bracket_high=83.0, side="NO",
            predicted_price=5, actual_price=6, predicted_edge=15.0,
        )
        defaults.update(kw)
        return db.upsert_shadow_trade(**defaults)

    def test_defaults_to_null(self):
        db = _fresh_db()
        self._upsert(db, "SHADOW-MD-1")
        row = db._conn.execute(
            "SELECT market_date FROM trades WHERE ticker='SHADOW-MD-1'"
        ).fetchone()
        assert row[0] is None

    def test_persists_market_date(self):
        db = _fresh_db()
        self._upsert(db, "SHADOW-MD-2", market_date="2026-11-17")
        row = db._conn.execute(
            "SELECT market_date FROM trades WHERE ticker='SHADOW-MD-2'"
        ).fetchone()
        assert row[0] == "2026-11-17"

    def test_dedup_hit_does_not_change_market_date(self):
        """Same non-overwrite contract as is_next_day/p_yes_raw: the dedup
        key (station, bracket_low, bracket_high, side, direction, day) does
        not include market_date, so a second poll's upsert on the same bracket
        must only refresh actual_price, never alter the already-stored
        market_date."""
        db = _fresh_db()
        row_id1, created1 = self._upsert(
            db, "SHADOW-MD-3", market_date="2026-11-17", actual_price=6,
        )
        assert created1 is True
        row_id2, created2 = self._upsert(
            db, "SHADOW-MD-3-again", market_date="1999-01-01", actual_price=9,
        )
        # Second call reuses the SAME dedup key (same station/bracket/side/
        # direction/day) -- the ticker argument is irrelevant to the dedup
        # key, so this is an UPDATE of row_id1, not a second insert.
        assert created2 is False
        assert row_id2 == row_id1
        row = db._conn.execute(
            "SELECT market_date, actual_price FROM trades WHERE id=?", (row_id1,),
        ).fetchone()
        assert row[0] == "2026-11-17"
        assert row[1] == 9

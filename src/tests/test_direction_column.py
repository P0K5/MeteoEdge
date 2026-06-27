"""Tests for direction column on candidates/trades/settlements (Issue #456)."""
import pytest

from src.data.db import Database


def _fresh_db() -> Database:
    return Database(":memory:")


class TestDirectionColumnFreshInstall:
    """Fresh DB must have direction column in all three tables."""

    def _col_names(self, db: Database, table: str) -> set[str]:
        rows = db._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {row[1] for row in rows}

    def test_candidates_has_direction(self):
        db = _fresh_db()
        assert "direction" in self._col_names(db, "candidates")

    def test_trades_has_direction(self):
        db = _fresh_db()
        assert "direction" in self._col_names(db, "trades")

    def test_settlements_has_direction(self):
        db = _fresh_db()
        assert "direction" in self._col_names(db, "settlements")


class TestDirectionColumnMigration:
    """_migrate() must add the column idempotently to an existing DB missing it."""

    def _legacy_db(self) -> Database:
        """Simulate a pre-direction DB by dropping the direction column after init.

        Uses PRAGMA foreign_keys=OFF during the table rebuild so that SQLite
        doesn't block the DROP TABLE on tables referenced by foreign keys.
        """
        db = _fresh_db()
        db._conn.execute("PRAGMA foreign_keys=OFF")
        for table in ("candidates", "trades", "settlements"):
            info = db._conn.execute(f"PRAGMA table_info({table})").fetchall()
            cols_without = [r[1] for r in info if r[1] != "direction"]
            cols_def = ", ".join(cols_without)
            db._conn.executescript(f"""
                CREATE TABLE {table}_bak AS SELECT {cols_def} FROM {table};
                DROP TABLE {table};
                ALTER TABLE {table}_bak RENAME TO {table};
            """)
        db._conn.execute("PRAGMA foreign_keys=ON")
        db._conn.commit()
        return db

    def test_migration_adds_direction_to_candidates(self):
        db = self._legacy_db()
        cols_before = {r[1] for r in db._conn.execute("PRAGMA table_info(candidates)").fetchall()}
        assert "direction" not in cols_before
        db._migrate()
        cols_after = {r[1] for r in db._conn.execute("PRAGMA table_info(candidates)").fetchall()}
        assert "direction" in cols_after

    def test_migration_adds_direction_to_trades(self):
        db = self._legacy_db()
        db._migrate()
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(trades)").fetchall()}
        assert "direction" in cols

    def test_migration_adds_direction_to_settlements(self):
        db = self._legacy_db()
        db._migrate()
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(settlements)").fetchall()}
        assert "direction" in cols

    def test_migration_is_idempotent(self):
        db = _fresh_db()
        # Calling _migrate() again must not raise
        db._migrate()
        db._migrate()


class TestDirectionColumnReadWrite:
    """Writes with direction='low' and reads filtering by direction work correctly."""

    def test_insert_candidate_high_default(self):
        db = _fresh_db()
        db.insert_candidate(
            ts="2026-07-05T10:00:00Z", station="KJFK",
            ticker="TEST-HIGH", bracket_low=75.0, bracket_high=79.0,
            side="YES", predicted_price=60, predicted_edge=0.08,
            market_price=55, confidence=0.65, minutes_to_settlement=120.0,
        )
        rows = db._conn.execute("SELECT direction FROM candidates").fetchall()
        assert rows[0][0] == "high"

    def test_insert_candidate_low_explicit(self):
        db = _fresh_db()
        db.insert_candidate(
            ts="2026-07-05T10:00:00Z", station="KJFK",
            ticker="TEST-LOW", bracket_low=55.0, bracket_high=59.0,
            side="NO", predicted_price=30, predicted_edge=0.10,
            market_price=35, confidence=0.70, minutes_to_settlement=90.0,
            direction="low",
        )
        rows = db._conn.execute("SELECT direction FROM candidates WHERE ticker='TEST-LOW'").fetchall()
        assert rows[0][0] == "low"

    def test_insert_trade_direction_stored(self):
        db = _fresh_db()
        db.insert_trade(
            ts="2026-07-05T10:00:00Z", station="KJFK",
            ticker="TEST-LOW", bracket_low=55.0, bracket_high=59.0,
            side="NO", predicted_price=30, actual_price=32,
            predicted_edge=0.10, mode="shadow", capital_before=100.0,
            direction="low",
        )
        rows = db.get_trades(limit=None, direction="low")
        assert len(rows) == 1
        assert rows[0]["direction"] == "low"

    def test_get_trades_direction_filter(self):
        db = _fresh_db()
        db.insert_trade(
            ts="2026-07-05T10:00:00Z", station="KJFK", ticker="HIGH-1",
            bracket_low=75.0, bracket_high=79.0, side="YES",
            predicted_price=60, actual_price=62, predicted_edge=0.08,
            mode="paper", capital_before=100.0, direction="high",
        )
        db.insert_trade(
            ts="2026-07-05T10:01:00Z", station="KJFK", ticker="LOW-1",
            bracket_low=55.0, bracket_high=59.0, side="NO",
            predicted_price=30, actual_price=32, predicted_edge=0.10,
            mode="shadow", capital_before=100.0, direction="low",
        )
        high_rows = db.get_trades(limit=None, direction="high")
        low_rows = db.get_trades(limit=None, direction="low")
        all_rows = db.get_trades(limit=None)
        assert len(high_rows) == 1 and high_rows[0]["ticker"] == "HIGH-1"
        assert len(low_rows) == 1 and low_rows[0]["ticker"] == "LOW-1"
        assert len(all_rows) == 2

    def test_insert_settlement_direction_stored(self):
        db = _fresh_db()
        db.insert_settlement(
            ts="2026-07-05T18:00:00Z", station="KJFK",
            ticker="LOW-SETTLE", bracket_low=55.0, bracket_high=59.0,
            actual_high_f=57.0, resolved_yes=1, direction="low",
        )
        rows = db.get_settlements("KJFK", "2026-07-05T00:00:00Z", direction="low")
        assert len(rows) == 1
        assert rows[0]["direction"] == "low"

    def test_get_settlements_direction_filter(self):
        db = _fresh_db()
        db.insert_settlement(
            ts="2026-07-05T18:00:00Z", station="KJFK",
            ticker="HIGH-S", bracket_low=75.0, bracket_high=79.0,
            actual_high_f=77.0, resolved_yes=1, direction="high",
        )
        db.insert_settlement(
            ts="2026-07-05T18:01:00Z", station="KJFK",
            ticker="LOW-S", bracket_low=55.0, bracket_high=59.0,
            actual_high_f=57.0, resolved_yes=1, direction="low",
        )
        low_rows = db.get_settlements("KJFK", "2026-01-01T00:00:00Z", direction="low")
        assert len(low_rows) == 1 and low_rows[0]["ticker"] == "LOW-S"

    def test_legacy_rows_default_to_high(self):
        """Rows inserted without direction must read back as 'high'."""
        db = _fresh_db()
        db._conn.execute(
            "INSERT INTO trades(ts,station,ticker,bracket_low,bracket_high,side,"
            "predicted_price,actual_price,predicted_edge,mode,capital_before) "
            "VALUES('2026-07-05T10:00:00Z','KJFK','LEGACY',75,79,'YES',60,62,0.08,'paper',100)"
        )
        db._conn.commit()
        rows = db.get_trades(limit=None, direction="high")
        assert any(r["ticker"] == "LEGACY" for r in rows)

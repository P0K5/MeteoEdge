"""Tests for the is_next_day column on candidates/snapshot_archive (issue #687)
and the follow-up trades.is_next_day + candidates.today_position_open columns
(issue #704).

Covers:
- Fresh DB/analytics.db has is_next_day, defaulting to 0
- Migration adds the column idempotently to a pre-existing DB missing it
- insert_candidate() accepts and persists is_next_day, defaulting to 0
- ArchiveDatabase.insert_snapshots() carries is_next_day through, defaulting
  to 0 for legacy rows written before the column existed (no key in the dict)
- trades.is_next_day: fresh install, migration, upsert_shadow_trade() carries
  it through, get_trades(is_next_day=...) filters
- candidates.today_position_open: fresh install, migration, insert_candidate()
  carries it through
- Database.has_open_live_position() -- the Gap 2 open-position lookup
"""
import tempfile
from pathlib import Path

from src.data.db import Database
from src.data.archive_db import ArchiveDatabase


def _fresh_db() -> Database:
    return Database(":memory:")


def _col_names(db: Database, table: str) -> set[str]:
    rows = db._conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


class TestIsNextDayFreshInstall:
    def test_candidates_has_is_next_day(self):
        db = _fresh_db()
        assert "is_next_day" in _col_names(db, "candidates")


class TestIsNextDayMigration:
    def _legacy_db(self) -> Database:
        """Simulate a pre-is_next_day DB by dropping the column after init."""
        db = _fresh_db()
        db._conn.execute("PRAGMA foreign_keys=OFF")
        info = db._conn.execute("PRAGMA table_info(candidates)").fetchall()
        cols_without = [r[1] for r in info if r[1] != "is_next_day"]
        cols_def = ", ".join(cols_without)
        db._conn.executescript(f"""
            CREATE TABLE candidates_bak AS SELECT {cols_def} FROM candidates;
            DROP TABLE candidates;
            ALTER TABLE candidates_bak RENAME TO candidates;
        """)
        db._conn.execute("PRAGMA foreign_keys=ON")
        db._conn.commit()
        return db

    def test_migration_adds_is_next_day(self):
        db = self._legacy_db()
        assert "is_next_day" not in _col_names(db, "candidates")
        db._migrate()
        assert "is_next_day" in _col_names(db, "candidates")

    def test_migration_is_idempotent(self):
        db = _fresh_db()
        db._migrate()
        db._migrate()
        assert "is_next_day" in _col_names(db, "candidates")


class TestIsNextDayReadWrite:
    def test_insert_candidate_defaults_to_zero(self):
        db = _fresh_db()
        db.insert_candidate(
            ts="2026-07-11T10:00:00Z", station="KORD",
            ticker="TEST-1", bracket_low=81.0, bracket_high=83.0,
            side="NO", predicted_price=5, predicted_edge=15.0,
            market_price=79, confidence=0.95, minutes_to_settlement=120.0,
        )
        row = db._conn.execute(
            "SELECT is_next_day FROM candidates WHERE ticker='TEST-1'"
        ).fetchone()
        assert row[0] == 0

    def test_insert_candidate_with_is_next_day_true(self):
        db = _fresh_db()
        db.insert_candidate(
            ts="2026-07-11T10:00:00Z", station="KORD",
            ticker="TEST-2", bracket_low=81.0, bracket_high=83.0,
            side="YES", predicted_price=70, predicted_edge=15.0,
            market_price=60, confidence=0.60, minutes_to_settlement=1200.0,
            is_next_day=1,
        )
        row = db._conn.execute(
            "SELECT is_next_day FROM candidates WHERE ticker='TEST-2'"
        ).fetchone()
        assert row[0] == 1


class TestTradesIsNextDayFreshInstall:
    def test_trades_has_is_next_day(self):
        db = _fresh_db()
        assert "is_next_day" in _col_names(db, "trades")


class TestTradesIsNextDayMigration:
    def _legacy_db(self) -> Database:
        """Simulate a pre-#704 DB missing trades.is_next_day.

        Deliberately NOT using "CREATE TABLE ... AS SELECT" (as the sibling
        candidates-table helpers above do): that construct drops CHECK
        constraints from the resulting schema, which would make the
        unrelated trades.mode CHECK-widening migration (_migrate(), "extend
        trades.mode CHECK to include 'shadow'") think this is a pre-#376
        table and rebuild it from a hardcoded column list that predates
        several later columns -- silently discarding data this test isn't
        exercising. Recreating the real DDL (minus is_next_day) keeps the
        'shadow' CHECK intact so only the migration under test fires.
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
                direction       TEXT NOT NULL DEFAULT 'high'
            );
            """
        )
        db._conn.execute("PRAGMA foreign_keys=ON")
        db._conn.commit()
        return db

    def test_migration_adds_is_next_day(self):
        db = self._legacy_db()
        assert "is_next_day" not in _col_names(db, "trades")
        db._migrate()
        assert "is_next_day" in _col_names(db, "trades")

    def test_migration_is_idempotent(self):
        db = _fresh_db()
        db._migrate()
        db._migrate()
        assert "is_next_day" in _col_names(db, "trades")


class TestUpsertShadowTradeIsNextDay:
    def _upsert(self, db, ticker, **kw):
        defaults = dict(
            ts="2026-07-11T10:00:00Z", station="KORD", ticker=ticker,
            bracket_low=81.0, bracket_high=83.0, side="NO",
            predicted_price=5, actual_price=6, predicted_edge=15.0,
        )
        defaults.update(kw)
        return db.upsert_shadow_trade(**defaults)

    def test_defaults_to_zero(self):
        db = _fresh_db()
        self._upsert(db, "SHADOW-1")
        row = db._conn.execute(
            "SELECT is_next_day FROM trades WHERE ticker='SHADOW-1'"
        ).fetchone()
        assert row[0] == 0

    def test_is_next_day_true_persists(self):
        db = _fresh_db()
        self._upsert(db, "SHADOW-2", is_next_day=1)
        row = db._conn.execute(
            "SELECT is_next_day FROM trades WHERE ticker='SHADOW-2'"
        ).fetchone()
        assert row[0] == 1

    def test_get_trades_filters_by_is_next_day(self):
        """The critical Gap 1 regression guard: once a next-day shadow trade
        lands in `trades`, get_trades(is_next_day=0) must exclude it so
        trades-based consumers (promotion gate, prob-cap report,
        shadow-health) never silently mix same-day and next-day rows.

        Uses distinct brackets for the two rows -- upsert_shadow_trade()'s
        dedup key is (station, bracket_low, bracket_high, side, direction,
        day) and does NOT include ticker, so same-bracket rows would collide
        and the second upsert would just update the first row's actual_price
        instead of inserting a second row.
        """
        db = _fresh_db()
        self._upsert(db, "SAME-DAY", bracket_low=81.0, bracket_high=83.0, is_next_day=0)
        self._upsert(db, "NEXT-DAY", bracket_low=91.0, bracket_high=93.0, is_next_day=1)

        same_day_only = db.get_trades(mode="shadow", limit=None, is_next_day=0)
        assert {t["ticker"] for t in same_day_only} == {"SAME-DAY"}

        next_day_only = db.get_trades(mode="shadow", limit=None, is_next_day=1)
        assert {t["ticker"] for t in next_day_only} == {"NEXT-DAY"}

        both = db.get_trades(mode="shadow", limit=None)
        assert {t["ticker"] for t in both} == {"SAME-DAY", "NEXT-DAY"}


class TestTodayPositionOpenFreshInstall:
    def test_candidates_has_today_position_open(self):
        db = _fresh_db()
        assert "today_position_open" in _col_names(db, "candidates")


class TestTodayPositionOpenMigration:
    def _legacy_db(self) -> Database:
        db = _fresh_db()
        db._conn.execute("PRAGMA foreign_keys=OFF")
        info = db._conn.execute("PRAGMA table_info(candidates)").fetchall()
        cols_without = [r[1] for r in info if r[1] != "today_position_open"]
        cols_def = ", ".join(cols_without)
        db._conn.executescript(f"""
            CREATE TABLE candidates_bak AS SELECT {cols_def} FROM candidates;
            DROP TABLE candidates;
            ALTER TABLE candidates_bak RENAME TO candidates;
        """)
        db._conn.execute("PRAGMA foreign_keys=ON")
        db._conn.commit()
        return db

    def test_migration_adds_today_position_open(self):
        db = self._legacy_db()
        assert "today_position_open" not in _col_names(db, "candidates")
        db._migrate()
        assert "today_position_open" in _col_names(db, "candidates")

    def test_migration_is_idempotent(self):
        db = _fresh_db()
        db._migrate()
        db._migrate()
        assert "today_position_open" in _col_names(db, "candidates")


class TestTodayPositionOpenReadWrite:
    def test_insert_candidate_defaults_to_zero(self):
        db = _fresh_db()
        db.insert_candidate(
            ts="2026-07-11T10:00:00Z", station="KORD",
            ticker="TPO-1", bracket_low=81.0, bracket_high=83.0,
            side="NO", predicted_price=5, predicted_edge=15.0,
            market_price=79, confidence=0.95, minutes_to_settlement=120.0,
            is_next_day=1,
        )
        row = db._conn.execute(
            "SELECT today_position_open FROM candidates WHERE ticker='TPO-1'"
        ).fetchone()
        assert row[0] == 0

    def test_insert_candidate_with_today_position_open_true(self):
        db = _fresh_db()
        db.insert_candidate(
            ts="2026-07-11T10:00:00Z", station="KORD",
            ticker="TPO-2", bracket_low=81.0, bracket_high=83.0,
            side="YES", predicted_price=70, predicted_edge=15.0,
            market_price=60, confidence=0.60, minutes_to_settlement=1200.0,
            is_next_day=1, today_position_open=1,
        )
        row = db._conn.execute(
            "SELECT today_position_open FROM candidates WHERE ticker='TPO-2'"
        ).fetchone()
        assert row[0] == 1


class TestHasOpenLivePosition:
    """Database.has_open_live_position() -- the Gap 2 lookup scanner.py uses
    to record today_position_open at next-day-evaluation time."""

    def test_no_trades_returns_false(self):
        db = _fresh_db()
        assert db.has_open_live_position("KORD") is False

    def test_open_live_position_returns_true(self):
        db = _fresh_db()
        db.insert_trade(
            ts="2026-07-11T10:00:00Z", station="KORD", ticker="LIVE-OPEN",
            bracket_low=81.0, bracket_high=83.0, side="YES",
            predicted_price=70, actual_price=70, predicted_edge=15.0,
            mode="live", capital_before=100.0, outcome="filled",
        )
        assert db.has_open_live_position("KORD") is True

    def test_settled_live_position_returns_false(self):
        """A live trade that has already settled is no longer "open"."""
        db = _fresh_db()
        db.insert_trade(
            ts="2026-07-11T10:00:00Z", station="KORD", ticker="LIVE-SETTLED",
            bracket_low=81.0, bracket_high=83.0, side="YES",
            predicted_price=70, actual_price=70, predicted_edge=15.0,
            mode="live", capital_before=100.0, outcome="filled",
            settled_at="2026-07-11T20:00:00Z",
        )
        assert db.has_open_live_position("KORD") is False

    def test_shadow_position_does_not_count(self):
        """Shadow trades are not real capital exposure -- must not count as
        an open LIVE position."""
        db = _fresh_db()
        db.upsert_shadow_trade(
            ts="2026-07-11T10:00:00Z", station="KORD", ticker="SHADOW-ONLY",
            bracket_low=81.0, bracket_high=83.0, side="YES",
            predicted_price=70, actual_price=70, predicted_edge=15.0,
        )
        assert db.has_open_live_position("KORD") is False

    def test_other_station_does_not_count(self):
        db = _fresh_db()
        db.insert_trade(
            ts="2026-07-11T10:00:00Z", station="KATL", ticker="OTHER-STATION",
            bracket_low=81.0, bracket_high=83.0, side="YES",
            predicted_price=70, actual_price=70, predicted_edge=15.0,
            mode="live", capital_before=100.0, outcome="filled",
        )
        assert db.has_open_live_position("KORD") is False


class TestArchiveSnapshotIsNextDay:
    def _fresh_archive(self, tmp_path) -> ArchiveDatabase:
        return ArchiveDatabase(path=str(tmp_path / "analytics.db"))

    def test_fresh_install_has_column(self, tmp_path):
        with self._fresh_archive(tmp_path) as db:
            rows = db._conn.execute("PRAGMA table_info(snapshot_archive)").fetchall()
            assert "is_next_day" in {r[1] for r in rows}

    def test_migration_adds_column_to_legacy_db(self, tmp_path):
        db_path = tmp_path / "legacy_analytics.db"
        with ArchiveDatabase(path=str(db_path)) as db:
            info = db._conn.execute("PRAGMA table_info(snapshot_archive)").fetchall()
            cols_without = [r[1] for r in info if r[1] != "is_next_day"]
            cols_def = ", ".join(cols_without)
            db._conn.executescript(f"""
                CREATE TABLE snapshot_archive_bak AS SELECT {cols_def} FROM snapshot_archive;
                DROP TABLE snapshot_archive;
                ALTER TABLE snapshot_archive_bak RENAME TO snapshot_archive;
            """)
            db._conn.commit()
            rows = db._conn.execute("PRAGMA table_info(snapshot_archive)").fetchall()
            assert "is_next_day" not in {r[1] for r in rows}

        # Re-opening runs _migrate() again -- column should be added idempotently.
        with ArchiveDatabase(path=str(db_path)) as db:
            rows = db._conn.execute("PRAGMA table_info(snapshot_archive)").fetchall()
            assert "is_next_day" in {r[1] for r in rows}

    def test_insert_snapshots_carries_is_next_day_through(self, tmp_path):
        with self._fresh_archive(tmp_path) as db:
            db.insert_snapshots([{
                "ts": "2026-07-11T12:00:00Z", "station": "KMIA", "ticker": "T1",
                "is_next_day": 1,
            }])
            row = db._conn.execute(
                "SELECT is_next_day FROM snapshot_archive WHERE ticker='T1'"
            ).fetchone()
            assert row[0] == 1

    def test_insert_snapshots_defaults_missing_key_to_zero(self, tmp_path):
        """A legacy JSONL line written before is_next_day existed has no such
        key -- must default to 0, not fail the NOT NULL constraint."""
        with self._fresh_archive(tmp_path) as db:
            db.insert_snapshots([{
                "ts": "2026-07-11T12:00:00Z", "station": "KMIA", "ticker": "T2",
            }])
            row = db._conn.execute(
                "SELECT is_next_day FROM snapshot_archive WHERE ticker='T2'"
            ).fetchone()
            assert row[0] == 0

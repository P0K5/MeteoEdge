"""Migration test for scan_decisions.gate_verdict's dropped CHECK constraint
(issue #912).

Background: the CHECK constraint on gate_verdict lived inside a
``CREATE TABLE IF NOT EXISTS`` -- a no-op against a database that already
exists. When 'day_mismatch_shadow' was added to scanner.GATE_VERDICTS and
Database._SCAN_DECISION_GATE_VERDICTS (issue #820) but not to this
hand-written DDL string, every *existing* database kept rejecting the new
verdict with a raw ``sqlite3.IntegrityError`` at the SQL layer, even though
the Python validator in ``upsert_scan_decision`` accepted it. This is a
migration test in the same style as
``test_emos_crps_log_sigma_source_migration.py``'s ``_legacy_db()`` pattern:
it builds the table from the OLD DDL (the CHECK constraint that shipped to
production, pre-#912), then asserts the migration fixes it in place.

A prior attempt at this fix only widened the *hand-written* CHECK string
inside ``CREATE TABLE IF NOT EXISTS`` -- which, being IF NOT EXISTS, never
runs against an existing table. Any test built from the ``db`` fixture (a
freshly-created, post-fix schema) cannot detect that gap, so this test
deliberately starts from the pre-fix schema instead.
"""
import sqlite3

import pytest

from src.data.db import Database
from src.strategy.gate_verdicts import GATE_VERDICTS


def _fresh_db() -> Database:
    return Database(":memory:")


def _table_sql(db: Database, table: str) -> str:
    row = db._conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] if row else ""


# The exact CHECK constraint that shipped to production before issue #912 --
# 11 verdicts, missing 'day_mismatch_shadow' (added to the other two
# allow-lists by issue #820 but never to this DDL string).
_OLD_GATE_VERDICT_CHECK = (
    "'traded_live','shadow_only','next_day_shadow','entry_guard','timeout_today',"
    "'below_min_edge','above_max_edge','below_min_price','below_min_confidence',"
    "'margin_gate','mae_gate'"
)


class TestScanDecisionsGateVerdictCheckMigration:
    _LEGACY_COLS = (
        "station", "ticker", "date", "ts", "poll_ts", "bracket_low",
        "bracket_high", "side", "yes_ask", "no_ask", "current_high",
        "latest_temp", "forecast_high", "p_yes", "raw_p_yes",
        "capped_p_yes", "ev_yes", "ev_no", "ev_yes_raw", "ev_no_raw",
        "minutes_to_settlement", "emos_mode", "is_next_day",
        "gate_verdict", "gate_actual", "gate_threshold", "gate_unit",
        "gate_detail", "execution_mode", "ensemble_mean",
        "ensemble_members", "ensemble_range_low", "ensemble_range_high",
        "direction",
    )

    def _legacy_db(self) -> Database:
        """Simulate a pre-#912 DB: scan_decisions.gate_verdict still carries
        the old hand-written CHECK constraint (missing 'day_mismatch_shadow').
        """
        db = _fresh_db()
        # Seed rows through the normal (post-migration) API first, so the
        # rebuild below has data to carry across the "legacy schema" cut.
        db.upsert_scan_decision(
            ts="2026-07-30T12:00:00Z", station="KORD", ticker="T1", date="2026-07-30",
            bracket_low=80.0, bracket_high=82.0, gate_verdict="below_min_edge",
        )
        db.upsert_scan_decision(
            ts="2026-07-30T12:00:00Z", station="KATL", ticker="T2", date="2026-07-30",
            bracket_low=84.0, bracket_high=86.0, gate_verdict="shadow_only",
        )

        cols_csv = ",".join(self._LEGACY_COLS)
        with db._conn:
            db._conn.execute("CREATE TABLE scan_decisions_bak AS SELECT * FROM scan_decisions")
            db._conn.execute("DROP TABLE scan_decisions")
            # The exact pre-#912 production DDL: full column set, CHECK on
            # gate_verdict missing 'day_mismatch_shadow' (mirrors the schema
            # verified against data/meteoedge.db in the #912 review).
            db._conn.execute(
                f"""
                CREATE TABLE scan_decisions (
                    station               TEXT NOT NULL,
                    ticker                TEXT NOT NULL,
                    date                  TEXT NOT NULL,
                    ts                    TEXT NOT NULL,
                    poll_ts               TEXT NOT NULL,
                    bracket_low           REAL NOT NULL,
                    bracket_high          REAL NOT NULL,
                    side                  TEXT CHECK(side IN ('YES','NO') OR side IS NULL),
                    yes_ask               INTEGER,
                    no_ask                INTEGER,
                    current_high          REAL,
                    latest_temp           REAL,
                    forecast_high         REAL,
                    p_yes                 REAL,
                    raw_p_yes             REAL,
                    capped_p_yes          REAL,
                    ev_yes                REAL,
                    ev_no                 REAL,
                    ev_yes_raw            REAL,
                    ev_no_raw             REAL,
                    minutes_to_settlement REAL,
                    emos_mode             TEXT,
                    is_next_day           INTEGER NOT NULL DEFAULT 0,
                    gate_verdict          TEXT NOT NULL CHECK(gate_verdict IN ({_OLD_GATE_VERDICT_CHECK})),
                    gate_actual           REAL,
                    gate_threshold        REAL,
                    gate_unit             TEXT,
                    gate_detail           TEXT,
                    execution_mode        TEXT NOT NULL DEFAULT 'paper' CHECK(execution_mode IN ('live','paper')),
                    ensemble_mean         REAL,
                    ensemble_members      INTEGER,
                    ensemble_range_low    REAL,
                    ensemble_range_high   REAL,
                    direction             TEXT NOT NULL DEFAULT 'high',
                    PRIMARY KEY (station, ticker, date)
                )
                """
            )
            db._conn.execute(
                f"INSERT INTO scan_decisions ({cols_csv}) "
                f"SELECT {cols_csv} FROM scan_decisions_bak"
            )
            db._conn.execute("DROP TABLE scan_decisions_bak")
            db._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scan_decisions_station_date "
                "ON scan_decisions(station, date)"
            )
        db._conn.commit()
        return db

    def test_legacy_schema_has_the_old_check_constraint(self):
        db = self._legacy_db()
        sql = _table_sql(db, "scan_decisions")
        assert "CHECK(gate_verdict" in sql

    def test_legacy_schema_rejects_day_mismatch_shadow_at_the_sql_layer(self):
        """Reproduces the exact production failure mode from the #912 review:
        a raw insert of 'day_mismatch_shadow' against the pre-migration
        schema is rejected by SQLite's CHECK, independent of the Python
        validator (which would accept it -- this proves the DDL, not the
        validator, was the live blocker)."""
        db = self._legacy_db()
        assert "day_mismatch_shadow" in GATE_VERDICTS  # Python side already knows it
        with pytest.raises(sqlite3.IntegrityError):
            db._conn.execute(
                "INSERT INTO scan_decisions(station,ticker,date,ts,poll_ts,"
                "bracket_low,bracket_high,gate_verdict) "
                "VALUES('KORD','T3','2026-07-30','2026-07-30T12:00:00Z',"
                "'2026-07-30T12:00:00Z',80.0,82.0,'day_mismatch_shadow')"
            )

    def test_migration_removes_the_check_constraint(self):
        db = self._legacy_db()
        db._migrate()
        sql = _table_sql(db, "scan_decisions")
        assert "CHECK(gate_verdict" not in sql

    def test_migration_fixes_the_production_failure(self):
        """The actual regression proof: after migration, the exact insert
        that raised IntegrityError above now succeeds -- both at the raw SQL
        layer and through the normal upsert_scan_decision API."""
        db = self._legacy_db()
        db._migrate()

        # Raw SQL layer -- no CHECK left to reject it.
        db._conn.execute(
            "INSERT INTO scan_decisions(station,ticker,date,ts,poll_ts,"
            "bracket_low,bracket_high,gate_verdict) "
            "VALUES('KORD','T3','2026-07-30','2026-07-30T12:00:00Z',"
            "'2026-07-30T12:00:00Z',80.0,82.0,'day_mismatch_shadow')"
        )
        db._conn.commit()

        # Normal write path.
        db.upsert_scan_decision(
            ts="2026-07-30T12:00:00Z", station="KMDW", ticker="T4", date="2026-07-30",
            bracket_low=70.0, bracket_high=72.0, gate_verdict="day_mismatch_shadow",
        )
        row = db._conn.execute(
            "SELECT gate_verdict FROM scan_decisions WHERE station='KMDW' AND ticker='T4'"
        ).fetchone()
        assert row[0] == "day_mismatch_shadow"

    def test_migration_preserves_existing_rows(self):
        db = self._legacy_db()
        db._migrate()
        rows = db._conn.execute(
            "SELECT station, ticker, gate_verdict FROM scan_decisions ORDER BY ticker"
        ).fetchall()
        assert [(r[0], r[1], r[2]) for r in rows] == [
            ("KORD", "T1", "below_min_edge"),
            ("KATL", "T2", "shadow_only"),
        ]

    def test_migration_is_idempotent(self):
        db = self._legacy_db()
        db._migrate()
        db._migrate()
        sql = _table_sql(db, "scan_decisions")
        assert "CHECK(gate_verdict" not in sql
        rows = db._conn.execute("SELECT COUNT(*) FROM scan_decisions").fetchone()
        assert rows[0] == 2

    def test_migration_is_a_noop_on_a_fresh_db(self):
        """A freshly-created DB never had the CHECK to begin with -- running
        the migration must not error or drop data."""
        db = _fresh_db()
        db.upsert_scan_decision(
            ts="2026-07-30T12:00:00Z", station="KORD", ticker="T1", date="2026-07-30",
            bracket_low=80.0, bracket_high=82.0, gate_verdict="day_mismatch_shadow",
        )
        db._migrate()
        rows = db._conn.execute("SELECT COUNT(*) FROM scan_decisions").fetchone()
        assert rows[0] == 1


class TestGateVerdictSingleSourceOfTruth:
    """Issue #912 requirement: one definition, not three."""

    def test_scanner_and_db_share_the_same_frozenset_object(self):
        from src.strategy.scanner import GATE_VERDICTS as scanner_verdicts
        from src.data.db import Database

        assert scanner_verdicts is GATE_VERDICTS
        assert Database._SCAN_DECISION_GATE_VERDICTS is GATE_VERDICTS

    def test_fresh_schema_has_no_check_constraint_on_gate_verdict(self):
        """A brand-new DB also has no DDL CHECK on gate_verdict -- the only
        allow-list enforcement is the Python validator, so a future verdict
        added to GATE_VERDICTS never needs a matching DDL edit again."""
        db = _fresh_db()
        sql = _table_sql(db, "scan_decisions")
        assert "CHECK(gate_verdict" not in sql

    def test_day_mismatch_shadow_accepted_on_a_fresh_db(self):
        db = _fresh_db()
        db.upsert_scan_decision(
            ts="2026-07-30T12:00:00Z", station="KORD", ticker="T1", date="2026-07-30",
            bracket_low=80.0, bracket_high=82.0, gate_verdict="day_mismatch_shadow",
        )
        row = db._conn.execute(
            "SELECT gate_verdict FROM scan_decisions WHERE ticker='T1'"
        ).fetchone()
        assert row[0] == "day_mismatch_shadow"

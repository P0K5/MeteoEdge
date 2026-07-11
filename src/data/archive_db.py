"""Durable analytics store for intra-day snapshot telemetry (EPIC #345).

Isolated from the live trading database (data/meteoedge.db). Open via a
separate sqlite3 connection — never import or reuse the Database singleton.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

_DEFAULT_ARCHIVE_PATH = os.getenv("ARCHIVE_DB_PATH", "data/analytics.db")

_DDL = """
CREATE TABLE IF NOT EXISTS snapshot_archive (
    ts                    TEXT NOT NULL,
    station               TEXT NOT NULL,
    ticker                TEXT NOT NULL,
    bracket_low           REAL,
    bracket_high          REAL,
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
    minutes_to_settlement REAL,
    emos_mode             TEXT,
    is_next_day           INTEGER NOT NULL DEFAULT 0,
    UNIQUE(ts, ticker)
);
CREATE INDEX IF NOT EXISTS idx_sa_station_ts ON snapshot_archive(station, ts);
CREATE INDEX IF NOT EXISTS idx_sa_ticker_ts  ON snapshot_archive(ticker, ts);

CREATE TABLE IF NOT EXISTS position_snapshot_archive (
    ts                 TEXT NOT NULL,
    ticker             TEXT,
    no_token_id        TEXT NOT NULL,
    station            TEXT,
    bracket_low        REAL,
    bracket_high       REAL,
    entry_price        INTEGER,
    predicted_price    INTEGER,
    current_high       REAL,
    latest_temp        REAL,
    forecast_nws       REAL,
    forecast_secondary REAL,
    no_best_bid        INTEGER,
    no_best_bid_size   REAL,
    no_best_ask        INTEGER,
    p_yes_now          REAL,
    fair_value_now     INTEGER,
    weather_missing    INTEGER,
    UNIQUE(ts, no_token_id)
);
CREATE INDEX IF NOT EXISTS idx_psa_station_ts ON position_snapshot_archive(station, ts);
CREATE INDEX IF NOT EXISTS idx_psa_ticker_ts  ON position_snapshot_archive(ticker, ts)
"""

_SNAPSHOT_COLS = (
    "ts", "station", "ticker", "bracket_low", "bracket_high",
    "yes_ask", "no_ask", "current_high", "latest_temp", "forecast_high",
    "p_yes", "raw_p_yes", "capped_p_yes", "ev_yes", "ev_no",
    "minutes_to_settlement", "emos_mode", "is_next_day",
)

# Per-column defaults applied when a batch-inserted row is missing a key --
# needed for is_next_day (issue #687) since JSONL lines written before this
# column existed carry no such key, and the column is NOT NULL.
_SNAPSHOT_COL_DEFAULTS = {"is_next_day": 0}

_POSITION_SNAPSHOT_COLS = (
    "ts", "ticker", "no_token_id", "station", "bracket_low", "bracket_high",
    "entry_price", "predicted_price", "current_high", "latest_temp",
    "forecast_nws", "forecast_secondary", "no_best_bid", "no_best_bid_size",
    "no_best_ask", "p_yes_now", "fair_value_now", "weather_missing",
)


class ArchiveDatabase:
    """Wraps a SQLite connection to data/analytics.db for archival telemetry.

    Isolated from the live trading database (meteoedge.db). Creates the
    analytics.db file and schema on first construction; idempotent.
    """

    def __init__(self, path: "str | Path" = _DEFAULT_ARCHIVE_PATH) -> None:
        """Open (creating if necessary) the analytics database at *path*."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        for stmt in _DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                self._conn.execute(stmt)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Idempotent schema migrations for analytics.db files created before
        a column existed (mirrors src/data/db.py's _migrate pattern).
        """
        for table, col, definition in [
            # Issue #687: discriminator for next-day (forecast-only,
            # shadow-only) rows -- CREATE TABLE IF NOT EXISTS above only
            # covers brand-new databases; pre-existing analytics.db files
            # need this ALTER TABLE to gain the column.
            ("snapshot_archive", "is_next_day", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            try:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError:
                pass  # column already exists

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def insert_snapshots(self, rows: list) -> int:
        """Batch-insert scanner snapshots; idempotent via INSERT OR IGNORE.

        Returns the number of rows actually inserted (0 on full duplicate batch).
        """
        if not rows:
            return 0
        placeholders = ",".join("?" * len(_SNAPSHOT_COLS))
        sql = (
            f"INSERT OR IGNORE INTO snapshot_archive "
            f"({','.join(_SNAPSHOT_COLS)}) VALUES ({placeholders})"
        )
        params = [
            tuple(row.get(c, _SNAPSHOT_COL_DEFAULTS.get(c)) for c in _SNAPSHOT_COLS)
            for row in rows
        ]
        with self._lock:
            with self._conn:
                cur = self._conn.executemany(sql, params)
        return cur.rowcount

    def insert_position_snapshots(self, rows: list) -> int:
        """Batch-insert position snapshots; idempotent via INSERT OR IGNORE.

        Returns the number of rows actually inserted (0 on full duplicate batch).
        """
        if not rows:
            return 0
        placeholders = ",".join("?" * len(_POSITION_SNAPSHOT_COLS))
        sql = (
            f"INSERT OR IGNORE INTO position_snapshot_archive "
            f"({','.join(_POSITION_SNAPSHOT_COLS)}) VALUES ({placeholders})"
        )
        params = [
            tuple(row.get(c) for c in _POSITION_SNAPSHOT_COLS) for row in rows
        ]
        with self._lock:
            with self._conn:
                cur = self._conn.executemany(sql, params)
        return cur.rowcount

    def get_snapshot_series(self, station: str, date: str) -> list[dict]:
        """Return all snapshot_archive rows for *station* on *date*, ordered by ts.

        Args:
            station: METAR station code (e.g. "KORD").
            date: ISO date string "YYYY-MM-DD".

        Returns:
            List of row dicts ordered by ts ascending.  Empty list when no rows
            match (station unknown or date has no data).
        """
        from datetime import date as dt_date, timedelta
        d = dt_date.fromisoformat(date)
        next_d = (d + timedelta(days=1)).isoformat()
        sql = (
            "SELECT * FROM snapshot_archive "
            "WHERE station = ? AND ts >= ? AND ts < ? "
            "ORDER BY ts"
        )
        with self._lock:
            cur = self._conn.execute(sql, (station, date, next_d))
            return [dict(row) for row in cur.fetchall()]

    def get_position_snapshot_series(self, ticker: str, date: str) -> list[dict]:
        """Return all position_snapshot_archive rows for *ticker* on *date*, ordered by ts.

        Args:
            ticker: Market ticker string (e.g. "HIGH-TEMP-KORD-2026-06-15-90-94").
            date: ISO date string "YYYY-MM-DD".

        Returns:
            List of row dicts ordered by ts ascending.  Empty list when no rows
            match (ticker unknown or date has no data).
        """
        from datetime import date as dt_date, timedelta
        d = dt_date.fromisoformat(date)
        next_d = (d + timedelta(days=1)).isoformat()
        sql = (
            "SELECT * FROM position_snapshot_archive "
            "WHERE ticker = ? AND ts >= ? AND ts < ? "
            "ORDER BY ts"
        )
        with self._lock:
            cur = self._conn.execute(sql, (ticker, date, next_d))
            return [dict(row) for row in cur.fetchall()]

    def get_max_archived_ts(self, table: str) -> "str | None":
        """Return the maximum ts recorded in *table*, or None if the table is empty.

        Used by the ETL to determine the high-water mark for incremental loading.

        Args:
            table: Either "snapshot_archive" or "position_snapshot_archive".
        """
        allowed = {"snapshot_archive", "position_snapshot_archive"}
        if table not in allowed:
            raise ValueError(f"Unknown archive table: {table!r}")
        cur = self._conn.execute(f"SELECT MAX(ts) FROM {table}")  # noqa: S608
        row = cur.fetchone()
        return row[0] if row and row[0] is not None else None
